from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from host import agent_scripts
from host.agent_scripts import SCRIPT_TIMEOUT_SECONDS, script_path_error
from host.runtime.agent_runtime import script_runner, thread_scope


def _fake_launcher(body: str) -> list[str]:
    """A stand-in for run-agent-script: same argv shape, no root or systemd.

    It consumes the optional --thread-scope pair exactly as the real launcher
    does and then runs the script path it was handed, so the adapter's argv,
    output, and exit-status handling are exercised end to end.
    """
    return ["/bin/bash", "-c", body, "fake-run-agent-script"]


LAUNCHER = _fake_launcher(
    """
if [ "$1" = "--thread-scope" ]; then shift 2; fi
exec /bin/bash "$1"
"""
)


class ScriptPathTests(unittest.TestCase):
    def test_accepts_a_plain_script_path_under_the_agent_home(self) -> None:
        self.assertIsNone(
            script_path_error("/mnt/kern-agent/agent-home/scripts/nightly-backup.sh")
        )
        self.assertIsNone(script_path_error("/mnt/kern-agent/agent-home/run.sh"))

    def test_rejects_paths_that_could_never_be_an_agent_script(self) -> None:
        for path in (
            "",
            "scripts/backup.sh",
            "/etc/cron.daily/backup.sh",
            "/mnt/kern-agent/agent-home/scripts/backup",
            "/mnt/kern-agent/agent-home/../../etc/backup.sh",
            "/mnt/kern-agent/agent-home/./backup.sh",
            "/mnt/kern-agent/agent-home/scripts/../backup.sh",
            "/mnt/kern-agent/agent-home/back up.sh",
            "/mnt/kern-agent/agent-home/backup.sh; rm -rf /",
            "/mnt/kern-agent/agent-home/$(whoami).sh",
            "/mnt/kern-agent/agent-home/backup.sh\nrm -rf /",
            "/mnt/kern-agent/agent-home/" + "a" * 600 + ".sh",
            None,
            5,
        ):
            with self.subTest(path=path):
                self.assertIsNotNone(script_path_error(path))

    def test_the_agent_home_itself_is_not_a_script(self) -> None:
        self.assertIsNotNone(script_path_error("/mnt/kern-agent/agent-home"))
        self.assertIsNotNone(script_path_error("/mnt/kern-agent/agent-home/"))


class ScriptSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        # Stand the whole contract up over a temporary home: the adapter checks
        # the path against it before spawning anything, so the scripts these
        # tests run have to live somewhere it accepts.
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        home = patch.object(agent_scripts, "AGENT_HOME", self.home.name)
        home.start()
        self.addCleanup(home.stop)
        cwd = patch.object(script_runner, "AGENT_CWD", self.home.name)
        cwd.start()
        self.addCleanup(cwd.stop)

    def write_script(self, body: str, name: str = "job.sh") -> str:
        path = Path(self.home.name) / name
        path.write_text(body)
        return str(path)

    def long_running_server(
        self, on_ready: object | None = None
    ) -> script_runner.ScriptSession:
        """A session over a script that runs until something stops it.

        The script `exec`s, so the spawned process is the whole run: killing it
        closes the output pipe. A script that instead left a child holding that
        pipe is freed in production by the thread scope's cgroup teardown,
        which a test command deliberately does not perform.
        """
        self.long_running_path = self.write_script("exec sleep 30\n", "sleep.sh")
        server = script_runner.ScriptSession(LAUNCHER, on_ready=on_ready)  # type: ignore[arg-type]
        server.start()
        self.addCleanup(server.close)
        return server

    def run_script(
        self,
        body: str,
        *,
        command: list[str] | None = None,
        thread_id: str | None = None,
        on_ready: object | None = None,
    ) -> tuple[str, str, list[str]]:
        messages: list[str] = []
        path = self.write_script(body)
        server = script_runner.ScriptSession(
            command or LAUNCHER,
            thread_id=thread_id,
            on_ready=on_ready,  # type: ignore[arg-type]
        )
        server.start()
        session_id, output = script_runner.run_turn(
            server, path, None, "bash", "fixed", messages.append
        )
        return session_id, output, messages

    def test_output_is_recorded_and_no_session_is_reported(self) -> None:
        session_id, output, messages = self.run_script("echo hello from the script\n")
        self.assertEqual(output, "hello from the script")
        self.assertEqual(messages, ["hello from the script"])
        # Nothing to resume: the orchestrator reads the empty id as "no
        # provider session to persist", so every run starts fresh.
        self.assertEqual(session_id, "")

    def test_standard_error_is_interleaved_with_standard_output(self) -> None:
        _session_id, output, _messages = self.run_script(
            "echo first\necho second >&2\necho third\n"
        )
        self.assertEqual(output.splitlines(), ["first", "second", "third"])

    def test_a_silent_script_still_records_a_message(self) -> None:
        _session_id, output, messages = self.run_script("exit 0\n")
        self.assertEqual(output, "")
        self.assertEqual(messages, ["The script finished with no output."])

    def test_a_failing_script_records_its_output_then_fails_the_turn(self) -> None:
        messages: list[str] = []
        path = self.write_script("echo progress so far\necho boom >&2\nexit 3\n")
        server = script_runner.ScriptSession(LAUNCHER)
        server.start()
        with self.assertRaises(script_runner.ScriptRunError) as caught:
            script_runner.run_turn(server, path, None, "bash", "fixed", messages.append)
        # The diagnostics survive as the run's message even though the run
        # itself is a failure.
        self.assertEqual(messages, ["progress so far\nboom"])
        self.assertIn("status 3", str(caught.exception))
        self.assertIn("boom", str(caught.exception))

    def test_a_malformed_path_never_reaches_a_process(self) -> None:
        server = script_runner.ScriptSession(["/bin/false"])
        server.start()
        with self.assertRaises(script_runner.ScriptRunError) as caught:
            script_runner.run_turn(
                server, "/etc/passwd", None, "bash", "fixed", MagicMock()
            )
        self.assertIn(self.home.name, str(caught.exception))

    def test_the_script_is_run_by_path_without_an_executable_bit(self) -> None:
        path = self.write_script("echo ran anyway\n")
        os.chmod(path, 0o600)
        server = script_runner.ScriptSession(LAUNCHER)
        server.start()
        _session_id, output = script_runner.run_turn(
            server, path, None, "bash", "fixed", MagicMock()
        )
        self.assertEqual(output, "ran anyway")

    def test_long_output_keeps_the_end_and_says_so(self) -> None:
        _session_id, output, _messages = self.run_script(
            f"for i in $(seq 1 {script_runner.MAX_OUTPUT_CHARS}); do echo line $i; done\n"
        )
        self.assertLessEqual(len(output), script_runner.MAX_OUTPUT_CHARS)
        self.assertTrue(output.startswith(script_runner.TRUNCATION_NOTICE))
        # The tail is what a failing script explains itself in.
        self.assertTrue(output.rstrip().endswith(f"line {script_runner.MAX_OUTPUT_CHARS}"))

    def test_output_is_bounded_while_it_is_read_not_after(self) -> None:
        # An unattended script can be arbitrarily noisy. The tail is trimmed as
        # the pipe drains, so a runaway loop cannot grow the admin API's memory
        # for the length of the turn just to be truncated at the end.
        chunks = [b"a" * script_runner.READ_CHUNK_BYTES] * 50 + [b"the-last-line\n"]

        class Stream:
            def __init__(self) -> None:
                self.reads = 0
                self.remaining = list(chunks)

            def read1(self, size: int) -> bytes:
                self.reads += 1
                return self.remaining.pop(0) if self.remaining else b""

            def close(self) -> None:
                return

        stream = Stream()
        proc = MagicMock()
        proc.stdout = stream
        proc.wait.return_value = 0
        tail = script_runner._BoundedTail()
        with patch.object(script_runner, "MAX_OUTPUT_CHARS", 200):
            script_runner._collect_output(proc, tail)
            output = tail.text()
        # Read incrementally rather than in one buffered gulp...
        self.assertEqual(stream.reads, len(chunks) + 1)
        # ...and what survives is a bounded tail that still ends the run.
        self.assertLessEqual(len(output), 200)
        self.assertTrue(output.startswith(script_runner.TRUNCATION_NOTICE))
        self.assertTrue(output.endswith("the-last-line"))

    def test_a_timed_out_script_still_records_what_it_printed(self) -> None:
        # The run whose output matters most: a script that reports where it got
        # to and then hangs. Abandoning it must not throw that away.
        messages: list[str] = []
        path = self.write_script("echo reached-step-three\nexec sleep 30\n", "hang.sh")
        server = script_runner.ScriptSession(LAUNCHER)
        server.start()
        self.addCleanup(server.close)
        with patch.object(script_runner, "TURN_TIMEOUT_SECONDS", 2):
            with self.assertRaises(script_runner.ScriptRunError) as caught:
                script_runner.run_turn(
                    server, path, None, "bash", "fixed", messages.append
                )
        self.assertIn("timed out", str(caught.exception))
        self.assertEqual(messages, ["reached-step-three"])

    def test_output_that_is_not_valid_utf8_is_kept_with_replacement(self) -> None:
        # A script is arbitrary and may shell out to a binary-emitting tool.
        # One bad byte must not discard the run's output, and must not depend
        # on the admin service's locale.
        _session_id, output, messages = self.run_script(
            r"printf 'caf\xe9 done\n'" + "\n"
        )
        self.assertEqual(output, "caf� done")
        self.assertEqual(messages, ["caf� done"])

    def test_a_character_split_across_two_reads_decodes_once(self) -> None:
        # read1 returns what has arrived, so a multi-byte character can be cut
        # in half by the pipe; incremental decoding rejoins it.
        halves = ["é".encode()[:1], "é".encode()[1:], b"\n"]

        class Stream:
            def __init__(self) -> None:
                self.remaining = list(halves)

            def read1(self, size: int) -> bytes:
                return self.remaining.pop(0) if self.remaining else b""

            def close(self) -> None:
                return

        proc = MagicMock()
        proc.stdout = Stream()
        proc.wait.return_value = 0
        tail = script_runner._BoundedTail()
        script_runner._collect_output(proc, tail)
        self.assertEqual(tail.text(), "é")

    def test_the_path_is_passed_after_an_optional_thread_scope_pair(self) -> None:
        recorded: list[list[str]] = []
        real_popen = subprocess.Popen

        def record(argv: list[str], **kwargs: object) -> subprocess.Popen[str]:
            recorded.append(list(argv))
            return real_popen(argv, **kwargs)  # type: ignore[arg-type]

        with patch.object(subprocess, "Popen", side_effect=record):
            self.run_script("echo ok\n", thread_id="schedule-4-run-9")
        self.assertEqual(recorded[0][-3:-1], ["--thread-scope", "schedule-4-run-9"])
        self.assertTrue(recorded[0][-1].endswith("/job.sh"))

    def test_a_rejected_start_kills_the_run_instead_of_finishing_it(self) -> None:
        # on_ready returns False when the turn was stopped between admission
        # and the process starting; the script must not run to completion with
        # nowhere to record its result.
        server = self.long_running_server(on_ready=lambda: False)
        with self.assertRaises(script_runner.ScriptRunError) as caught:
            script_runner.run_turn(
                server, self.long_running_path, None, "bash", "fixed", MagicMock()
            )
        self.assertIn("stopped during startup", str(caught.exception))

    def test_a_script_that_outlives_its_budget_is_killed_and_reported(self) -> None:
        server = self.long_running_server()
        with patch.object(script_runner, "TURN_TIMEOUT_SECONDS", 0.3):
            started = time.monotonic()
            with self.assertRaises(script_runner.ScriptRunError) as caught:
                script_runner.run_turn(
                    server, self.long_running_path, None, "bash", "fixed", MagicMock()
                )
        self.assertLess(time.monotonic() - started, 15)
        self.assertIn("timed out", str(caught.exception))

    def test_the_budget_is_the_fixed_fifteen_minutes(self) -> None:
        self.assertEqual(script_runner.TURN_TIMEOUT_SECONDS, SCRIPT_TIMEOUT_SECONDS)
        self.assertEqual(SCRIPT_TIMEOUT_SECONDS, 15 * 60)

    def test_interrupt_before_the_process_starts_prevents_the_run(self) -> None:
        path = self.write_script("echo should not run\n")
        server = script_runner.ScriptSession(LAUNCHER)
        server.start()
        server.interrupt()
        with self.assertRaises(script_runner.ScriptRunError):
            script_runner.run_turn(server, path, None, "bash", "fixed", MagicMock())

    def test_a_live_run_is_interruptible(self) -> None:
        started = threading.Event()

        def ready() -> bool:
            started.set()
            return True

        server = self.long_running_server(on_ready=ready)
        failures: list[BaseException] = []

        def run() -> None:
            try:
                script_runner.run_turn(
                    server, self.long_running_path, None, "bash", "fixed", MagicMock()
                )
            except BaseException as exc:  # noqa: BLE001 - reported to the test
                failures.append(exc)

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        self.assertTrue(started.wait(timeout=15))
        server.interrupt()
        worker.join(timeout=15)
        self.assertFalse(worker.is_alive())
        self.assertIsInstance(failures[0], script_runner.ScriptRunError)

    def test_close_stops_the_thread_scope_under_the_production_launcher(self) -> None:
        server = script_runner.ScriptSession(
            script_runner.DEFAULT_COMMAND, thread_id="schedule-1-run-1"
        )
        with patch.object(subprocess, "run") as runner:
            runner.return_value = subprocess.CompletedProcess([], 0)
            server.close()
        self.assertEqual(
            runner.call_args.args[0],
            [*thread_scope.STOP_COMMAND, "schedule-1-run-1"],
        )

    def test_close_does_not_stop_a_scope_for_a_test_command(self) -> None:
        server = script_runner.ScriptSession(LAUNCHER, thread_id="schedule-1-run-1")
        with patch.object(subprocess, "run") as runner:
            server.close()
        runner.assert_not_called()


class ScriptAccountStatusTests(unittest.TestCase):
    def test_the_runtime_is_always_active_with_no_account(self) -> None:
        # There is no provider to log into, so the orchestrator's provider
        # contract is satisfied without any credential state.
        self.assertEqual(script_runner.account_status(), ("active", None, {}))


if __name__ == "__main__":
    unittest.main()
