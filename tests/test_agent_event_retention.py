"""Large details expire independently of the longer-lived conversation rows."""

from contextlib import contextmanager
import unittest
from unittest.mock import MagicMock, patch

import pg_harness
from host.runtime.admin_api import service
from host.runtime.core import db
from host.runtime.core.state import events


class DetailExpiryMaintenanceTests(unittest.TestCase):
    def test_batches_commit_separately_and_reuse_cutoffs(self):
        read = MagicMock()
        read.fetchone.return_value = (150, "2026-05-31T12:00:00Z")
        writes = []
        committed = []

        @contextmanager
        def mutation():
            self.assertEqual(len(writes), len(committed))
            cur = MagicMock()
            cur.fetchall.return_value = [(1,), (2,)] if not writes else [(3,)]
            writes.append(cur)
            yield cur
            committed.append(cur)

        with (
            patch.object(events.db, "transaction") as transaction,
            patch.object(events, "mutation", mutation),
            patch.object(events, "AGENT_EVENT_DETAIL_BATCH_SIZE", 2),
        ):
            transaction.return_value.__enter__.return_value = read
            self.assertEqual(events.expire_agent_event_details(), 3)
        self.assertEqual(len(committed), 2)
        self.assertEqual([cur.execute.call_args.args[1] for cur in writes], [
            (150, "2026-05-31T12:00:00Z", 2),
            (150, "2026-05-31T12:00:00Z", 2),
        ])

    def test_history_inside_sequence_window_needs_no_write(self):
        with patch.object(events.db, "transaction") as transaction, patch.object(events, "mutation") as mutation:
            transaction.return_value.__enter__.return_value.fetchone.return_value = (0, "2026-05-31T12:00:00Z")
            self.assertEqual(events.expire_agent_event_details(), 0)
        mutation.assert_not_called()

    def test_pass_stops_at_batch_limit_even_with_a_backlog(self):
        with (
            patch.object(events.db, "transaction") as transaction,
            patch.object(events, "mutation") as mutation,
            patch.object(events, "AGENT_EVENT_DETAIL_BATCH_SIZE", 2),
            patch.object(events, "AGENT_EVENT_DETAIL_BATCHES_PER_PASS", 3),
        ):
            transaction.return_value.__enter__.return_value.fetchone.return_value = (150, "2026-05-31T12:00:00Z")
            mutation.return_value.__enter__.return_value.fetchall.return_value = [(1,), (2,)]
            self.assertEqual(events.expire_agent_event_details(), 6)
        self.assertEqual(mutation.call_count, 3)

    def test_hourly_maintenance_runs_expiry_after_ordinary_transaction(self):
        order = []

        @contextmanager
        def mutation():
            yield MagicMock()
            order.append("commit")

        with (
            patch.object(service.state, "mutation", mutation),
            patch.object(service.state, "prune_event_logs"),
            patch.object(service.state, "prune_host_diagnostics"),
            patch.object(service.state, "prune_pending_pushes"),
            patch.object(service.state, "prune_turn_usage"),
            patch.object(service.state, "prune_bedrock_usage"),
            patch.object(service.state, "prune_thread_sessions"),
            patch.object(service.tools_host, "maintain_approvals"),
            patch.object(service.state, "expire_agent_event_details", side_effect=lambda: order.append("expire")),
        ):
            service.prune_state()
        self.assertEqual(order, ["commit", "expire"])


