"""Real PostgreSQL coverage of boot recovery, claims and persistent settings."""
import unittest
from unittest.mock import patch

import pg_harness
from host.runtime.admin_api import service, threads
from host.runtime.agent_runtime import orchestrator, turn_retries as retries
from host.runtime.core import db, pgclient, state
from host.runtime.workspace import schedules
from host.runtime.workspace.web_apps import backend as apps


class RecoveryStateTests(unittest.TestCase):
    def setUp(self):
        pg_harness.reset_database()
        self.enterContext(patch.object(retries.time, "time", return_value=1000))
        self.enterContext(patch.object(orchestrator, "runtime_status", return_value="active"))
        self.enterContext(patch.object(orchestrator, "runtime_network_enabled", return_value=True))
        self.enterContext(patch.object(threads, "_recalled_memory_pages", return_value=([], "")))
        self.enterContext(patch.object(threads, "_memory_task_query", return_value=""))
        self.enterContext(patch.object(orchestrator, "launch_turn"))
        orchestrator._LIVE.clear()
        self.addCleanup(orchestrator._LIVE.clear)

    def seed(self, thread="thread-1", runtime="codex", attempts=0):
        with state.mutation() as cur:
            if thread.startswith("thread-"):
                cur.execute("INSERT INTO chat_threads (thread_id) VALUES (%s)", (thread,))
            state.save_thread_session(cur, runtime, thread, "old-provider-session", state.utc_now(), "gpt-6.2-astra", "high")
            state.save_thread_recovery(cur, thread, attempts, None)

    def plan(self, thread="thread-1", error="capacity"):
        with state.mutation() as cur:
            check = retries.record_outcome(cur, thread, "codex", "gpt-6.2-astra", "high", 0, error)
        return check or retries.current(thread)

    def test_future_deadline_and_counter_survive_boot_without_duplicate(self):
        self.seed(attempts=2)
        before = self.plan()
        self.assertEqual(service.initialize_state(), [])
        self.assertEqual(retries.current("thread-1"), before)
        self.assertEqual(state.thread_recovery("thread-1")["attempts"], 2)
        self.assertEqual(before.due_at, 4600)
        with patch.object(retries.time, "time", return_value=4600):
            retries.deliver_due(threads.retry_failed_turn)
            retries.deliver_due(threads.retry_failed_turn)
        self.assertEqual(state.thread_session_config("thread-1")["run_number"], 1)
        self.assertEqual(state.thread_recovery("thread-1"), {"attempts": 3, "pending": None})

    def test_restart_uses_shared_counter_and_stops_at_cap(self):
        for attempts in (3, 5):
            thread = f"thread-{attempts}"
            self.seed(thread, attempts=attempts)
            with state.mutation() as cur:
                state.start_thread_run(cur, thread)
            self.assertEqual(service.initialize_state(), [thread])
            retry = retries.current(thread)
            if attempts == 5:
                self.assertIsNone(retry)
            else:
                self.assertEqual((retry.kind, retry.attempt), ("restart", 4))
                self.assertEqual(service.initialize_state(), [])
                self.assertEqual(retries.current(thread), retry)

    def test_claim_and_product_switch_roll_back_if_admission_fails(self):
        with patch.object(apps, "active_agent_runtimes", return_value=["codex"]):
            app = apps.create_web_app()["app_id"]
        apps.set_app_agent_settings(app, {"agent_runtime": "codex", "model": "gpt-6.2-astra", "effort": "high"})
        self.seed(app)
        retry = self.plan(app, "limit")
        with patch.object(retries.recovery_policy, "limit_recovery", return_value=("codex-2", 1000)):
            retry = retries.resolve(retry)
        with patch.object(orchestrator, "admit_turn", side_effect=RuntimeError("admission failed")):
            with self.assertRaisesRegex(RuntimeError, "admission failed"):
                threads.retry_failed_turn(retry)
        self.assertEqual(retries.current(app), retry)
        self.assertEqual(state.thread_session_config(app)["agent_runtime"], "codex")
        with db.transaction() as cur:
            cur.execute("SELECT agent_runtime FROM web_apps WHERE app_id = %s", (app,))
            self.assertEqual(cur.fetchone()[0], "codex")

    def test_standing_switch_preserves_triggers_and_records_revision(self):
        definition = {"name": "Research", "triggers": [], "purpose": "Keep paused",
                      "agent_runtime": "codex", "model": "gpt-6.2-astra", "effort": "high"}
        schedule = schedules.create_schedule(definition, actor="user")
        thread = schedule["thread_id"]
        self.seed(thread, attempts=1)
        retry = self.plan(thread, "limit")
        with patch.object(retries.recovery_policy, "limit_recovery", return_value=("codex-2", 1000)):
            retry = retries.resolve(retry)
        threads.retry_failed_turn(retry)
        self.assertEqual(state.thread_session_config(thread)["agent_runtime"], "codex-2")
        self.assertEqual(state.thread_recovery(thread), {"attempts": 2, "pending": None})
        with db.transaction() as cur:
            cur.execute("SELECT agent_runtime, name, triggers, purpose, revision FROM schedules WHERE thread_id = %s", (thread,))
            row = cur.fetchone()
            self.assertEqual(tuple(row), ("codex-2", "Research", [], "Keep paused", 2))
            cur.execute("SELECT agent_runtime FROM schedule_revisions WHERE schedule_id = %s ORDER BY revision DESC LIMIT 1", (schedule["id"],))
            self.assertEqual(cur.fetchone()[0], "codex-2")
        # A previously claimed trigger may still carry the old settings. It
        # must fail instead of rotating the completed recovery back to Codex.
        turn = next(turn for turn in orchestrator._LIVE.values() if turn.thread_id == thread)
        orchestrator._finish_turn(turn)
        orchestrator._close_turn(turn, None)
        with self.assertRaisesRegex(Exception, "agent settings changed"):
            threads.send_thread_message(thread, {
                "message": "stale trigger", "agent_runtime": "codex", "model": "gpt-6.2-astra", "effort": "high",
            }, None, operator_sent_message=True, retry_attempt=0)
        self.assertEqual(state.thread_session_config(thread)["agent_runtime"], "codex-2")

    def test_changed_or_archived_destination_stops_recovery(self):
        self.seed()
        retry = self.plan()
        with db.transaction() as cur:
            cur.execute("UPDATE chat_threads SET archived = TRUE WHERE thread_id = 'thread-1'")
        with patch.object(retries.time, "time", return_value=1300), \
                patch.object(retries.host_errors, "report_warning"):
            retries.deliver_due(threads.retry_failed_turn)
        self.assertIsNone(retries.pending("thread-1"))
        self.assertEqual(state.thread_session_config("thread-1")["run_number"], 0)

    def test_completion_and_pending_write_are_atomic(self):
        self.seed()
        with self.assertRaises(RuntimeError), state.mutation() as cur:
            retries.record_outcome(cur, "thread-1", "codex", "gpt-6.2-astra", "high", 0, "capacity")
            state.append_agent_event(cur, "thread.error", "thread-1", {"error_message": "capacity"})
            raise RuntimeError("rollback terminal outcome")
        self.assertIsNone(retries.pending("thread-1"))
        self.assertEqual(state.page_thread_events("thread-1", None, 20, None, None), [])

    def test_limit_check_is_not_persisted_or_restored_at_boot(self):
        self.seed()
        check = self.plan(error="limit")
        self.assertEqual(check.kind, "limit")
        self.assertIsNone(retries.pending("thread-1"))
        self.assertEqual(service.initialize_state(), [])
        self.assertEqual(state.pending_thread_recoveries(), [])

    def test_typed_storage_rejects_partial_pending_rows_and_invalid_counters(self):
        self.seed()
        for assignment in ("retry_kind = 'transient'", "attempts = 6", "attempts = -1"):
            with self.subTest(assignment=assignment), self.assertRaises(pgclient.Error), state.mutation() as cur:
                cur.execute(f"UPDATE thread_recovery SET {assignment} WHERE thread_id = 'thread-1'")

    def test_stop_event_failure_rolls_back_pending_cancellation(self):
        self.seed()
        retry = self.plan()
        with patch.object(orchestrator, "stop_thread_turn", return_value=False), \
                patch.object(state, "append_agent_event", side_effect=RuntimeError("event write failed")):
            with self.assertRaisesRegex(RuntimeError, "event write failed"):
                threads.stop_thread("thread-1")
        self.assertEqual(retries.current("thread-1"), retry)
