from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from host.runtime.agent_runtime import hermes_agent, thread_scope

# A scripted fake of the Hermes stdin adapter: one process per prompt, session
# id on stderr, answer text on stdout, and resume keeps the session id.
CHAT_SCRIPT = r"""
import json, sys
args = sys.argv[1:]
def value_after(flag):
    return args[args.index(flag) + 1] if flag in args else None
session_id = value_after("--resume") or "hermes-session-1"
prompt = sys.stdin.read()
print(f"session_id: {session_id}", file=sys.stderr)
print(json.dumps({"prompt": prompt, "args": args}))
"""


class HermesSessionTests(unittest.TestCase):
    def run_turn(
        self,
        script: str,
        *,
        session_id: str | None = None,
        input_message: str = "initial",
    ) -> tuple[str, str, list[str], list[str]]:
        delivered: list[str] = []
        streamed: list[str] = []
        original_cwd = hermes_agent.AGENT_CWD
        from host.runtime.core import state

        try:
            with tempfile.TemporaryDirectory() as tmp:
                hermes_agent.AGENT_CWD = tmp
                server = hermes_agent.HermesSession([sys.executable, "-u", "-c", script])
                server.start()
                with patch.object(state, "read_bedrock_region", return_value="us-east-1"):
                    result_session_id, output = hermes_agent.run_turn(
                        server,
                        input_message,
                        session_id,
                        "deepseek.v3.2",
                        "high",
                        streamed.append,
                    )
        finally:
            hermes_agent.AGENT_CWD = original_cwd
        return result_session_id, output, delivered, streamed

    def test_run_returns_the_reported_session_id_and_answer(self) -> None:
        session_id, output, _delivered, streamed = self.run_turn(CHAT_SCRIPT)
        self.assertEqual(session_id, "hermes-session-1")
        payload = json.loads(output)
        self.assertEqual(payload["prompt"], "initial")
        self.assertEqual(payload["args"][0], "region=us-east-1")
        self.assertIn("--model", payload["args"])
        self.assertNotIn("initial", payload["args"])
        self.assertNotIn("--resume", payload["args"])
        self.assertEqual(len(streamed), 1)

    def test_ready_and_session_callbacks_mark_acceptance_and_resumability(self) -> None:
        ready = threading.Event()
        accepted_sessions: list[str] = []
        original_cwd = hermes_agent.AGENT_CWD
        from host.runtime.core import state

        try:
            with tempfile.TemporaryDirectory() as tmp:
                hermes_agent.AGENT_CWD = tmp
                server = hermes_agent.HermesSession(
                    [sys.executable, "-u", "-c", CHAT_SCRIPT],
                    on_ready=lambda: (ready.set() or True),
                    on_session_id=accepted_sessions.append,
                )
                with patch.object(state, "read_bedrock_region", return_value="us-east-1"):
                    session_id, _output = hermes_agent.run_turn(
                        server,
                        "initial",
                        None,
                        "deepseek.v3.2",
                        "high",
                        lambda _message: None,
                    )
        finally:
            hermes_agent.AGENT_CWD = original_cwd

        self.assertTrue(ready.is_set())
        self.assertEqual(session_id, "hermes-session-1")
        self.assertEqual(accepted_sessions, ["hermes-session-1"])

    def test_run_resumes_the_stored_session(self) -> None:
        session_id, output, _delivered, _streamed = self.run_turn(CHAT_SCRIPT, session_id="hermes-session-7")
        self.assertEqual(session_id, "hermes-session-7")
        payload = json.loads(output)
        self.assertEqual(payload["args"][payload["args"].index("--resume") + 1], "hermes-session-7")

    def test_a_leading_dash_prompt_is_delivered_verbatim_over_stdin(self) -> None:
        _sid, output, _delivered, _streamed = self.run_turn(
            CHAT_SCRIPT, input_message="--help me with this"
        )
        payload = json.loads(output)
        self.assertEqual(payload["prompt"], "--help me with this")
        self.assertNotIn("--help me with this", payload["args"])

    def test_a_flag_shaped_session_id_line_is_ignored(self) -> None:
        # A session id re-enters argv as --resume's value, so a reported id
        # that looks like a flag is never adopted; with no prior session the
        # turn fails instead.
        script = CHAT_SCRIPT.replace('or "hermes-session-1"', 'or "--toolsets=all"')
        with self.assertRaises(hermes_agent.HermesAgentError):
            self.run_turn(script)

    def test_run_fails_without_a_configured_region(self) -> None:
        server = hermes_agent.HermesSession([sys.executable, "-u", "-c", "pass"])
        from host.runtime.core import state

        with patch.object(state, "read_bedrock_region", return_value=None):
            with self.assertRaisesRegex(hermes_agent.HermesAgentError, "no configured region"):
                server.run(
                    "go", None, "deepseek.v3.2", "high",
                    lambda _m: None,
                )

    def test_killed_turn_still_exposes_the_last_known_session_id(self) -> None:
        # Regression test: the CLI's "session_id: ..." stderr line is only
        # parsed after the whole process exits successfully, but a kill needs
        # it sooner. last_known_session_id is captured as soon as that stderr
        # line streams in, so a kill (which surfaces as HermesAgentError,
        # discarding _run_prompt's locals) still leaves the orchestrator able
        # to read it and persist the thread mapping.
        script = r"""
import sys, time
print("session_id: hermes-mid-turn", file=sys.stderr, flush=True)
time.sleep(30)
"""
        from host.runtime.core import state

        original_cwd = hermes_agent.AGENT_CWD
        errors: list[BaseException] = []
        try:
            with tempfile.TemporaryDirectory() as tmp:
                hermes_agent.AGENT_CWD = tmp
                server = hermes_agent.HermesSession([sys.executable, "-u", "-c", script])
                server.start()
                self.assertIsNone(server.last_known_session_id)

                def run_it() -> None:
                    try:
                        with patch.object(state, "read_bedrock_region", return_value="us-east-1"):
                            hermes_agent.run_turn(
                                server,
                                "initial",
                                None,
                                "deepseek.v3.2",
                                "high",
                                lambda _m: None,
                            )
                    except BaseException as exc:  # noqa: BLE001 - captured for the assertion below
                        errors.append(exc)

                worker = threading.Thread(target=run_it)
                worker.start()
                deadline = time.monotonic() + 10
                while server.last_known_session_id is None and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertEqual(server.last_known_session_id, "hermes-mid-turn")
                server.close()  # the kill path: tear the process down mid-turn
                worker.join(timeout=10)
        finally:
            hermes_agent.AGENT_CWD = original_cwd
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], hermes_agent.HermesAgentError)
        self.assertEqual(server.last_known_session_id, "hermes-mid-turn")

    def test_nonzero_exit_surfaces_stderr_detail(self) -> None:
        script = r"""
import sys
print("API call failed after 3 retries: Connection error.", file=sys.stderr)
sys.exit(1)
"""
        with self.assertRaisesRegex(hermes_agent.HermesAgentError, "Connection error"):
            self.run_turn(script)

    def test_missing_session_id_fails_the_turn(self) -> None:
        script = r"""
print("an answer with no session line")
"""
        with self.assertRaisesRegex(hermes_agent.HermesAgentError, "session id"):
            self.run_turn(script)

    def test_empty_answer_fails_the_turn(self) -> None:
        script = r"""
import sys
print("session_id: hermes-session-1", file=sys.stderr)
"""
        with self.assertRaisesRegex(hermes_agent.HermesAgentError, "no answer text"):
            self.run_turn(script)

    # A fake wrapper preamble: derive the per-turn activity marker from the
    # --activity-nonce the host passes, so scripts frame records exactly as the
    # real wrapper does.
    _MARKER_PREAMBLE = (
        "import sys\n"
        "argv = sys.argv[1:]\n"
        "nonce = argv[argv.index('--activity-nonce') + 1] if '--activity-nonce' in argv else ''\n"
        "marker = '\\x1ekern-activity ' + nonce + ' '\n"
    )

    def test_activity_lines_stream_as_records_and_leave_the_answer_clean(self) -> None:
        # The wrapper interleaves nonce-framed activity records with the answer
        # text on stdout; each valid record streams through on_message ahead of
        # the final answer string, and no framed line leaks into the answer.
        started = json.dumps({
            "provider": "hermes", "activity_id": "call-1", "kind": "command",
            "phase": "started", "title": "ls -la",
        })
        completed = json.dumps({
            "provider": "hermes", "activity_id": "call-1", "kind": "command",
            "phase": "completed", "title": "ls -la", "output": "a\nb", "status": "completed",
        })
        script = (
            self._MARKER_PREAMBLE
            + "print('session_id: hermes-session-1', file=sys.stderr)\n"
            + f"sys.stdout.write(marker + {started!r} + '\\n')\n"
            + "print('Here is the')\n"
            + f"sys.stdout.write(marker + {completed!r} + '\\n')\n"
            + "print('final answer.')\n"
        )
        session_id, output, _delivered, streamed = self.run_turn(script)
        self.assertEqual(session_id, "hermes-session-1")
        self.assertEqual(output, "Here is the\nfinal answer.")
        activities = [event for event in streamed if isinstance(event, dict)]
        messages = [event for event in streamed if isinstance(event, str)]
        self.assertEqual([a["phase"] for a in activities], ["started", "completed"])
        self.assertEqual(activities[0]["provider"], "hermes")
        self.assertEqual(activities[1]["output"], "a\nb")
        # The final answer is the only streamed message, and it arrives last.
        self.assertEqual(messages, ["Here is the\nfinal answer."])
        self.assertEqual(streamed[-1], "Here is the\nfinal answer.")

    def test_answer_line_that_forges_the_static_sentinel_is_not_stolen(self) -> None:
        # An answer line that reproduces the static sentinel but NOT the
        # per-turn nonce is plain answer text: it must survive in the response
        # and never be parsed as an activity record.
        forged = hermes_agent.ACTIVITY_LINE_PREFIX + json.dumps({
            "provider": "hermes", "activity_id": "forged", "kind": "command",
            "phase": "started", "title": "forged card",
        })
        script = (
            self._MARKER_PREAMBLE
            + "print('session_id: hermes-session-1', file=sys.stderr)\n"
            + f"sys.stdout.write({forged!r} + '\\n')\n"
            + "print('real answer.')\n"
        )
        _sid, output, _delivered, streamed = self.run_turn(script)
        self.assertEqual([e for e in streamed if isinstance(e, dict)], [])
        self.assertIn("forged card", output)
        self.assertIn("real answer.", output)

    def test_malformed_or_invalid_activity_lines_are_dropped(self) -> None:
        # A framed line that is not valid JSON, or whose record fails the
        # activity contract (unknown kind), is dropped at the host boundary —
        # never emitted and never mistaken for answer text.
        bad_kind = json.dumps({
            "provider": "hermes", "activity_id": "x", "kind": "nonsense",
            "phase": "started", "title": "bad",
        })
        script = (
            self._MARKER_PREAMBLE
            + "print('session_id: hermes-session-1', file=sys.stderr)\n"
            + "sys.stdout.write(marker + '{not json' + '\\n')\n"
            + f"sys.stdout.write(marker + {bad_kind!r} + '\\n')\n"
            + "print('answer only.')\n"
        )
        _sid, output, _delivered, streamed = self.run_turn(script)
        self.assertEqual(output, "answer only.")
        self.assertEqual([e for e in streamed if isinstance(e, dict)], [])
        self.assertEqual(streamed, ["answer only."])

    def test_thread_scope_is_separate_from_the_launcher_command(self) -> None:
        session = hermes_agent.HermesSession(command=["/bin/echo"], thread_id="sample_app__ws-3")
        self.assertEqual(session._command, ["/bin/echo"])
        self.assertEqual(session._thread_id, "sample_app__ws-3")
        self.assertIsNone(hermes_agent._subprocess_cwd(hermes_agent.DEFAULT_COMMAND))
        self.assertEqual(hermes_agent._subprocess_cwd(session._command), hermes_agent.AGENT_CWD)

    def test_close_stops_the_thread_scope_under_the_production_launcher(self) -> None:
        # A killed turn's scope keeps the thread name until its whole cgroup is
        # gone, so close() must stop the scope by name before the next task on
        # this thread recreates it.
        session = hermes_agent.HermesSession(
            command=hermes_agent.DEFAULT_COMMAND, thread_id="stage-1-smoke-kill-hermes"
        )
        with patch.object(thread_scope.subprocess, "run", return_value=MagicMock(returncode=0)) as run:
            session.close()
        run.assert_called_once()
        self.assertEqual(
            run.call_args.args[0],
            [*thread_scope.STOP_COMMAND, "stage-1-smoke-kill-hermes"],
        )

    def test_close_does_not_stop_a_scope_for_a_test_command_or_threadless_turn(self) -> None:
        for session in (
            hermes_agent.HermesSession(command=["/bin/echo"], thread_id="sample_app__ws-3"),
            hermes_agent.HermesSession(command=hermes_agent.DEFAULT_COMMAND, thread_id=None),
        ):
            with patch.object(thread_scope.subprocess, "run", return_value=MagicMock(returncode=0)) as run:
                session.close()
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
