"""Calendar matching, independent best-effort delivery, and atomic definitions."""
from datetime import datetime, timezone
from http import HTTPStatus
import unittest
from unittest.mock import patch

import pg_harness
from host.runtime.core import db
from host.runtime.workspace import schedules
from host.runtime.workspace.host_api import WorkspaceError

SESSION = {"agent_runtime": "codex", "model": "gpt-6.1-sol", "effort": "high"}
DAILY = {"type": "daily", "times": ["08:00", "20:00"], "prompt": "Research"}
WEEKLY = {"type": "weekly", "days": ["mon", "fri"], "time": "08:00", "prompt": "Review"}


def instant(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class CalendarTriggerTests(unittest.TestCase):
    def test_next_time_is_fixed_across_delays_and_midnight(self):
        schedule = {"triggers": [DAILY]}
        for after, expected in [
            ("2026-09-28T07:59:59Z", "2026-09-28T08:00:00Z"),
            ("2026-09-28T08:00:00Z", "2026-09-28T20:00:00Z"),
            ("2026-09-28T08:00:31Z", "2026-09-28T20:00:00Z"),
            ("2026-09-28T20:00:01Z", "2026-09-29T08:00:00Z"),
        ]:
            with self.subTest(after=after):
                self.assertEqual(schedules._next_run(schedule, instant(after)), expected)

    def test_weekly_rollover_and_mixed_triggers(self):
        self.assertEqual(schedules._next_run({"triggers": [WEEKLY]}, instant("2026-10-02T08:00:00Z")), "2026-10-05T08:00:00Z")
        self.assertEqual(schedules._next_run({"triggers": [DAILY, WEEKLY]}, instant("2026-10-02T08:00:00Z")), "2026-10-02T20:00:00Z")
        sunday = {**WEEKLY, "days": ["sun"], "time": "00:00"}
        self.assertEqual(schedules._next_run({"triggers": [sunday]}, instant("2026-10-04T00:00:00Z")), "2026-10-11T00:00:00Z")
        self.assertIsNone(schedules._next_run({"triggers": []}, datetime.now(timezone.utc)))

    def test_matching_uses_utc_and_selected_weekdays(self):
        self.assertTrue(schedules._matches(WEEKLY, instant("2026-09-28T10:00:30+02:00")))
        self.assertFalse(schedules._matches(WEEKLY, instant("2026-09-29T08:00:00Z")))
        self.assertFalse(schedules._matches(DAILY, instant("2026-09-28T08:01:00Z")))

    def test_validates_all_fields_and_bounds_without_database_access(self):
        valid = {"name": "Research", "triggers": [DAILY, WEEKLY], **SESSION}
        self.assertEqual(schedules._validated_fields(valid)["triggers"], [DAILY, WEEKLY])
        for triggers in [None, {}, [DAILY] * 6, [None],
                         [{**DAILY, "type": "interval"}],
                         [{**DAILY, "times": []}],
                         [{**DAILY, "times": ["08:00"] * 25}],
                         [{**DAILY, "times": ["08:00", "08:00"]}],
                         [{**DAILY, "times": ["24:00"]}],
                         [{**DAILY, "times": ["8:00"]}],
                         [{**DAILY, "times": [8]}],
                         [{**DAILY, "prompt": " "}],
                         [{**DAILY, "prompt": "x" * 12001}],
                         [{**DAILY, "days": ["mon"]}],
                         [{**WEEKLY, "days": []}],
                         [{**WEEKLY, "days": ["mon", "mon"]}],
                         [{**WEEKLY, "days": ["monday"]}],
                         [{**WEEKLY, "days": [{}]}],
                         [{**WEEKLY, "time": "09:00:00"}],
                         [{**WEEKLY, "times": ["09:00"]}]]:
            with self.subTest(triggers=triggers):
                with self.assertRaises(WorkspaceError) as rejected:
                    schedules._validated_fields({**valid, "triggers": triggers})
                self.assertEqual(rejected.exception.status, HTTPStatus.BAD_REQUEST)
        self.assertEqual(schedules._validated_triggers([{**DAILY, "times": [f"{hour:02}:00" for hour in range(24)]}])[0]["times"], [f"{hour:02}:00" for hour in range(24)])

    def test_five_triggers_keep_independent_prompts_and_schedules(self):
        triggers = [{**(DAILY if index % 2 == 0 else WEEKLY), "prompt": f"Task {index}"}
                    for index in range(5)]
        self.assertEqual(schedules._validated_triggers(triggers), triggers)

    def test_script_paths_validated_for_every_trigger(self):
        fields = {"name": "Scripts", "agent_runtime": "script", "model": "bash", "effort": "fixed"}
        good = {**DAILY, "prompt": "/mnt/kern-agent/agent-home/job.sh"}
        self.assertEqual(len(schedules._validated_fields({**fields, "triggers": [good, good]})["triggers"]), 2)
        with self.assertRaises(WorkspaceError):
            schedules._validated_fields({**fields, "triggers": [good, DAILY]})

    def test_old_api_fields_are_rejected(self):
        with self.assertRaises(WorkspaceError) as rejected:
            schedules.create_schedule({"name": "Old", "message": "hello", "cadence": "daily", "daily_time": "08:00", **SESSION}, actor="agent")
        self.assertEqual(rejected.exception.status, HTTPStatus.BAD_REQUEST)


class CalendarTriggerDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pg_harness.ensure_database()

    def setUp(self):
        pg_harness.reset_database()
        self.addCleanup(db.close_pool)

    def create(self, triggers):
        return schedules.create_schedule({"name": "Calendar", "triggers": triggers, **SESSION}, actor="agent")

    def due(self, schedule, time="2026-09-28T08:00:00Z"):
        with db.transaction() as cur:
            cur.execute("UPDATE schedules SET next_run_at = %s WHERE id = %s", (time, schedule["id"]))

    def test_coincident_triggers_attempt_independently_despite_failure(self):
        schedule = self.create([DAILY, WEEKLY])
        self.due(schedule)
        with patch.object(schedules, "call_admin_api", side_effect=[WorkspaceError(HTTPStatus.CONFLICT, "busy"), {"status": "accepted", "thread": {}}]) as send, patch.object(schedules.host_errors, "report_warning") as diagnostic:
            self.assertEqual(schedules.run_due(instant("2026-09-28T08:00:30Z")), 2)
        self.assertEqual([call.args[2]["message"].split("\n\n")[-1] for call in send.call_args_list], ["Research", "Review"])
        diagnostic.assert_called_once()
        self.assertEqual(diagnostic.call_args.kwargs["context"]["trigger_index"], 0)
        current = schedules.load_schedule(schedule["id"])
        self.assertEqual(current["next_run_at"], "2026-09-28T20:00:00Z")
        self.assertEqual(current["revision"], 1)
        self.assertEqual(schedules.run_due(instant("2026-09-28T08:00:45Z")), 0)

    def test_downtime_skips_old_minutes_and_preserves_last_attempt(self):
        schedule = self.create([DAILY, WEEKLY])
        self.due(schedule)
        with patch.object(schedules, "call_admin_api") as send:
            self.assertEqual(schedules.run_due(instant("2026-09-28T09:00:00Z")), 0)
        send.assert_not_called()
        current = schedules.load_schedule(schedule["id"])
        self.assertIsNone(current["last_run_at"])
        self.assertEqual(current["next_run_at"], "2026-09-28T20:00:00Z")

    def test_empty_list_atomic_update_conflict_and_restore(self):
        schedule = self.create([DAILY, WEEKLY])
        fields = {"name": schedule["name"], "triggers": [], **SESSION, "expected_revision": 1}
        stopped = schedules.update_schedule(schedule["id"], fields, actor="agent")
        self.assertIsNone(stopped["next_run_at"])
        self.assertEqual(stopped["triggers"], [])
        self.assertEqual(stopped["thread_id"], schedule["thread_id"])
        with self.assertRaises(WorkspaceError) as stale:
            schedules.update_schedule(schedule["id"], fields, actor="agent")
        self.assertEqual(stale.exception.status, HTTPStatus.CONFLICT)
        restored = schedules.restore_revision(schedule["id"], 1, {"expected_revision": 2})
        self.assertEqual(restored["triggers"], [DAILY, WEEKLY])
        self.assertEqual(restored["revision"], 3)
        self.assertIsNotNone(restored["next_run_at"])
        summary = schedules.list_active_schedules({})["schedules"][0]
        self.assertTrue(all("prompt" not in trigger for trigger in summary["triggers"]))
        history = schedules.list_revisions(schedule["id"], {})["revisions"]
        self.assertEqual([row["triggers"] for row in history], [[DAILY, WEEKLY], [], [DAILY, WEEKLY]])

    def test_prompt_only_edit_preserves_due_time_and_invalid_batch_does_not_write(self):
        schedule = self.create([DAILY])
        fields = {"name": schedule["name"], "triggers": [{**DAILY, "prompt": "New"}], **SESSION, "expected_revision": 1}
        updated = schedules.update_schedule(schedule["id"], fields, actor="agent")
        self.assertEqual(updated["next_run_at"], schedule["next_run_at"])
        with self.assertRaises(WorkspaceError):
            schedules.update_schedule(schedule["id"], {**fields, "expected_revision": 2, "triggers": [DAILY, {**WEEKLY, "days": []}]}, actor="agent")
        self.assertEqual(schedules.load_schedule(schedule["id"]), updated)
