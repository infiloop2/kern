"""One-time reminder ownership, durable delivery and archive lifecycle."""
from concurrent.futures import ThreadPoolExecutor
import threading
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
import unittest
from unittest.mock import patch, MagicMock

import pg_harness
from host.runtime.core import db, pgclient
from host.runtime.workspace import reminders, agent_api, agent_messages, schedules, service
from host.runtime.workspace.chat import backend as chat
from host.runtime.workspace.web_apps import backend as apps
from host.runtime.workspace.host_api import WorkspaceError
from host.runtime.admin_api import service as admin_service
from host.agent_messages import INPUT_KINDS

SESSION = {"agent_runtime": "codex", "model": "gpt-6.1-sol", "effort": "high"}


class ReminderValidationTests(unittest.TestCase):
    def test_unicode_limit_and_time_normalization(self):
        future = (datetime.now(timezone.utc) + timedelta(days=1)).replace(microsecond=0)
        message, due = reminders._fields({"message": "😀" * 1000, "at": future.astimezone(timezone(timedelta(hours=2))).isoformat()})
        self.assertEqual(len(message), 1000)
        self.assertEqual(due, future.strftime("%Y-%m-%dT%H:%M:%SZ"))
        now = datetime.now(timezone.utc)
        due = reminders._fields({"message": "Check", "after": "2h"})[1]
        self.assertLess(abs((datetime.fromisoformat(due) - now).total_seconds() - 7200), 2)

    def test_bad_requests_fail_before_database_access(self):
        valid = {"message": "Check", "after": "2h"}
        for body in [None, {}, {**valid, "thread_id": "thread-2"},
                     {**valid, "message": " "}, {**valid, "message": "😀" * 1001},
                     {**valid, "after": "0m"}, {**valid, "after": "-1h"},
                     {**valid, "after": 3}, {**valid, "at": "2099-01-01T00:00:00Z"},
                     {"message": "Check", "at": "2099-01-01"},
                     {"message": "Check", "at": "2000-01-01T00:00:00Z"}]:
            with self.subTest(body=str(body)[:80]), self.assertRaises(WorkspaceError):
                reminders._fields(body)

    def test_seven_day_limit(self):
        reminders._fields({"message": "Check", "after": "7d"})
        for fields in [{"after": "8d"}, {"after": "604801s"},
                       {"at": (datetime.now(timezone.utc) + timedelta(days=8)).isoformat()}]:
            with self.assertRaisesRegex(WorkspaceError, "next 7 days"):
                reminders._fields({"message": "Check", **fields})

    def test_created_reminders_cannot_be_changed_or_cancelled(self):
        body = {"message": "Changed", "after": "2h"}
        for method in ("PUT", "DELETE"):
            with self.subTest(method=method), patch.object(db, "transaction") as txn:
                with self.assertRaises(WorkspaceError) as error:
                    agent_api.dispatch_call(method, "/agent/self/reminders", body if method == "PUT" else None, peer_thread_id="thread-1")
                self.assertEqual(error.exception.status, HTTPStatus.METHOD_NOT_ALLOWED)
                with self.assertRaises(WorkspaceError) as error:
                    agent_api.dispatch_call(method, "/agent/self/reminders/1", body if method == "PUT" else None, peer_thread_id="thread-1")
                self.assertEqual(error.exception.status, HTTPStatus.NOT_FOUND)
                for path, status in [("/reminders/thread-1", HTTPStatus.METHOD_NOT_ALLOWED),
                                     ("/reminders/thread-1/1", HTTPStatus.NOT_FOUND)]:
                    handler = object.__new__(service.Handler)
                    handler.path = path
                    with patch.object(handler, "_read_body", return_value=body), patch.object(handler, "_send_json") as send:
                        handler._handle(method)
                    self.assertEqual(send.call_args.args[0], status)
                txn.assert_not_called()

    def test_agent_route_binds_only_authenticated_self(self):
        with patch.object(agent_api.agent_notices, "record"), patch.object(reminders, "route", return_value={}) as route:
            agent_api.dispatch_call("POST", "/agent/self/reminders", {"message": "Check", "after": "2h"}, peer_thread_id="thread-7")
            self.assertEqual(route.call_args.args[1], "thread-7")
            with self.assertRaises(WorkspaceError):
                agent_api.dispatch_call("GET", "/agent/self/reminders", None)

    def test_reminder_uses_the_shared_admin_notice_contract(self):
        from types import SimpleNamespace
        for kind in INPUT_KINDS:
            with self.subTest(kind=kind), patch.object(admin_service, "thread_route", return_value={"status": "accepted"}) as route:
                request = SimpleNamespace(path="/v1/threads/thread-1/messages", method="POST", query={},
                                          principal=admin_service.WorkspacePrincipal(),
                                          body={"message": "Check", "kern_notice": {"kind": kind, "summary": "Check"}})
                self.assertEqual(admin_service._thread_route_request(request), {"status": "accepted"})
                route.assert_called_once()
                request.body["kern_notice"]["reminder_id"] = "9"
                with self.assertRaisesRegex(admin_service.ApiError, "invalid Kern message notice"):
                    admin_service._thread_route_request(request)

    def test_all_admission_errors_finish_as_failed(self):
        for status, error in [
            (HTTPStatus.CONFLICT, "the agent is starting; retry shortly"),
            (HTTPStatus.CONFLICT, "the agent is finishing; retry shortly"),
            (HTTPStatus.CONFLICT, "Claude Code cannot accept another message while running; wait for it to finish"),
            (HTTPStatus.TOO_MANY_REQUESTS, "runtime capacity reached"),
            (HTTPStatus.BAD_GATEWAY, "host admin request failed"),
            (HTTPStatus.LOCKED, "app is locked"),
            (HTTPStatus.CONFLICT, "runtime is disabled"), (HTTPStatus.CONFLICT, "model is no longer offered"),
        ]:
            with self.subTest(error=error), patch.object(db, "transaction") as txn, patch.object(reminders, "_owner"), patch.object(agent_messages, "_deliver_message", side_effect=WorkspaceError(status, error)):
                cur = txn.return_value.__enter__.return_value
                cur.fetchone.return_value = (1, "thread-1", "Check", "2099-01-01T00:00:00Z", "pending", None, "2098-01-01T00:00:00Z", None)
                reminders._deliver(1, "thread-1", "2099-01-02T00:00:00Z")
                updates = [call for call in cur.execute.call_args_list if call.args[0].startswith("UPDATE")]
                self.assertEqual(len(updates), 1)
                self.assertEqual(updates[0].args[1][:2], ("failed", error))

    def test_completed_timestamp_is_captured_after_admission(self):
        completed = datetime(2099, 1, 2, 0, 10, tzinfo=timezone.utc)
        with patch.object(db, "transaction") as txn, patch.object(reminders, "_owner"), patch.object(reminders, "datetime") as clock, patch.object(agent_messages, "_deliver_message") as send:
            cur = txn.return_value.__enter__.return_value
            cur.fetchone.return_value = (1, "thread-1", "Check", "2099-01-01T00:00:00Z", "pending", None, "2098-01-01T00:00:00Z", None)
            send.side_effect = lambda *args: setattr(clock.now, "return_value", completed)
            reminders._deliver(1, "thread-1", "2099-01-02T00:00:00Z")
            updates = [call for call in cur.execute.call_args_list if call.args[0].startswith("UPDATE")]
            self.assertEqual(updates[0].args[1][2], "2099-01-02T00:10:00Z")

    def test_slow_delivery_does_not_block_polling_or_other_targets(self):
        release = threading.Event()
        started = threading.Event()
        def deliver(reminder_id, thread_id, due):
            if thread_id == "thread-1":
                started.set()
                if not release.wait(5):
                    raise TimeoutError("test delivery was not released")
        with patch.object(db, "transaction") as txn, patch.object(reminders, "_deliver", side_effect=deliver) as send:
            cur = txn.return_value.__enter__.return_value
            cur.fetchall.side_effect = [[(1, "thread-1"), (2, "thread-1"), (3, "thread-2")], [(4, "thread-3")]]
            cur.execute.side_effect = lambda query, params: [pgclient._encode_parameter(value) for value in params]
            with ThreadPoolExecutor(max_workers=reminders.MAX_DELIVERIES) as executor:
                active = {}
                try:
                    reminders.run_due(executor, active)
                    self.assertTrue(started.wait(1))
                    active["thread-2"].result(timeout=1)
                    reminders.run_due(executor, active)
                    active["thread-3"].result(timeout=1)
                    self.assertFalse(active["thread-1"].done())
                    self.assertIn("thread-1", cur.execute.call_args.args[1][1:])
                    self.assertEqual({call.args[0] for call in send.call_args_list}, {1, 3, 4})
                finally:
                    release.set()

    def test_delivery_concurrency_has_no_unbounded_queue(self):
        from concurrent.futures import Future
        with patch.object(db, "transaction") as txn:
            cur = txn.return_value.__enter__.return_value
            cur.fetchall.return_value = [(i, f"thread-{i}") for i in range(1, 101)]
            executor = MagicMock()
            executor.submit.side_effect = lambda *args: Future()
            active = {}
            reminders.run_due(executor, active)
            reminders.run_due(executor, active)
            self.assertEqual(executor.submit.call_count, reminders.MAX_DELIVERIES)
            self.assertEqual(len(active), reminders.MAX_DELIVERIES)
            self.assertEqual(cur.execute.call_count, 1)


class ReminderDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pg_harness.ensure_database()

    def setUp(self):
        pg_harness.reset_database()
        self.addCleanup(db.close_pool)
        with db.transaction() as cur:
            cur.execute("INSERT INTO chat_threads (thread_id, spawned_by_thread_id) VALUES ('thread-1', NULL), ('thread-2', 'thread-1')")
        with patch.object(apps, "active_agent_runtimes", return_value=["codex"]):
            self.app = apps.create_web_app()["app_id"]
        self.schedule = schedules.create_schedule({"name": "Reminders", "triggers": [], **SESSION}, actor="agent")

    def run_due(self, now):
        with ThreadPoolExecutor(max_workers=reminders.MAX_DELIVERIES) as executor:
            active = {}
            reminders.run_due(executor, active, now)
            for future in active.values():
                future.result(timeout=5)

    def create(self, thread="thread-1", **changes):
        return reminders.route("POST", thread, {"message": "Check deployment", "after": "1h", **changes}, {})["reminder"]

    def pending(self, thread):
        return reminders.route("GET", thread, None, {})

    def test_all_agent_types_delivery_uses_current_settings_and_notice(self):
        for target in ["thread-1", "thread-2", self.app, self.schedule["thread_id"]]:
            self.create(target)
        with patch.object(agent_messages, "call_admin_api", return_value={"status": "accepted"}) as send:
            self.run_due(datetime.now(timezone.utc) + timedelta(hours=2))
        self.assertEqual(send.call_count, 4)
        for call in send.call_args_list:
            self.assertEqual(call.args[2]["kern_notice"]["kind"], "reminder")
            self.assertIn("Check deployment", call.args[2]["message"])
            self.assertIn("Reminder you scheduled for", call.args[2]["message"])
        with patch.object(agent_messages, "call_admin_api") as send:
            self.run_due(datetime.now(timezone.utc) + timedelta(hours=3))
            send.assert_not_called()
        self.assertEqual(self.pending("thread-1")["reminders"][0]["status"], "delivered")

    def test_rejected_changes_preserve_original_delivery(self):
        row = self.create()
        for thread in ("thread-1", "thread-2"):
            for method in ("PUT", "DELETE"):
                with self.assertRaises(WorkspaceError):
                    agent_api.dispatch_call(method, f"/agent/self/reminders/{row['id']}",
                                            {"message": "Changed", "after": "2h"} if method == "PUT" else None, peer_thread_id=thread)
        self.assertEqual(self.pending("thread-1")["reminders"], [row])
        with patch.object(agent_messages, "call_admin_api", return_value={"status": "accepted"}) as send:
            self.run_due(datetime.now(timezone.utc) + timedelta(hours=1, minutes=1))
        send.assert_called_once()
        self.assertIn(row["message"], send.call_args.args[2]["message"])
        self.assertEqual(self.pending("thread-1")["reminders"][0]["status"], "delivered")

    def test_delivery_errors_are_final_and_never_retried(self):
        errors = [(HTTPStatus.CONFLICT, "the agent is finishing; retry shortly"),
                  (HTTPStatus.TOO_MANY_REQUESTS, "runtime capacity reached"),
                  (HTTPStatus.BAD_GATEWAY, "host admin request failed")]
        for status, message in errors:
            with self.subTest(status=status):
                row = self.create()
                due = datetime.now(timezone.utc) + timedelta(hours=2)
                with patch.object(agent_messages, "call_admin_api", side_effect=WorkspaceError(status, message)) as send:
                    self.run_due(due)
                    self.run_due(due + timedelta(minutes=1))
                send.assert_called_once()
                finished = next(r for r in self.pending("thread-1")["reminders"] if r["id"] == row["id"])
                self.assertEqual((finished["status"], finished["error"]), ("failed", message))
                self.assertEqual(self.pending("thread-1")["pending_count"], 0)

    def test_archived_agents_fail_when_due_and_restore_does_not_revive_failures(self):
        targets = ["thread-1", "thread-2", self.app, self.schedule["thread_id"]]
        for target in targets:
            self.create(target)
        with patch.object(chat, "call_admin_api", return_value={"thread": {"status": "idle"}}), patch.object(apps, "call_admin_api", return_value={"thread": {"status": "idle"}}), patch.object(schedules, "call_admin_api", return_value={"thread": {"status": "idle"}}):
            chat.archive_chat_thread("thread-1")
            chat.archive_chat_thread("thread-2")
            apps.set_web_app_archived(self.app, True)
            schedules.delete_schedule(self.schedule["id"], {"expected_revision": ["1"]}, actor="user")
            for target in targets:
                self.assertEqual(self.pending(target)["pending_count"], 1)
                with self.assertRaises(WorkspaceError):
                    self.create(target)
            with patch.object(agent_messages, "_deliver_message") as send:
                self.run_due(datetime.now(timezone.utc) + timedelta(hours=2))
                send.assert_not_called()
            for target in targets:
                self.assertEqual(self.pending(target)["reminders"][0]["status"], "failed")
            chat.unarchive_chat_thread("thread-1")
            apps.set_web_app_archived(self.app, False)
        self.assertEqual(self.pending("thread-1")["pending_count"], 0)
        self.assertEqual(self.pending(self.app)["pending_count"], 0)

    def test_spawned_auto_archive_waits_only_while_reminder_is_pending(self):
        self.create("thread-2")
        summary = {"thread_id": "thread-2", "status": "idle", "last_used_at": "2000-01-01T00:00:00Z"}
        with patch.object(chat, "_host_thread_summaries", return_value=[summary]), patch.object(chat, "call_admin_api", return_value={"thread": summary}):
            self.assertEqual(chat.archive_idle_spawned_agents(), 0)
            with patch.object(agent_messages, "_deliver_message"):
                self.run_due(datetime.now(timezone.utc) + timedelta(hours=2))
            self.assertEqual(chat.archive_idle_spawned_agents(), 1)

    def test_pending_limit_is_durable_and_delivery_frees_capacity(self):
        for _ in range(5):
            self.create()
        with self.assertRaisesRegex(WorkspaceError, "at most 5"):
            self.create()
        with patch.object(agent_messages, "_deliver_message"):
            self.run_due(datetime.now(timezone.utc) + timedelta(hours=2))
        self.create()
        self.assertEqual(self.pending("thread-1")["pending_count"], 5)

    def update_schedule(self, current, **changes):
        body = {key: current[key] for key in ("name", "triggers", "agent_runtime", "model", "effort")}
        return schedules.update_schedule(current["id"], {**body, **changes, "expected_revision": current["revision"]}, actor="user")

    def test_bash_conversion_and_revision_restore_fail_at_delivery(self):
        thread = self.schedule["thread_id"]
        self.create(thread)
        script = self.update_schedule(self.schedule, agent_runtime="script", model="bash", effort="fixed")
        with patch.object(agent_messages, "_deliver_message") as send:
            self.run_due(datetime.now(timezone.utc) + timedelta(hours=2))
            send.assert_not_called()
        model = self.update_schedule(script, **SESSION)
        self.assertEqual(self.pending(thread)["reminders"][0]["status"], "failed")
        self.create(thread)
        restored = schedules.restore_revision(model["id"], script["revision"], {"expected_revision": model["revision"]})
        with patch.object(agent_messages, "_deliver_message") as send:
            self.run_due(datetime.now(timezone.utc) + timedelta(hours=2))
            send.assert_not_called()
        self.update_schedule(restored, **SESSION)
        self.assertEqual(self.pending(thread)["pending_count"], 0)

    def test_pruning_schedule_removes_only_its_reminder_history(self):
        self.create(self.schedule["thread_id"])
        self.create("thread-1")
        with patch.object(schedules, "call_admin_api", return_value={"thread": {"status": "idle"}}):
            schedules.delete_schedule(self.schedule["id"], {"expected_revision": ["1"]}, actor="user")
        self.assertEqual(schedules.prune_deleted(datetime.now(timezone.utc) + timedelta(days=91)), 1)
        with db.transaction() as cur:
            cur.execute("SELECT thread_id FROM agent_reminders")
            self.assertEqual(cur.fetchall(), [("thread-1",)])

    def test_pagination_is_stable_when_pending_entries_finish(self):
        # Initial pending entries stay visible even beneath 100 newer history rows.
        old = self.create()
        with db.transaction() as cur:
            cur.execute("INSERT INTO agent_reminders (thread_id, message, due_at, created_at, status)"
                        " SELECT 'thread-1', 'history', '2000-01-01T00:00:00Z', '2000-01-01T00:00:00Z', 'delivered' FROM generate_series(1, 101)")
        latest = self.create()
        first = self.pending("thread-1")
        self.assertEqual({r["id"] for r in first["pending"]}, {old["id"], latest["id"]})
        with patch.object(agent_messages, "_deliver_message"):
            reminders._deliver(latest["id"], "thread-1", reminders._timestamp(datetime.now(timezone.utc) + timedelta(hours=2)))
        second = reminders.route("GET", "thread-1", None, {"before": [str(first["next_before"])]})
        ids = [r["id"] for page in (first, second) for r in page["reminders"]]
        self.assertEqual(len(ids), 103)
        self.assertEqual(len(set(ids)), 103)
        self.assertEqual(second["next_before"], None)
        self.assertEqual([r["id"] for r in second["pending"]], [old["id"]])

    def test_failed_batch_leaves_later_reminders_for_next_poll(self):
        with db.transaction() as cur:
            cur.execute("INSERT INTO chat_threads (thread_id) SELECT 'thread-' || i FROM generate_series(3, 6) AS i")
        for i in range(3, 7):
            self.create(f"thread-{i}")
        self.create("thread-2")
        due = datetime.now(timezone.utc) + timedelta(hours=2)
        def send(cur, thread, body):
            if thread != "thread-2":
                raise WorkspaceError(HTTPStatus.TOO_MANY_REQUESTS, "runtime capacity reached")
        with patch.object(agent_messages, "_deliver_message", side_effect=send):
            self.run_due(due)
            self.assertEqual(self.pending("thread-2")["pending_count"], 1)
            self.run_due(due)
        self.assertEqual(self.pending("thread-3")["pending_count"], 0)
        self.assertEqual(self.pending("thread-2")["reminders"][0]["status"], "delivered")

    def test_daily_history_cleanup_is_global_and_preserves_pending(self):
        pending = self.create()
        with db.transaction() as cur:
            cur.execute("INSERT INTO agent_reminders (thread_id, message, due_at, created_at, finished_at, status)"
                        " VALUES ('thread-1', 'old', '2000-01-01T00:00:00Z', '2000-01-01T00:00:00Z', '2000-01-01T00:00:00Z', 'delivered'),"
                        " ('thread-2', 'new', '2001-01-01T00:00:00Z', '2001-01-01T00:00:00Z', '2001-01-01T00:00:00Z', 'failed')")
        with patch.object(reminders, "MAX_HISTORY", 1):
            reminders.prune_history()
        self.assertEqual([r["id"] for r in self.pending("thread-1")["reminders"]], [pending["id"]])
        self.assertEqual(self.pending("thread-2")["reminders"][0]["message"], "new")
