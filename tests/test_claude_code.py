from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

from host.runtime.admin_api import claude_code, thread_scope


class ClaudeCodeTests(unittest.TestCase):
    def setUp(self) -> None:
        # The launch path reads the operator's web-search toggle from the
        # database, which these process-level tests do not provision.
        web_search = patch("host.runtime.core.state.read_claude_web_search", return_value=False)
        web_search.start()
        self.addCleanup(web_search.stop)

    def test_structured_assistant_content_emits_text_reasoning_and_tool(self) -> None:
        emitted = []
        claude_code._emit_claude_content(
            {
                "type": "assistant",
                "message": {
                    "id": "message-1",
                    "content": [
                        {"type": "thinking", "thinking": "I should inspect the repo."},
                        {"type": "text", "text": "I’m checking it now."},
                        {"type": "text", "text": "\nThen I’ll test it."},
                        {
                            "type": "tool_use",
                            "id": "tool-1",
                            "name": "Bash",
                            "input": {"command": "pytest -q"},
                        },
                    ],
                },
            },
            emitted.append,
        )

        self.assertEqual(emitted[0]["kind"], "reasoning")
        self.assertEqual(emitted[1], "I’m checking it now.\nThen I’ll test it.")
        self.assertEqual(emitted[2]["activity_id"], "tool-1")
        self.assertEqual(emitted[2]["title"], "pytest -q")
        self.assertEqual(emitted[2]["phase"], "started")

    def test_tool_result_completes_the_matching_activity(self) -> None:
        emitted = []
        claude_code._emit_claude_tool_results(
            {
                "type": "user",
                "message": {
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": "tool-1",
                        "content": "all tests passed",
                    }],
                },
            },
            emitted.append,
        )

        self.assertEqual(emitted[0]["activity_id"], "tool-1")
        self.assertEqual(emitted[0]["phase"], "completed")
        self.assertEqual(emitted[0]["output"], "all tests passed")

    def test_tool_progress_updates_the_matching_activity(self) -> None:
        emitted = []
        claude_code._emit_claude_stream_status(
            {
                "type": "tool_progress",
                "tool_use_id": "tool-1",
                "tool_name": "Bash",
                "elapsed_time_seconds": 4.5,
            },
            emitted.append,
        )

        self.assertEqual(emitted[0]["activity_id"], "tool-1")
        self.assertEqual(emitted[0]["phase"], "started")
        self.assertEqual(emitted[0]["title"], "Tool progress")
        self.assertEqual(emitted[0]["status"], "4.5s")

    def test_claude_tool_titles_cover_file_search_and_fallback_tools(self) -> None:
        cases = (
            ("Read", {"input": {"file_path": "/workspace/app.py"}}, "Read: /workspace/app.py"),
            ("Write", {"input": {"path": "/workspace/out.txt"}}, "Write: /workspace/out.txt"),
            ("Edit", {"input": {"file_path": "/workspace/edit.py"}}, "Edit: /workspace/edit.py"),
            ("Glob", {"input": {"pattern": "**/*.py"}}, "Glob: **/*.py"),
            ("Grep", {"input": {"pattern": "needle"}}, "Grep: needle"),
            ("Bash", {"input": {}}, "Shell command"),
            ("WebSearch", {"input": {"query": "Kern"}}, "Web search"),
            ("WebFetch", {"input": {"url": "https://example.com"}}, "Fetch web page"),
            ("CustomTool", {"input": {}}, "Tool: CustomTool"),
        )
        for name, block, expected in cases:
            with self.subTest(name=name):
                self.assertEqual(claude_code._claude_tool_title(name, block), expected)

    def test_claude_tools_map_to_provider_neutral_activity_kinds(self) -> None:
        expected = {
            "Bash": "command",
            "Write": "file_change",
            "Edit": "file_change",
            "WebSearch": "search",
            "WebFetch": "search",
            "Read": "tool",
            "Grep": "tool",
            "CustomTool": "tool",
        }
        for name, kind in expected.items():
            with self.subTest(name=name):
                self.assertEqual(claude_code._claude_tool_kind(name), kind)

    def test_server_tool_use_is_normalized_as_search_activity(self) -> None:
        emitted = []
        claude_code._emit_claude_content(
            {
                "message": {
                    "id": "message-1",
                    "content": [{
                        "type": "server_tool_use",
                        "name": "WebSearch",
                        "input": {"query": "latest release"},
                    }],
                },
            },
            emitted.append,
        )

        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0]["activity_id"], "message-1:0")
        self.assertEqual(emitted[0]["kind"], "search")
        self.assertEqual(emitted[0]["title"], "Web search")
        self.assertIn("latest release", emitted[0]["detail"])

    def test_claude_activity_id_fallback_order_is_stable(self) -> None:
        self.assertEqual(
            claude_code._claude_message_id(
                {"uuid": "message-uuid"},
                {"id": "block-id", "tool_use_id": "tool-id"},
                2,
            ),
            "block-id",
        )
        self.assertEqual(
            claude_code._claude_message_id(
                {"uuid": "message-uuid"},
                {"tool_use_id": "tool-id"},
                2,
            ),
            "tool-id",
        )
        self.assertEqual(
            claude_code._claude_message_id(
                {"uuid": "message-uuid"},
                {},
                2,
            ),
            "message-uuid",
        )
        self.assertEqual(
            claude_code._claude_message_id(
                {"message": {"id": "assistant-message"}},
                {},
                2,
            ),
            "assistant-message:2",
        )
        self.assertTrue(
            claude_code._claude_message_id({}, {}, 2).startswith("claude:2:")
        )

    def test_tool_results_cover_structured_errors_and_suffix_variants(self) -> None:
        emitted = []
        claude_code._emit_claude_tool_results(
            {
                "uuid": "user-message",
                "message": {
                    "content": [{
                        "type": "server_tool_result",
                        "tool_use_id": "tool-2",
                        "content": {"error": "request failed"},
                        "is_error": True,
                    }],
                },
            },
            emitted.append,
        )

        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0]["activity_id"], "tool-2")
        self.assertEqual(emitted[0]["status"], "failed")
        self.assertIn("request failed", emitted[0]["output"])

    def test_stream_status_covers_init_and_summary_events(self) -> None:
        emitted = []
        claude_code._emit_claude_stream_status(
            {
                "type": "system",
                "subtype": "init",
                "uuid": "init-1",
                "model": "claude-opus-5",
                "cwd": "/workspace",
                "tools": ["Bash"],
                "permissionMode": "bypassPermissions",
                "claude_code_version": "1.2.3",
            },
            emitted.append,
        )
        for message_type in ("tool_use_summary", "rate_limit_event", "auth_status"):
            claude_code._emit_claude_stream_status(
                {
                    "type": message_type,
                    "uuid": message_type,
                    "status": "ok",
                },
                emitted.append,
            )

        self.assertEqual(
            [event["title"] for event in emitted],
            [
                "Claude session initialized",
                "Tool summary",
                "Rate limit status",
                "Authentication status",
            ],
        )
        self.assertIn("/workspace", emitted[0]["detail"])
        self.assertTrue(all(event["phase"] == "completed" for event in emitted))

    def test_malformed_claude_content_is_ignored(self) -> None:
        emitted = []
        for message in (
            {},
            {"message": "not an object"},
            {"message": {"content": "not a list"}},
            {"message": {"content": ["not a block", {"type": "unknown"}]}},
        ):
            claude_code._emit_claude_content(message, emitted.append)
            claude_code._emit_claude_tool_results(message, emitted.append)

        self.assertEqual(emitted, [])

    def test_one_malformed_claude_block_does_not_hide_later_content(self) -> None:
        emitted = []
        with patch(
            "host.runtime.admin_api.claude_code._claude_tool_title",
            side_effect=ValueError("malformed provider block"),
        ):
            claude_code._emit_claude_content(
                {
                    "message": {
                        "content": [
                            {"type": "tool_use", "name": "Broken", "input": {}},
                            {"type": "text", "text": "The remaining message is visible."},
                        ],
                    },
                },
                emitted.append,
            )

        self.assertEqual(emitted, ["The remaining message is visible."])

    def test_claude_parser_does_not_hide_persistence_failures(self) -> None:
        def reject(_message: str | dict[str, object]) -> None:
            raise RuntimeError("database unavailable")

        with self.assertRaisesRegex(RuntimeError, "database unavailable"):
            claude_code._emit_claude_content(
                {"message": {"content": [{"type": "text", "text": "persist me"}]}},
                reject,
            )

    def test_thread_scope_is_separate_from_the_launcher_command(self) -> None:
        # The web-search decision must remain the launcher's first argument, so
        # app attribution stores its scope id separately from the command.
        session = claude_code.ClaudeCodeSession(command=["/bin/echo"], thread_id="sample_app__ws-3")
        self.assertEqual(session._command, ["/bin/echo"])
        self.assertEqual(session._thread_id, "sample_app__ws-3")
        self.assertEqual(claude_code.ClaudeCodeSession(command=["/bin/echo"])._command, ["/bin/echo"])
        self.assertIsNone(claude_code._subprocess_cwd(claude_code.DEFAULT_COMMAND))
        self.assertEqual(claude_code._subprocess_cwd(session._command), claude_code.AGENT_CWD)

    def test_close_stops_the_thread_scope_under_the_production_launcher(self) -> None:
        # A killed turn's scope keeps the thread name until its whole cgroup is
        # gone, so close() must stop the scope by name before the next task on
        # this thread recreates it.
        session = claude_code.ClaudeCodeSession(
            command=claude_code.DEFAULT_COMMAND, thread_id="stage-1-smoke-kill-claude"
        )
        with patch.object(thread_scope.subprocess, "run", return_value=MagicMock(returncode=0)) as run:
            session.close()
        run.assert_called_once()
        self.assertEqual(
            run.call_args.args[0],
            [*thread_scope.STOP_COMMAND, "stage-1-smoke-kill-claude"],
        )

    def test_close_stops_the_scope_when_the_cli_outlives_stdin_eof(self) -> None:
        # Claude Code keeps running after stdin EOF while it still has live
        # background subagents, and the production launcher runs as root, so the
        # unprivileged kill fails with EPERM. close() must not wait on that
        # process's pipes: the reader threads hold each buffer's lock across
        # their blocking read, so closing a pipe from this thread would block
        # until the CLI happens to write to it — forever, for a silent stderr.
        # A close that never returns leaves the orchestrator's thread fence up
        # for good (every later message on that thread stays queued) and never
        # reaches the scope teardown that is the only real kill.
        script = r"""
import sys, time
for line in sys.stdin:
    pass
time.sleep(120)
"""
        session = claude_code.ClaudeCodeSession(
            command=claude_code.DEFAULT_COMMAND, thread_id="stage-1-outlives-eof"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", script],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        session._proc = proc
        assert proc.stdout is not None and proc.stderr is not None
        threading.Thread(target=session._read_stdout, args=(proc.stdout,), daemon=True).start()
        threading.Thread(target=session._read_stderr, args=(proc.stderr,), daemon=True).start()

        returned = threading.Event()
        with patch.object(thread_scope.subprocess, "run", return_value=MagicMock(returncode=0)) as run, patch.object(
            proc, "kill", side_effect=PermissionError(1, "Operation not permitted")
        ):
            closer = threading.Thread(
                target=lambda: (session.close(), returned.set()), daemon=True
            )
            closer.start()
            # Two five-second waits around the denied kill, and nothing more.
            self.assertTrue(returned.wait(timeout=30), "close() blocked on the agent's pipes")
            closer.join(timeout=5)
        run.assert_called_once()
        self.assertEqual(
            run.call_args.args[0],
            [*thread_scope.STOP_COMMAND, "stage-1-outlives-eof"],
        )

    def test_close_stops_the_scope_even_when_the_shutdown_raises(self) -> None:
        # The scope teardown frees the thread, so a surprise from the process
        # handle must not skip it and wedge the thread permanently.
        session = claude_code.ClaudeCodeSession(
            command=claude_code.DEFAULT_COMMAND, thread_id="stage-1-raising-close"
        )
        session._proc = SimpleNamespace(  # type: ignore[assignment]
            stdin=None,
            stdout=None,
            stderr=None,
            wait=Mock(side_effect=RuntimeError("handle is gone")),
        )
        with patch.object(thread_scope.subprocess, "run", return_value=MagicMock(returncode=0)) as run:
            with self.assertRaises(RuntimeError):
                session.close()
        run.assert_called_once()
        self.assertEqual(
            run.call_args.args[0],
            [*thread_scope.STOP_COMMAND, "stage-1-raising-close"],
        )

    def test_close_does_not_stop_a_scope_for_a_test_command_or_threadless_turn(self) -> None:
        for session in (
            claude_code.ClaudeCodeSession(command=["/bin/echo"], thread_id="sample_app__ws-3"),
            claude_code.ClaudeCodeSession(command=claude_code.DEFAULT_COMMAND, thread_id=None),
        ):
            with patch.object(thread_scope.subprocess, "run", return_value=MagicMock(returncode=0)) as run:
                session.close()
            run.assert_not_called()

    def test_run_after_close_rejects_before_spawning_the_cli(self) -> None:
        session = claude_code.ClaudeCodeSession(command=["/bin/echo"])
        session.close()
        with (
            patch("host.runtime.core.state.read_claude_web_search", return_value=False),
            patch.object(claude_code.subprocess, "Popen") as popen,
            self.assertRaisesRegex(claude_code.ClaudeCodeError, "turn was closed"),
        ):
            session.run(
                "must not run",
                None,
                "claude-sonnet-5",
                "high",
                lambda _message: None,
            )
        popen.assert_not_called()

    def test_steer_flushes_interrupt_then_message_without_waiting(self) -> None:
        session = claude_code.ClaudeCodeSession(command=["fake-claude"])
        writes: list[dict[str, object]] = []

        class RecordingStdin:
            def write(self, value: str) -> int:
                writes.append(json.loads(value))
                return len(value)

            def flush(self) -> None:
                return

        session._proc = SimpleNamespace(  # type: ignore[assignment]
            stdin=RecordingStdin(),
            poll=lambda: None,
        )
        session._accepting_steers = True

        session.steer("respond now")

        self.assertEqual(
            [message["type"] for message in writes],
            ["control_request", "user"],
        )
        self.assertEqual(
            writes[0]["request"],
            {"subtype": "interrupt", "cancel_queued": True},
        )
        self.assertEqual(
            writes[1]["message"],
            {"role": "user", "content": "respond now"},
        )
        self.assertIsInstance(writes[1].get("uuid"), str)
        self.assertEqual(session.take_delivered_steers(), 1)

    def test_steer_message_write_failure_is_not_counted_as_delivered(self) -> None:
        session = claude_code.ClaudeCodeSession(command=["fake-claude"])
        writes: list[dict[str, object]] = []

        class FailingStdin:
            def write(self, value: str) -> int:
                message = json.loads(value)
                if message["type"] == "user":
                    raise BrokenPipeError("closed")
                writes.append(message)
                return len(value)

            def flush(self) -> None:
                return

        session._proc = SimpleNamespace(  # type: ignore[assignment]
            stdin=FailingStdin(),
            poll=lambda: None,
        )
        session._accepting_steers = True

        with self.assertRaisesRegex(claude_code.ClaudeCodeError, "closed"):
            session.steer("must not be sent")

        self.assertEqual([message["type"] for message in writes], ["control_request"])
        self.assertEqual(session.take_delivered_steers(), 0)

    def test_rapid_steers_keep_each_interrupt_next_to_its_message(self) -> None:
        session = claude_code.ClaudeCodeSession(command=["fake-claude"])
        writes: list[dict[str, object]] = []

        class RecordingStdin:
            def write(self, value: str) -> int:
                writes.append(json.loads(value))
                return len(value)

            def flush(self) -> None:
                return

        session._proc = SimpleNamespace(  # type: ignore[assignment]
            stdin=RecordingStdin(),
            poll=lambda: None,
        )
        session._accepting_steers = True
        workers = [
            threading.Thread(target=session.steer, args=(text,))
            for text in ("first", "second")
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=1)

        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(
            [message["type"] for message in writes],
            ["control_request", "user", "control_request", "user"],
        )
        self.assertEqual(session.take_delivered_steers(), 2)

    def test_stdout_queues_ordered_responses_for_own_interrupts(self) -> None:
        session = claude_code.ClaudeCodeSession(command=["fake-claude"])
        frames = [
            {
                "type": "control_response",
                "response": {
                    "subtype": "success",
                    "request_id": "another-control-request",
                },
            },
            {
                "type": "control_response",
                "response": {
                    "subtype": "error",
                    "request_id": "kern-interrupt-1",
                },
            },
            {
                "type": "control_response",
                "response": {
                    "subtype": "success",
                    "request_id": "kern-interrupt-2",
                    "response": {"cancelled": ["message-1"]},
                },
            },
            {"type": "result", "subtype": "success"},
        ]
        session._read_stdout(io.StringIO(
            "".join(json.dumps(frame) + "\n" for frame in frames)
        ))

        self.assertEqual(
            [
                session._messages.get_nowait(),
                session._messages.get_nowait(),
                session._messages.get_nowait(),
            ],
            [
                {
                    "type": claude_code.INTERRUPT_RESPONSE_MESSAGE_TYPE,
                    "interrupt_id": 1,
                },
                {
                    "type": claude_code.INTERRUPT_RESPONSE_MESSAGE_TYPE,
                    "interrupt_id": 2,
                },
                {"type": "result", "subtype": "success"},
            ],
        )

    def test_read_claude_account_reads_helper_json(self) -> None:
        command = [
            sys.executable,
            "-c",
            "import json; print(json.dumps({'account_id':'acct','organization_id':'org','access_token_sha256':'hash'}))",
        ]
        self.assertEqual(
            claude_code.read_claude_account(command),
            {"account_id": "acct", "organization_id": "org", "access_token_sha256": "hash"},
        )
        self.assertIsNone(claude_code.read_claude_account([sys.executable, "-c", "import sys; sys.exit(1)"]))

    def test_read_attested_identity_parses_helper_json(self) -> None:
        command = [
            sys.executable,
            "-c",
            "import json; print(json.dumps({'access_token_sha256':'hash','account_uuid':'acct','email':'op@example.com'}))",
        ]
        self.assertEqual(
            claude_code.read_attested_identity(command),
            {"access_token_sha256": "hash", "account_uuid": "acct", "email": "op@example.com"},
        )

    def test_read_attested_identity_passes_expected_token_hash(self) -> None:
        command = [
            sys.executable,
            "-c",
            (
                "import json, sys; "
                "assert sys.argv[-2:] == ['--expected-token-sha256', 'hash']; "
                "print(json.dumps({'access_token_sha256':'hash','account_uuid':'acct'}))"
            ),
        ]
        self.assertEqual(
            claude_code.read_attested_identity(command, expected_token_sha256="hash"),
            {"access_token_sha256": "hash", "account_uuid": "acct"},
        )

    def test_read_attested_identity_raises_with_helper_stderr_detail(self) -> None:
        command = [
            sys.executable,
            "-c",
            "import sys; print('could not reach the Claude profile endpoint', file=sys.stderr); sys.exit(1)",
        ]
        with self.assertRaises(claude_code.ClaudeCodeError) as error:
            claude_code.read_attested_identity(command)
        self.assertIn("could not reach the Claude profile endpoint", str(error.exception))

    def test_read_attested_identity_rejects_incomplete_response(self) -> None:
        command = [sys.executable, "-c", "import json; print(json.dumps({'account_uuid': 'acct'}))"]
        with self.assertRaises(claude_code.ClaudeCodeError):
            claude_code.read_attested_identity(command)

    def test_account_status_maps_missing_helper_to_awaiting_login(self) -> None:
        original_command = claude_code.DEFAULT_COMMAND
        original_account = claude_code.DEFAULT_ACCOUNT_COMMAND
        claude_code.DEFAULT_COMMAND = [sys.executable, "-c", "import sys; sys.exit(1)", "--"]
        claude_code.DEFAULT_ACCOUNT_COMMAND = [sys.executable, "-c", "import sys; sys.exit(2)"]
        try:
            self.assertEqual(claude_code.account_status(), ("awaiting_login", None, None))
        finally:
            claude_code.DEFAULT_COMMAND = original_command
            claude_code.DEFAULT_ACCOUNT_COMMAND = original_account

    def test_account_status_requires_claude_ai_oauth_and_account_pin(self) -> None:
        original_command = claude_code.DEFAULT_COMMAND
        original_account = claude_code.DEFAULT_ACCOUNT_COMMAND
        claude_code.DEFAULT_COMMAND = [
            sys.executable,
            "-c",
            "import json; print(json.dumps({'loggedIn': True, 'authMethod': 'claude.ai'}))",
            "--",
        ]
        claude_code.DEFAULT_ACCOUNT_COMMAND = [
            sys.executable,
            "-c",
            "import json; print(json.dumps({'account_id':'acct','organization_id':'org','access_token_sha256':'hash'}))",
        ]
        try:
            self.assertEqual(
                claude_code.account_status(),
                ("active", None, {"account_id": "acct", "organization_id": "org", "access_token_sha256": "hash"}),
            )
            claude_code.DEFAULT_COMMAND = [
                sys.executable,
                "-c",
                "import json; print(json.dumps({'loggedIn': True, 'authMethod': 'console'}))",
                "--",
            ]
            status, detail, account = claude_code.account_status()
            self.assertEqual(status, "error")
            self.assertIn("Claude.ai OAuth", detail or "")
            self.assertIsNone(account)
        finally:
            claude_code.DEFAULT_COMMAND = original_command
            claude_code.DEFAULT_ACCOUNT_COMMAND = original_account

    def test_account_status_fills_metadata_from_status_when_helper_has_only_token_hash(self) -> None:
        original_command = claude_code.DEFAULT_COMMAND
        original_account = claude_code.DEFAULT_ACCOUNT_COMMAND
        claude_code.DEFAULT_COMMAND = [
            sys.executable,
            "-c",
            (
                "import json; print(json.dumps({"
                "'loggedIn': True, 'authMethod': 'claude.ai', "
                "'email': 'user@example.com', 'orgId': 'org_123', "
                "'subscriptionType': 'max'}))"
            ),
            "--",
        ]
        claude_code.DEFAULT_ACCOUNT_COMMAND = [
            sys.executable,
            "-c",
            "import json; print(json.dumps({'access_token_sha256':'hash'}))",
        ]
        try:
            self.assertEqual(
                claude_code.account_status(),
                (
                    "active",
                    None,
                    {
                        # No account_id from CLI output: the trusted id is
                        # always set by the orchestrator's anchor/attestation.
                        "access_token_sha256": "hash",
                        "email": "user@example.com",
                        "organization_id": "org_123",
                        "plan_type": "max",
                    },
                ),
            )
        finally:
            claude_code.DEFAULT_COMMAND = original_command
            claude_code.DEFAULT_ACCOUNT_COMMAND = original_account

    def test_read_claude_usage_parses_usage_result_text(self) -> None:
        command = [
            sys.executable,
            "-c",
            (
                "import json; print(json.dumps({"
                "'type': 'result', "
                "'result': 'You are currently using your subscription to power your Claude Code usage\\n\\n"
                "Current session: 0% used · resets Jul 2, 1am (UTC)\\n"
                "Current week (all models): 12.5% used · resets Jul 2, 3:59pm (UTC)\\n"
                "Current week (Fable): 78% used · resets Jul 2, 3:59pm (UTC)'"
                "}))"
            ),
        ]

        year = datetime.now(timezone.utc).year
        self.assertEqual(claude_code.read_claude_usage(command), {
            "current_session_used_percent": 0,
            "current_session_resets_at": int(datetime(year, 7, 2, 1, tzinfo=timezone.utc).timestamp()),
            "weekly_used_percent": 12.5,
            "weekly_resets_at": int(datetime(year, 7, 2, 15, 59, tzinfo=timezone.utc).timestamp()),
            "fable_weekly_used_percent": 78,
            "fable_weekly_resets_at": int(datetime(year, 7, 2, 15, 59, tzinfo=timezone.utc).timestamp()),
        })

    def test_parse_claude_usage_assigns_early_january_reset_to_next_year(self) -> None:
        result = (
            "Current session: 1% used · resets Jan 1, 1am (UTC)\n"
            "Current week (all models): 2% used · resets Jan 2, 3:59pm (UTC)"
        )

        self.assertEqual(
            claude_code._parse_claude_usage_result(
                result,
                now=datetime(2026, 12, 31, 12, tzinfo=timezone.utc),
            ),
            {
                "current_session_used_percent": 1,
                "current_session_resets_at": int(datetime(2027, 1, 1, 1, tzinfo=timezone.utc).timestamp()),
                "weekly_used_percent": 2,
                "weekly_resets_at": int(datetime(2027, 1, 2, 15, 59, tzinfo=timezone.utc).timestamp()),
            },
        )

    def test_parse_claude_usage_drops_only_the_invalid_reset_date(self) -> None:
        result = (
            "Current session: 1% used · resets Feb 30, 1am (UTC)\n"
            "Current week (all models): 2% used · resets Mar 1, 3:59pm (UTC)"
        )

        self.assertEqual(
            claude_code._parse_claude_usage_result(
                result,
                now=datetime(2026, 2, 1, tzinfo=timezone.utc),
            ),
            {
                "current_session_used_percent": 1,
                "weekly_used_percent": 2,
                "weekly_resets_at": int(datetime(2026, 3, 1, 15, 59, tzinfo=timezone.utc).timestamp()),
            },
        )

    def test_parse_claude_usage_keeps_percent_when_reset_is_missing_or_foreign(self) -> None:
        # A window with no reset clause, or one in an unrecognized timezone,
        # still contributes its percent; only its resets_at is omitted.
        result = (
            "Current session: 3% used\n"
            "Current week (all models): 4% used · resets Jul 14, 4:30am (Asia/Calcutta)"
        )

        self.assertEqual(
            claude_code._parse_claude_usage_result(result, now=datetime(2026, 7, 13, tzinfo=timezone.utc)),
            {"current_session_used_percent": 3, "weekly_used_percent": 4},
        )

    def test_parse_claude_usage_windows_parse_independently(self) -> None:
        # A drifted session line must not blank the windows that still parse.
        result = (
            "Current session: no usage recorded yet\n"
            "Current week (all models): 5% used · resets Jul 14, 1am (UTC)\n"
            "Current week (Fable): 6% used · resets Jul 14, 1am (UTC)"
        )

        self.assertEqual(
            claude_code._parse_claude_usage_result(result, now=datetime(2026, 7, 13, tzinfo=timezone.utc)),
            {
                "weekly_used_percent": 5,
                "weekly_resets_at": int(datetime(2026, 7, 14, 1, tzinfo=timezone.utc).timestamp()),
                "fable_weekly_used_percent": 6,
                "fable_weekly_resets_at": int(datetime(2026, 7, 14, 1, tzinfo=timezone.utc).timestamp()),
            },
        )

    def test_parse_claude_usage_tracks_only_the_fable_model_week(self) -> None:
        # The Fable week is captured under fixed keys; other model weeks are
        # ignored, and the first Fable line wins.
        result = (
            "Current week (Fable): 6% used\n"
            "Current week (Fable): 99% used\n"
            "Current week (Opus): 42% used"
        )

        self.assertEqual(
            claude_code._parse_claude_usage_result(result, now=datetime(2026, 7, 13, tzinfo=timezone.utc)),
            {"fable_weekly_used_percent": 6},
        )

    def test_read_claude_usage_ignores_unknown_result_text(self) -> None:
        command = [sys.executable, "-c", "import json; print(json.dumps({'result': 'not usage'}))"]

        self.assertEqual(claude_code.read_claude_usage(command), {})

    def test_read_claude_usage_rejects_invalid_oauth_even_when_cli_exits_zero(self) -> None:
        command = [
            sys.executable,
            "-c",
            (
                "import json; print(json.dumps({"
                "'type': 'result', 'subtype': 'error', 'is_error': True, "
                "'result': 'Failed to authenticate. API Error: 401 Invalid authentication credentials'"
                "}))"
            ),
        ]

        with self.assertRaises(claude_code.ClaudeAuthenticationError):
            claude_code.read_claude_usage(command)

    def test_account_status_reads_helper_identity_metadata(self) -> None:
        original_command = claude_code.DEFAULT_COMMAND
        original_account = claude_code.DEFAULT_ACCOUNT_COMMAND
        claude_code.DEFAULT_COMMAND = [
            sys.executable,
            "-c",
            "import json; print(json.dumps({'loggedIn': True, 'authMethod': 'claude.ai'}))",
            "--",
        ]
        claude_code.DEFAULT_ACCOUNT_COMMAND = [
            sys.executable,
            "-c",
            "import json; print(json.dumps({'access_token_sha256':'hash','account_id':'acct'}))",
        ]
        try:
            self.assertEqual(
                claude_code.account_status(),
                (
                    "active",
                    None,
                    {
                        "access_token_sha256": "hash",
                        "account_id": "acct",
                    },
                ),
            )
        finally:
            claude_code.DEFAULT_COMMAND = original_command
            claude_code.DEFAULT_ACCOUNT_COMMAND = original_account

    def test_account_status_errors_when_logged_in_but_token_hash_is_missing(self) -> None:
        original_command = claude_code.DEFAULT_COMMAND
        original_account = claude_code.DEFAULT_ACCOUNT_COMMAND
        claude_code.DEFAULT_COMMAND = [
            sys.executable,
            "-c",
            "import json; print(json.dumps({'loggedIn': True, 'authMethod': 'claude.ai'}))",
            "--",
        ]
        claude_code.DEFAULT_ACCOUNT_COMMAND = [sys.executable, "-c", "import sys; sys.exit(1)"]
        try:
            status, detail, account = claude_code.account_status()
            self.assertEqual(status, "error")
            self.assertIn("OAuth token metadata", detail or "")
            self.assertIsNone(account)
        finally:
            claude_code.DEFAULT_COMMAND = original_command
            claude_code.DEFAULT_ACCOUNT_COMMAND = original_account

    def test_login_process_extracts_login_url(self) -> None:
        script = (
            "import sys, time; "
            "print('Opening browser to sign in...'); "
            "print('If the browser didn\\'t open, visit: https://claude.com/cai/oauth/authorize?code=true'); "
            "sys.stdout.flush(); "
            "time.sleep(1)"
        )
        process = claude_code.ClaudeLoginProcess([sys.executable, "-u", "-c", script, "--"])
        original_cwd = claude_code.AGENT_CWD
        try:
            with tempfile.TemporaryDirectory() as tmp:
                claude_code.AGENT_CWD = tmp
                login = process.start()
                self.assertEqual(login.login_url, "https://claude.com/cai/oauth/authorize?code=true")
        finally:
            claude_code.AGENT_CWD = original_cwd
            process.close()

    def test_default_login_helper_does_not_require_admin_access_to_agent_home(self) -> None:
        script = (
            "import sys, time; "
            "print('If the browser didn\\'t open, visit: https://claude.com/cai/oauth/authorize?code=true'); "
            "sys.stdout.flush(); "
            "time.sleep(1)"
        )
        original_command = claude_code.DEFAULT_COMMAND
        original_cwd = claude_code.AGENT_CWD
        process = None
        try:
            claude_code.DEFAULT_COMMAND = [sys.executable, "-u", "-c", script, "--"]
            claude_code.AGENT_CWD = "/definitely/not-readable-by-admin"
            process = claude_code.ClaudeLoginProcess()
            login = process.start()
            self.assertEqual(login.login_url, "https://claude.com/cai/oauth/authorize?code=true")
        finally:
            claude_code.DEFAULT_COMMAND = original_command
            claude_code.AGENT_CWD = original_cwd
            if process is not None:
                process.close()

    def test_login_process_times_out_without_login_url(self) -> None:
        process = claude_code.ClaudeLoginProcess(
            [sys.executable, "-u", "-c", "import time; time.sleep(5)", "--"],
            start_timeout=0.1,
        )
        original_cwd = claude_code.AGENT_CWD
        try:
            with tempfile.TemporaryDirectory() as tmp:
                claude_code.AGENT_CWD = tmp
                with self.assertRaises(claude_code.ClaudeTimeout):
                    process.start()
        finally:
            claude_code.AGENT_CWD = original_cwd
            process.close()

    def test_complete_oauth_login_always_closes_process(self) -> None:
        class FakeLoginProcess:
            completed_code: str | None = None
            closed = False

            def complete(self, code: str) -> None:
                self.completed_code = code

            def close(self) -> None:
                self.closed = True

        process = FakeLoginProcess()
        with claude_code._login_lock:
            original = claude_code._login_process
            claude_code._login_process = process  # type: ignore[assignment]
        try:
            claude_code.complete_oauth_login("CODE-123")
            self.assertEqual(process.completed_code, "CODE-123")
            self.assertTrue(process.closed)
            self.assertIsNone(claude_code._login_process)
        finally:
            with claude_code._login_lock:
                claude_code._login_process = original

    def test_close_login_process_clears_handle_when_close_fails(self) -> None:
        class FakeLoginProcess:
            def close(self) -> None:
                raise PermissionError("cannot signal helper")

        process = FakeLoginProcess()
        with claude_code._login_lock:
            original = claude_code._login_process
            claude_code._login_process = process  # type: ignore[assignment]
        try:
            with self.assertRaises(PermissionError):
                claude_code.close_login_process()
            self.assertIsNone(claude_code._login_process)
        finally:
            with claude_code._login_lock:
                claude_code._login_process = original

    def test_run_turn_waits_for_result_after_delivered_steer(self) -> None:
        # The fake CLI acknowledges the interrupt before accepting the steer.
        # The interrupted result belongs to the initial query; the following
        # success result belongs to the steered message and owns the host turn.
        script = r"""
import json, sys

session_id = "session-1"


def assistant(text):
    print(json.dumps({
        "type": "assistant",
        "session_id": session_id,
        "message": {"content": [{"type": "text", "text": text}]},
    }), flush=True)


def result(text):
    print(json.dumps({
        "type": "result",
        "subtype": "success",
        "session_id": session_id,
        "result": text,
    }), flush=True)


json.loads(sys.stdin.readline())
assistant("FIRST")
interrupt = json.loads(sys.stdin.readline())
assert interrupt["type"] == "control_request"
assert interrupt["request"]["subtype"] == "interrupt"
print(json.dumps({
    "type": "control_response",
    "response": {
        "subtype": "success",
        "request_id": interrupt["request_id"],
        "response": {"still_queued": []},
    },
}), flush=True)
steer = json.loads(sys.stdin.readline())
assert steer["message"]["content"] == "steer"
print(json.dumps({
    "type": "result",
    "subtype": "error_during_execution",
    "is_error": True,
    "terminal_reason": "aborted_tools",
    "session_id": session_id,
}), flush=True)
assistant("STEERED")
result("STEERED")
sys.stdin.readline()  # stay alive like the real CLI until stdin EOF
"""
        original_cwd = claude_code.AGENT_CWD
        first_message = threading.Event()
        result: list[tuple[str, str]] = []
        try:
            with tempfile.TemporaryDirectory() as tmp:
                claude_code.AGENT_CWD = tmp
                server = claude_code.ClaudeCodeSession([sys.executable, "-u", "-c", script])
                # The fake CLI idles on stdin after the turn, so without this
                # the child process, its pipes, and the reader threads outlive
                # the test.
                self.addCleanup(server.close)
                worker = threading.Thread(
                    target=lambda: result.append(
                        claude_code.run_turn(
                            server,
                            "initial",
                            None,
                            "claude-opus-5",
                            "high",
                            lambda _message: first_message.set(),
                        )
                    )
                )
                worker.start()
                self.assertTrue(first_message.wait(timeout=10))
                server.steer("steer")
                worker.join(timeout=10)
                self.assertFalse(worker.is_alive())
        finally:
            claude_code.AGENT_CWD = original_cwd
        self.assertEqual(len(result), 1)
        session_id, output = result[0]
        self.assertEqual(session_id, "session-1")
        self.assertEqual(output, "STEERED")

    def test_queued_old_result_waits_for_latest_interrupt_response(self) -> None:
        # Keep the turn driver blocked while stdout queues an initial success,
        # then two interrupt responses. The old result cannot finish ahead of
        # the newest response and replacement result.
        script = r"""
import json, sys

json.loads(sys.stdin.readline())
print(json.dumps({
    "type": "assistant",
    "session_id": "session-1",
    "message": {"content": [{"type": "text", "text": "READY"}]},
}), flush=True)
print(json.dumps({
    "type": "result",
    "subtype": "success",
    "session_id": "session-1",
    "result": "FIRST",
}), flush=True)

for expected in ("first steer", "second steer"):
    interrupt = json.loads(sys.stdin.readline())
    assert interrupt["request"]["subtype"] == "interrupt"
    print(json.dumps({
        "type": "control_response",
        "response": {
            "subtype": "success",
            "request_id": interrupt["request_id"],
        },
    }), flush=True)
    steer = json.loads(sys.stdin.readline())
    assert steer["message"]["content"] == expected

print(json.dumps({
    "type": "result",
    "subtype": "error_during_execution",
    "is_error": True,
    "terminal_reason": "aborted_tools",
    "session_id": "session-1",
}), flush=True)
print(json.dumps({
    "type": "result",
    "subtype": "success",
    "session_id": "session-1",
    "result": "SECOND",
}), flush=True)
sys.stdin.readline()  # stay alive until the test closes stdin
"""
        original_cwd = claude_code.AGENT_CWD
        ready = threading.Event()
        release_driver = threading.Event()
        result: list[tuple[str, str]] = []
        errors: list[Exception] = []

        def hold_run_driver(_message: str | dict[str, object]) -> None:
            ready.set()
            release_driver.wait(timeout=10)

        def run() -> None:
            try:
                result.append(claude_code.run_turn(
                    server,
                    "initial",
                    None,
                    "claude-opus-5",
                    "high",
                    hold_run_driver,
                ))
            except Exception as exc:
                errors.append(exc)

        try:
            with tempfile.TemporaryDirectory() as tmp:
                claude_code.AGENT_CWD = tmp
                server = claude_code.ClaudeCodeSession(
                    [sys.executable, "-u", "-c", script]
                )
                self.addCleanup(server.close)
                worker = threading.Thread(target=run)
                worker.start()
                self.assertTrue(ready.wait(timeout=10))
                try:
                    server.steer("first steer")
                    server.steer("second steer")
                finally:
                    release_driver.set()
                worker.join(timeout=10)
                self.assertFalse(worker.is_alive())
        finally:
            release_driver.set()
            claude_code.AGENT_CWD = original_cwd

        self.assertEqual(errors, [])
        self.assertEqual(result, [("session-1", "SECOND")])

    def test_rapid_steer_cancelled_replacement_finishes_without_delay(self) -> None:
        # The second cancel_queued interrupt removes the first replacement;
        # the newest replacement result finishes immediately.
        script = r"""
import json, sys

initial = json.loads(sys.stdin.readline())
assert isinstance(initial.get("uuid"), str)
print(json.dumps({
    "type": "assistant",
    "session_id": "session-1",
    "message": {"content": [{"type": "text", "text": "READY"}]},
}), flush=True)

first_interrupt = json.loads(sys.stdin.readline())
print(json.dumps({
    "type": "control_response",
    "response": {
        "subtype": "success",
        "request_id": first_interrupt["request_id"],
        "response": {"still_queued": [], "cancelled": []},
    },
}), flush=True)
first = json.loads(sys.stdin.readline())
assert first["message"]["content"] == "first steer"
assert isinstance(first.get("uuid"), str)

second_interrupt = json.loads(sys.stdin.readline())
print(json.dumps({
    "type": "control_response",
    "response": {
        "subtype": "success",
        "request_id": second_interrupt["request_id"],
        "response": {
            "still_queued": [],
            "cancelled": [first["uuid"]],
        },
    },
}), flush=True)
second = json.loads(sys.stdin.readline())
assert second["message"]["content"] == "second steer"
assert isinstance(second.get("uuid"), str)

print(json.dumps({
    "type": "result",
    "subtype": "error_during_execution",
    "is_error": True,
    "terminal_reason": "aborted_streaming",
    "session_id": "session-1",
}), flush=True)
print(json.dumps({
    "type": "result",
    "subtype": "success",
    "session_id": "session-1",
    "result": "SECOND",
}), flush=True)
sys.stdin.readline()  # stay alive until the test closes stdin
"""
        original_cwd = claude_code.AGENT_CWD
        ready = threading.Event()
        result: list[tuple[str, str]] = []
        errors: list[Exception] = []

        def run() -> None:
            try:
                result.append(claude_code.run_turn(
                    server,
                    "initial",
                    None,
                    "claude-opus-5",
                    "high",
                    lambda _message: ready.set(),
                ))
            except Exception as exc:
                errors.append(exc)

        try:
            with tempfile.TemporaryDirectory() as tmp:
                claude_code.AGENT_CWD = tmp
                server = claude_code.ClaudeCodeSession(
                    [sys.executable, "-u", "-c", script]
                )
                self.addCleanup(server.close)
                worker = threading.Thread(target=run)
                worker.start()
                self.assertTrue(ready.wait(timeout=10))
                server.steer("first steer")
                started = time.monotonic()
                server.steer("second steer")
                worker.join(timeout=1)
                elapsed = time.monotonic() - started
                self.assertFalse(worker.is_alive())
        finally:
            claude_code.AGENT_CWD = original_cwd

        self.assertLess(elapsed, 1)
        self.assertEqual(errors, [])
        self.assertEqual(result, [("session-1", "SECOND")])

    def test_startup_cancelled_initial_finishes_without_delay(self) -> None:
        # A startup steer can cancel the uuid-stamped initial prompt while it
        # is still in Claude's pre-dispatch window. No abort result exists for
        # that prompt; the replacement result is the only completion needed.
        script = r"""
import json, sys

initial = json.loads(sys.stdin.readline())
assert isinstance(initial.get("uuid"), str)
interrupt = json.loads(sys.stdin.readline())
print(json.dumps({
    "type": "control_response",
    "response": {
        "subtype": "success",
        "request_id": interrupt["request_id"],
        "response": {
            "still_queued": [],
            "cancelled": [initial["uuid"]],
        },
    },
}), flush=True)
replacement = json.loads(sys.stdin.readline())
assert replacement["message"]["content"] == "startup steer"
print(json.dumps({
    "type": "result",
    "subtype": "success",
    "session_id": "session-1",
    "result": "STARTUP",
}), flush=True)
sys.stdin.readline()  # stay alive until the test closes stdin
"""
        original_cwd = claude_code.AGENT_CWD
        ready = threading.Event()
        result: list[tuple[str, str]] = []
        errors: list[Exception] = []

        def run() -> None:
            try:
                result.append(claude_code.run_turn(
                    server,
                    "initial",
                    None,
                    "claude-opus-5",
                    "high",
                    lambda _message: None,
                ))
            except Exception as exc:
                errors.append(exc)

        try:
            with tempfile.TemporaryDirectory() as tmp:
                claude_code.AGENT_CWD = tmp
                server = claude_code.ClaudeCodeSession(
                    [sys.executable, "-u", "-c", script],
                    on_ready=lambda: ready.set() or True,
                )
                self.addCleanup(server.close)
                worker = threading.Thread(target=run)
                worker.start()
                self.assertTrue(ready.wait(timeout=10))
                started = time.monotonic()
                server.steer("startup steer")
                worker.join(timeout=1)
                elapsed = time.monotonic() - started
                self.assertFalse(worker.is_alive())
        finally:
            claude_code.AGENT_CWD = original_cwd

        self.assertLess(elapsed, 1)
        self.assertEqual(errors, [])
        self.assertEqual(result, [("session-1", "STARTUP")])

    def test_rapid_startup_steers_ignore_abort_until_latest_success(self) -> None:
        # Both replacements are flushed before the first response. The abort
        # boundary is not final; the newest replacement's success is.
        script = r"""
import json, sys

initial = json.loads(sys.stdin.readline())
first_interrupt = json.loads(sys.stdin.readline())
first_replacement = json.loads(sys.stdin.readline())
assert first_replacement["message"]["content"] == "first replacement"
second_interrupt = json.loads(sys.stdin.readline())
second_replacement = json.loads(sys.stdin.readline())
assert second_replacement["message"]["content"] == "second replacement"

print(json.dumps({
    "type": "control_response",
    "response": {
        "subtype": "success",
        "request_id": first_interrupt["request_id"],
        "response": {
            "still_queued": [],
            "cancelled": [initial["uuid"]],
        },
    },
}), flush=True)
print(json.dumps({
    "type": "control_response",
    "response": {
        "subtype": "error",
        "request_id": second_interrupt["request_id"],
        "error": "no active query",
    },
}), flush=True)
print(json.dumps({
    "type": "result",
    "subtype": "error_during_execution",
    "is_error": True,
    "terminal_reason": "aborted_streaming",
    "session_id": "session-1",
}), flush=True)
print(json.dumps({
    "type": "result",
    "subtype": "success",
    "session_id": "session-1",
    "result": "SECOND",
}), flush=True)
"""
        original_cwd = claude_code.AGENT_CWD
        first_ready = threading.Event()
        result: list[tuple[str, str]] = []

        def run() -> None:
            result.append(
                claude_code.run_turn(
                    server,
                    "initial",
                    None,
                    "claude-opus-5",
                    "high",
                    lambda _message: None,
                )
            )

        try:
            with tempfile.TemporaryDirectory() as tmp:
                claude_code.AGENT_CWD = tmp
                server = claude_code.ClaudeCodeSession(
                    [sys.executable, "-u", "-c", script],
                    on_ready=lambda: first_ready.set() or True,
                )
                self.addCleanup(server.close)
                worker = threading.Thread(target=run)
                worker.start()
                self.assertTrue(first_ready.wait(timeout=10))
                server.steer("first replacement")
                server.steer("second replacement")
                worker.join(timeout=10)
                self.assertFalse(worker.is_alive())
        finally:
            claude_code.AGENT_CWD = original_cwd

        self.assertEqual(result, [("session-1", "SECOND")])

    def test_multiple_abort_boundaries_wait_for_latest_steer_success(self) -> None:
        # Multiple rapid/rejected interrupts may emit multiple abort results;
        # none outranks the newest replacement's later success.
        script = r"""
import json, sys

json.loads(sys.stdin.readline())  # initial
print(json.dumps({
    "type": "assistant",
    "session_id": "session-1",
    "message": {"content": [{"type": "text", "text": "INITIAL_READY"}]},
}), flush=True)

first_interrupt = json.loads(sys.stdin.readline())
first_replacement = json.loads(sys.stdin.readline())
second_interrupt = json.loads(sys.stdin.readline())
second_replacement = json.loads(sys.stdin.readline())
print(json.dumps({
    "type": "control_response",
    "response": {
        "subtype": "success",
        "request_id": first_interrupt["request_id"],
        "response": {"still_queued": [], "cancelled": []},
    },
}), flush=True)
print(json.dumps({
    "type": "control_response",
    "response": {
        "subtype": "success",
        "request_id": second_interrupt["request_id"],
        "response": {
            "still_queued": [],
            "cancelled": [first_replacement["uuid"]],
        },
    },
}), flush=True)
print(json.dumps({
    "type": "result",
    "subtype": "error_during_execution",
    "is_error": True,
    "terminal_reason": "aborted_streaming",
    "session_id": "session-1",
}), flush=True)
print(json.dumps({
    "type": "assistant",
    "session_id": "session-1",
    "message": {"content": [{"type": "text", "text": "REPLACEMENT_READY"}]},
}), flush=True)

third_interrupt = json.loads(sys.stdin.readline())
print(json.dumps({
    "type": "control_response",
    "response": {
        "subtype": "error",
        "request_id": third_interrupt["request_id"],
        "error": "no active query",
    },
}), flush=True)
third_replacement = json.loads(sys.stdin.readline())
assert third_replacement["message"]["content"] == "third replacement"
assert second_replacement["message"]["content"] == "second replacement"
print(json.dumps({
    "type": "result",
    "subtype": "error_during_execution",
    "is_error": True,
    "terminal_reason": "aborted_tools",
    "session_id": "session-1",
}), flush=True)
print(json.dumps({
    "type": "result",
    "subtype": "success",
    "session_id": "session-1",
    "result": "THIRD",
}), flush=True)
"""
        original_cwd = claude_code.AGENT_CWD
        initial_ready = threading.Event()
        replacement_ready = threading.Event()
        result: list[tuple[str, str]] = []

        def on_message(message: str | dict[str, object]) -> None:
            if message == "INITIAL_READY":
                initial_ready.set()
            elif message == "REPLACEMENT_READY":
                replacement_ready.set()

        def run() -> None:
            result.append(
                claude_code.run_turn(
                    server,
                    "initial",
                    None,
                    "claude-opus-5",
                    "high",
                    on_message,
                )
            )

        try:
            with tempfile.TemporaryDirectory() as tmp:
                claude_code.AGENT_CWD = tmp
                server = claude_code.ClaudeCodeSession(
                    [sys.executable, "-u", "-c", script]
                )
                self.addCleanup(server.close)
                worker = threading.Thread(target=run)
                worker.start()
                self.assertTrue(initial_ready.wait(timeout=10))
                server.steer("first replacement")
                server.steer("second replacement")
                self.assertTrue(replacement_ready.wait(timeout=10))
                server.steer("third replacement")
                worker.join(timeout=10)
                self.assertFalse(worker.is_alive())
        finally:
            claude_code.AGENT_CWD = original_cwd

        self.assertEqual(result, [("session-1", "THIRD")])

    def test_run_turn_delivers_a_steer_that_arrives_right_as_the_result_is_processed(self) -> None:
        # The completion callback is the final atomic boundary with the host's
        # delivery lock. A direct steer observed there keeps the CLI open for
        # its result instead of letting the just-completed turn close it.
        script = r"""
import json, sys

json.loads(sys.stdin.readline())
for text in ("FIRST",):
    print(json.dumps({
        "type": "assistant",
        "session_id": "session-1",
        "message": {"content": [{"type": "text", "text": text}]},
    }), flush=True)
    print(json.dumps({
        "type": "result",
        "subtype": "success",
        "session_id": "session-1",
        "result": text,
    }), flush=True)
interrupt = json.loads(sys.stdin.readline())
assert interrupt["request"]["subtype"] == "interrupt"
print(json.dumps({
    "type": "control_response",
    "response": {
        "subtype": "success",
        "request_id": interrupt["request_id"],
    },
}), flush=True)
steer = json.loads(sys.stdin.readline())
assert steer["message"]["content"] == "late steer"
print(json.dumps({
    "type": "assistant",
    "session_id": "session-1",
    "message": {"content": [{"type": "text", "text": "STEERED"}]},
}), flush=True)
print(json.dumps({
    "type": "result",
    "subtype": "success",
    "session_id": "session-1",
    "result": "STEERED",
}), flush=True)
"""
        original_cwd = claude_code.AGENT_CWD
        calls = 0

        def finish_turn(_session_id: str, _output: str) -> int:
            nonlocal calls
            calls += 1
            if calls == 1:
                server.steer("late steer")
                return server.take_delivered_steers()
            return 0

        try:
            with tempfile.TemporaryDirectory() as tmp:
                claude_code.AGENT_CWD = tmp
                server = claude_code.ClaudeCodeSession([sys.executable, "-u", "-c", script])
                self.addCleanup(server.close)
                server.start()
                session_id, output = claude_code.run_turn(
                    server,
                    "initial",
                    None,
                    "claude-opus-5",
                    "high",
                    lambda _message: None,
                    finish_turn,
                )
        finally:
            claude_code.AGENT_CWD = original_cwd
        self.assertEqual(session_id, "session-1")
        self.assertEqual(output, "STEERED")

    def test_run_turn_continues_after_a_steered_abort_boundary(self) -> None:
        # Claude reports the interruption as an error result, then starts the
        # newest message in the same session. That boundary is not final.
        script = r"""
import json, sys

json.loads(sys.stdin.readline())
print(json.dumps({
    "type": "assistant",
    "session_id": "session-1",
    "message": {"content": [{"type": "text", "text": "READY"}]},
}), flush=True)
interrupt = json.loads(sys.stdin.readline())
assert interrupt["request"]["subtype"] == "interrupt"
print(json.dumps({
    "type": "control_response",
    "response": {
        "subtype": "success",
        "request_id": interrupt["request_id"],
        "response": {"still_queued": []},
    },
}), flush=True)
steer = json.loads(sys.stdin.readline())
assert steer["message"]["content"] == "steer"
print(json.dumps({
    "type": "result",
    "subtype": "error_during_execution",
    "is_error": True,
    "terminal_reason": "aborted_streaming",
    "session_id": "session-1",
}), flush=True)
print(json.dumps({
    "type": "assistant",
    "session_id": "session-1",
    "message": {"content": [{"type": "text", "text": "STEERED"}]},
}), flush=True)
print(json.dumps({
    "type": "result",
    "subtype": "success",
    "session_id": "session-1",
    "result": "STEERED",
}), flush=True)
sys.stdin.readline()  # stay alive like the real CLI until stdin EOF
"""
        original_cwd = claude_code.AGENT_CWD
        ready = threading.Event()
        result: list[tuple[str, str]] = []
        try:
            with tempfile.TemporaryDirectory() as tmp:
                claude_code.AGENT_CWD = tmp
                server = claude_code.ClaudeCodeSession([sys.executable, "-u", "-c", script])
                # The script idles on stdin after the result; close it so the
                # child and its reader threads do not outlive the test.
                self.addCleanup(server.close)
                worker = threading.Thread(
                    target=lambda: result.append(
                        claude_code.run_turn(
                            server,
                            "initial",
                            None,
                            "claude-opus-5",
                            "high",
                            lambda _message: ready.set(),
                        )
                    )
                )
                worker.start()
                self.assertTrue(ready.wait(timeout=10))
                server.steer("steer")
                worker.join(timeout=10)
                self.assertFalse(worker.is_alive())
        finally:
            claude_code.AGENT_CWD = original_cwd
        self.assertEqual(len(result), 1)
        session_id, output = result[0]
        self.assertEqual(session_id, "session-1")
        self.assertEqual(output, "STEERED")

    def test_run_turn_rejects_an_unacknowledged_aborted_result(self) -> None:
        script = r"""
import json, sys

json.loads(sys.stdin.readline())
print(json.dumps({
    "type": "result",
    "subtype": "error_during_execution",
    "is_error": True,
    "terminal_reason": "aborted_tools",
    "session_id": "session-1",
}), flush=True)
"""
        original_cwd = claude_code.AGENT_CWD
        try:
            with tempfile.TemporaryDirectory() as tmp:
                claude_code.AGENT_CWD = tmp
                server = claude_code.ClaudeCodeSession(
                    [sys.executable, "-u", "-c", script]
                )
                self.addCleanup(server.close)
                with self.assertRaisesRegex(
                    claude_code.ClaudeCodeError,
                    "error_during_execution",
                ):
                    claude_code.run_turn(
                        server,
                        "initial",
                        None,
                        "claude-opus-5",
                        "high",
                        lambda _message: None,
                    )
        finally:
            claude_code.AGENT_CWD = original_cwd

    def test_run_turn_ignores_abort_boundary_after_interrupt_rejection(self) -> None:
        script = r"""
import json, sys

json.loads(sys.stdin.readline())  # initial message
print(json.dumps({
    "type": "assistant",
    "session_id": "session-1",
    "message": {"content": [{"type": "text", "text": "READY"}]},
}), flush=True)
interrupt = json.loads(sys.stdin.readline())
print(json.dumps({
    "type": "control_response",
    "response": {
        "subtype": "error",
        "request_id": interrupt["request_id"],
        "error": "no active query",
    },
}), flush=True)
json.loads(sys.stdin.readline())  # replacement user message
print(json.dumps({
    "type": "result",
    "subtype": "error_during_execution",
    "is_error": True,
    "terminal_reason": "aborted_streaming",
    "session_id": "session-1",
}), flush=True)
print(json.dumps({
    "type": "result",
    "subtype": "success",
    "session_id": "session-1",
    "result": "REPLACEMENT",
}), flush=True)
"""
        original_cwd = claude_code.AGENT_CWD
        ready = threading.Event()
        result: list[tuple[str, str]] = []
        try:
            with tempfile.TemporaryDirectory() as tmp:
                claude_code.AGENT_CWD = tmp
                server = claude_code.ClaudeCodeSession(
                    [sys.executable, "-u", "-c", script]
                )
                self.addCleanup(server.close)

                def run() -> None:
                    result.append(
                        claude_code.run_turn(
                            server,
                            "initial",
                            None,
                            "claude-opus-5",
                            "high",
                            lambda _message: ready.set(),
                        )
                    )

                worker = threading.Thread(target=run)
                worker.start()
                self.assertTrue(ready.wait(timeout=10))
                server.steer("replacement")
                worker.join(timeout=10)
                self.assertFalse(worker.is_alive())
        finally:
            claude_code.AGENT_CWD = original_cwd

        self.assertEqual(result, [("session-1", "REPLACEMENT")])

    def test_run_turn_accepts_success_after_a_rejected_interrupt(self) -> None:
        script = r"""
import json, sys

json.loads(sys.stdin.readline())  # initial message
print(json.dumps({
    "type": "assistant",
    "session_id": "session-1",
    "message": {"content": [{"type": "text", "text": "READY"}]},
}), flush=True)
interrupt = json.loads(sys.stdin.readline())
print(json.dumps({
    "type": "control_response",
    "response": {
        "subtype": "error",
        "request_id": interrupt["request_id"],
        "error": "no active query",
    },
}), flush=True)
json.loads(sys.stdin.readline())  # merged user message
print(json.dumps({
    "type": "assistant",
    "session_id": "session-1",
    "message": {"content": [{"type": "text", "text": "MERGED"}]},
}), flush=True)
print(json.dumps({
    "type": "result",
    "subtype": "success",
    "session_id": "session-1",
    "result": "MERGED",
}), flush=True)
sys.stdin.readline()  # idle until Kern closes stdin
"""
        original_cwd = claude_code.AGENT_CWD
        ready = threading.Event()
        result: list[tuple[str, str]] = []
        try:
            with tempfile.TemporaryDirectory() as tmp:
                claude_code.AGENT_CWD = tmp
                server = claude_code.ClaudeCodeSession(
                    [sys.executable, "-u", "-c", script]
                )
                self.addCleanup(server.close)
                worker = threading.Thread(
                    target=lambda: result.append(claude_code.run_turn(
                        server,
                        "initial",
                        None,
                        "claude-opus-5",
                        "high",
                        lambda _message: ready.set(),
                    ))
                )
                worker.start()
                self.assertTrue(ready.wait(timeout=10))
                server.steer("merged")
                worker.join(timeout=10)
                self.assertFalse(worker.is_alive())
        finally:
            claude_code.AGENT_CWD = original_cwd

        self.assertEqual(result, [("session-1", "MERGED")])

    def test_run_turn_discards_stale_messages_from_previous_process(self) -> None:
        script = r"""
import json, sys

json.loads(sys.stdin.readline())
print(json.dumps({
    "type": "result",
    "subtype": "success",
    "session_id": "fresh-session",
    "result": "FRESH",
}), flush=True)
"""
        original_cwd = claude_code.AGENT_CWD
        try:
            with tempfile.TemporaryDirectory() as tmp:
                claude_code.AGENT_CWD = tmp
                server = claude_code.ClaudeCodeSession([sys.executable, "-u", "-c", script])
                self.addCleanup(server.close)
                server._messages.put({
                    "type": "result",
                    "subtype": "success",
                    "session_id": "stale-session",
                    "result": "STALE",
                })
                session_id, output = claude_code.run_turn(
                    server,
                    "initial",
                    None,
                    "claude-opus-5",
                    "high",
                    lambda _message: None,
                )
        finally:
            claude_code.AGENT_CWD = original_cwd
        self.assertEqual(session_id, "fresh-session")
        self.assertEqual(output, "FRESH")

    def test_killed_turn_still_exposes_the_last_known_session_id(self) -> None:
        # A kill tears the process down from outside run(): the read loop's
        # next _require_proc() call finds it dead and raises, discarding
        # run()'s local result_session_id along with every other local. The
        # orchestrator needs the session_id anyway, to persist it even for a
        # killed turn (see orchestrator._finish_task) — last_known_session_id
        # is the attribute-based escape hatch for that.
        script = r"""
import json, sys, time

json.loads(sys.stdin.readline())
print(json.dumps({
    "type": "assistant",
    "session_id": "mid-turn-session",
    "message": {"content": [{"type": "text", "text": "partial"}]},
}), flush=True)
time.sleep(30)
"""
        original_cwd = claude_code.AGENT_CWD
        received = threading.Event()
        errors: list[BaseException] = []
        try:
            with tempfile.TemporaryDirectory() as tmp:
                claude_code.AGENT_CWD = tmp
                ready = threading.Event()
                accepted_sessions: list[str] = []
                server = claude_code.ClaudeCodeSession(
                    [sys.executable, "-u", "-c", script],
                    on_ready=lambda: (ready.set() or True),
                    on_session_id=accepted_sessions.append,
                )
                server.start()
                self.assertIsNone(server.last_known_session_id)

                def run_it() -> None:
                    try:
                        claude_code.run_turn(
                            server,
                            "initial",
                            None,
                            "claude-opus-5",
                            "high",
                            lambda _message: received.set(),
                        )
                    except BaseException as exc:  # noqa: BLE001 - captured for the assertion below
                        errors.append(exc)

                worker = threading.Thread(target=run_it)
                worker.start()
                self.assertTrue(received.wait(timeout=10))
                self.assertTrue(ready.is_set())
                self.assertEqual(accepted_sessions, ["mid-turn-session"])
                server.close()  # the kill path: tear the process down mid-turn
                worker.join(timeout=10)
        finally:
            claude_code.AGENT_CWD = original_cwd
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], claude_code.ClaudeCodeError)
        self.assertEqual(server.last_known_session_id, "mid-turn-session")

    def test_init_session_is_not_published_until_the_message_has_activity(self) -> None:
        script = r"""
import json, sys
json.loads(sys.stdin.readline())
print(json.dumps({
    "type": "system",
    "subtype": "init",
    "session_id": "empty-init-session",
}), flush=True)
print(json.dumps({
    "type": "assistant",
    "session_id": "accepted-session",
    "message": {"content": [{"type": "text", "text": "done"}]},
}), flush=True)
print(json.dumps({
    "type": "result",
    "subtype": "success",
    "session_id": "accepted-session",
    "result": "done",
}), flush=True)
"""
        original_cwd = claude_code.AGENT_CWD
        accepted_sessions: list[str] = []
        try:
            with tempfile.TemporaryDirectory() as tmp:
                claude_code.AGENT_CWD = tmp
                server = claude_code.ClaudeCodeSession(
                    [sys.executable, "-u", "-c", script],
                    on_ready=lambda: True,
                    on_session_id=accepted_sessions.append,
                )
                self.addCleanup(server.close)
                session_id, output = claude_code.run_turn(
                    server,
                    "initial",
                    None,
                    "claude-opus-5",
                    "high",
                    lambda _message: None,
                )
        finally:
            claude_code.AGENT_CWD = original_cwd

        self.assertEqual((session_id, output), ("accepted-session", "done"))
        self.assertNotIn("empty-init-session", accepted_sessions)
        self.assertTrue(accepted_sessions)
        self.assertEqual(set(accepted_sessions), {"accepted-session"})

    def test_default_turn_helper_does_not_require_admin_access_to_agent_home(self) -> None:
        script = r"""
import json, sys

json.loads(sys.stdin.readline())
print(json.dumps({
    "type": "result",
    "subtype": "success",
    "session_id": "default-helper-session",
    "result": "OK",
}), flush=True)
"""
        original_command = claude_code.DEFAULT_COMMAND
        original_cwd = claude_code.AGENT_CWD
        try:
            claude_code.DEFAULT_COMMAND = [sys.executable, "-u", "-c", script]
            claude_code.AGENT_CWD = "/definitely/not-readable-by-admin"
            server = claude_code.ClaudeCodeSession()
            self.addCleanup(server.close)
            with patch("host.runtime.core.state.read_claude_web_search", return_value=False):
                session_id, output = claude_code.run_turn(
                    server,
                    "initial",
                    None,
                    "claude-opus-5",
                    "high",
                    lambda _message: None,
                )
        finally:
            claude_code.DEFAULT_COMMAND = original_command
            claude_code.AGENT_CWD = original_cwd
        self.assertEqual(session_id, "default-helper-session")
        self.assertEqual(output, "OK")

    def test_task_launch_uses_managed_user_settings_without_safe_mode(self) -> None:
        script = r"""
import json, pathlib, sys

pathlib.Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:]))
json.loads(sys.stdin.readline())
print(json.dumps({
    "type": "result",
    "subtype": "success",
    "session_id": "argv-session",
    "result": "OK",
}), flush=True)
"""
        original_cwd = claude_code.AGENT_CWD
        try:
            with tempfile.TemporaryDirectory() as tmp:
                claude_code.AGENT_CWD = tmp
                argv_path = Path(tmp) / "argv.json"
                server = claude_code.ClaudeCodeSession(
                    [sys.executable, "-u", "-c", script, str(argv_path)],
                    thread_id="sample_app__ws-3",
                )
                self.addCleanup(server.close)
                with patch("host.runtime.core.state.read_claude_web_search", return_value=False):
                    claude_code.run_turn(
                        server,
                        "initial",
                        None,
                        "claude-fable-5",
                        "ultracode",
                        lambda _message: None,
                    )
                argv = json.loads(argv_path.read_text())
        finally:
            claude_code.AGENT_CWD = original_cwd

        self.assertIn("--setting-sources", argv)
        self.assertIn("user", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "claude-fable-5")
        self.assertEqual(argv[argv.index("--effort") + 1], "ultracode")
        self.assertIn("--strict-mcp-config", argv)
        self.assertEqual(
            argv[:3],
            ["web-search=off", "--thread-scope", "sample_app__ws-3"],
        )
        self.assertNotIn("--append-system-prompt", argv)
        self.assertNotIn("--dangerously-skip-permissions", argv)
        self.assertNotIn("--safe-mode", argv)
        self.assertNotIn("--permission-mode", argv)

    def test_web_search_decision_is_passed_to_launcher(self) -> None:
        # The orchestrator states the operator's decision to the launcher as the
        # first argument (web-search=on/off); the launcher, not this side, builds
        # the WebSearch deny. So run() must emit the token and must NOT build a
        # --settings override itself.
        script = r"""
import json, pathlib, sys

pathlib.Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:]))
json.loads(sys.stdin.readline())
print(json.dumps({
    "type": "result",
    "subtype": "success",
    "session_id": "argv-session",
    "result": "OK",
}), flush=True)
"""
        original_cwd = claude_code.AGENT_CWD
        try:
            with tempfile.TemporaryDirectory() as tmp:
                claude_code.AGENT_CWD = tmp
                argv_path = Path(tmp) / "argv.json"
                # The injected command stands in for run-claude-code and receives
                # the decision exactly as the real launcher would.
                command = [sys.executable, "-u", "-c", script, str(argv_path)]
                for web_search, expected_token in ((False, "web-search=off"), (True, "web-search=on")):
                    session = claude_code.ClaudeCodeSession(command)
                    self.addCleanup(session.close)
                    with patch("host.runtime.core.state.read_claude_web_search", return_value=web_search):
                        claude_code.run_turn(
                            session, "initial", None, "claude-opus-5", "high",
                            lambda _message: None,
                        )
                    argv = json.loads(argv_path.read_text())
                    # The decision leads the Claude flags so the launcher can
                    # consume it before forwarding the rest.
                    self.assertEqual(argv[0], expected_token)
                    self.assertNotIn("--settings", argv)
        finally:
            claude_code.AGENT_CWD = original_cwd


class ToolsMcpConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        # The launch path reads the operator's web-search toggle from the
        # database, which these process-level tests do not provision.
        web_search = patch("host.runtime.core.state.read_claude_web_search", return_value=False)
        web_search.start()
        self.addCleanup(web_search.stop)

    def test_run_passes_the_bundled_tools_mcp_config(self) -> None:
        import json

        config = json.loads(claude_code.TOOLS_MCP_CONFIG)
        shim = config["mcpServers"]["kern"]
        self.assertEqual(shim["command"], "/usr/bin/python3")
        self.assertEqual(shim["args"], ["-m", "host.runtime.agent_shim.mcp_shim"])
        self.assertEqual(shim["env"], {"PYTHONPATH": "/opt/kern-host"})

        # Echo the CLI argv back through the turn result to pin the flags the
        # runtime actually passes.
        script = r"""
import json, sys
json.loads(sys.stdin.readline())
print(json.dumps({
    "type": "result",
    "subtype": "success",
    "session_id": "argv-session",
    "result": json.dumps(sys.argv[1:]),
}), flush=True)
"""
        original_cwd = claude_code.AGENT_CWD
        try:
            with tempfile.TemporaryDirectory() as tmp:
                claude_code.AGENT_CWD = tmp
                server = claude_code.ClaudeCodeSession([sys.executable, "-u", "-c", script])
                self.addCleanup(server.close)
                server.start()
                _session_id, output = claude_code.run_turn(
                    server,
                    "initial",
                    None,
                    "claude-opus-5",
                    "high",
                    lambda _message: None,
                )
        finally:
            claude_code.AGENT_CWD = original_cwd
        argv = json.loads(output)
        self.assertIn("--strict-mcp-config", argv)
        self.assertIn("--mcp-config", argv)
        self.assertEqual(argv[argv.index("--mcp-config") + 1], claude_code.TOOLS_MCP_CONFIG)
        self.assertNotIn("--append-system-prompt", argv)
        # Safe mode would drop every non-SDK MCP server (verified against the
        # pinned CLI), silently disabling the bundled tools.
        self.assertNotIn("--safe-mode", argv)


if __name__ == "__main__":
    unittest.main()
