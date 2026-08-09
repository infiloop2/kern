"""Orchestrator tests for the thread-only admission model.

There is no queue and no workers: a message either starts a turn immediately
(idle thread, runtime active, capacity available), steers the running turn, or
is rejected with a retry hint. Turns are driven here the way the service does
it — orchestrator.admit_turn inside a state.mutation() plus launch_turn, or
the full synchronous service.send_thread_message path — against fake provider
servers/run_turn seams.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

import pg_harness

from host.runtime.admin_api import orchestrator, service
from host.runtime.admin_api.errors import ApiError
from host.runtime.core import db, state
from host.network_integrations.claude import guard as claude_guard
from host.network_integrations.claude.manifest import ClaudeIntegration


def anthropic_request_denied(config, method, host, path, headers, attest_account=None):
    return claude_guard.request_denied(
        config, method, host, path, "", headers, b"", attest_account
    )
from host.runtime.core.state import save_network_policy as save_policy
from host.runtime.core.state import (
    read_claude_account,
    read_openai_account,
    read_proxy_claude_account_id,
    read_proxy_openai_account_id,
    save_claude_account,
    save_openai_account,
    save_proxy_claude_account_id,
    save_proxy_openai_account_id,
)


# The default (model, effort) per runtime for seeded threads and turns.
DEFAULT_SESSION = {
    "codex": ("gpt-5.6-terra", "high"),
    "claude_code": ("claude-opus-5", "high"),
    "hermes": ("qwen.qwen3-coder-next", "high"),
}


class FakeServer:
    """Stands in for CodexAppServer/ClaudeCodeSession: records lifecycle calls
    and lets a test observe close/fence behaviour."""

    instances: list["FakeServer"] = []

    def __init__(
        self,
        command: object = None,
        thread_id: str | None = None,
        on_ready=None,
        on_session_id=None,
    ) -> None:
        self.started = 0
        self.closed = False
        self.interrupted = False
        self.thread_id = thread_id
        self.on_ready = on_ready
        self.on_session_id = on_session_id
        self.steered: list[str] = []
        self._delivered_steers = 0
        FakeServer.instances.append(self)

    def start(self, init_timeout: float = 60.0) -> None:
        self.started += 1
        if self.on_ready is not None and not self.on_ready():
            raise RuntimeError("execution stopped during startup")

    def alive(self) -> bool:
        return self.started > 0 and not self.closed

    def close(self) -> None:
        self.closed = True

    def interrupt(self) -> None:
        self.interrupted = True
        self.closed = True

    def steer(self, message: str) -> None:
        self.steered.append(message)
        self._delivered_steers += 1

    def take_delivered_steers(self) -> int:
        delivered = self._delivered_steers
        self._delivered_steers = 0
        return delivered


def read_agent_events() -> list[dict[str, object]]:
    """Every agent event, oldest first (tests inspect whole logs; the runtime
    only ever pages)."""
    with db.transaction() as cur:
        cur.execute(f"SELECT {state._EVENT_FIELDS} FROM agent_events ORDER BY seq")
        return [state._event_dict(row) for row in cur.fetchall()]


def thread_events(thread_id: str) -> list[dict[str, object]]:
    return [event for event in read_agent_events() if event["thread_id"] == thread_id]


def event_summary(events: list[dict[str, object]]) -> list[tuple[object, object]]:
    return [(event["event_type"], event.get("payload", {}).get("message")) for event in events]


def seed_oauth_login(key: str, record: dict[str, object] | None) -> None:
    with state.mutation() as cur:
        state.set_oauth_login(cur, key, record)


def save_approved_openai_account(account_id: str, **extra: object) -> None:
    save_openai_account(
        {"account_id": account_id, "operator_approval": orchestrator.OPENAI_OPERATOR_APPROVAL, **extra}
    )


def save_attested_claude_account(account_id: str, **extra: object) -> None:
    save_claude_account(
        {"account_id": account_id, "identity_attestation": orchestrator.CLAUDE_IDENTITY_ATTESTATION, **extra}
    )


class OrchestratorTests(unittest.TestCase):
    def setUp(self) -> None:
        pg_harness.reset_database()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.proxy_temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.addCleanup(self.proxy_temp_dir.cleanup)
        self.env_patch = patch.dict(
            "os.environ",
            {"KERN_STATE_DIR": self.temp_dir.name, "KERN_PROXY_STATE_DIR": self.proxy_temp_dir.name},
        )
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        FakeServer.instances = []
        orchestrator._LIVE.clear()
        self.addCleanup(orchestrator._LIVE.clear)
        save_policy(
            {
                "network_integrations": {"openai": {"enabled": True}, "claude": {"enabled": True}},
            },
            "2026-06-08T00:00:00Z",
        )
        self.server_patch = patch.object(orchestrator.codex_app_server, "CodexAppServer", FakeServer)
        self.server_patch.start()
        self.addCleanup(self.server_patch.stop)
        # Unit tests mock provider traffic. A steady Claude status refresh
        # includes a live /usage probe and rereads the credential in case the
        # CLI rotated it; default both seams to a successful unchanged token.
        self.claude_usage_patch = patch.object(orchestrator.claude_code, "read_claude_usage", return_value={})
        self.claude_usage_patch.start()
        self.addCleanup(self.claude_usage_patch.stop)

        def current_claude_credential() -> dict[str, object] | None:
            token_hash = read_claude_account().get("access_token_sha256")
            return {"access_token_sha256": token_hash} if token_hash else None

        self.claude_account_patch = patch.object(
            orchestrator.claude_code,
            "read_claude_account",
            side_effect=current_claude_credential,
        )
        self.claude_account_patch.start()
        self.addCleanup(self.claude_account_patch.stop)
        # Live-validation verdicts are process-global memos; isolate tests.
        # Reset on the way out too: the other classes in this file do not all
        # clear these, so leaving a memo behind makes them order-dependent.
        orchestrator._CLAUDE_LIVE_PROBE = None
        orchestrator._CLAUDE_ATTESTATION_MEMO = None
        orchestrator.codex_app_server.clear_live_validation_failure()
        self.addCleanup(orchestrator.codex_app_server.clear_live_validation_failure)
        self.addCleanup(setattr, orchestrator, "_CLAUDE_ATTESTATION_MEMO", None)
        self.addCleanup(setattr, orchestrator, "_CLAUDE_LIVE_PROBE", None)
        orchestrator._set_runtime_status("codex", "active")
        orchestrator._set_runtime_status("claude_code", "active")

    # -- helpers ---------------------------------------------------------------------

    def register_live_turn(
        self,
        runtime: str,
        thread_id: str,
        server: FakeServer | None = None,
        *,
        finished: bool = False,
        stopped: bool = False,
    ) -> orchestrator._Turn:
        model, effort = DEFAULT_SESSION[runtime]
        with state.mutation() as cur:
            if state.thread_session_config(thread_id, cur) is None:
                state.save_thread_session(
                    cur, runtime, thread_id, None, state.utc_now(), model, effort
                )
            run_number = state.start_thread_run(cur, thread_id)
        turn = orchestrator._Turn(runtime, thread_id, model, effort, run_number)
        turn.server = server
        turn.phase = (
            orchestrator.ExecutionPhase.FINISHING
            if finished or stopped
            else orchestrator.ExecutionPhase.RUNNING
        )
        with orchestrator._LIVE_LOCK:
            orchestrator._LIVE[f"{runtime}:{thread_id}"] = turn
        return turn

    def admit(self, thread_id: str, runtime: str = "codex", message: str = "hello") -> orchestrator._Turn | None:
        """Just the admission decision, inside its own mutation (no launch)."""
        model, effort = DEFAULT_SESSION[runtime]
        after_commit = []
        with state.mutation(after_commit=after_commit) as cur:
            state.save_thread_session(
                cur, runtime, thread_id, None, state.utc_now(), model, effort
            )
            return orchestrator.admit_turn(
                cur, after_commit, thread_id, runtime, model, effort, message
            )

    def start_turn(
        self,
        thread_id: str,
        runtime: str = "codex",
        message: str | None = None,
    ) -> orchestrator._Turn:
        """Admit and launch a turn the way the service does (admission and the
        session row in one mutation, launch after commit)."""
        model, effort = DEFAULT_SESSION[runtime]
        message = message or f"turn on {thread_id}"
        after_commit = []
        with state.mutation(after_commit=after_commit) as cur:
            config = state.thread_session_config(thread_id, cur)
            provider_session_id = config.get("provider_session_id") if config else None
            state.save_thread_session(
                cur, runtime, thread_id, provider_session_id, state.utc_now(), model, effort
            )
            turn = orchestrator.admit_turn(
                cur, after_commit, thread_id, runtime, model, effort, message
            )
            assert turn is not None
        orchestrator.launch_turn(turn, message, provider_session_id)
        return turn

    def send_message(self, thread_id: str, message: str, runtime: str = "codex") -> dict[str, object]:
        body: dict[str, object] = {"message": message}
        if state.thread_session_config(thread_id) is None:
            model, effort = DEFAULT_SESSION[runtime]
            body |= {"agent_runtime": runtime, "model": model, "effort": effort}
        return service.send_thread_message(thread_id, body)

    def wait_for(self, condition, message: str = "condition") -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(0.005)
        self.fail(f"timed out waiting for {message}")

    def wait_until_idle(self, thread_id: str) -> None:
        self.wait_for(
            lambda: thread_id not in orchestrator.live_thread_ids(), f"{thread_id} to go idle"
        )

    def run_turn_stub(self, release: threading.Event | None = None):
        """A run_turn replacement: returns ("codex-<input message>", "done"),
        optionally blocking until the test releases it."""

        def fake_run_turn(
            server,
            input_message,
            provider_session_id,
            model,
            effort,
            on_message,
            finish_turn=None,
        ):
            if release is not None:
                if not release.wait(timeout=10):
                    raise AssertionError("test never released the fake turn")
            return f"codex-{input_message}", "done"

        return fake_run_turn

    # -- admission -------------------------------------------------------------------

    def test_message_to_idle_thread_runs_and_records_the_message(self) -> None:
        observed_config: list[tuple[str, str]] = []

        def fake_run_turn(server, input_message, provider_session_id, model, effort, on_message):
            observed_config.append((model, effort))
            return "codex-chat", "done"

        with patch.object(orchestrator.codex_app_server, "run_turn", fake_run_turn):
            response = self.send_message("chat", "hello")
            self.assertEqual(response["status"], "accepted")
            self.assertEqual(response["thread"]["thread_id"], "chat")
            self.wait_until_idle("chat")

        self.assertEqual(observed_config, [("gpt-5.6-terra", "high")])
        events = thread_events("chat")
        self.assertEqual(
            event_summary(events),
            [("thread.message", "hello")],
        )
        message_event = next(event for event in events if event["event_type"] == "thread.message")
        self.assertEqual(message_event["payload"]["source"], "user")
        self.assertEqual(state.thread_session_config("chat")["provider_session_id"], "codex-chat")
        self.assertEqual(len(FakeServer.instances), 1)
        self.assertTrue(FakeServer.instances[0].closed)
        self.assertEqual(orchestrator._LIVE, {})

    def test_message_to_live_thread_synchronously_steers_and_then_records_it(self) -> None:
        running = threading.Event()
        release = threading.Event()

        def fake_run_turn(server, input_message, provider_session_id, model, effort, on_message):
            running.set()
            if not release.wait(timeout=10):
                raise AssertionError("test never released the turn")
            on_message({
                "provider": "codex",
                "activity_id": "command-1",
                "kind": "command",
                "phase": "completed",
                "title": "pytest",
            })
            return "codex-t1", "done"

        try:
            with patch.object(orchestrator.codex_app_server, "run_turn", fake_run_turn):
                self.send_message("t1", "go")
                self.assertTrue(running.wait(timeout=10))
                self.assertEqual(self.send_message("t1", "first")["status"], "accepted")
                self.assertEqual(self.send_message("t1", "second")["status"], "accepted")
                self.assertEqual(FakeServer.instances[0].steered, ["first", "second"])
                release.set()
                self.wait_until_idle("t1")
        finally:
            release.set()
        events = thread_events("t1")
        self.assertEqual(
            event_summary(events),
            [
                ("thread.message", "go"),
                ("thread.message", "first"),
                ("thread.message", "second"),
                ("thread.activity", None),
            ],
        )
        activity_event = next(event for event in events if event["event_type"] == "thread.activity")
        self.assertEqual(
            activity_event["payload"]["activity"]["activity_id"],
            f"{state.thread_session_config('t1')['run_number']}:command-1",
        )
        for message in ("first", "second"):
            event = next(e for e in events if e.get("payload", {}).get("message") == message)
            self.assertEqual(event["payload"]["source"], "user")

    def test_starting_is_retryable_until_the_provider_accepts_the_initial_message(self) -> None:
        turn = self.admit("chat", message="initial")
        assert turn is not None
        server = FakeServer()
        turn.server = server

        with self.assertRaises(ApiError) as starting:
            orchestrator.steer_live_turn("chat", "codex", "too early")
        self.assertEqual(starting.exception.status.value, 409)
        self.assertEqual(
            starting.exception.message,
            "the agent is starting; retry shortly",
        )
        self.assertEqual(event_summary(thread_events("chat")), [("thread.message", "initial")])

        self.assertTrue(orchestrator._provider_ready(turn))
        self.assertTrue(orchestrator.steer_live_turn("chat", "codex", "accepted"))
        self.assertEqual(server.steered, ["accepted"])
        self.assertEqual(
            event_summary(thread_events("chat")),
            [("thread.message", "initial"), ("thread.message", "accepted")],
        )

        self.assertTrue(orchestrator.stop_thread_turn("chat"))
        orchestrator._close_turn(turn, server)
        self.assertNotIn("codex:chat", orchestrator._LIVE)

    def test_provider_rejection_after_running_fails_instead_of_retrying_startup(self) -> None:
        class RejectingServer(FakeServer):
            def steer(self, message: str) -> None:
                del message
                raise orchestrator.codex_app_server.CodexAppServerError(
                    "provider is not accepting input"
                )

        server = RejectingServer()
        turn = self.register_live_turn("codex", "chat", server)

        with self.assertRaises(ApiError) as rejected:
            orchestrator.steer_live_turn("chat", "codex", "new direction")

        self.assertEqual(rejected.exception.status.value, 502)
        self.assertIn("rejected the message", rejected.exception.message)
        self.assertEqual(turn.phase, orchestrator.ExecutionPhase.FINISHING)
        self.assertTrue(server.interrupted)
        self.assertEqual(state.thread_session_config("chat")["status"], "idle")
        events = thread_events("chat")
        self.assertEqual(event_summary(events), [("thread.error", None)])
        self.assertNotIn(
            "new direction",
            [event.get("payload", {}).get("message") for event in events],
        )
        orchestrator._close_turn(turn, server)

    def test_codex_completion_race_is_retryable_without_a_thread_error(self) -> None:
        class FinishingServer(FakeServer):
            def steer(self, message: str) -> None:
                del message
                raise orchestrator.codex_app_server.CodexTurnFinishing(
                    "Codex turn is finishing"
                )

        server = FinishingServer()
        turn = self.register_live_turn("codex", "chat", server)

        with self.assertRaises(ApiError) as finishing:
            orchestrator.steer_live_turn("chat", "codex", "too late")

        self.assertEqual(finishing.exception.status.value, 409)
        self.assertEqual(
            finishing.exception.message,
            "the agent is finishing; retry shortly",
        )
        self.assertEqual(turn.phase, orchestrator.ExecutionPhase.RUNNING)
        self.assertFalse(server.interrupted)
        self.assertEqual(thread_events("chat"), [])
        self.assertNotIn("too late", server.steered)
        self.assertTrue(orchestrator.stop_thread_turn("chat"))
        orchestrator._close_turn(turn, server)

    def test_eleventh_concurrent_turn_per_runtime_is_rejected_with_429(self) -> None:
        self.assertEqual(orchestrator.TURN_LIMIT_PER_RUNTIME, 10)
        release = threading.Event()
        started: list[str] = []

        def fake_run_turn(server, input_message, provider_session_id, model, effort, on_message):
            started.append(input_message)
            if not release.wait(timeout=10):
                raise AssertionError("never released")
            return f"codex-{input_message}", "done"

        try:
            with patch.object(orchestrator.codex_app_server, "run_turn", fake_run_turn):
                thread_ids = [
                    f"t{index}"
                    for index in range(1, orchestrator.TURN_LIMIT_PER_RUNTIME + 1)
                ]
                for thread_id in thread_ids:
                    self.assertEqual(self.send_message(thread_id, thread_id)["status"], "accepted")
                self.wait_for(
                    lambda: len(started) == orchestrator.TURN_LIMIT_PER_RUNTIME,
                    "all ten turns to start",
                )

                with self.assertRaises(ApiError) as caught:
                    self.send_message("t11", "one too many")
                self.assertEqual(caught.exception.status.value, 429)
                self.assertIn("already running 10 concurrent threads", caught.exception.message)
                # A message for a live thread is a steer, never capacity-bound.
                self.assertEqual(self.send_message("t1", "still steerable")["status"], "accepted")

                release.set()
                for thread_id in thread_ids:
                    self.wait_until_idle(thread_id)
                # Capacity freed: the rejected thread now starts.
                self.assertEqual(self.send_message("t11", "retry")["status"], "accepted")
                self.wait_until_idle("t11")
        finally:
            release.set()

    def test_runtime_turn_pools_are_independent(self) -> None:
        save_policy(
            {
                "network_integrations": {
                    "openai": {"enabled": True},
                    "claude": {"enabled": True},
                    "bedrock": {"enabled": True},
                },
            },
            "2026-06-08T00:00:01Z",
        )
        orchestrator._set_runtime_status("hermes", "active")
        for runtime in ("codex", "claude_code", "hermes"):
            for index in range(orchestrator.TURN_LIMIT_PER_RUNTIME):
                server = FakeServer()
                server.started = 1
                self.register_live_turn(runtime, f"{runtime}-{index}", server)
        for runtime in ("codex", "claude_code", "hermes"):
            with self.assertRaises(ApiError) as caught:
                self.admit(f"{runtime}-overflow", runtime=runtime)
            self.assertEqual(caught.exception.status.value, 429)
        self.assertEqual(len(orchestrator._LIVE), 3 * orchestrator.TURN_LIMIT_PER_RUNTIME)

    def test_finished_turn_still_fences_its_thread_and_counts_against_capacity(self) -> None:
        # A turn can be terminal (finished) while its process is still closing.
        # Its thread stays fenced with a retry hint, and the entry still holds
        # a capacity slot, so the runtime process cap cannot be exceeded.
        for index in range(orchestrator.TURN_LIMIT_PER_RUNTIME):
            server = FakeServer()
            server.started = 1
            self.register_live_turn("codex", f"closing-{index}", server, finished=True)

        with self.assertRaises(ApiError) as fenced:
            self.admit("closing-0")
        self.assertEqual(fenced.exception.status.value, 409)
        self.assertIn("agent is finishing", fenced.exception.message)
        with self.assertRaises(ApiError) as capacity:
            self.admit("fresh-thread")
        self.assertEqual(capacity.exception.status.value, 429)

    def test_thread_admission_is_blocked_while_the_previous_process_closes(self) -> None:
        # The real closing window: the turn completed (terminal event durable)
        # but its finally is still inside server.close(). The thread 409s with
        # the retry hint; other threads are unaffected; after the close the
        # thread accepts a new turn.
        release_close = threading.Event()

        class SlowCloseServer(FakeServer):
            def close(self) -> None:
                if not release_close.wait(timeout=10):
                    raise AssertionError("test never released the slow close")
                super().close()

        try:
            with (
                patch.object(orchestrator.codex_app_server, "CodexAppServer", SlowCloseServer),
                patch.object(orchestrator.codex_app_server, "run_turn", self.run_turn_stub()),
            ):
                self.send_message("chat", "first")
                self.wait_for(
                    lambda: state.thread_session_config("chat")["status"] == "idle",
                    "the run state to become idle",
                )
                with self.assertRaises(ApiError) as caught:
                    self.send_message("chat", "too soon")
                self.assertEqual(caught.exception.status.value, 409)
                self.assertIn("agent is finishing", caught.exception.message)
                self.assertEqual(self.send_message("other", "fine")["status"], "accepted")
                release_close.set()
                self.wait_until_idle("chat")
                self.wait_until_idle("other")
                self.assertEqual(self.send_message("chat", "retry")["status"], "accepted")
                self.wait_until_idle("chat")
        finally:
            release_close.set()

    def test_hermes_live_turn_rejects_steers(self) -> None:
        server = FakeServer()
        server.started = 1
        self.register_live_turn("hermes", "hermes-busy", server)
        with self.assertRaises(ApiError) as caught:
            orchestrator.steer_live_turn("hermes-busy", "hermes", "hello")
        self.assertEqual(caught.exception.status.value, 409)
        self.assertIn("Hermes cannot accept another message", caught.exception.message)

    def test_direct_steers_do_not_accumulate_in_a_host_mailbox(self) -> None:
        model, effort = DEFAULT_SESSION["codex"]
        with state.mutation() as cur:
            state.save_thread_session(
                cur,
                "codex",
                "chat",
                "codex-existing",
                "2026-06-08T00:00:01Z",
                model,
                effort,
            )
        turn = self.register_live_turn("codex", "chat", FakeServer())
        for index in range(25):
            self.assertTrue(
                orchestrator.steer_live_turn("chat", "codex", f"steer {index}")
            )
        self.assertEqual(
            turn.server.steered,
            [f"steer {index}" for index in range(25)],
        )

    def test_acknowledged_steer_refreshes_recency_without_changing_session(self) -> None:
        model, effort = DEFAULT_SESSION["codex"]
        with state.mutation() as cur:
            state.save_thread_session(
                cur,
                "codex",
                "chat",
                "codex-existing",
                "2026-06-08T00:00:01Z",
                model,
                effort,
            )
        self.register_live_turn("codex", "chat", FakeServer())

        with patch.object(orchestrator, "utc_now", return_value="2026-06-08T00:00:09Z"):
            self.assertTrue(orchestrator.steer_live_turn("chat", "codex", "new direction"))

        config = state.thread_session_config("chat")
        self.assertIsNotNone(config)
        assert config is not None
        self.assertEqual(config["last_used_at"], "2026-06-08T00:00:09Z")
        self.assertEqual(config["provider_session_id"], "codex-existing")
        self.assertEqual(event_summary(thread_events("chat")), [("thread.message", "new direction")])

    def test_admission_rejects_a_non_active_runtime_without_refreshing(self) -> None:
        # A cached non-active status is the rejection verdict as-is: admission
        # must not wait on a refresh (whose status helper can be slow).
        orchestrator._set_runtime_status("codex", "awaiting_login")
        with patch.object(
            orchestrator,
            "refresh_runtime_status",
            side_effect=AssertionError("a cached non-active admission must not refresh"),
        ):
            with self.assertRaises(ApiError) as caught:
                self.admit("chat")
        self.assertEqual(caught.exception.status.value, 409)
        self.assertEqual(
            caught.exception.message,
            "Codex runtime is awaiting_login; messages run only while it is active",
        )
        self.assertEqual(orchestrator._LIVE, {})
        self.assertEqual(thread_events("chat"), [])

    def test_admission_rejects_a_policy_disabled_runtime_despite_cached_active_status(self) -> None:
        save_policy({"network_integrations": {}}, "2026-06-08T00:00:01Z")
        self.assertEqual(orchestrator.runtime_status("codex"), "active")  # stale cache
        with self.assertRaises(ApiError) as caught:
            self.admit("chat")
        self.assertEqual(caught.exception.status.value, 409)
        self.assertIn("Codex runtime is deactivated", caught.exception.message)
        self.assertEqual(orchestrator._LIVE, {})

    def test_failed_send_mutation_never_publishes_the_admitted_turn(self) -> None:
        # The live fence is published only after commit, so a failed mutation
        # rolls the events back without any in-memory cleanup path.
        with patch.object(service.state, "save_thread_session", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.send_message("chat", "hello")
        self.assertEqual(orchestrator._LIVE, {})
        self.assertEqual(thread_events("chat"), [])
        # The thread is usable immediately afterwards.
        with patch.object(orchestrator.codex_app_server, "run_turn", self.run_turn_stub()):
            self.assertEqual(self.send_message("chat", "hello")["status"], "accepted")
            self.wait_until_idle("chat")

    def test_failed_post_ack_steer_write_is_ambiguous_without_a_history_event(self) -> None:
        model, effort = DEFAULT_SESSION["codex"]
        with state.mutation() as cur:
            state.save_thread_session(
                cur,
                "codex",
                "chat",
                "codex-existing",
                "2026-06-08T00:00:01Z",
                model,
                effort,
            )
        turn = self.register_live_turn("codex", "chat", FakeServer())
        with patch.object(state, "append_agent_event", side_effect=RuntimeError("write failed")):
            with self.assertRaises(RuntimeError):
                orchestrator.steer_live_turn("chat", "codex", "possibly delivered")
        self.assertEqual(turn.server.steered, ["possibly delivered"])
        self.assertEqual(thread_events("chat"), [])
        self.assertEqual(
            state.thread_session_config("chat")["last_used_at"],
            "2026-06-08T00:00:01Z",
        )

    def test_agent_runtime_status_reports_active_thread_ids(self) -> None:
        self.register_live_turn("codex", "t2", FakeServer())
        self.register_live_turn("codex", "t1", FakeServer())
        self.register_live_turn("claude_code", "c1", FakeServer())
        runtimes = {record["type"]: record for record in orchestrator.agent_runtime_status()["runtimes"]}
        self.assertEqual(runtimes["codex"]["active_thread_ids"], ["t1", "t2"])
        self.assertEqual(runtimes["claude_code"]["active_thread_ids"], ["c1"])
        self.assertEqual(runtimes["hermes"]["active_thread_ids"], [])

    # -- turn execution --------------------------------------------------------------

    def test_startup_timeout_finalizes_the_database_before_interrupting(self) -> None:
        turn = self.admit("slow-start", message="hello")
        assert turn is not None
        server = FakeServer()
        turn.server = server

        orchestrator._starting_timed_out(turn)

        self.assertEqual(turn.phase, orchestrator.ExecutionPhase.FINISHING)
        self.assertEqual(state.thread_session_config("slow-start")["status"], "idle")
        self.assertTrue(server.interrupted)
        self.assertEqual(
            thread_events("slow-start")[-1]["payload"]["error_message"],
            "agent startup timed out",
        )
        # Durable finalization does not lift the process fence.
        self.assertEqual(service.get_thread("slow-start")["status"], "running")
        with self.assertRaises(ApiError) as finishing:
            self.send_message("slow-start", "retry")
        self.assertEqual(
            finishing.exception.message,
            "the agent is finishing; retry shortly",
        )
        orchestrator._close_turn(turn, server)
        self.assertEqual(service.get_thread("slow-start")["status"], "idle")

    def test_provider_session_callback_persists_while_the_process_is_live(self) -> None:
        session_published = threading.Event()
        release = threading.Event()

        def live_run(server, input_message, provider_session_id, model, effort, on_message):
            del input_message, provider_session_id, model, effort, on_message
            server.on_session_id("codex-live-session")
            session_published.set()
            if not release.wait(timeout=10):
                raise AssertionError("test never released the provider")
            return "codex-live-session", "done"

        try:
            with patch.object(orchestrator.codex_app_server, "run_turn", live_run):
                self.send_message("chat", "hello")
                self.assertTrue(session_published.wait(timeout=10))
                self.assertEqual(
                    state.thread_session_config("chat")["provider_session_id"],
                    "codex-live-session",
                )
                self.assertIn("codex:chat", orchestrator._LIVE)
                release.set()
                self.wait_until_idle("chat")
        finally:
            release.set()

    def test_empty_provider_session_callback_never_erases_history(self) -> None:
        model, effort = DEFAULT_SESSION["codex"]
        with state.mutation() as cur:
            state.save_thread_session(
                cur,
                "codex",
                "chat",
                "codex-existing",
                state.utc_now(),
                model,
                effort,
            )
            run_number = state.start_thread_run(cur, "chat")
        turn = orchestrator._Turn("codex", "chat", model, effort, run_number)

        with self.assertRaisesRegex(ValueError, "empty session"):
            orchestrator._provider_session_accepted(turn, "  ")

        self.assertEqual(
            state.thread_session_config("chat")["provider_session_id"],
            "codex-existing",
        )

    def test_policy_reconciliation_stops_an_admitted_turn_before_process_spawn(self) -> None:
        # A policy update owns the active-to-deactivated transition. If it
        # lands after admission but before execution, the transition fails the
        # turn and the worker observes its stopped flag before spawning.
        turn = self.admit("stale-active", message="stale")
        assert turn is not None
        save_policy({"network_integrations": {}}, "2026-06-08T00:00:01Z")
        orchestrator.reconcile_runtime_status_after_policy_change()

        orchestrator._run_turn(turn, "stale", None)

        self.assertEqual(FakeServer.instances, [])
        self.assertEqual(
            event_summary(thread_events("stale-active"))[-1], ("thread.error", None)
        )
        failed = thread_events("stale-active")[-1]
        self.assertEqual(
            failed["payload"]["error_message"],
            orchestrator.DEACTIVATED_REASON,
        )
        self.assertEqual(orchestrator._LIVE, {})

    def test_stop_during_server_start_closes_the_process_after_start(self) -> None:
        start_entered = threading.Event()
        release_start = threading.Event()

        class StartRaceServer(FakeServer):
            def __init__(self) -> None:
                super().__init__()
                self.running = False
                self.close_calls = 0

            def start(self, init_timeout: float = 60.0) -> None:
                del init_timeout
                start_entered.set()
                if not release_start.wait(timeout=10):
                    raise AssertionError("test never released server start")
                self.started += 1
                self.running = True

            def close(self) -> None:
                self.close_calls += 1
                self.running = False
                self.closed = True

        server = StartRaceServer()
        turn = self.admit("stop-during-start", message="hi")
        assert turn is not None
        run_turn = MagicMock(side_effect=AssertionError("stopped turn reached provider"))
        worker = threading.Thread(
            target=orchestrator._run_turn,
            args=(turn, "hi", None),
        )
        stop_result: list[bool] = []
        stopper = threading.Thread(
            target=lambda: stop_result.append(
                orchestrator.stop_thread_turn("stop-during-start")
            ),
        )
        try:
            with (
                patch.object(orchestrator, "_new_agent_server", return_value=server),
                patch.object(orchestrator.codex_app_server, "run_turn", run_turn),
            ):
                worker.start()
                self.assertTrue(start_entered.wait(timeout=10))
                stopper.start()
                self.wait_for(
                    lambda: turn.phase == orchestrator.ExecutionPhase.FINISHING,
                    "stop state to commit",
                )
                # Stop returns once the durable state is final. The execution
                # thread still owns process teardown and the live fence.
                stopper.join(timeout=10)
                self.assertFalse(stopper.is_alive())
                release_start.set()
                worker.join(timeout=10)
        finally:
            release_start.set()
            stopper.join(timeout=10)
            worker.join(timeout=10)

        self.assertFalse(stopper.is_alive())
        self.assertFalse(worker.is_alive())
        self.assertEqual(stop_result, [True])
        self.assertEqual(server.started, 1)
        self.assertEqual(server.close_calls, 1)
        self.assertFalse(server.running)
        run_turn.assert_not_called()
        self.assertEqual(
            event_summary(thread_events("stop-during-start")),
            [("thread.message", "hi"), ("thread.stopped", None)],
        )
        self.assertEqual(orchestrator._LIVE, {})

    def test_claude_stop_before_run_spawn_is_honored_by_the_adapter(self) -> None:
        run_entered = threading.Event()
        release_run = threading.Event()
        spawned = threading.Event()
        server = FakeServer()

        def guarded_claude_run(
            server,
            input_message,
            provider_session_id,
            model,
            effort,
            on_message,
            finish_turn,
        ):
            del input_message, provider_session_id, model, effort, on_message, finish_turn
            run_entered.set()
            if not release_run.wait(timeout=10):
                raise AssertionError("test never released Claude run")
            if server.closed:
                raise orchestrator.claude_code.ClaudeCodeError("Claude Code turn was closed")
            spawned.set()
            return "claude-session", "done"

        turn = self.admit(
            "claude-stop-before-spawn",
            runtime="claude_code",
            message="hi",
        )
        assert turn is not None
        worker = threading.Thread(
            target=orchestrator._run_turn,
            args=(turn, "hi", None),
        )
        try:
            with (
                patch.object(orchestrator, "_new_agent_server", return_value=server),
                patch.object(orchestrator, "refresh_runtime_status", return_value="active"),
                patch.object(orchestrator.claude_code, "run_turn", guarded_claude_run),
            ):
                worker.start()
                self.assertTrue(run_entered.wait(timeout=10))
                self.assertTrue(
                    orchestrator.stop_thread_turn("claude-stop-before-spawn")
                )
                self.assertTrue(server.closed)
                release_run.set()
                worker.join(timeout=10)
        finally:
            release_run.set()
            worker.join(timeout=10)

        self.assertFalse(worker.is_alive())
        self.assertFalse(spawned.is_set())
        self.assertEqual(
            event_summary(thread_events("claude-stop-before-spawn")),
            [("thread.message", "hi"), ("thread.stopped", None)],
        )
        self.assertEqual(orchestrator._LIVE, {})

    def test_each_turn_runs_on_a_fresh_server_and_resumes_the_recorded_session(self) -> None:
        seen: list[str | None] = []

        def recording_run_turn(server, input_message, provider_session_id, model, effort, on_message):
            seen.append(provider_session_id)
            return f"codex-{input_message}", "done"

        with patch.object(orchestrator.codex_app_server, "run_turn", recording_run_turn):
            self.send_message("chat", "first")
            self.wait_until_idle("chat")
            self.send_message("chat", "second")
            self.wait_until_idle("chat")

        self.assertEqual(seen, [None, "codex-first"])
        self.assertEqual(len(FakeServer.instances), 2)
        self.assertTrue(all(server.closed for server in FakeServer.instances))
        self.assertEqual(state.thread_session_config("chat")["provider_session_id"], "codex-second")

    def test_claude_runtime_records_and_resumes_session_id(self) -> None:
        save_attested_claude_account("acct", access_token_sha256="f" * 64)
        seen: list[str | None] = []
        seen_config: list[tuple[str, str]] = []

        def fake_run_turn(
            server,
            input_message,
            session_id,
            model,
            effort,
            on_message,
            finish_turn=None,
        ):
            seen.append(session_id)
            seen_config.append((model, effort))
            return "claude-session-1", "done"

        with (
            patch.object(orchestrator.claude_code, "ClaudeCodeSession", FakeServer),
            patch.object(orchestrator.claude_code, "run_turn", fake_run_turn),
            patch.object(
                orchestrator.claude_code,
                "account_status",
                return_value=("active", None, {"account_id": "acct", "access_token_sha256": "f" * 64}),
            ),
        ):
            service.send_thread_message(
                "chat",
                {
                    "message": "hi",
                    "agent_runtime": "claude_code",
                    "model": "claude-fable-5",
                    "effort": "ultracode",
                },
            )
            self.wait_until_idle("chat")
            service.send_thread_message("chat", {"message": "again"})
            self.wait_until_idle("chat")

        self.assertEqual(seen, [None, "claude-session-1"])
        self.assertEqual(seen_config, [("claude-fable-5", "ultracode")] * 2)
        self.assertEqual(state.thread_session_config("chat")["provider_session_id"], "claude-session-1")

    def test_claude_finish_turn_atomically_chooses_between_steer_and_finish(self) -> None:
        # Direct steer delivery and the finish decision share the turn's
        # delivery lock. A steer delivered first is observed as one additional
        # result; after completion, a message hits the closing fence.
        save_attested_claude_account("acct", access_token_sha256="f" * 64)
        turn_running = threading.Event()
        steer_sent = threading.Event()
        results: dict[str, object] = {}

        def fake_run_turn(server, input_message, session_id, model, effort, on_message, finish_turn):
            turn_running.set()
            if not steer_sent.wait(timeout=10):
                raise AssertionError("steer never sent")
            results["first_finish"] = finish_turn("claude-session-1", "done")
            results["second_finish"] = finish_turn("claude-session-1", "done")
            try:
                service.send_thread_message("chat", {"message": "too late"})
                results["post_finish"] = "accepted"
            except ApiError as exc:
                results["post_finish"] = (exc.status.value, exc.message)
            return "claude-session-1", "done"

        claude_patches = (
            patch.object(orchestrator.claude_code, "ClaudeCodeSession", FakeServer),
            patch.object(
                orchestrator.claude_code,
                "account_status",
                return_value=("active", None, {"account_id": "acct", "access_token_sha256": "f" * 64}),
            ),
        )
        with claude_patches[0], claude_patches[1]:
            with patch.object(orchestrator.claude_code, "run_turn", fake_run_turn):
                self.send_message("chat", "start", runtime="claude_code")
                self.assertTrue(turn_running.wait(timeout=10))
                self.assertEqual(self.send_message("chat", "queued steer")["status"], "accepted")
                steer_sent.set()
                self.wait_until_idle("chat")

            self.assertEqual(results["first_finish"], 1)
            self.assertEqual(results["second_finish"], 0)
            self.assertEqual(
                results["post_finish"],
                (409, "the agent is finishing; retry shortly"),
            )
            self.assertEqual(
                event_summary(thread_events("chat")),
                [
                    ("thread.message", "start"),
                    ("thread.message", "queued steer"),
                ],
            )
            self.assertEqual(state.thread_session_config("chat")["provider_session_id"], "claude-session-1")

            # After the close the thread accepts a new turn, resuming the session.
            seen: list[str | None] = []

            def recording_run_turn(
                server, input_message, session_id, model, effort, on_message, finish_turn=None
            ):
                seen.append(session_id)
                return "claude-session-2", "done"

            with patch.object(orchestrator.claude_code, "run_turn", recording_run_turn):
                self.assertEqual(self.send_message("chat", "next turn")["status"], "accepted")
                self.wait_until_idle("chat")
            self.assertEqual(seen, ["claude-session-1"])

    def test_claude_turn_updates_rotated_token_metadata_before_the_turn(self) -> None:
        # The Claude CLI refreshes its OAuth access token on its own schedule.
        # Turn-start convergence updates the stored token metadata, while the
        # proxy authorizes both hashes through the one account-UUID rule.
        old_token = "old-token"
        fresh_token = "fresh-token"
        claude_guard.clear_token_attestation_cache()
        self.addCleanup(claude_guard.clear_token_attestation_cache)
        policy = ClaudeIntegration(enabled=True, web_search=False)
        save_attested_claude_account("acct", access_token_sha256=hashlib.sha256(old_token.encode()).hexdigest())
        save_proxy_claude_account_id("acct")
        old_headers = [("Authorization", f"Bearer {old_token}")]
        self.assertIsNone(
            anthropic_request_denied(
                policy, "POST", "api.anthropic.com", "/v1/messages", old_headers,
                lambda _token: "acct",
            )
        )

        with (
            patch.object(orchestrator.claude_code, "ClaudeCodeSession", FakeServer),
            patch.object(orchestrator.claude_code, "run_turn", self.run_turn_stub()),
            patch.object(
                orchestrator.claude_code,
                "account_status",
                return_value=(
                    "active",
                    None,
                    {"account_id": "acct", "access_token_sha256": hashlib.sha256(fresh_token.encode()).hexdigest()},
                ),
            ),
            patch.object(
                orchestrator.claude_code,
                "read_attested_identity",
                return_value={
                    "access_token_sha256": hashlib.sha256(fresh_token.encode()).hexdigest(),
                    "account_uuid": "acct",
                },
            ),
        ):
            self.send_message("chat", "hi", runtime="claude_code")
            self.wait_until_idle("chat")

        self.assertEqual(event_summary(thread_events("chat"))[-1], ("thread.message", "hi"))
        self.assertEqual(read_claude_account()["access_token_sha256"], hashlib.sha256(fresh_token.encode()).hexdigest())
        self.assertEqual(read_proxy_claude_account_id(), "acct")
        fresh_headers = [("Authorization", f"Bearer {fresh_token}")]
        self.assertIsNone(
            anthropic_request_denied(
                policy, "POST", "api.anthropic.com", "/v1/messages", fresh_headers,
                lambda _token: "acct",
            )
        )

    def test_app_turn_server_receives_its_direct_thread_id(self) -> None:
        with patch.object(orchestrator.codex_app_server, "run_turn", self.run_turn_stub()):
            self.start_turn("app-1")
            self.wait_until_idle("app-1")
        self.assertEqual(FakeServer.instances[-1].thread_id, "app-1")
        self.assertFalse(hasattr(FakeServer.instances[-1], "workspace_instructions"))

        def failing_run_turn(server, input_message, provider_session_id, model, effort, on_message):
            raise RuntimeError("turn exploded")

        with patch.object(orchestrator.codex_app_server, "run_turn", failing_run_turn):
            self.start_turn("app-1")
            self.wait_until_idle("app-1")
        self.assertEqual(event_summary(thread_events("app-1"))[-1], ("thread.error", None))

    def test_non_app_thread_is_still_passed_to_its_runtime_scope(self) -> None:
        with patch.object(orchestrator.codex_app_server, "run_turn", self.run_turn_stub()):
            self.send_message("chat", "hi")
            self.wait_until_idle("chat")
        self.assertEqual(FakeServer.instances[-1].thread_id, "chat")
        self.assertFalse(hasattr(FakeServer.instances[-1], "workspace_instructions"))

    def test_chat_threads_use_their_direct_thread_id(self) -> None:
        with patch.object(orchestrator.codex_app_server, "run_turn", self.run_turn_stub()):
            self.start_turn("thread-1")
            self.wait_until_idle("thread-1")
        self.assertEqual(FakeServer.instances[-1].thread_id, "thread-1")

    def test_failed_turn_records_turn_failed_and_closes_the_server(self) -> None:
        def failing_run_turn(server, input_message, provider_session_id, model, effort, on_message):
            raise orchestrator.codex_app_server.CodexAppServerError("turn failed")

        with patch.object(orchestrator.codex_app_server, "run_turn", failing_run_turn):
            self.send_message("chat", "hi")
            self.wait_until_idle("chat")
        events = thread_events("chat")
        self.assertEqual(events[-1]["event_type"], "thread.error")
        self.assertEqual(events[-1]["payload"]["error_message"], "turn failed")
        self.assertNotIn("codex:chat", orchestrator._LIVE)
        self.assertTrue(FakeServer.instances[0].closed)

    def test_server_acquire_failure_fails_the_turn_instead_of_orphaning_it(self) -> None:
        # The turn was admitted (its events recorded) before the server exists.
        # If spawning the server blows up, the failure must land on the turn;
        # an escaped exception would leave the thread fenced forever.
        def exploding_server(
            command: object = None,
            thread_id: str | None = None,
            **_callbacks,
        ) -> FakeServer:
            raise OSError("cannot spawn app-server")

        with patch.object(orchestrator.codex_app_server, "CodexAppServer", exploding_server):
            self.send_message("chat", "hi")
            self.wait_until_idle("chat")
        events = thread_events("chat")
        self.assertEqual(events[-1]["event_type"], "thread.error")
        self.assertIn("cannot spawn app-server", events[-1]["payload"]["error_message"])
        self.assertNotIn("codex:chat", orchestrator._LIVE)

    def test_worker_start_failure_fails_the_turn_and_releases_capacity(self) -> None:
        worker = MagicMock()
        worker.start.side_effect = RuntimeError("thread limit reached")
        with patch.object(orchestrator.threading, "Thread", return_value=worker):
            response = self.send_message("chat", "hi")

        self.assertEqual(response["status"], "accepted")
        failed = thread_events("chat")[-1]
        self.assertEqual(failed["event_type"], "thread.error")
        self.assertIn("thread limit reached", failed["payload"]["error_message"])
        self.assertNotIn("codex:chat", orchestrator._LIVE)

    def test_failed_idle_transition_leaves_run_retryable(self) -> None:
        turn = self.register_live_turn("codex", "chat", FakeServer())
        with patch.object(state, "finish_thread_run", side_effect=RuntimeError("write failed")):
            with self.assertRaises(RuntimeError):
                orchestrator._finish_turn(turn, provider_session_id="session-1")

        self.assertEqual(turn.phase, orchestrator.ExecutionPhase.RUNNING)
        self.assertIsNone(turn.provider_session_id)
        self.assertEqual(state.thread_session_config("chat")["status"], "running")

        orchestrator._finish_turn(turn, provider_session_id="session-1")
        self.assertEqual(turn.phase, orchestrator.ExecutionPhase.FINISHING)
        self.assertEqual(turn.provider_session_id, "session-1")
        self.assertEqual(state.thread_session_config("chat")["status"], "idle")
        self.assertEqual(thread_events("chat"), [])

    # -- stop ------------------------------------------------------------------------

    def test_stop_thread_turn_cancels_and_persists_the_mid_turn_session_id(self) -> None:
        running = threading.Event()
        release = threading.Event()

        def blocking_run_turn(server, input_message, provider_session_id, model, effort, on_message):
            server.on_session_id("codex-mid-turn")
            running.set()
            if not release.wait(timeout=10):
                raise AssertionError("never released")
            if server.closed:  # what a real run_turn does on a dead server
                raise orchestrator.codex_app_server.CodexAppServerError("Codex app-server is not running")
            return "codex-x", "done"

        try:
            with patch.object(orchestrator.codex_app_server, "run_turn", blocking_run_turn):
                self.send_message("chat", "hi")
                self.assertTrue(running.wait(timeout=10))
                self.assertEqual(
                    state.thread_session_config("chat")["provider_session_id"],
                    "codex-mid-turn",
                )

                self.assertEqual(service.stop_thread("chat"), {"status": "accepted"})
                # The turn is terminal but its thread stays fenced until the
                # owning turn thread has persisted the session id and the
                # close completed.
                self.assertIn("codex:chat", orchestrator._LIVE)
                with self.assertRaises(ApiError) as caught:
                    self.send_message("chat", "too soon")
                self.assertEqual(caught.exception.status.value, 409)
                self.assertIn("agent is finishing", caught.exception.message)
                # A second stop finds no stoppable turn.
                self.assertFalse(orchestrator.stop_thread_turn("chat"))
                release.set()
                self.wait_until_idle("chat")
        finally:
            release.set()

        self.assertTrue(FakeServer.instances[0].closed)
        self.assertEqual(
            event_summary(thread_events("chat")),
            [("thread.message", "hi"), ("thread.stopped", None)],
        )
        # The adapter callback persisted the mid-turn session id immediately,
        # before Stop or process teardown.
        self.assertEqual(state.thread_session_config("chat")["provider_session_id"], "codex-mid-turn")
        self.assertFalse(orchestrator.stop_thread_turn("chat"))

    def test_provider_events_after_stop_are_discarded(self) -> None:
        running = threading.Event()
        release = threading.Event()

        def late_output(server, input_message, provider_session_id, model, effort, on_message):
            running.set()
            if not release.wait(timeout=10):
                raise AssertionError("never released")
            on_message("late answer")
            on_message({
                "provider": "codex",
                "activity_id": "late-command",
                "kind": "command",
                "phase": "completed",
                "title": "late command",
            })
            return "codex-late", "done"

        try:
            with patch.object(orchestrator.codex_app_server, "run_turn", late_output):
                self.send_message("chat", "hi")
                self.assertTrue(running.wait(timeout=10))
                self.assertTrue(orchestrator.stop_thread_turn("chat"))
                release.set()
                self.wait_until_idle("chat")
        finally:
            release.set()

        self.assertEqual(
            event_summary(thread_events("chat")),
            [("thread.message", "hi"), ("thread.stopped", None)],
        )

    def test_all_runtimes_persist_stopped_first_turn_session_before_releasing_the_fence(self) -> None:
        # Every adapter exposes the provider id it learned mid-turn. The
        # orchestrator reads that one generic attribute, persists it, and only
        # then releases the same-thread fence.
        for runtime in ("codex", "claude_code", "hermes"):
            with self.subTest(runtime=runtime):
                thread_id = f"{runtime}-chat"
                session_id = f"{runtime}-mid-turn"
                running = threading.Event()
                release = threading.Event()
                provider = MagicMock()

                def blocking_run_turn(server, *_args):
                    server.on_session_id(session_id)
                    running.set()
                    if not release.wait(timeout=10):
                        raise AssertionError("never released")
                    raise RuntimeError("provider process stopped")

                provider.run_turn.side_effect = blocking_run_turn
                with (
                    patch.object(orchestrator, "runtime_network_enabled", return_value=True),
                    patch.object(orchestrator, "runtime_status", return_value="active"),
                    patch.object(orchestrator, "refresh_runtime_status", return_value="active"),
                    patch.object(
                        orchestrator,
                        "_new_agent_server",
                        side_effect=lambda _runtime, thread, on_ready, on_session_id: FakeServer(
                            thread_id=thread,
                            on_ready=on_ready,
                            on_session_id=on_session_id,
                        ),
                    ),
                    patch.object(orchestrator, "_provider_module", return_value=provider),
                ):
                    self.start_turn(thread_id, runtime=runtime)
                    try:
                        self.assertTrue(running.wait(timeout=10))
                        self.assertTrue(orchestrator.stop_thread_turn(thread_id))

                        self.assertIn(f"{runtime}:{thread_id}", orchestrator._LIVE)
                        with self.assertRaises(ApiError) as caught:
                            self.admit(thread_id, runtime=runtime)
                        self.assertEqual(caught.exception.status.value, 409)
                    finally:
                        release.set()
                        self.wait_until_idle(thread_id)

                self.assertEqual(
                    state.thread_session_config(thread_id)["provider_session_id"], session_id
                )
                self.assertEqual(
                    event_summary(thread_events(thread_id))[-1], ("thread.stopped", None)
                )
                self.assertNotIn(f"{runtime}:{thread_id}", orchestrator._LIVE)

    def test_stop_during_server_boot_abandons_the_turn(self) -> None:
        start_entered = threading.Event()
        release_start = threading.Event()
        run_turn_calls: list[str] = []

        class BlockingStartServer(FakeServer):
            def start(self, init_timeout: float = 60.0) -> None:
                start_entered.set()
                if not release_start.wait(timeout=10):
                    raise AssertionError("test never released the slow start")
                super().start(init_timeout)

        def recording_run_turn(server, input_message, provider_session_id, model, effort, on_message):
            run_turn_calls.append(input_message)
            return "codex-x", "done"

        try:
            with (
                patch.object(orchestrator.codex_app_server, "CodexAppServer", BlockingStartServer),
                patch.object(orchestrator.codex_app_server, "run_turn", recording_run_turn),
            ):
                self.send_message("boot", "hi")
                self.assertTrue(start_entered.wait(timeout=10))
                self.assertTrue(orchestrator.stop_thread_turn("boot"))
                release_start.set()
                self.wait_until_idle("boot")
        finally:
            release_start.set()

        # The turn was abandoned: no provider turn ran and the only terminal
        # event is the cancellation.
        self.assertEqual(run_turn_calls, [])
        self.assertEqual(
            event_summary(thread_events("boot")),
            [("thread.message", "hi"), ("thread.stopped", None)],
        )
        self.assertTrue(FakeServer.instances[0].closed)

    def test_stop_with_no_running_turn_is_rejected(self) -> None:
        self.assertFalse(orchestrator.stop_thread_turn("nope"))
        with state.mutation() as cur:
            state.save_thread_session(cur, "codex", "idle", None, state.utc_now(), *DEFAULT_SESSION["codex"])
        with self.assertRaises(ApiError) as caught:
            service.stop_thread("idle")
        self.assertEqual(caught.exception.status.value, 409)
        self.assertEqual(caught.exception.message, "the thread has no running work")

    # -- deactivation / reset --------------------------------------------------------

    def test_deactivate_runtime_fails_live_turns_and_closes_only_that_runtime(self) -> None:
        codex_busy = FakeServer()
        codex_busy.started = 1
        claude_busy = FakeServer()
        claude_busy.started = 1
        codex_turn = self.register_live_turn("codex", "codex-running", codex_busy)
        self.register_live_turn("claude_code", "claude-running", claude_busy)

        orchestrator._stop_runtime_processes("codex", "provider disabled")

        # The turn is failed and its process closed, but only the owning turn
        # thread releases the fence (after persisting any mid-turn session id).
        self.assertTrue(codex_busy.closed)
        self.assertFalse(claude_busy.closed)
        self.assertEqual(
            set(orchestrator._LIVE), {"codex:codex-running", "claude_code:claude-running"}
        )
        failed = thread_events("codex-running")
        self.assertEqual(event_summary(failed), [("thread.error", None)])
        self.assertEqual(failed[0]["payload"]["error_message"], "provider disabled")
        self.assertEqual(thread_events("claude-running"), [])

        # Mimic the stopped turn's owning thread reaching its finally.
        orchestrator._close_turn(codex_turn, codex_busy)
        self.assertEqual(set(orchestrator._LIVE), {"claude_code:claude-running"})

    def test_runtime_status_loss_clears_pin_and_fails_live_turn(self) -> None:
        save_approved_openai_account("acct-old")
        save_proxy_openai_account_id("acct-old")
        codex_busy = FakeServer()
        codex_busy.started = 1
        self.register_live_turn("codex", "codex-running", codex_busy)

        with patch.object(orchestrator.codex_app_server, "account_status", return_value=("awaiting_login", None, None)):
            self.assertEqual(orchestrator.refresh_runtime_status("codex"), "awaiting_login")

        self.assertEqual(read_openai_account().get("account_id"), "acct-old")
        self.assertIsNone(read_proxy_openai_account_id())
        self.assertTrue(codex_busy.closed)
        self.assertIn("codex:codex-running", orchestrator._LIVE)
        failed = thread_events("codex-running")
        self.assertEqual(event_summary(failed), [("thread.error", None)])
        self.assertEqual(
            failed[0]["payload"]["error_message"],
            "Codex runtime became awaiting_login",
        )

        def account_status_without_reseed() -> tuple[str, str | None, None]:
            self.assertIsNone(read_proxy_openai_account_id())
            return "awaiting_login", None, None

        with patch.object(orchestrator.codex_app_server, "account_status", side_effect=account_status_without_reseed):
            self.assertEqual(orchestrator.refresh_runtime_status("codex"), "awaiting_login")

        self.assertIsNone(read_proxy_openai_account_id())

    def test_failed_status_refresh_transaction_keeps_the_cached_verdict(self) -> None:
        orchestrator._set_runtime_status("codex", "active")
        with (
            patch.object(
                orchestrator.codex_app_server,
                "account_status",
                return_value=("awaiting_login", None, None),
            ),
            patch.object(
                orchestrator,
                "_sync_runtime_proxy_pin_in",
                side_effect=RuntimeError("database write failed"),
            ),
        ):
            with self.assertRaises(RuntimeError):
                orchestrator.refresh_runtime_status("codex")

        # In-memory status is published by the transaction's after-commit
        # callback, so it cannot diverge when the durable update rolls back.
        self.assertEqual(orchestrator.runtime_status("codex"), "active")

    def test_reset_deletes_the_linked_account_guard_and_nothing_else(self) -> None:
        seed_oauth_login(
            "codex",
            {
                "status": "awaiting_login",
                "device_code": "X",
                "login_id": "login",
                "login_url": "https://auth.openai.com/device",
                "expires_at": "2099-06-08T00:10:00Z",
            },
        )
        save_approved_openai_account("acct-local")
        save_proxy_openai_account_id("acct-local")

        self.assertIsNone(orchestrator.reset_linked_account("codex"))

        # Guard state is gone and cached status no longer admits new turns.
        self.assertEqual(read_openai_account(), {})
        self.assertIsNone(read_proxy_openai_account_id())
        self.assertIsNone(state.oauth_login("codex"))
        self.assertEqual(orchestrator.runtime_status("codex"), "awaiting_login")
        reset_events = [event for event in read_agent_events() if event["event_type"] == "agent_runtime.linked_account_reset"]
        self.assertEqual([event.get("payload") for event in reset_events], [{"agent_runtime": "codex"}])

    def test_reset_fails_live_runtime_turns(self) -> None:
        server = FakeServer()
        server.started = 1
        turn = self.register_live_turn("codex", "chat", server)
        save_approved_openai_account("acct-local")
        save_proxy_openai_account_id("acct-local")

        self.assertIsNone(orchestrator.reset_linked_account("codex"))
        orchestrator._close_turn(turn, server)

        self.assertTrue(server.closed)
        self.assertNotIn("codex:chat", orchestrator._LIVE)
        events = thread_events("chat")
        self.assertEqual(event_summary(events), [("thread.error", None)])
        self.assertEqual(
            events[0]["payload"]["error_message"],
            "linked provider account was reset by the operator",
        )
        self.assertEqual(read_openai_account(), {})
        self.assertIsNone(read_proxy_openai_account_id())

    def test_reset_continues_when_runtime_close_fails_and_the_fence_holds(self) -> None:
        class FailingCloseServer(FakeServer):
            def close(self) -> None:
                raise PermissionError("cannot signal helper")

        bad_server = FailingCloseServer()
        bad_server.started = 1
        good_server = FakeServer()
        good_server.started = 1
        bad_turn = self.register_live_turn("codex", "chat", bad_server)
        good_turn = self.register_live_turn("codex", "other", good_server)
        save_approved_openai_account("acct-local")
        save_proxy_openai_account_id("acct-local")

        self.assertIsNone(orchestrator.reset_linked_account("codex"))
        # The owning turn thread releases the successfully closed turn. A
        # failed close remains fenced even when its owner attempts the release.
        orchestrator._close_turn(good_turn, good_server)
        orchestrator._close_turn(bad_turn, bad_server)

        self.assertEqual(
            event_summary(thread_events("chat")),
            [("thread.error", None), ("thread.error", None)],
        )
        self.assertEqual(event_summary(thread_events("other")), [("thread.error", None)])
        self.assertTrue(bad_server.interrupted)
        self.assertTrue(good_server.closed)
        # The failed close keeps its entry fenced so no new turn can start on
        # that thread while the old process may still live.
        self.assertEqual(set(orchestrator._LIVE), {"codex:chat"})
        self.assertEqual(
            orchestrator._LIVE["codex:chat"].phase,
            orchestrator.ExecutionPhase.FINISHING,
        )
        with self.assertRaises(ApiError) as caught:
            self.admit("chat")
        self.assertEqual(caught.exception.status.value, 409)
        self.assertEqual(read_openai_account(), {})
        self.assertIsNone(read_proxy_openai_account_id())

    # -- runtime status refresh / anchors / attestation ------------------------------

    def test_active_runtime_refresh_stamps_usage_last_checked_at(self) -> None:
        save_approved_openai_account("acct")
        with (
            patch.object(orchestrator, "utc_now", return_value="2026-06-29T23:10:00Z"),
            patch.object(
                orchestrator.codex_app_server,
                "account_status",
                return_value=(
                    "active",
                    None,
                    {
                        "account_id": "acct",
                        "codex_usage": {
                            "rate_limits": {
                                "primary": {
                                    "used_percent": 8,
                                    "window_duration_mins": 300,
                                    "resets_at": 1782788897,
                                }
                            }
                        },
                    },
                ),
            ),
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("codex"), "active")

        account = read_openai_account()
        self.assertEqual(account["codex_usage"]["last_checked_at"], "2026-06-29T23:10:00Z")

    def test_refresh_without_fresh_usage_clears_stored_usage_snapshot(self) -> None:
        # The setUp default probe returns {} (an unparseable /usage response).
        # Absence stays structural: old percentages must not look current when
        # the provider did not return any usage windows.
        stored_usage = {
            "current_session_used_percent": 14,
            "weekly_used_percent": 31,
            "last_checked_at": "2026-06-29T22:00:00Z",
        }
        save_attested_claude_account("acct", access_token_sha256="f" * 64, claude_usage=dict(stored_usage))
        with patch.object(
            orchestrator.claude_code,
            "account_status",
            return_value=("active", None, {"access_token_sha256": "f" * 64}),
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("claude_code"), "active")

        self.assertNotIn("claude_usage", read_claude_account())

    def test_claude_refresh_rejects_token_attested_to_another_account(self) -> None:
        # Token rotation is allowed; a different *account* is not. The new
        # token's owner comes from the provider's profile endpoint, so forged
        # local metadata cannot help: the attested uuid decides.
        save_attested_claude_account("acct-trusted", access_token_sha256="0" * 64)
        save_proxy_claude_account_id("acct-trusted")

        with (
            patch.object(
                orchestrator.claude_code,
                "account_status",
                return_value=("active", None, {"account_id": "acct-trusted", "access_token_sha256": "1" * 64}),
            ),
            patch.object(
                orchestrator.claude_code,
                "read_attested_identity",
                return_value={"access_token_sha256": "1" * 64, "account_uuid": "acct-attacker"},
            ),
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("claude_code"), "error")

        self.assertEqual(read_claude_account()["account_id"], "acct-trusted")
        self.assertIsNone(read_proxy_claude_account_id())
        record = orchestrator.runtime_status_record("claude_code")
        self.assertIn("account changed", record["error_message"])

    def test_claude_refresh_rejects_attested_anchor_email_collision(self) -> None:
        save_attested_claude_account("acct-trusted", email="op@example.com", access_token_sha256="0" * 64)

        with (
            patch.object(
                orchestrator.claude_code,
                "account_status",
                return_value=(
                    "active",
                    None,
                    {"account_id": "acct-trusted", "email": "op@example.com", "access_token_sha256": "1" * 64},
                ),
            ),
            patch.object(
                orchestrator.claude_code,
                "read_attested_identity",
                return_value={"access_token_sha256": "1" * 64, "account_uuid": "acct-attacker", "email": "op@example.com"},
            ),
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("claude_code"), "error")

        self.assertEqual(read_claude_account()["account_id"], "acct-trusted")
        self.assertIsNone(read_proxy_claude_account_id())
        self.assertIn("account changed", orchestrator.runtime_status_record("claude_code").get("error_message", ""))

    def test_claude_refresh_skips_attestation_for_anchored_token_and_ignores_local_metadata(self) -> None:
        # An unchanged token was attested when it was anchored: no identity
        # attestation call is needed after the live usage probe, and the identity
        # saved comes from the anchor, so forged local metadata never lands.
        save_attested_claude_account("acct-trusted", email="op@example.com", access_token_sha256="f" * 64)

        with (
            patch.object(
                orchestrator.claude_code,
                "account_status",
                return_value=("active", None, {"account_id": "forged-uuid", "access_token_sha256": "f" * 64}),
            ),
            patch.object(
                orchestrator.claude_code,
                "read_attested_identity",
                side_effect=AssertionError("anchored token must not re-attest"),
            ),
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("claude_code"), "active")

        self.assertEqual(read_claude_account()["account_id"], "acct-trusted")
        self.assertEqual(read_claude_account()["email"], "op@example.com")
        self.assertEqual(read_proxy_claude_account_id(), "acct-trusted")

    def test_claude_legacy_anchor_without_login_stays_awaiting(self) -> None:
        # Pre-attestation releases could anchor Claude by local agent-writable
        # metadata such as email. That row is not a trusted anchor, so with no
        # operator login in flight the agent cannot self-promote it: the runtime
        # stays awaiting_login, never attests, and the stale row is left intact
        # until a fresh operator login re-captures it (see the login test below).
        save_claude_account(
            {"account_id": "op@example.com", "email": "op@example.com", "access_token_sha256": "f" * 64}
        )

        with (
            patch.object(
                orchestrator.claude_code,
                "account_status",
                return_value=("active", None, {"account_id": "op@example.com", "access_token_sha256": "f" * 64}),
            ),
            patch.object(
                orchestrator.claude_code,
                "read_attested_identity",
                side_effect=AssertionError("legacy Claude row must not attest without an operator login"),
            ) as attest,
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("claude_code"), "awaiting_login")

        attest.assert_not_called()
        account = read_claude_account()
        self.assertEqual(account["account_id"], "op@example.com")
        self.assertEqual(account["email"], "op@example.com")
        self.assertNotIn("identity_attestation", account)
        self.assertIsNone(read_proxy_claude_account_id())

    def test_claude_legacy_anchor_recaptured_by_operator_login_without_reset(self) -> None:
        # A pre-attestation upgrade row plus a completed operator login re-captures
        # through first-capture attestation, overwriting the legacy identity in
        # place. No separate reset is required (parity with an unapproved OpenAI
        # row, which a plain re-login also re-captures).
        save_claude_account(
            {"account_id": "op@example.com", "email": "stale@example.com", "access_token_sha256": "0" * 64}
        )
        orchestrator._set_runtime_status("claude_code", "awaiting_login")
        seed_oauth_login(
            "claude",
            {
                "status": "completed",
                "login_url": "https://claude.com/cai/oauth/authorize",
                "expires_at": "2099-06-08T00:10:00Z",
                "access_token_sha256": "f" * 64,
            },
        )

        with (
            patch.object(
                orchestrator.claude_code,
                "account_status",
                return_value=("active", None, {"account_id": "forged-uuid", "access_token_sha256": "f" * 64}),
            ),
            patch.object(
                orchestrator.claude_code,
                "read_attested_identity",
                return_value={
                    "access_token_sha256": "f" * 64,
                    "account_uuid": "acct-real",
                    "email": "op@example.com",
                    "organization_uuid": "org-real",
                },
            ),
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("claude_code"), "active")

        account = read_claude_account()
        self.assertEqual(account["account_id"], "acct-real")
        self.assertEqual(account["email"], "op@example.com")
        self.assertEqual(account["identity_attestation"], orchestrator.CLAUDE_IDENTITY_ATTESTATION)
        self.assertEqual(read_proxy_claude_account_id(), "acct-real")
        self.assertIsNone(state.oauth_login("claude"))

    def test_claude_attestation_failure_is_retryable(self) -> None:
        save_attested_claude_account("acct-trusted", access_token_sha256="0" * 64)
        probe = ("active", None, {"account_id": "acct-trusted", "access_token_sha256": "1" * 64})

        with (
            patch.object(orchestrator.claude_code, "account_status", return_value=probe),
            patch.object(
                orchestrator.claude_code,
                "read_attested_identity",
                side_effect=orchestrator.claude_code.ClaudeCodeError("could not reach the Claude profile endpoint"),
            ),
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("claude_code"), "error")
        self.assertIn(
            "could not reach", orchestrator.runtime_status_record("claude_code").get("error_message", "")
        )
        self.assertEqual(read_claude_account()["access_token_sha256"], "0" * 64)
        self.assertIsNone(read_proxy_claude_account_id())

        # A failed attestation is memoized for CLAUDE_LIVE_PROBE_RETRY_SECONDS
        # so the five-second poll does not refetch the profile; simulate the
        # retry window elapsing.
        orchestrator._CLAUDE_ATTESTATION_MEMO = None
        with (
            patch.object(orchestrator.claude_code, "account_status", return_value=probe),
            patch.object(
                orchestrator.claude_code,
                "read_attested_identity",
                return_value={"access_token_sha256": "1" * 64, "account_uuid": "acct-trusted"},
            ),
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("claude_code"), "active")
        self.assertEqual(read_claude_account()["access_token_sha256"], "1" * 64)
        self.assertEqual(read_proxy_claude_account_id(), "acct-trusted")

    def test_claude_first_capture_anchors_attested_identity(self) -> None:
        orchestrator._set_runtime_status("claude_code", "awaiting_login")
        seed_oauth_login(
            "claude",
            {
                "status": "completed",
                "login_url": "https://claude.com/cai/oauth/authorize",
                "expires_at": "2099-06-08T00:10:00Z",
                "access_token_sha256": "f" * 64,
            },
        )

        with (
            patch.object(
                orchestrator.claude_code,
                "account_status",
                return_value=("active", None, {"account_id": "forged-uuid", "access_token_sha256": "f" * 64}),
            ),
            patch.object(
                orchestrator.claude_code,
                "read_attested_identity",
                return_value={
                    "access_token_sha256": "f" * 64,
                    "account_uuid": "acct-real",
                    "email": "op@example.com",
                    "organization_uuid": "org-real",
                },
            ),
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("claude_code"), "active")

        account = read_claude_account()
        self.assertEqual(account["account_id"], "acct-real")
        self.assertEqual(account["email"], "op@example.com")
        self.assertEqual(account["organization_id"], "org-real")
        self.assertEqual(read_proxy_claude_account_id(), "acct-real")
        self.assertIsNone(state.oauth_login("claude"))

    def test_claude_first_capture_backfills_usage_after_identity_publish(self) -> None:
        # Attestation, not the usage probe, validates a first-capture token:
        # its account identity only goes live when the refresh commits. The
        # refresh then reads usage once, so the admin UI shows it immediately instead of
        # after the next five-minute recheck.
        orchestrator._set_runtime_status("claude_code", "awaiting_login")
        seed_oauth_login(
            "claude",
            {
                "status": "completed",
                "login_url": "https://claude.com/cai/oauth/authorize",
                "expires_at": "2099-06-08T00:10:00Z",
                "access_token_sha256": "f" * 64,
            },
        )
        pin_at_probe: list[str] = []

        def probe() -> dict[str, object]:
            pin_at_probe.append(str(read_proxy_claude_account_id() or ""))
            return {"current_session_used_percent": 14, "weekly_used_percent": 31}

        with (
            patch.object(
                orchestrator.claude_code,
                "account_status",
                return_value=("active", None, {"access_token_sha256": "f" * 64}),
            ),
            patch.object(
                orchestrator.claude_code,
                "read_attested_identity",
                return_value={"access_token_sha256": "f" * 64, "account_uuid": "acct-real"},
            ),
            patch.object(orchestrator.claude_code, "read_claude_usage", side_effect=probe),
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("claude_code"), "active")

        self.assertEqual(pin_at_probe, ["acct-real"])  # exactly one probe, after the pin went live
        usage = read_claude_account()["claude_usage"]
        self.assertEqual(usage["current_session_used_percent"], 14)
        self.assertEqual(usage["weekly_used_percent"], 31)
        self.assertIn("last_checked_at", usage)

    def test_claude_rotated_token_replaces_old_usage_after_metadata_publish(self) -> None:
        save_attested_claude_account(
            "acct-real",
            access_token_sha256="a" * 64,
            claude_usage={"current_session_used_percent": 91, "last_checked_at": "old"},
        )
        with (
            patch.object(
                orchestrator.claude_code,
                "account_status",
                return_value=("active", None, {"access_token_sha256": "b" * 64}),
            ),
            patch.object(
                orchestrator.claude_code,
                "read_attested_identity",
                return_value={"access_token_sha256": "b" * 64, "account_uuid": "acct-real"},
            ),
            patch.object(
                orchestrator.claude_code,
                "read_claude_usage",
                return_value={"current_session_used_percent": 12},
            ) as usage_probe,
            patch.object(orchestrator, "utc_now", return_value="2026-07-16T14:00:00Z"),
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("claude_code"), "active")

        usage_probe.assert_called_once_with()
        self.assertEqual(
            read_claude_account()["claude_usage"],
            {
                "current_session_used_percent": 12,
                "last_checked_at": "2026-07-16T14:00:00Z",
            },
        )

    def test_claude_first_capture_requires_completed_token_hash(self) -> None:
        orchestrator._set_runtime_status("claude_code", "awaiting_login")
        seed_oauth_login(
            "claude",
            {
                "status": "completed",
                "login_url": "https://claude.com/cai/oauth/authorize",
                "expires_at": "2099-06-08T00:10:00Z",
            },
        )

        with (
            patch.object(
                orchestrator.claude_code,
                "account_status",
                return_value=("active", None, {"account_id": "acct-attacker", "access_token_sha256": "f" * 64}),
            ),
            patch.object(
                orchestrator.claude_code,
                "read_attested_identity",
                side_effect=AssertionError("unhashed completed Claude OAuth must not attest or anchor"),
            ),
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("claude_code"), "awaiting_login")

        self.assertEqual(orchestrator.runtime_status_record("claude_code"), {"status": "awaiting_login"})
        self.assertEqual(read_claude_account(), {})
        self.assertIsNone(read_proxy_claude_account_id())

    def test_claude_pending_oauth_cannot_attest_or_anchor_first_account(self) -> None:
        orchestrator._set_runtime_status("claude_code", "awaiting_login")
        seed_oauth_login(
            "claude",
            {
                "status": "awaiting_code",
                "login_url": "https://claude.com/cai/oauth/authorize",
                "expires_at": "2099-06-08T00:10:00Z",
            },
        )

        with (
            patch.object(
                orchestrator.claude_code,
                "account_status",
                return_value=("active", None, {"account_id": "acct-attacker", "access_token_sha256": "f" * 64}),
            ),
            patch.object(
                orchestrator.claude_code,
                "read_attested_identity",
                side_effect=AssertionError("pending Claude OAuth must not trigger direct attestation egress"),
            ),
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("claude_code"), "awaiting_login")

        self.assertEqual(orchestrator.runtime_status_record("claude_code"), {"status": "awaiting_login"})
        self.assertEqual(read_claude_account(), {})
        self.assertIsNone(read_proxy_claude_account_id())

    def test_a_turn_that_fails_before_a_session_id_keeps_the_threads_earlier_one(self) -> None:
        # A turn can fail before the CLI ever announces a session (deactivated
        # runtime, boot failure). Persisting that None would erase the thread's
        # history just as surely as dropping a real id does.
        model, effort = DEFAULT_SESSION["codex"]
        with state.mutation() as cur:
            state.save_thread_session(
                cur, "codex", "chat", "codex-earlier", "2026-07-27T00:00:00Z", model, effort
            )

        def boot_failure_run_turn(server, *_args, **_kwargs):
            raise RuntimeError("provider process stopped before announcing a session")

        with patch.object(orchestrator.codex_app_server, "run_turn", boot_failure_run_turn):
            self.send_message("chat", "hello")
            self.wait_until_idle("chat")

        self.assertEqual(thread_events("chat")[-1]["event_type"], "thread.error")
        self.assertEqual(state.thread_session_config("chat")["provider_session_id"], "codex-earlier")

    def test_codex_refresh_probe_runs_without_a_pin_and_publishes_it_at_commit(self) -> None:
        # There is no pre-probe pin seed: the probe itself needs no pin (its
        # guarded usage read is optional and fails soft), and the refresh's
        # commit is the one place the pin is published.
        save_approved_openai_account("acct-local")
        self.assertIsNone(read_proxy_openai_account_id())

        def account_status():
            self.assertIsNone(read_proxy_openai_account_id())
            return "active", None, {"account_id": "acct-local"}

        with patch.object(orchestrator.codex_app_server, "account_status", side_effect=account_status):
            self.assertEqual(orchestrator.refresh_runtime_status("codex"), "active")

        self.assertEqual(read_proxy_openai_account_id(), "acct-local")

    def test_explicit_codex_refresh_forces_provider_probe(self) -> None:
        with patch.object(
            orchestrator.codex_app_server,
            "account_status",
            return_value=("awaiting_login", None, None),
        ) as account_status:
            self.assertEqual(
                orchestrator.refresh_runtime_status("codex", force_provider_probe=True),
                "awaiting_login",
            )

        account_status.assert_called_once_with(force_provider_probe=True)

    def test_codex_legacy_openai_row_is_not_operator_approved(self) -> None:
        save_openai_account({"account_id": "acct-legacy"})

        with patch.object(
            orchestrator.codex_app_server,
            "account_status",
            return_value=("active", None, {"account_id": "acct-legacy"}),
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("codex"), "awaiting_login")

        self.assertEqual(read_openai_account().get("account_id"), "acct-legacy")
        self.assertIsNone(read_proxy_openai_account_id())
        self.assertEqual(orchestrator.runtime_status_record("codex"), {"status": "awaiting_login"})

    def test_codex_initial_oauth_login_can_capture_first_trusted_account(self) -> None:
        orchestrator._set_runtime_status("codex", "awaiting_login")
        seed_oauth_login(
            "codex",
            {
                "status": "awaiting_login",
                "device_code": "X",
                "login_id": "login",
                "login_url": "https://auth.openai.com/device",
                "expires_at": "2099-06-08T00:10:00Z",
            },
        )

        def account_status():
            # First-login capture runs after the status poll (the poller is
            # what reads the completed login off the parked server), so the pin
            # is not seeded yet while the poller reads the account.
            self.assertIsNone(read_proxy_openai_account_id())
            return "active", None, {"account_id": "acct-local"}

        with (
            patch.object(orchestrator.codex_app_server, "read_completed_device_login_account_id", return_value="acct-local"),
            patch.object(orchestrator.codex_app_server, "account_status", side_effect=account_status),
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("codex"), "active")

        self.assertEqual(read_openai_account().get("account_id"), "acct-local")
        self.assertEqual(read_openai_account().get("operator_approval"), orchestrator.OPENAI_OPERATOR_APPROVAL)
        self.assertEqual(read_proxy_openai_account_id(), "acct-local")
        self.assertIsNone(state.oauth_login("codex"))

    def test_codex_active_reauth_closes_the_parked_login_server(self) -> None:
        # A reauth against an already-approved anchor parks a login server that
        # first-login capture skips; the active commit must still close it, or
        # every later status check keeps polling the leftover login process.
        save_approved_openai_account("acct-local")
        orchestrator._set_runtime_status("codex", "awaiting_login")
        seed_oauth_login(
            "codex",
            {
                "status": "awaiting_login",
                "device_code": "X",
                "login_id": "relogin",
                "login_url": "https://auth.openai.com/device",
                "expires_at": "2099-06-08T00:10:00Z",
            },
        )

        class _Parked:
            def __init__(self) -> None:
                self.closed = False

            def close(self) -> None:
                self.closed = True

        parked = _Parked()
        with orchestrator.codex_app_server._login_lock:
            orchestrator.codex_app_server._parked_login = orchestrator.codex_app_server._ParkedLogin(
                server=parked, login_id="relogin"  # type: ignore[arg-type]
            )
        try:
            with patch.object(
                orchestrator.codex_app_server,
                "account_status",
                return_value=("active", None, {"account_id": "acct-local"}),
            ):
                self.assertEqual(orchestrator.refresh_runtime_status("codex"), "active")

            self.assertTrue(parked.closed)
            with orchestrator.codex_app_server._login_lock:
                self.assertIsNone(orchestrator.codex_app_server._parked_login)
            self.assertIsNone(state.oauth_login("codex"))
        finally:
            orchestrator.codex_app_server.close_login_server()

    def test_codex_pending_oauth_without_completed_login_cannot_capture_first_account(self) -> None:
        orchestrator._set_runtime_status("codex", "awaiting_login")
        seed_oauth_login(
            "codex",
            {
                "status": "awaiting_login",
                "device_code": "X",
                "login_id": "login",
                "login_url": "https://auth.openai.com/device",
                "expires_at": "2099-06-08T00:10:00Z",
            },
        )

        with (
            patch.object(orchestrator.codex_app_server, "read_completed_device_login_account_id", return_value=None),
            patch.object(
                orchestrator.codex_app_server,
                "account_status",
                return_value=("active", None, {"account_id": "acct-attacker"}),
            ),
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("codex"), "awaiting_login")

        self.assertEqual(read_openai_account(), {})
        self.assertIsNone(read_proxy_openai_account_id())
        self.assertIsNotNone(state.oauth_login("codex"))

    def test_codex_refresh_rejects_agent_changed_account_id(self) -> None:
        save_approved_openai_account("acct-trusted")
        save_proxy_openai_account_id("acct-trusted")

        with patch.object(
            orchestrator.codex_app_server,
            "account_status",
            return_value=("active", None, {"account_id": "acct-attacker"}),
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("codex"), "error")

        self.assertEqual(read_openai_account().get("account_id"), "acct-trusted")
        self.assertIsNone(read_proxy_openai_account_id())
        record = orchestrator.runtime_status_record("codex")
        self.assertEqual(record["status"], "error")
        self.assertIn("account changed", record["error_message"])

    def test_codex_refresh_without_oauth_cannot_create_first_account_anchor(self) -> None:
        with patch.object(
            orchestrator.codex_app_server,
            "account_status",
            return_value=("active", None, {"account_id": "acct-attacker"}),
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("codex"), "awaiting_login")

        self.assertEqual(read_openai_account(), {})
        self.assertIsNone(read_proxy_openai_account_id())

    def test_expired_codex_oauth_cannot_create_first_account_anchor(self) -> None:
        orchestrator._set_runtime_status("codex", "awaiting_login")
        seed_oauth_login(
            "codex",
            {
                "status": "awaiting_login",
                "device_code": "X",
                "login_id": "login",
                "login_url": "https://auth.openai.com/device",
                "expires_at": "2000-06-08T00:10:00Z",
            },
        )

        with (
            patch.object(
                orchestrator.codex_app_server,
                "read_completed_device_login_account_id",
                side_effect=AssertionError("expired OAuth must not seed provider pin"),
            ),
            patch.object(
                orchestrator.codex_app_server,
                "account_status",
                return_value=("active", None, {"account_id": "acct-attacker"}),
            ),
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("codex"), "awaiting_login")

        self.assertEqual(read_openai_account(), {})
        self.assertIsNone(read_proxy_openai_account_id())
        self.assertIsNone(state.oauth_login("codex"))

    def test_expired_claude_oauth_cannot_create_first_account_anchor(self) -> None:
        orchestrator._set_runtime_status("claude_code", "awaiting_login")
        seed_oauth_login(
            "claude",
            {
                "status": "awaiting_code",
                "login_url": "https://claude.com/cai/oauth/authorize",
                "expires_at": "2000-06-08T00:10:00Z",
            },
        )

        with (
            patch.object(
                orchestrator.claude_code,
                "account_status",
                return_value=("active", None, {"account_id": "acct-attacker", "access_token_sha256": "f" * 64}),
            ),
            patch.object(
                orchestrator.claude_code,
                "read_attested_identity",
                side_effect=AssertionError("expired OAuth must not attest unapproved Claude account"),
            ),
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("claude_code"), "awaiting_login")

        self.assertEqual(read_claude_account(), {})
        self.assertIsNone(read_proxy_claude_account_id())
        self.assertIsNone(state.oauth_login("claude"))

    def test_claude_refresh_without_oauth_cannot_attest_or_anchor_first_account(self) -> None:
        with (
            patch.object(
                orchestrator.claude_code,
                "account_status",
                return_value=("active", None, {"account_id": "acct-attacker", "access_token_sha256": "f" * 64}),
            ),
            patch.object(
                orchestrator.claude_code,
                "read_attested_identity",
                side_effect=AssertionError("unapproved Claude account must not trigger direct attestation egress"),
            ),
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("claude_code"), "awaiting_login")

        self.assertEqual(orchestrator.runtime_status_record("claude_code"), {"status": "awaiting_login"})
        self.assertEqual(read_claude_account(), {})
        self.assertIsNone(read_proxy_claude_account_id())

    def test_claude_refresh_deactivates_when_policy_disables_during_probe(self) -> None:
        # The one in-mutation policy re-check wins over the stale probe: the
        # attestation itself may still have run (a read-only profile fetch),
        # but nothing it produced is committed and the pin is cleared in the
        # same transaction as the deactivation.
        save_attested_claude_account("acct-trusted", access_token_sha256="0" * 64)
        save_proxy_claude_account_id("acct-trusted")

        with (
            patch.object(
                orchestrator.claude_code,
                "account_status",
                return_value=("active", None, {"account_id": "acct-trusted", "access_token_sha256": "1" * 64}),
            ),
            patch.object(orchestrator, "runtime_network_enabled", side_effect=[True, False]),
            patch.object(
                orchestrator.claude_code,
                "read_attested_identity",
                return_value={"access_token_sha256": "1" * 64, "account_uuid": "acct-trusted"},
            ),
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("claude_code"), "deactivated")

        self.assertEqual(read_claude_account()["account_id"], "acct-trusted")
        self.assertIsNone(read_proxy_claude_account_id())

    def test_claude_attestation_disallowed_means_no_helper_egress(self) -> None:
        save_attested_claude_account("acct-trusted", access_token_sha256="0" * 64)

        with (
            patch.object(
                orchestrator.claude_code,
                "account_status",
                return_value=("active", None, {"account_id": "acct-trusted", "access_token_sha256": "1" * 64}),
            ),
            patch.object(orchestrator, "_claude_attestation_allowed", return_value=False) as allowed,
            patch.object(
                orchestrator.claude_code,
                "read_attested_identity",
                side_effect=AssertionError("disallowed Claude attestation must not trigger helper egress"),
            ),
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("claude_code"), "error")

        self.assertEqual(allowed.call_count, 1)
        self.assertEqual(read_claude_account()["account_id"], "acct-trusted")
        self.assertIsNone(read_proxy_claude_account_id())

    def test_codex_refresh_clears_seeded_proxy_pin_when_account_is_not_active(self) -> None:
        save_approved_openai_account("acct-local")
        with patch.object(orchestrator.codex_app_server, "account_status", return_value=("awaiting_login", None, None)):
            self.assertEqual(orchestrator.refresh_runtime_status("codex"), "awaiting_login")

        self.assertEqual(read_openai_account().get("account_id"), "acct-local")
        self.assertIsNone(read_proxy_openai_account_id())

    def test_reset_during_slow_probe_cannot_resurrect_account(self) -> None:
        # The stale probe classified the runtime before the reset; the anchor
        # check inside the commit mutation is what stops it from re-approving
        # the logged-out account.
        save_approved_openai_account("acct-local")

        def account_status():
            self.assertIsNone(orchestrator.reset_linked_account("codex"))
            return "active", None, {"account_id": "acct-local"}

        with patch.object(orchestrator.codex_app_server, "account_status", side_effect=account_status):
            self.assertEqual(orchestrator.refresh_runtime_status("codex"), "awaiting_login")

        self.assertEqual(read_openai_account(), {})
        self.assertIsNone(read_proxy_openai_account_id())

        # Even if local agent credentials survive outside this orchestrator
        # helper, they stay unapproved: the next probe still reports
        # awaiting_login and re-anchors nothing.
        with patch.object(
            orchestrator.codex_app_server,
            "account_status",
            return_value=("active", None, {"account_id": "acct-local"}),
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("codex"), "awaiting_login")
        self.assertEqual(read_openai_account(), {})
        self.assertIsNone(read_proxy_openai_account_id())

    def test_active_refresh_publishes_pin_and_anchor_in_one_commit(self) -> None:
        # The pin is written inside the refresh's commit mutation, so anchor
        # and pin land together; a reset afterwards clears both together.
        save_approved_openai_account("acct-local")

        with patch.object(
            orchestrator.codex_app_server,
            "account_status",
            return_value=("active", None, {"account_id": "acct-local"}),
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("codex"), "active")
        self.assertEqual(read_openai_account().get("account_id"), "acct-local")
        self.assertEqual(read_proxy_openai_account_id(), "acct-local")

        self.assertIsNone(orchestrator.reset_linked_account("codex"))
        self.assertEqual(read_openai_account(), {})
        self.assertIsNone(read_proxy_openai_account_id())

    def test_stale_runtime_refresh_cannot_overwrite_disabled_policy_state(self) -> None:
        save_approved_openai_account("acct-old")
        save_proxy_openai_account_id("acct-old")

        def status_after_policy_flip():
            save_policy(
                {"network_integrations": {}},
                "2026-06-08T00:00:02Z",
            )
            return "awaiting_login", None, None

        with patch.object(orchestrator.codex_app_server, "account_status", side_effect=status_after_policy_flip):
            self.assertEqual(orchestrator.refresh_runtime_status("codex"), "deactivated")

        self.assertEqual(orchestrator.runtime_status("codex"), "deactivated")
        self.assertEqual(read_openai_account().get("account_id"), "acct-old")
        self.assertIsNone(read_proxy_openai_account_id())

    def test_runtime_refresh_rechecks_disabled_policy_inside_final_state_write(self) -> None:
        orchestrator._set_runtime_status("codex", "awaiting_login")

        with (
            patch.object(orchestrator.codex_app_server, "account_status", return_value=("active", None, "acct-new")),
            patch.object(orchestrator, "runtime_network_enabled", side_effect=[True, False]),
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("codex"), "deactivated")

        self.assertEqual(orchestrator.runtime_status("codex"), "deactivated")
        self.assertIsNone(read_openai_account().get("account_id"))
        self.assertIsNone(read_proxy_openai_account_id())

    def test_status_loop_rechecks_each_runtime_on_its_own_cadence(self) -> None:
        class StopLoop(Exception):
            pass

        now = [0.0]
        sleeps = {"count": 0}
        calls: list[str] = []

        def fake_refresh(runtime_type: str) -> str:
            calls.append(runtime_type)
            if runtime_type == "codex" and calls.count("codex") > 1:
                raise AssertionError("active Codex was rechecked at the pending-runtime cadence")
            return "active" if runtime_type == "codex" else "awaiting_login"

        def fake_sleep(seconds: float) -> None:
            sleeps["count"] += 1
            if sleeps["count"] >= 2:
                raise StopLoop
            now[0] += seconds

        with (
            patch.object(orchestrator, "refresh_runtime_status", fake_refresh),
            patch.object(orchestrator.time, "monotonic", lambda: now[0]),
            patch.object(orchestrator.time, "sleep", fake_sleep),
        ):
            with self.assertRaises(StopLoop):
                orchestrator.runtime_status_loop()

        self.assertEqual(calls.count("codex"), 1)
        self.assertEqual(calls.count("claude_code"), 2)

    def test_policy_change_refreshes_reenabled_runtime_without_waiting_for_poll_cadence(self) -> None:
        save_policy(
            {
                "network_integrations": {"openai": {"enabled": True}},
            },
            "2026-06-08T00:00:01Z",
        )
        calls: list[str] = []
        background: list[tuple[str, ...]] = []

        class InlineThread:
            def __init__(self, target, args, daemon):  # type: ignore[no-untyped-def]
                self.target = target
                self.args = args
                self.daemon = daemon

            def start(self) -> None:
                background.append(self.args[0])
                self.target(*self.args)

        def fake_refresh(runtime_type: str) -> str:
            calls.append(runtime_type)
            return "active"

        with (
            patch.object(orchestrator, "refresh_runtime_status", fake_refresh),
            patch.object(orchestrator.threading, "Thread", InlineThread),
        ):
            orchestrator.reconcile_runtime_status_after_policy_change()

        # The disabled runtime deactivates directly (no provider probe, no
        # refresh serialization); only the enabled one is refreshed, in the
        # background.
        self.assertEqual(calls, ["codex"])
        self.assertEqual(background, [("codex",)])
        self.assertEqual(orchestrator.runtime_status("claude_code"), "deactivated")

    # -- Hermes (AWS Bedrock) lifecycle -----------------------------------------------
    # Credential POST validates STS before atomically storing the one shared
    # credential and its display metadata. Steady-state status is local; later
    # provider failures surface on the turn that encounters them, and cost is
    # metered at the proxy rather than polled from a billing API.

    IDENTITY = {
        "access_key_id": "AKIAOPERATORKEY00001",
        "account_id": "123456789012",
        "arn": "arn:aws:iam::123456789012:user/hermes-bedrock",
        "user_id": "AIDAEXAMPLE",
    }

    def enable_bedrock(self) -> None:
        save_policy(
            {
                "network_integrations": {
                    "openai": {"enabled": True},
                    "claude": {"enabled": True},
                    "bedrock": {"enabled": True},
                },
            },
            "2026-06-08T00:00:00Z",
        )

    def connect_bedrock(self, access_key_id: str = "AKIAOPERATORKEY00001") -> None:
        account: dict[str, object] = dict(self.IDENTITY, access_key_id=access_key_id)
        with state.mutation() as cur:
            state.save_bedrock_credential(access_key_id, "S" * 40, "us-east-1", cur)
            state.save_bedrock_account(account, cur)

    def test_bedrock_credential_validation_is_independent_of_enablement(self) -> None:
        def attest(*, credential=None):  # type: ignore[no-untyped-def]
            self.assertEqual(credential, ("AKIAOPERATORKEY00001", "S" * 40))
            self.assertIsNone(state.read_bedrock_access_key_id())
            return dict(self.IDENTITY)

        with patch.object(
            orchestrator.bedrock_credentials, "read_attested_identity", side_effect=attest
        ):
            self.assertEqual(
                orchestrator.replace_and_validate_bedrock_credentials(
                    "AKIAOPERATORKEY00001", "S" * 40, "us-west-2"
                ),
                ("active", None),
            )

        self.assertEqual(state.read_bedrock_account()["account_id"], "123456789012")
        self.assertEqual(state.read_bedrock_region(), "us-west-2")
        self.assertEqual(
            state.read_bedrock_proxy_credential(),
            ("AKIAOPERATORKEY00001", "S" * 40, "us-west-2"),
        )

    def test_rejected_bedrock_replacement_preserves_the_old_row(self) -> None:
        self.connect_bedrock()
        with patch.object(
            orchestrator.bedrock_credentials,
            "read_attested_identity",
            side_effect=orchestrator.bedrock_credentials.BedrockAuthenticationError(
                "invalid candidate"
            ),
        ):
            status, error = orchestrator.replace_and_validate_bedrock_credentials(
                "AKIAREJECTEDKEY00001", "T" * 40, "us-west-2"
            )
        self.assertEqual(status, "error")
        self.assertIn("invalid candidate", error or "")
        self.assertEqual(
            state.read_bedrock_proxy_credential(),
            ("AKIAOPERATORKEY00001", "S" * 40, "us-east-1"),
        )
        self.assertEqual(state.read_bedrock_account()["account_id"], "123456789012")
        self.assertEqual(state.read_bedrock_region(), "us-east-1")

    def test_bedrock_disconnect_waits_for_an_older_connect(self) -> None:
        validation_started = threading.Event()
        release_validation = threading.Event()
        disconnect_done = threading.Event()

        def attest(*, credential=None):  # type: ignore[no-untyped-def]
            validation_started.set()
            self.assertTrue(release_validation.wait(2))
            return dict(self.IDENTITY)

        def disconnect() -> None:
            orchestrator.disconnect_bedrock_connection()
            disconnect_done.set()

        with patch.object(
            orchestrator.bedrock_credentials, "read_attested_identity", side_effect=attest
        ):
            connect_thread = threading.Thread(
                target=orchestrator.replace_and_validate_bedrock_credentials,
                args=("AKIAOPERATORKEY00001", "S" * 40, "us-west-2"),
            )
            connect_thread.start()
            self.assertTrue(validation_started.wait(2))
            disconnect_thread = threading.Thread(target=disconnect)
            disconnect_thread.start()
            self.assertFalse(disconnect_done.wait(0.05))
            release_validation.set()
            connect_thread.join(2)
            disconnect_thread.join(2)

        self.assertFalse(connect_thread.is_alive())
        self.assertFalse(disconnect_thread.is_alive())
        self.assertTrue(disconnect_done.is_set())
        self.assertIsNone(state.read_bedrock_proxy_credential())
        self.assertEqual(state.read_bedrock_account(), {})

    def test_newer_bedrock_connect_replaces_an_older_slow_connect(self) -> None:
        first_started = threading.Event()
        release_first = threading.Event()
        second_done = threading.Event()

        def attest(*, credential=None):  # type: ignore[no-untyped-def]
            if credential and credential[0] == "AKIAOPERATORKEY00001":
                first_started.set()
                self.assertTrue(release_first.wait(2))
                return dict(self.IDENTITY)
            return {
                **self.IDENTITY,
                "access_key_id": "AKIANEWEROPERATOR001",
                "account_id": "999999999999",
            }

        def connect_newer() -> None:
            orchestrator.replace_and_validate_bedrock_credentials(
                "AKIANEWEROPERATOR001",
                "T" * 40,
                "us-east-2",
            )
            second_done.set()

        with patch.object(
            orchestrator.bedrock_credentials, "read_attested_identity", side_effect=attest
        ):
            first_thread = threading.Thread(
                target=orchestrator.replace_and_validate_bedrock_credentials,
                args=("AKIAOPERATORKEY00001", "S" * 40, "us-west-2"),
            )
            first_thread.start()
            self.assertTrue(first_started.wait(2))
            second_thread = threading.Thread(target=connect_newer)
            second_thread.start()
            self.assertFalse(second_done.wait(0.05))
            release_first.set()
            first_thread.join(2)
            second_thread.join(2)

        self.assertFalse(first_thread.is_alive())
        self.assertFalse(second_thread.is_alive())
        self.assertTrue(second_done.is_set())
        self.assertEqual(
            state.read_bedrock_proxy_credential(),
            ("AKIANEWEROPERATOR001", "T" * 40, "us-east-2"),
        )
        self.assertEqual(state.read_bedrock_account()["account_id"], "999999999999")

    def test_bedrock_validation_requires_matching_identity_key(self) -> None:
        identity = dict(self.IDENTITY, access_key_id="AKIADIFFERENTKEY00001")
        with patch.object(
            orchestrator.bedrock_credentials, "read_attested_identity", return_value=identity
        ):
            status, error = orchestrator.replace_and_validate_bedrock_credentials(
                "AKIAOPERATORKEY00001", "S" * 40, "us-east-1"
            )
        self.assertEqual(status, "error")
        self.assertIn("different access key id", error or "")
        self.assertIsNone(state.read_bedrock_access_key_id())

    def test_bedrock_without_a_connected_credential_awaits_connection(self) -> None:
        self.enable_bedrock()
        # No credential stored: account_status awaits operator input and no
        # provider probe runs.
        with patch.object(
            orchestrator.hermes_agent,
            "account_status",
            return_value=("awaiting_login", None, None),
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("hermes"), "awaiting_login")
        self.assertEqual(state.read_bedrock_account(), {})
        self.assertIsNone(state.read_bedrock_proxy_credential())

    def test_bedrock_status_is_local_and_makes_no_aws_call(self) -> None:
        # Steady-state and forced refreshes alike stay local: STS ran once at
        # submission, and cost needs no provider read because the proxy meters
        # it out of each response.
        self.enable_bedrock()
        self.connect_bedrock()
        with patch.object(
            orchestrator.bedrock_credentials,
            "read_attested_identity",
            side_effect=AssertionError("a Bedrock refresh must not call STS"),
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("hermes"), "active")
            self.assertEqual(orchestrator.refresh_runtime_status("hermes", force_provider_probe=True), "active")

    def test_bedrock_disconnect_deletes_the_one_credential(self) -> None:
        self.enable_bedrock()
        self.connect_bedrock()
        with state.mutation() as cur:
            state.save_bedrock_account({"account_id": "123456789012", "access_key_id": "AKIAOPERATORKEY00001"}, cur)
        orchestrator.disconnect_bedrock_connection()
        self.assertEqual(state.read_bedrock_account(), {})
        self.assertIsNone(state.read_bedrock_proxy_credential())
        self.assertIsNone(state.read_bedrock_credential_secret())

    def test_bedrock_disabled_policy_deactivates_hermes(self) -> None:
        # setUp's policy has no Bedrock integration, so Hermes deactivates
        # without any provider probe.
        with patch.object(orchestrator.hermes_agent, "account_status", side_effect=AssertionError("no probe")):
            self.assertEqual(orchestrator.refresh_runtime_status("hermes"), "deactivated")

    def test_hermes_uses_the_bedrock_provider_status(self) -> None:
        save_policy(
            {
                "network_integrations": {
                    "bedrock": {"enabled": True},
                },
            },
            "2026-06-08T00:00:00Z",
        )
        identity = {
            "access_key_id": "AKIAHERMESOPERATOR01",
            "account_id": "999999999999",
            "arn": "arn:aws:iam::999999999999:user/hermes-bedrock",
        }
        with state.mutation() as cur:
            state.save_bedrock_credential("AKIAHERMESOPERATOR01", "S" * 40, "us-east-1", cur)
            state.save_bedrock_account(identity, cur)
        self.assertEqual(orchestrator.refresh_runtime_status("hermes"), "active")
        self.assertEqual(state.read_bedrock_account()["account_id"], "999999999999")
        self.assertEqual(
            state.read_bedrock_proxy_credential(),
            ("AKIAHERMESOPERATOR01", "S" * 40, "us-east-1"),
        )
        self.assertEqual(orchestrator.runtime_status("hermes"), "active")
        self.assertIn("hermes", orchestrator._RUNTIME_STATUSES)
        orchestrator.disconnect_bedrock_connection()
        self.assertEqual(state.read_bedrock_account(), {})
        self.assertIsNone(state.read_bedrock_credential_secret())
        self.assertEqual(orchestrator.runtime_status("hermes"), "awaiting_login")


class StartBackgroundLoopsOrderTests(unittest.TestCase):
    def test_start_background_loops_refreshes_github_credentials_before_loops(self) -> None:
        order: list[str] = []
        with (
            patch(
                "host.runtime.admin_api.orchestrator.github_credential.reconcile",
                side_effect=lambda: order.append("refresh"),
            ),
            patch(
                "host.runtime.admin_api.orchestrator.threading.Thread",
                side_effect=lambda *a, **k: order.append("thread") or _NoopThread(),
            ),
        ):
            orchestrator.start_background_loops()
        # The synchronous refresh must land before any background loop thread
        # is spawned, so a turn cannot be admitted against a stale token.
        self.assertEqual(order[0], "refresh")
        self.assertIn("thread", order)


class ClaudeLiveStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        orchestrator._CLAUDE_LIVE_PROBE = None
        self.addCleanup(setattr, orchestrator, "_CLAUDE_LIVE_PROBE", None)

    def stored_account(self, token_hash: str) -> dict[str, str]:
        return {
            "account_id": "acct",
            "access_token_sha256": token_hash,
            "identity_attestation": orchestrator.CLAUDE_IDENTITY_ATTESTATION,
        }

    def test_invalid_steady_token_requires_login(self) -> None:
        account = {"access_token_sha256": "old"}
        with (
            patch.object(orchestrator, "read_claude_account", return_value=self.stored_account("old")),
            patch.object(
                orchestrator.claude_code,
                "read_claude_usage",
                side_effect=orchestrator.claude_code.ClaudeAuthenticationError("invalid"),
            ),
            patch.object(orchestrator.claude_code, "read_claude_account", return_value=account),
        ):
            self.assertEqual(orchestrator._live_claude_status(account), ("awaiting_login", None, None))

    def test_refresh_rotation_is_attested_instead_of_failed_by_the_old_proxy_pin(self) -> None:
        account = {"access_token_sha256": "old", "plan_type": "max"}
        with (
            patch.object(orchestrator, "read_claude_account", return_value=self.stored_account("old")),
            patch.object(
                orchestrator.claude_code,
                "read_claude_usage",
                side_effect=orchestrator.claude_code.ClaudeCodeError(
                    "Claude bearer token does not match the configured account"
                ),
            ),
            patch.object(
                orchestrator.claude_code,
                "read_claude_account",
                return_value={"access_token_sha256": "new"},
            ),
        ):
            self.assertEqual(
                orchestrator._live_claude_status(account),
                ("active", None, {"access_token_sha256": "new", "plan_type": "max"}),
            )

    def test_first_capture_uses_attestation_without_a_pre_pin_usage_probe(self) -> None:
        account = {"access_token_sha256": "new"}
        with (
            patch.object(orchestrator, "read_claude_account", return_value={}),
            patch.object(orchestrator.claude_code, "read_claude_usage") as usage,
        ):
            self.assertEqual(orchestrator._live_claude_status(account), ("active", None, account))
        usage.assert_not_called()

    def test_failed_authentication_is_not_reprobed_until_the_token_changes(self) -> None:
        account = {"access_token_sha256": "old"}
        with (
            patch.object(orchestrator, "read_claude_account", return_value=self.stored_account("old")),
            patch.object(
                orchestrator.claude_code,
                "read_claude_usage",
                side_effect=orchestrator.claude_code.ClaudeAuthenticationError("invalid"),
            ) as probe,
            patch.object(orchestrator.claude_code, "read_claude_account", return_value=dict(account)),
        ):
            self.assertEqual(orchestrator._live_claude_status(account), ("awaiting_login", None, None))
            # The rejected token stays rejected without further provider
            # traffic; recovery is an operator login, which mints a new token.
            self.assertEqual(orchestrator._live_claude_status(account), ("awaiting_login", None, None))
        self.assertEqual(probe.call_count, 1)

    def test_new_token_bypasses_a_failure_verdict(self) -> None:
        with (
            patch.object(orchestrator, "read_claude_account", return_value=self.stored_account("old")),
            patch.object(
                orchestrator.claude_code,
                "read_claude_usage",
                side_effect=orchestrator.claude_code.ClaudeAuthenticationError("invalid"),
            ) as probe,
            patch.object(orchestrator.claude_code, "read_claude_account", return_value={"access_token_sha256": "old"}),
        ):
            self.assertEqual(
                orchestrator._live_claude_status({"access_token_sha256": "old"}), ("awaiting_login", None, None)
            )
            relogged = {"access_token_sha256": "new"}
            self.assertEqual(orchestrator._live_claude_status(relogged), ("active", None, relogged))
        self.assertEqual(probe.call_count, 1)

    def test_active_probe_verdict_is_reused_within_the_retry_window(self) -> None:
        account = {"access_token_sha256": "old"}
        fetched_usage = {"current_session_used_percent": 14}
        usage = {**fetched_usage, "last_checked_at": "2026-07-16T14:00:00Z"}
        with (
            patch.object(orchestrator, "read_claude_account", return_value=self.stored_account("old")),
            patch.object(orchestrator.claude_code, "read_claude_usage", return_value=dict(fetched_usage)) as probe,
            patch.object(orchestrator.claude_code, "read_claude_account", return_value=dict(account)),
            patch.object(orchestrator, "utc_now", return_value="2026-07-16T14:00:00Z"),
        ):
            expected = ("active", None, {"access_token_sha256": "old", "claude_usage": usage})
            self.assertEqual(orchestrator._live_claude_status(account), expected)
            self.assertEqual(orchestrator._live_claude_status(account), expected)
            self.assertEqual(probe.call_count, 1)
            assert orchestrator._CLAUDE_LIVE_PROBE is not None
            orchestrator._CLAUDE_LIVE_PROBE["at"] -= orchestrator.CLAUDE_LIVE_PROBE_RETRY_SECONDS + 1
            self.assertEqual(orchestrator._live_claude_status(account), expected)
        self.assertEqual(probe.call_count, 2)

    def test_forced_active_probe_bypasses_the_retry_window(self) -> None:
        account = {"access_token_sha256": "old"}
        with (
            patch.object(orchestrator, "read_claude_account", return_value=self.stored_account("old")),
            patch.object(
                orchestrator.claude_code,
                "read_claude_usage",
                side_effect=[
                    {"current_session_used_percent": 14},
                    {"current_session_used_percent": 27},
                ],
            ) as probe,
            patch.object(orchestrator.claude_code, "read_claude_account", return_value=dict(account)),
            patch.object(orchestrator, "utc_now", return_value="2026-07-16T14:00:00Z"),
        ):
            first = orchestrator._live_claude_status(account)
            cached = orchestrator._live_claude_status(account)
            forced = orchestrator._live_claude_status(account, force_probe=True)

        self.assertEqual(first, cached)
        assert first[2] is not None
        assert forced[2] is not None
        self.assertEqual(first[2]["claude_usage"]["current_session_used_percent"], 14)
        self.assertEqual(forced[2]["claude_usage"]["current_session_used_percent"], 27)
        self.assertEqual(probe.call_count, 2)

    def test_error_verdict_is_reused_within_the_retry_window(self) -> None:
        account = {"access_token_sha256": "old"}
        expected = ("error", "could not validate Claude authentication: proxy unreachable", None)
        with (
            patch.object(orchestrator, "read_claude_account", return_value=self.stored_account("old")),
            patch.object(
                orchestrator.claude_code,
                "read_claude_usage",
                side_effect=orchestrator.claude_code.ClaudeCodeError("proxy unreachable"),
            ) as probe,
            patch.object(orchestrator.claude_code, "read_claude_account", return_value=dict(account)),
        ):
            self.assertEqual(orchestrator._live_claude_status(account), expected)
            self.assertEqual(orchestrator._live_claude_status(account), expected)
            self.assertEqual(probe.call_count, 1)
            assert orchestrator._CLAUDE_LIVE_PROBE is not None
            orchestrator._CLAUDE_LIVE_PROBE["at"] -= orchestrator.CLAUDE_LIVE_PROBE_RETRY_SECONDS + 1
            self.assertEqual(orchestrator._live_claude_status(account), expected)
        self.assertEqual(probe.call_count, 2)


class _NoopThread:
    def start(self) -> None:
        return None

if __name__ == "__main__":
    unittest.main()