class DetailExpiryPostgresTests(unittest.TestCase):
    def setUp(self):
        pg_harness.reset_database()

    def insert(self, seq, created_at, *, thread="thread-1", event_type="thread.activity", payload=None):
        values = payload if payload is not None else {
            "message": "Keep the original text", "source": "user",
            "activity": {"activity_id": "tool-1", "kind": "tool", "phase": "completed",
                         "title": "Research", "status": "completed", "detail": "arguments", "output": "findings"},
            "notice": {"kind": "memory_injection", "summary": "Memory injected", "details": "injected text"},
            "memory_page_ids": ["saved-memory"], "memory_recall_details": "recall diagnostics",
            "historical_context": "transferred history", "error_message": "retained error", "agent_runtime": "codex",
        }
        with db.transaction() as cur:
            columns = ["seq", "created_at", "thread_id", "event_type", *values]
            params = [seq, created_at, thread, event_type, *[
                db.jsonb(value) if isinstance(value, (dict, list)) else value for value in values.values()
            ]]
            cur.execute(f"INSERT INTO agent_events ({', '.join(columns)}) VALUES ({', '.join(['%s'] * len(params))})", params)

    def read(self):
        with db.transaction() as cur:
            cur.execute(f"SELECT {events._EVENT_FIELDS} FROM agent_events ORDER BY seq")
            return {row[0]: events._event_dict(row) for row in cur.fetchall()}

    def test_both_global_cutoffs_all_five_fields_and_idempotence(self):
        old = "2026-05-30T12:00:00Z"
        self.insert(10, old)
        self.insert(20, "2026-05-31T12:00:00Z")  # Exactly three months: keep.
        self.insert(30, "2026-08-01T00:00:00Z")  # Outside count, inside age.
        self.insert(40, old, event_type="thread.message", payload={"message": "User request", "source": "user"})
        self.insert(50, old, event_type="thread.message", payload={"message": "Assistant answer", "source": "agent"})
        self.insert(60, old, event_type="thread.notice", payload={
            "message": "Delivered peer request", "source": "user",
            "notice": {"kind": "agent_message", "summary": "Peer request", "details": "extra details"},
        })
        self.insert(150, old, thread="thread-2")  # Inclusive sequence cutoff, another thread.
        self.insert(151, old, thread="thread-2")  # Old, but inside newest window.
        self.insert(200, old, thread="thread-2")
        before = self.read()
        with (
            patch.object(events, "AGENT_EVENT_DETAIL_LIMIT", 50),
            patch.object(events, "AGENT_EVENT_DETAIL_BATCH_SIZE", 2),
            patch.object(events, "AGENT_EVENT_DETAIL_BATCHES_PER_PASS", 1),
            patch.object(events, "utc_now", return_value="2026-08-31T12:00:00Z"),
        ):
            self.assertEqual(events.expire_agent_event_details(), 2)
            # The next scheduled pass resumes without a persisted cursor.
            self.assertEqual(events.expire_agent_event_details(), 1)
            self.assertEqual(events.expire_agent_event_details(), 0)
        after = self.read()
        self.assertEqual(set(after), set(before))
        for seq in (20, 30, 40, 50, 151, 200):
            self.assertEqual(after[seq], before[seq])
        for seq in (10, 60, 150):
            expected = before[seq]
            payload = expected["payload"]
            if "activity" in payload:
                del payload["activity"]["detail"], payload["activity"]["output"]
            del payload["notice"]["details"]
            payload.pop("memory_recall_details", None)
            payload.pop("historical_context", None)
            self.assertEqual(after[seq], expected)
        with db.transaction() as cur:
            cur.execute("SELECT memory_recall_details, historical_context FROM agent_events WHERE seq = 10")
            self.assertEqual(cur.fetchone(), (None, None))

    def test_calendar_month_cutoff_uses_utc_and_clamps_leap_day(self):
        self.insert(1, "2024-02-29T11:59:59Z")
        self.insert(2, "2024-02-29T12:00:00Z")
        self.insert(100, "2024-01-01T00:00:00Z")
        with (
            patch.object(events, "AGENT_EVENT_DETAIL_LIMIT", 1),
            patch.object(events, "utc_now", return_value="2024-05-31T14:00:00+02:00"),
        ):
            self.assertEqual(events.expire_agent_event_details(), 1)
        rows = self.read()
        self.assertNotIn("output", rows[1]["payload"]["activity"])
        self.assertEqual(rows[2]["payload"]["activity"]["output"], "findings")
        self.assertEqual(rows[100]["payload"]["activity"]["output"], "findings")
