from __future__ import annotations

import importlib.util
import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path

# The launcher wrapper lives outside the importable package tree and its
# filename is not a valid module name, so load it by path. Its module-level
# imports are stdlib only (the Hermes packages are imported lazily inside
# main()), so this is safe to load in the host test environment.
_WRAPPER_PATH = Path(__file__).resolve().parents[1] / "host" / "bootstrap" / "helpers" / "hermes-stdin.py"
_spec = importlib.util.spec_from_file_location("hermes_stdin", _WRAPPER_PATH)
assert _spec is not None and _spec.loader is not None
hermes_stdin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hermes_stdin)


# A fixed per-turn marker for the tests, standing in for the host-minted nonce.
_TEST_MARKER = hermes_stdin.ACTIVITY_LINE_PREFIX + "testnonce "


def _emitted(hook, **kwargs) -> list[dict]:
    """Invoke a hook and parse the activity records it wrote to stdout."""
    buffer = io.StringIO()
    previous = hermes_stdin._ACTIVITY_MARKER
    hermes_stdin._ACTIVITY_MARKER = _TEST_MARKER
    try:
        with redirect_stdout(buffer):
            result = hook(**kwargs)
    finally:
        hermes_stdin._ACTIVITY_MARKER = previous
    assert result is None  # tool-call observers must never return a directive
    records = []
    # Split on newlines only: the sentinel opens with an ASCII Record
    # Separator, which str.splitlines() would itself treat as a line break.
    for line in buffer.getvalue().split("\n"):
        if not line:
            continue
        assert line.startswith(_TEST_MARKER)
        records.append(json.loads(line[len(_TEST_MARKER):]))
    return records


class HermesStdinActivityTests(unittest.TestCase):
    def test_tool_kind_buckets_by_tool_name(self) -> None:
        self.assertEqual(hermes_stdin._tool_kind("terminal"), "command")
        self.assertEqual(hermes_stdin._tool_kind("process"), "command")
        self.assertEqual(hermes_stdin._tool_kind("write_file"), "file_change")
        self.assertEqual(hermes_stdin._tool_kind("patch"), "file_change")
        self.assertEqual(hermes_stdin._tool_kind("search_files"), "search")
        self.assertEqual(hermes_stdin._tool_kind("read_file"), "tool")
        self.assertEqual(hermes_stdin._tool_kind("kern_some_mcp_tool"), "tool")

    def test_tool_titles_summarize_the_call(self) -> None:
        self.assertEqual(hermes_stdin._tool_title("terminal", {"command": "ls -la"}), "ls -la")
        self.assertEqual(hermes_stdin._tool_title("write_file", {"path": "a.py"}), "Write: a.py")
        self.assertEqual(hermes_stdin._tool_title("patch", {"path": "a.py"}), "Edit: a.py")
        self.assertEqual(hermes_stdin._tool_title("read_file", {"path": "a.py"}), "Read: a.py")
        self.assertEqual(hermes_stdin._tool_title("search_files", {"pattern": "TODO"}), "Search: TODO")
        self.assertEqual(hermes_stdin._tool_title("kern_x", {}), "Tool: kern_x")

    def test_pre_tool_call_emits_a_started_record(self) -> None:
        records = _emitted(
            hermes_stdin._on_pre_tool_call,
            tool_name="terminal",
            args={"command": "ls", "workdir": "/tmp"},
            tool_call_id="call-9",
        )
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["provider"], "hermes")
        self.assertEqual(record["activity_id"], "call-9")
        self.assertEqual(record["kind"], "command")
        self.assertEqual(record["phase"], "started")
        self.assertEqual(record["title"], "ls")
        self.assertIn("/tmp", record["detail"])

    def test_post_tool_call_success_and_error(self) -> None:
        ok = _emitted(
            hermes_stdin._on_post_tool_call,
            tool_name="terminal",
            args={"command": "ls"},
            tool_call_id="call-9",
            result="file-a\nfile-b",
            status="ok",
        )[0]
        self.assertEqual(ok["phase"], "completed")
        self.assertEqual(ok["activity_id"], "call-9")
        self.assertEqual(ok["status"], "completed")
        self.assertEqual(ok["output"], "file-a\nfile-b")

        err = _emitted(
            hermes_stdin._on_post_tool_call,
            tool_name="terminal",
            args={"command": "nope"},
            tool_call_id="call-9",
            status="error",
            error_message="command not found",
        )[0]
        self.assertEqual(err["status"], "failed")
        self.assertEqual(err["output"], "command not found")

    def test_write_file_detail_omits_file_contents(self) -> None:
        record = _emitted(
            hermes_stdin._on_pre_tool_call,
            tool_name="write_file",
            args={"path": "big.txt", "content": "SECRET-PAYLOAD" * 1000},
            tool_call_id="w-1",
        )[0]
        self.assertEqual(record["kind"], "file_change")
        self.assertEqual(record["title"], "Write: big.txt")
        self.assertNotIn("SECRET-PAYLOAD", record.get("detail") or "")

    def test_large_output_is_clipped(self) -> None:
        record = _emitted(
            hermes_stdin._on_post_tool_call,
            tool_name="terminal",
            args={"command": "cat huge"},
            tool_call_id="c-1",
            result="x" * (hermes_stdin._OUTPUT_MAX_BYTES + 5000),
            status="ok",
        )[0]
        self.assertLessEqual(len(record["output"].encode("utf-8")), hermes_stdin._OUTPUT_MAX_BYTES + 64)
        self.assertTrue(record["output"].endswith("… (truncated)"))

    def test_hooks_never_raise_on_bad_input(self) -> None:
        # Best-effort emission: a broken payload must not surface an exception
        # into Hermes's tool loop.
        with redirect_stdout(io.StringIO()):
            self.assertIsNone(hermes_stdin._on_pre_tool_call(tool_name=None, args="not-a-dict"))
            self.assertIsNone(hermes_stdin._on_post_tool_call())

    def test_no_activity_is_emitted_without_a_nonce_marker(self) -> None:
        # With no per-turn marker set, the hooks stay silent: the untrusted
        # channel is only opened once the host mints and passes its nonce.
        previous = hermes_stdin._ACTIVITY_MARKER
        hermes_stdin._ACTIVITY_MARKER = None
        buffer = io.StringIO()
        try:
            with redirect_stdout(buffer):
                hermes_stdin._on_pre_tool_call(tool_name="terminal", args={"command": "ls"}, tool_call_id="c1")
                hermes_stdin._on_post_tool_call(tool_name="terminal", args={"command": "ls"}, result="ok", status="ok", tool_call_id="c1")
        finally:
            hermes_stdin._ACTIVITY_MARKER = previous
        self.assertEqual(buffer.getvalue(), "")

    def test_activity_prefix_matches_the_host_adapter(self) -> None:
        # The wrapper and the host adapter must agree byte-for-byte or the
        # host would treat activity lines as answer text.
        from host.runtime.agent_runtime import hermes_agent

        self.assertEqual(hermes_stdin.ACTIVITY_LINE_PREFIX, hermes_agent.ACTIVITY_LINE_PREFIX)


if __name__ == "__main__":
    unittest.main()
