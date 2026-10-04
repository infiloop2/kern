"""Tests for the SQL migration runner (host.runtime.deploy.migrate).

Database tests use a dedicated database on the scratch cluster so each test
can build its required schema version without disturbing other tests.
"""

from __future__ import annotations

from contextlib import redirect_stderr
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pg_harness

from host.runtime.deploy import migrate
from host.runtime.core import db, state


def _write(directory: Path, name: str, up: str) -> None:
    (directory / name).write_text(f"-- migrate:up\n{up}\n")


def _workspace_ledger_adoption_sql() -> str:
    """Return the exact SQL bootstrap executes between its two migration runs."""
    bootstrap = (
        Path(__file__).resolve().parents[1] / "host" / "bootstrap" / "bootstrap.sh"
    ).read_text()
    function = bootstrap.split("adopt_workspace_migration_history() {", 1)[1]
    heredoc = function.split("<<'SQL'\n", 1)[1]
    return heredoc.split("\nSQL", 1)[0]


class MigrationFileTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.migrations = Path(directory.name)

    def test_forward_only_file_loads_without_down_section(self) -> None:
        _write(self.migrations, "0001_first.sql", "CREATE TABLE first (id INT);")
        self.assertEqual(migrate.load_migrations(self.migrations), [
            migrate.Migration(1, "first", "CREATE TABLE first (id INT);")
        ])

    def test_malformed_migration_files_are_rejected(self) -> None:
        for name, sql, error in [
            ("0001_first.sql", "SELECT 1;", "missing"),
            ("not_versioned.sql", "-- migrate:up\nSELECT 1;", "must be named"),
            ("0001_first.sql", "-- migrate:up\n", "empty up section"),
            ("0001_first.sql", "-- migrate:up\nSELECT 1;\n-- migrate:down\nSELECT 2;", "down sections are unsupported"),
        ]:
            with self.subTest(name=name, sql=sql):
                path = self.migrations / name
                path.write_text(sql)
                with self.assertRaisesRegex(migrate.MigrationError, error):
                    migrate.load_migrations(self.migrations)
                path.unlink()

    def test_duplicate_versions_are_rejected(self) -> None:
        _write(self.migrations, "0001_first.sql", "SELECT 1;")
        _write(self.migrations, "0001_second.sql", "SELECT 2;")
        with self.assertRaisesRegex(migrate.MigrationError, "duplicate migration version"):
            migrate.load_migrations(self.migrations)

    def test_repo_files_load_in_forward_only_format(self) -> None:
        self.assertTrue(migrate.load_migrations())

    def test_down_command_is_rejected_before_opening_database(self) -> None:
        with patch.object(db, "transaction") as transaction, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                migrate.main(["down"])
        self.assertEqual(raised.exception.code, 2)
        transaction.assert_not_called()


class MigrateRunnerTests(unittest.TestCase):
    DB_NAME = "kern_migrate_test"

    def setUp(self) -> None:
        pg_harness.create_database(self.DB_NAME)
        self.env_patch = patch.dict("os.environ", {"KERN_DB_NAME": self.DB_NAME})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        # Close pooled connections to this class's database before the env
        # restore, so no later test checks one out against the wrong database.
        self.addCleanup(db.close_pool)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.migrations = Path(self.temp_dir.name)

    def test_notice_migration_preserves_history_and_bounded_context(self) -> None:
        migrate.up(target=85, quiet=True)
        with db.transaction() as cur:
            cur.execute("INSERT INTO agent_events (created_at, event_type, thread_id, message, historical_context)"
                        " VALUES ('now', 'thread.context_added', 'thread-1', 'Historical context transferred.', 'bounded preview') RETURNING seq")
            context_seq = cur.fetchone()[0]
            cur.execute("INSERT INTO agent_events (created_at, event_type, thread_id, source, message)"
                        " VALUES ('now', 'thread.message', 'thread-1', 'user', 'This is an automated message from Kern.') RETURNING seq")
            input_seq = cur.fetchone()[0]
        self.assertEqual(migrate.up(target=86, quiet=True), [86])
        with db.transaction() as cur:
            cur.execute("SELECT seq, event_type, source, historical_context, notice FROM agent_events ORDER BY seq")
            rows = cur.fetchall()
        self.assertEqual(rows[0], (context_seq, 'thread.notice', None, 'bounded preview', {
            'kind': 'history_transfer', 'summary': 'Historical context transferred.',
        }))
        self.assertEqual(rows[1], (input_seq, 'thread.message', 'user', None, None))

    def test_browser_state_migration_constraints(self) -> None:
        migrate.up(target=77, quiet=True)
        self.assertEqual(migrate.up(target=78, quiet=True), [78])
        with db.transaction() as cur:
            cur.execute("INSERT INTO browser_settings (mode) VALUES ('direct')")
        with self.assertRaises(Exception):
            with db.transaction() as cur:
                cur.execute("UPDATE browser_settings SET mode='decodo'")

    def table_names(self) -> set[str]:
        with db.transaction() as cur:
            cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            return {row[0] for row in cur.fetchall()}

    def test_codex_sol_6_1_speed_migration(self) -> None:
        migrate.up(target=80, quiet=True)
        runtimes = ("codex", "codex-2", "codex-3")
        with db.transaction() as cur:
            for index, runtime in enumerate(runtimes, 780):
                cur.execute(
                    "INSERT INTO thread_sessions (agent_runtime, thread_id, model, effort, provider_session_id)"
                    " VALUES (%s, %s, 'gpt-6-sol', 'ultra', 'old-provider')",
                    (runtime, f"old-sol-{runtime}"),
                )
                cur.execute(
                    "INSERT INTO web_apps (app_id, name, revision, agent_runtime, agent_model,"
                    " agent_effort, created_at, updated_at)"
                    " VALUES (%s, 'Sol app', 0, %s, 'gpt-6-sol', 'high', 'now', 'now')",
                    (f"app-{index}", runtime),
                )
                cur.execute(
                    "INSERT INTO schedules (id, thread_id, name, triggers,"
                    " agent_runtime, model, effort, next_run_at, created_at, updated_at)"
                    " VALUES (%s, %s, 'Sol job', %s::jsonb, %s,"
                    " 'gpt-6-sol', 'max', 'now', 'now', 'now')",
                    (index, f"schedule-{index}", '[{"type":"daily","times":["09:00"],"prompt":"hello"}]', runtime),
                )
        self.assertEqual(migrate.up(target=81, quiet=True), [81])
        with db.transaction() as cur:
            cur.execute("SELECT agent_model, agent_effort FROM web_apps ORDER BY app_id")
            self.assertEqual(cur.fetchall(), [("gpt-6.1-sol", "high")] * 3)
            cur.execute("SELECT model, effort FROM schedules ORDER BY id")
            self.assertEqual(cur.fetchall(), [("gpt-6.1-sol", "max")] * 3)
            cur.execute("SELECT model, effort, provider_session_id FROM thread_sessions ORDER BY thread_id")
            self.assertEqual(cur.fetchall(), [("gpt-6-sol", "ultra", "old-provider")] * 3)
            for runtime in runtimes:
                for model in ("gpt-6.1-sol", "gpt-6-astra", "gpt-6-luna"):
                    for effort in ("high-fast", "high-ultrafast"):
                        cur.execute(
                            "INSERT INTO thread_sessions (agent_runtime, thread_id, model, effort, provider_session_id)"
                            " VALUES (%s, %s, %s, %s, 'new-provider')",
                            (runtime, f"new-{runtime}-{model}-{effort}", model, effort),
                        )

    def test_sol_6_1_host_usage_migration_preserves_history(self) -> None:
        migrate.up(target=80, quiet=True)
        with db.transaction() as cur:
            cur.execute(
                "INSERT INTO host_inference_usage (provider, model, day, requests, cost_usd)"
                " VALUES ('openai', 'gpt-6-sol', CURRENT_DATE, 2, 0.02)"
            )
        migrate.up(target=81, quiet=True)
        state.record_host_inference_usage("openai", "gpt-6.1-sol", {
            "input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 10,
        }, 0.000262)
        with db.transaction() as cur:
            cur.execute("SELECT model, requests, cost_usd FROM host_inference_usage ORDER BY model")
            rows = cur.fetchall()
            by_model = {row[0]: (row[1], float(row[2])) for row in rows}
            self.assertEqual(by_model, {"gpt-6-sol": (2, 0.02), "gpt-6.1-sol": (1, 0.000262)})

    def test_custom_content_guard_migration_preserves_existing_domains(self) -> None:
        version = next(item.version for item in migrate.load_migrations()
                       if item.name == "custom_domain_parameter_guard")
        migrate.up(target=version - 1, quiet=True)
        with db.transaction() as cur:
            cur.execute("INSERT INTO allowed_domains (domain) VALUES ('api.example.com')")
        migrate.up(target=version, quiet=True)
        with db.transaction() as cur:
            cur.execute("SELECT domain, guard_request_content FROM allowed_domains")
            self.assertEqual(cur.fetchall(), [("api.example.com", False)])
            cur.execute("UPDATE allowed_domains SET guard_request_content = TRUE")

    def test_calendar_trigger_migration_preserves_cadences_and_history(self) -> None:
        import json
        from datetime import datetime, timedelta, timezone
        from host.runtime.workspace import schedules

        migrate.up(target=78, quiet=True)
        # Exact daily/weekly mappings, both trigger limits, and irregular edges.
        intervals = [360, 10080, 1000, 30, 60, 480, 720, 12, 10, 5,
                     1440, 2016, 2520, 3360, 5040, 1680, 2880, 1439]
        fixtures = [(1, "daily", None, "09:00"),
                    *[(i, "interval", minutes, None) for i, minutes in enumerate(intervals, 2)]]
        live_metadata = ("id, thread_id, name, agent_runtime, model, effort, revision,"
                         " deleted_at, last_run_at, created_at, updated_at, purpose")
        history_metadata = ("id, schedule_id, revision, name, agent_runtime, model, effort,"
                            " deleted, actor, created_at, purpose")
        with db.transaction() as cur:
            for identity, cadence, minutes, time in fixtures:
                cur.execute(
                    "INSERT INTO schedules (id, thread_id, name, message, cadence, interval_minutes, daily_time,"
                    " agent_runtime, model, effort, revision, next_run_at, last_run_at, created_at, updated_at, purpose)"
                    " VALUES (%s, %s, 'Retained', 'Original prompt', %s, %s, %s,"
                    " 'codex', 'gpt-6-astra', 'high', 2, %s, '2026-09-20T00:00:00Z',"
                    " '2026-09-19T00:00:00Z', '2026-09-20T00:00:00Z', 'Retained purpose')",
                    (identity, f"schedule-{identity}", cadence, minutes, time,
                     "2026-09-29T09:00:00Z" if time else "2026-09-29T15:55:14+02:00"),
                )
            for revision, prompt, actor in [(1, "Earlier prompt", "agent"), (2, "Original prompt", "user")]:
                cur.execute(
                    "INSERT INTO schedule_revisions (schedule_id, revision, name, message, cadence, interval_minutes, daily_time,"
                    " agent_runtime, model, effort, deleted, actor, created_at, purpose)"
                    " SELECT id, %s, name, %s, cadence, interval_minutes, daily_time,"
                    " agent_runtime, model, effort, FALSE, %s, updated_at, purpose FROM schedules",
                    (revision, prompt, actor),
                )
            # A prior revision has its own cadence; deletion keeps older restore points.
            cur.execute("UPDATE schedule_revisions SET cadence = 'daily', interval_minutes = NULL, daily_time = '07:15' WHERE schedule_id = 2 AND revision = 1")
            cur.execute("UPDATE schedules SET deleted_at = updated_at, revision = 3 WHERE id = 5")
            cur.execute(
                "INSERT INTO schedule_revisions (schedule_id, revision, name, message, cadence, interval_minutes, daily_time,"
                " agent_runtime, model, effort, deleted, actor, created_at, purpose)"
                " SELECT id, revision, name, message, cadence, interval_minutes, daily_time,"
                " agent_runtime, model, effort, TRUE, 'user', updated_at, purpose FROM schedules WHERE id = 5"
            )
            cur.execute(f"SELECT {live_metadata} FROM schedules ORDER BY id")
            before_live = cur.fetchall()
            cur.execute(f"SELECT {history_metadata} FROM schedule_revisions ORDER BY id")
            before_history = cur.fetchall()
        self.assertEqual(migrate.up(target=79, quiet=True), [79])
        with db.transaction() as cur:
            cur.execute(f"SELECT {live_metadata} FROM schedules ORDER BY id")
            self.assertEqual(cur.fetchall(), before_live)
            cur.execute(f"SELECT {history_metadata} FROM schedule_revisions ORDER BY id")
            self.assertEqual(cur.fetchall(), before_history)
            cur.execute("SELECT id, triggers::text, next_run_at FROM schedules ORDER BY id")
            rows = cur.fetchall()
            cur.execute("SELECT schedule_id, revision, triggers::text FROM schedule_revisions ORDER BY schedule_id, revision")
            history = cur.fetchall()
            cur.execute("SELECT count(*) FROM pg_proc WHERE proname = 'calendar_triggers'")
            self.assertEqual(cur.fetchone(), (0,))

        converted = {identity: json.loads(triggers) for identity, triggers, _ in rows}
        self.assertEqual(converted[1], [{"type": "daily", "times": ["09:00"], "prompt": "Original prompt"}])
        anchor = datetime(2026, 9, 29, 13, 55, tzinfo=timezone.utc)
        for (identity, _, minutes, _), (_, _, next_run) in zip(fixtures, rows):
            with self.subTest(minutes=minutes):
                triggers = converted[identity]
                self.assertEqual(schedules._validated_triggers(triggers), triggers)
                self.assertEqual(next_run, "2026-09-29T09:00:00Z" if identity == 1 else "2026-09-29T13:55:00Z")
                if minutes is None:
                    continue
                exact = (minutes <= 1440 and 1440 % minutes == 0 and 1440 // minutes <= 120
                         or 10080 % minutes == 0 and 10080 // minutes <= 5)
                if not exact:
                    self.assertEqual(triggers, [{"type": "daily", "times": ["13:55"], "prompt": "Original prompt"}])
                # Compare actual calendar occurrences across two weeks to the
                # former interval (or the explicit once-daily edge fallback).
                gap = timedelta(minutes=minutes if exact else 1440)
                expected = anchor
                cursor = anchor - timedelta(seconds=1)
                while expected < anchor + timedelta(days=14):
                    next_time = schedules._next_run({"triggers": triggers}, cursor)
                    self.assertEqual(next_time, expected.strftime("%Y-%m-%dT%H:%M:%SZ"))
                    cursor = expected
                    expected += gap
        self.assertEqual(len(converted[9]), 5)  # 12 minutes: 120 times, five triggers.
        self.assertEqual(len(converted[5]), 2)  # 30 minutes: 48 times, two triggers.
        for identity, revision, raw in history:
            triggers = json.loads(raw)
            self.assertEqual(schedules._validated_triggers(triggers), triggers)
            if identity == 2 and revision == 1:
                self.assertEqual(triggers, [{"type": "daily", "times": ["07:15"], "prompt": "Earlier prompt"}])
            else:
                prompt = "Earlier prompt" if revision == 1 else "Original prompt"
                self.assertEqual(triggers, [{**trigger, "prompt": prompt} for trigger in converted[identity]])
        restored = schedules.restore_revision(5, 2, {"expected_revision": 3})
        self.assertFalse(restored["deleted"])
        self.assertEqual(restored["triggers"], converted[5])
        restored_old = schedules.restore_revision(2, 1, {"expected_revision": 2})
        self.assertEqual(restored_old["triggers"], [{"type": "daily", "times": ["07:15"], "prompt": "Earlier prompt"}])

    def test_sonnet_5_5_migration_preserves_history(self) -> None:
        migrate.up(target=75, quiet=True)
        with db.transaction() as cur:
            cur.execute(
                "INSERT INTO thread_sessions (agent_runtime, thread_id, model, effort, provider_session_id)"
                " VALUES ('claude_code', 'thread-old-sonnet', 'claude-sonnet-5', 'high', 'old-provider')"
            )
            cur.execute(
                "INSERT INTO web_apps (app_id, name, revision, agent_runtime, agent_model,"
                " agent_effort, created_at, updated_at)"
                " VALUES ('app-76', 'Sonnet app', 0, 'claude_code', 'claude-sonnet-5', 'high', 'now', 'now')"
            )
            cur.execute(
                "INSERT INTO schedules (id, thread_id, name, message, cadence, interval_minutes,"
                " agent_runtime, model, effort, next_run_at, created_at, updated_at)"
                " VALUES (76, 'schedule-76', 'Sonnet job', 'hello', 'interval', 60,"
                " 'claude_code', 'claude-sonnet-5', 'max', 'now', 'now', 'now')"
            )
        with self.assertRaises(Exception):
            with db.transaction() as cur:
                cur.execute(
                    "INSERT INTO thread_sessions (agent_runtime, thread_id, model, effort)"
                    " VALUES ('claude_code', 'thread-new-sonnet', 'claude-sonnet-5-5', 'high')"
                )

        self.assertEqual(migrate.up(target=76, quiet=True), [76])
        with db.transaction() as cur:
            cur.execute("SELECT agent_model FROM web_apps WHERE app_id = 'app-76'")
            self.assertEqual(cur.fetchone(), ("claude-sonnet-5-5",))
            cur.execute("SELECT model FROM schedules WHERE id = 76")
            self.assertEqual(cur.fetchone(), ("claude-sonnet-5-5",))
            cur.execute(
                "SELECT model, provider_session_id FROM thread_sessions"
                " WHERE thread_id = 'thread-old-sonnet'"
            )
            self.assertEqual(cur.fetchone(), ("claude-sonnet-5", "old-provider"))
            cur.execute(
                "INSERT INTO thread_sessions (agent_runtime, thread_id, model, effort, provider_session_id)"
                " VALUES ('claude_code', 'thread-new-sonnet', 'claude-sonnet-5-5', 'ultracode', 'new-provider')"
            )
            cur.execute(
                "INSERT INTO agent_events (created_at, event_type, thread_id, message, source)"
                " VALUES ('2026-09-29T00:00:00Z', 'thread.message', 'thread-new-sonnet', 'retained', 'agent')"
            )

    def test_spawned_chat_migration_preserves_existing_chats(self) -> None:
        migrate.up(target=73, quiet=True)
        with db.transaction() as cur:
            cur.execute("INSERT INTO chat_threads (thread_id, archived) VALUES ('thread-1', FALSE)")
        self.assertEqual(migrate.up(target=74, quiet=True), [74])
        with db.transaction() as cur:
            cur.execute("SELECT thread_id, archived, spawned_by_thread_id FROM chat_threads")
            self.assertEqual(cur.fetchall(), [("thread-1", False, None)])
            cur.execute(
                "INSERT INTO chat_threads (thread_id, archived, spawned_by_thread_id)"
                " VALUES ('thread-2', FALSE, 'thread-1')"
            )

    def test_grok_4_7_migration_preserves_history(self) -> None:
        migrate.up(target=71, quiet=True)
        with db.transaction() as cur:
            cur.execute(
                "INSERT INTO thread_sessions (agent_runtime, thread_id, model, effort, provider_session_id)"
                " VALUES ('grok', 'thread-old-grok', 'grok-4.6', 'high', 'old-provider')"
            )
            cur.execute(
                "INSERT INTO web_apps (app_id, name, revision, agent_runtime, agent_model,"
                " agent_effort, created_at, updated_at)"
                " VALUES ('app-73', 'Old Grok app', 0, 'grok', 'grok-4.6', 'high', 'now', 'now')"
            )
            cur.execute(
                "INSERT INTO schedules (id, thread_id, name, message, cadence, interval_minutes,"
                " agent_runtime, model, effort, next_run_at, created_at, updated_at)"
                " VALUES (73, 'schedule-73', 'Old Grok job', 'hello', 'interval', 60,"
                " 'grok-2', 'grok-4.6', 'xhigh', 'now', 'now', 'now')"
            )
        with self.assertRaises(Exception):
            with db.transaction() as cur:
                cur.execute(
                    "INSERT INTO thread_sessions (agent_runtime, thread_id, model, effort)"
                    " VALUES ('grok', 'thread-new-grok', 'grok-4.7', 'high')"
                )

        self.assertEqual(migrate.up(target=72, quiet=True), [72])
        with db.transaction() as cur:
            cur.execute("SELECT agent_model FROM web_apps WHERE app_id = 'app-73'")
            self.assertEqual(cur.fetchone(), ("grok-4.7",))
            cur.execute("SELECT model FROM schedules WHERE id = 73")
            self.assertEqual(cur.fetchone(), ("grok-4.7",))
            cur.execute(
                "SELECT model, provider_session_id FROM thread_sessions"
                " WHERE thread_id = 'thread-old-grok'"
            )
            self.assertEqual(cur.fetchone(), ("grok-4.6", "old-provider"))
            for runtime in ("grok", "grok-2"):
                cur.execute(
                    "INSERT INTO thread_sessions"
                    " (agent_runtime, thread_id, model, effort, provider_session_id)"
                    " VALUES (%s, %s, 'grok-4.7', 'xhigh', 'new-provider')",
                    (runtime, f"thread-{runtime}-4.7"),
                )
            cur.execute(
                "INSERT INTO web_apps (app_id, name, revision, agent_runtime, agent_model,"
                " agent_effort, created_at, updated_at)"
                " VALUES ('app-72', 'Grok app', 0, 'grok', 'grok-4.7', 'high', 'now', 'now')"
            )
            cur.execute(
                "INSERT INTO schedules (id, thread_id, name, message, cadence, interval_minutes,"
                " agent_runtime, model, effort, next_run_at, created_at, updated_at)"
                " VALUES (72, 'schedule-72', 'Grok job', 'hello', 'interval', 60,"
                " 'grok-2', 'grok-4.7', 'xhigh', 'now', 'now', 'now')"
            )

    def test_swarm_migration_retires_usage_buckets_and_preserves_jev(self) -> None:
        source = Path(__file__).resolve().parents[1] / "host" / "migrations"
        for path in sorted(source.glob("*.sql")):
            if int(path.name.split("_", 1)[0]) < 71:
                (self.migrations / path.name).write_text(path.read_text())
        migrate.up(directory=self.migrations, quiet=True)
        with db.transaction() as cur:
            cur.execute(
                "INSERT INTO host_inference_usage (provider, model, day, requests, cost_usd)"
                " VALUES ('openai', 'gpt-5.6-luna', CURRENT_DATE, 1, 0.01),"
                " ('openai', 'other', CURRENT_DATE, 1, 0),"
                " ('typesafe', 'jev', CURRENT_DATE, 2, 0.02)"
            )
        path = source / "0071_swarm_state.sql"
        (self.migrations / path.name).write_text(path.read_text())
        self.assertEqual(migrate.up(directory=self.migrations, quiet=True), [71])
        with db.transaction() as cur:
            cur.execute("SELECT model, requests, cost_usd FROM host_inference_usage")
            rows = cur.fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][:2], ("jev", 2))
            self.assertAlmostEqual(float(rows[0][2]), 0.02)
            cur.execute(
                "INSERT INTO host_inference_usage (provider, model, day)"
                " VALUES ('openai', 'gpt-6-luna', CURRENT_DATE)"
            )

    def test_up_applies_pending_migrations_in_order_and_records_them(self) -> None:
        _write(self.migrations, "0001_first.sql", "CREATE TABLE first (id INT);")
        _write(
            self.migrations,
            "0002_second.sql",
            "CREATE TABLE second (first_like INT); INSERT INTO second SELECT 1 FROM first;",
        )

        applied = migrate.up(directory=self.migrations, quiet=True)

        self.assertEqual(applied, [1, 2])
        self.assertLessEqual({"first", "second", "schema_migrations"}, self.table_names())
        status = migrate.status(directory=self.migrations)
        self.assertEqual(status, [(1, "first", True), (2, "second", True)])

    def test_up_is_idempotent_and_applies_only_new_versions(self) -> None:
        _write(self.migrations, "0001_first.sql", "CREATE TABLE first (id INT);")
        self.assertEqual(migrate.up(directory=self.migrations, quiet=True), [1])
        self.assertEqual(migrate.up(directory=self.migrations, quiet=True), [])
        _write(self.migrations, "0002_second.sql", "CREATE TABLE second (id INT);")
        self.assertEqual(migrate.up(directory=self.migrations, quiet=True), [2])

    def test_existing_ledger_prevents_replay_after_down_section_cleanup(self) -> None:
        legacy_sql = (
            "-- migrate:up\n"
            "CREATE TABLE retained (value TEXT); INSERT INTO retained VALUES ('existing data');\n"
            "-- migrate:down\nDROP TABLE retained;\n"
        )
        path = self.migrations / "0001_retained.sql"
        path.write_text(legacy_sql)
        # Seed a database as the old runner did, before changing the file format.
        with db.transaction() as cur:
            cur.execute(legacy_sql.split("-- migrate:down", 1)[0])
            migrate.applied_versions(cur)
            cur.execute(
                "INSERT INTO schema_migrations (version, name, applied_at)"
                " VALUES (1, 'retained', '2026-09-01T00:00:00Z')"
            )
            cur.execute("SELECT version, name, applied_at FROM schema_migrations")
            ledger = cur.fetchall()

        path.write_text(legacy_sql.split("-- migrate:down", 1)[0].rstrip() + "\n")
        # Bootstrap's bounded pass and subsequent full pass must both skip it.
        self.assertEqual(migrate.up(target=13, directory=self.migrations, quiet=True), [])
        self.assertEqual(migrate.up(directory=self.migrations, quiet=True), [])
        with db.transaction() as cur:
            cur.execute("SELECT value FROM retained")
            self.assertEqual(cur.fetchall(), [("existing data",)])
            cur.execute("SELECT version, name, applied_at FROM schema_migrations")
            self.assertEqual(cur.fetchall(), ledger)

    def test_a_failing_migration_rolls_back_and_leaves_the_previous_version(self) -> None:
        _write(self.migrations, "0001_first.sql", "CREATE TABLE first (id INT);")
        migrate.up(directory=self.migrations, quiet=True)
        _write(self.migrations, "0002_second.sql", "CREATE TABLE second (id INT);")
        _write(self.migrations, "0003_broken.sql", "CREATE TABLE third (id INT); SELECT no_such_column;")

        with self.assertRaises(Exception):
            migrate.up(directory=self.migrations, quiet=True)

        self.assertNotIn("second", self.table_names())
        self.assertNotIn("third", self.table_names())
        self.assertEqual(migrate.status(directory=self.migrations), [
            (1, "first", True), (2, "second", False), (3, "broken", False),
        ])

    def test_shared_app_recovery_migration_starts_from_live_state(self) -> None:
        import json
        from host.runtime.workspace.web_apps import backend

        migrate.up(target=64, quiet=True)
        # Seed the old schema directly; the current API writes shared recovery.
        with db.transaction() as cur:
            for app_id in ["app-1", "app-2", "app-3"]:
                cur.execute(
                    "INSERT INTO web_apps (app_id, name, revision, created_at, updated_at,"
                    " agent_runtime, agent_model, agent_effort)"
                    " VALUES (%s, %s, 0, 'now', 'now', 'codex', 'gpt-6-astra', 'high')",
                    (app_id, app_id),
                )
                cur.execute("INSERT INTO web_app_collection_state (app_id) VALUES (%s)", (app_id,))
        live_rows = {"leads": {"a": {"v": 2}, "b": {"v": 9}}}
        with db.transaction() as cur:
            # Both an existing checkpoint and a live state newer than its last
            # checkpoint become one fresh, accurate baseline. app-3 stays empty.
            for app_id in ["app-1", "app-2"]:
                cur.execute(
                    "UPDATE web_apps SET revision = 10, html = '<main>live</main>',"
                    " data_json = %s WHERE app_id = %s",
                    (json.dumps({"count": 10}), app_id),
                )
                backend._restore_collection_snapshot(cur, app_id, json.dumps(live_rows), "now")
            cur.execute(
                "INSERT INTO web_app_revisions (app_id, revision, actor, kind, html,"
                " css, javascript, data_json, collections_json, created_at)"
                " VALUES ('app-1', 10, 'agent', 'collection', '<main>live</main>',"
                " '', '', %s, %s, '2026-09-21T00:00:00Z')",
                (json.dumps({"count": 10}), json.dumps(live_rows)),
            )
        migrate.up(quiet=True)
        for app_id in ["app-1", "app-2", "app-3"]:
            state = backend.load_app_state(app_id)
            self.assertEqual(state["revision"], 0 if app_id == "app-3" else 10)
            self.assertEqual(state["html"], "" if app_id == "app-3" else "<main>live</main>")
            self.assertEqual(state["data"], {} if app_id == "app-3" else {"count": 10})
            points = backend.list_revisions(app_id, {})["revisions"]
            self.assertEqual([(r["revision"], r["kind"]) for r in points], [(state["revision"], "migration")])
            with db.transaction() as cur:
                expected_rows = {} if app_id == "app-3" else live_rows
                self.assertEqual(json.loads(backend.recovery.collection_snapshot(cur, app_id, state["revision"])), expected_rows)
            if app_id != "app-3":
                # First post-upgrade collection write must retain the live UI
                # and document, even when old history had a checkpoint gap.
                backend.apply_collection_actions(app_id, "leads", {
                    "expected_revision": 10, "operations": [
                        {"action": "delete", "id": "a"},
                    ],
                })
                restored = backend.restore_revision(app_id, 10)["app"]
                self.assertEqual(restored["html"], state["html"])
                self.assertEqual(restored["data"], state["data"])
                self.assertEqual(backend.query_collection(app_id, "leads", {})["rows"],
                                 [{"id": key, "value": value} for key, value in live_rows["leads"].items()])

    def test_repo_migrations_apply_and_repeat_cleanly(self) -> None:
        # Every shipped migration must apply to a fresh database exactly once.
        applied = migrate.up(quiet=True)
        self.assertGreaterEqual(len(applied), 1)
        tables = self.table_names()
        self.assertIn("thread_sessions", tables)
        # The thread-only model dropped the task queue.
        self.assertNotIn("tasks", tables)
        with db.transaction() as cur:
            cur.execute(
                "SELECT indexname FROM pg_indexes WHERE schemaname = 'public'"
                " AND tablename = 'agent_events'"
            )
            event_indexes = {str(name) for (name,) in cur.fetchall()}
        self.assertLessEqual(
            {
                "agent_events_message_search_idx",
                "agent_events_message_time_idx",
                "agent_events_thread_message_seq_idx",
                "agent_events_message_seq_idx",
            },
            event_indexes,
        )

        self.assertEqual(migrate.up(quiet=True), [])
        self.assertTrue(all(applied for _, _, applied in migrate.status()))

    def test_astra_model_migration_allows_sessions(self) -> None:
        self.assertEqual(migrate.up(target=50, quiet=True), list(range(1, 51)))
        with self.assertRaises(Exception):
            with db.transaction() as cur:
                cur.execute(
                    "INSERT INTO thread_sessions"
                    " (agent_runtime, thread_id, model, effort)"
                    " VALUES ('codex', 'thread-astra', 'gpt-6-astra', 'ultra')"
                )

        self.assertEqual(migrate.up(target=51, quiet=True), [51])
        with db.transaction() as cur:
            cur.execute(
                "INSERT INTO web_apps"
                " (app_id, name, archived, revision, agent_runtime, agent_model, agent_effort,"
                " created_at, updated_at) VALUES"
                " ('app-51', 'Astra App', FALSE, 0, 'codex', 'gpt-6-astra', 'ultra',"
                " '2026-09-04T00:00:00Z', '2026-09-04T00:00:00Z')"
            )
            cur.execute(
                "INSERT INTO schedules"
                " (id, thread_id, name, message, cadence, interval_minutes, agent_runtime,"
                " model, effort, next_run_at, created_at, updated_at) VALUES"
                " (51, 'schedule-51', 'Astra Schedule', 'Run with Astra', 'interval', 60,"
                " 'codex', 'gpt-6-astra', 'ultra', '2026-09-04T01:00:00Z',"
                " '2026-09-04T00:00:00Z', '2026-09-04T00:00:00Z')"
            )
            cur.execute(
                "INSERT INTO schedule_revisions"
                " (schedule_id, revision, name, message, cadence, interval_minutes,"
                " agent_runtime, model, effort, deleted, actor, created_at) VALUES"
                " (51, 1, 'Astra Schedule', 'Run with Astra', 'interval', 60,"
                " 'codex', 'gpt-6-astra', 'ultra', FALSE, 'user',"
                " '2026-09-04T00:00:00Z')"
            )
            cur.execute(
                "INSERT INTO thread_sessions"
                " (agent_runtime, thread_id, provider_session_id, model, effort) VALUES"
                " ('codex', 'thread-astra', 'provider-chat', 'gpt-6-astra', 'ultra'),"
                " ('codex', 'app-51', 'provider-app', 'gpt-6-astra', 'ultra'),"
                " ('codex', 'schedule-51', 'provider-schedule', 'gpt-6-astra', 'ultra')"
            )
            cur.execute(
                "INSERT INTO agent_events"
                " (created_at, event_type, thread_id, message, source)"
                " VALUES ('2026-09-04T00:00:00Z', 'thread.message',"
                " 'thread-astra', 'Astra transcript', 'agent')"
            )

    def test_gpt_6_sol_luna_migration_preserves_retired_rows_and_account_bindings(self) -> None:
        migrate.up(target=68, quiet=True)
        runtimes = ("codex", "codex-2", "codex-3")
        with db.transaction() as cur:
            for runtime in runtimes:
                for model in ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"):
                    cur.execute(
                        "INSERT INTO thread_sessions (agent_runtime, thread_id, model, effort)"
                        " VALUES (%s, %s, %s, 'high')",
                        (runtime, f"old-{runtime}-{model}", model),
                    )
            cur.execute(
                "INSERT INTO thread_sessions (agent_runtime, thread_id, model, effort)"
                " VALUES ('claude_code', 'thread-opus', 'claude-opus-5-5', 'max')"
            )
        self.assertEqual(migrate.up(target=69, quiet=True), [69])
        expected = []
        with db.transaction() as cur:
            for index, runtime in enumerate(runtimes, 1):
                for model, effort in (("gpt-6-sol", "ultra"), ("gpt-6-luna", "max")):
                    thread = f"new-{runtime}-{model}"
                    cur.execute(
                        "INSERT INTO thread_sessions"
                        " (agent_runtime, thread_id, provider_session_id, model, effort)"
                        " VALUES (%s, %s, 'provider-session', %s, %s)",
                        (runtime, thread, model, effort),
                    )
                    expected.append((runtime, thread, model, effort, "provider-session"))
                cur.execute(
                    "INSERT INTO web_apps (app_id, name, revision, agent_runtime, agent_model,"
                    " agent_effort, created_at, updated_at)"
                    " VALUES (%s, 'Sol app', 0, %s, 'gpt-6-sol', 'ultra', 'now', 'now')",
                    (f"app-{index}", runtime),
                )
                cur.execute(
                    "INSERT INTO schedules (id, thread_id, name, message, cadence, interval_minutes,"
                    " agent_runtime, model, effort, next_run_at, created_at, updated_at)"
                    " VALUES (%s, %s, 'Luna job', 'hello', 'interval', 60, %s,"
                    " 'gpt-6-luna', 'max', 'now', 'now', 'now')",
                    (index, f"schedule-{index}", runtime),
                )
        for runtime in runtimes:
            with self.subTest(runtime=runtime), self.assertRaises(Exception):
                with db.transaction() as cur:
                    cur.execute(
                        "INSERT INTO thread_sessions (agent_runtime, thread_id, model, effort)"
                        " VALUES (%s, 'invalid-luna', 'gpt-6-luna', 'ultra')", (runtime,),
                    )
        with db.transaction() as cur:
            cur.execute(
                "SELECT agent_runtime, thread_id, model, effort, provider_session_id"
                " FROM thread_sessions WHERE thread_id LIKE 'new-%'"
            )
            self.assertCountEqual(cur.fetchall(), expected)
            cur.execute("SELECT count(*) FROM thread_sessions WHERE thread_id LIKE 'old-%'")
            self.assertEqual(cur.fetchone(), (9,))
            cur.execute("SELECT model, effort FROM thread_sessions WHERE thread_id = 'thread-opus'")
            self.assertEqual(cur.fetchone(), ("claude-opus-5-5", "max"))
            cur.execute("SELECT agent_runtime, agent_model, agent_effort FROM web_apps")
            self.assertCountEqual(cur.fetchall(), [(r, "gpt-6-sol", "ultra") for r in runtimes])
            cur.execute("SELECT agent_runtime, model, effort FROM schedules")
            self.assertCountEqual(cur.fetchall(), [(r, "gpt-6-luna", "max") for r in runtimes])

    def test_glm_model_migration_allows_sessions(self) -> None:
        self.assertEqual(migrate.up(target=58, quiet=True), list(range(1, 59)))
        with self.assertRaises(Exception):
            with db.transaction() as cur:
                cur.execute(
                    "INSERT INTO thread_sessions"
                    " (agent_runtime, thread_id, model, effort)"
                    " VALUES ('hermes', 'thread-glm', 'zai.glm-5', 'high')"
                )

        self.assertEqual(migrate.up(target=59, quiet=True), [59])
        with db.transaction() as cur:
            cur.execute(
                "INSERT INTO web_apps"
                " (app_id, name, archived, revision, agent_runtime, agent_model, agent_effort,"
                " created_at, updated_at) VALUES"
                " ('app-59', 'GLM App', FALSE, 0, 'hermes', 'zai.glm-5', 'high',"
                " '2026-09-16T00:00:00Z', '2026-09-16T00:00:00Z')"
            )
            cur.execute(
                "INSERT INTO schedules"
                " (id, thread_id, name, message, cadence, interval_minutes, agent_runtime,"
                " model, effort, next_run_at, created_at, updated_at) VALUES"
                " (59, 'schedule-59', 'GLM Schedule', 'Run with GLM', 'interval', 60,"
                " 'hermes', 'zai.glm-5', 'high', '2026-09-16T01:00:00Z',"
                " '2026-09-16T00:00:00Z', '2026-09-16T00:00:00Z')"
            )
            cur.execute(
                "INSERT INTO schedule_revisions"
                " (schedule_id, revision, name, message, cadence, interval_minutes,"
                " agent_runtime, model, effort, deleted, actor, created_at) VALUES"
                " (59, 1, 'GLM Schedule', 'Run with GLM', 'interval', 60,"
                " 'hermes', 'zai.glm-5', 'high', FALSE, 'user',"
                " '2026-09-16T00:00:00Z')"
            )
            cur.execute(
                "INSERT INTO thread_sessions"
                " (agent_runtime, thread_id, provider_session_id, model, effort) VALUES"
                " ('hermes', 'thread-glm', 'provider-chat', 'zai.glm-5', 'high'),"
                " ('hermes', 'app-59', 'provider-app', 'zai.glm-5', 'high'),"
                " ('hermes', 'schedule-59', 'provider-schedule', 'zai.glm-5', 'high')"
            )
            cur.execute(
                "INSERT INTO agent_events"
                " (created_at, event_type, thread_id, message, source)"
                " VALUES ('2026-09-16T00:00:00Z', 'thread.message',"
                " 'thread-glm', 'GLM transcript', 'agent')"
            )

    def test_third_codex_migration_admits_the_runtime(self) -> None:
        self.assertEqual(migrate.up(target=62, quiet=True), list(range(1, 63)))
        with self.assertRaises(Exception):
            with db.transaction() as cur:
                cur.execute(
                    "INSERT INTO thread_sessions"
                    " (agent_runtime, thread_id, model, effort)"
                    " VALUES ('codex-3', 'thread-codex-3', 'gpt-5.6-sol', 'high')"
                )

        self.assertEqual(migrate.up(target=63, quiet=True), [63])
        with db.transaction() as cur:
            cur.execute(
                "INSERT INTO web_apps"
                " (app_id, name, archived, revision, agent_runtime, agent_model, agent_effort,"
                " created_at, updated_at) VALUES"
                " ('app-62', 'Codex 3 App', FALSE, 0, 'codex-3', 'gpt-5.6-sol', 'high',"
                " '2026-09-20T00:00:00Z', '2026-09-20T00:00:00Z')"
            )
            cur.execute(
                "INSERT INTO schedules"
                " (id, thread_id, name, message, cadence, interval_minutes, agent_runtime,"
                " model, effort, next_run_at, created_at, updated_at) VALUES"
                " (62, 'schedule-62', 'Codex 3 Schedule', 'Run on Codex 3', 'interval', 60,"
                " 'codex-3', 'gpt-5.6-sol', 'high', '2026-09-20T01:00:00Z',"
                " '2026-09-20T00:00:00Z', '2026-09-20T00:00:00Z')"
            )
            cur.execute(
                "INSERT INTO thread_sessions"
                " (agent_runtime, thread_id, provider_session_id, model, effort) VALUES"
                " ('codex-3', 'thread-codex-3', 'provider-chat', 'gpt-5.6-sol', 'high'),"
                " ('codex-3', 'app-62', 'provider-app', 'gpt-5.6-sol', 'high'),"
                " ('codex-3', 'schedule-62', 'provider-schedule', 'gpt-5.6-sol', 'high')"
            )
            cur.execute(
                "INSERT INTO agent_events"
                " (created_at, event_type, thread_id, message, source)"
                " VALUES ('2026-09-20T00:00:00Z', 'thread.message',"
                " 'thread-codex-3', 'Codex 3 transcript', 'agent')"
            )
            cur.execute(
                "INSERT INTO provider_accounts (provider, account_id)"
                " VALUES ('openai-3', 'acct-3')"
            )
            cur.execute(
                "INSERT INTO proxy_provider_pins (provider, account_id)"
                " VALUES ('openai-3', 'acct-3')"
            )

    def test_opus_5_5_migration_preserves_existing_sessions(self) -> None:
        self.assertEqual(migrate.up(target=67, quiet=True), list(range(1, 68)))
        with self.assertRaises(Exception):
            with db.transaction() as cur:
                cur.execute("INSERT INTO thread_sessions (agent_runtime, thread_id, model, effort) "
                            "VALUES ('claude_code', 'thread-new', 'claude-opus-5-5', 'high')")
        old_rows = [
            ("claude_code", "thread-old-opus", "claude-opus-5", "high"),
            ("claude_code", "thread-fable", "claude-fable-5-1", "ultracode"),
            ("claude_code", "thread-sonnet", "claude-sonnet-5", "max"),
            ("claude_code", "thread-alias", "opus", "high"),
            ("codex-3", "thread-codex", "gpt-6-astra", "ultra"),
            ("grok-2", "thread-grok", "grok-4.6", "xhigh"),
            ("hermes", "thread-hermes", "zai.glm-5", "high"),
            ("script", "thread-script", "bash", "fixed"),
        ]
        with db.transaction() as cur:
            for row in old_rows:
                cur.execute("INSERT INTO thread_sessions (agent_runtime, thread_id, model, effort, provider_session_id) "
                            "VALUES (%s, %s, %s, %s, 'old-provider')", row)
        self.assertEqual(migrate.up(target=68, quiet=True), [68])
        with db.transaction() as cur:
            for effort in ("high", "max", "ultracode"):
                cur.execute("INSERT INTO thread_sessions (agent_runtime, thread_id, model, effort, provider_session_id) "
                            "VALUES ('claude_code', %s, 'claude-opus-5-5', %s, 'new-provider')",
                            (f"thread-new-{effort}", effort))
                cur.execute("INSERT INTO agent_events (created_at, event_type, thread_id, message, source) "
                            "VALUES ('2026-09-22T00:00:00Z', 'thread.message', %s, 'preserved transcript', 'agent')",
                            (f"thread-new-{effort}",))
        for runtime, model, effort in (("claude_code", "claude-opus-5-5", "ultra"),
                                       ("codex", "claude-opus-5-5", "high"),
                                       ("codex", "gpt-5.6-luna", "ultra")):
            with self.subTest(runtime=runtime, model=model, effort=effort), self.assertRaises(Exception):
                with db.transaction() as cur:
                    cur.execute("INSERT INTO thread_sessions (agent_runtime, thread_id, model, effort) "
                                "VALUES (%s, 'thread-invalid', %s, %s)", (runtime, model, effort))
        with db.transaction() as cur:
            cur.execute("SELECT agent_runtime, thread_id, model, effort, provider_session_id "
                        "FROM thread_sessions WHERE thread_id NOT LIKE 'thread-new-%'")
            self.assertCountEqual(cur.fetchall(), [(*row, "old-provider") for row in old_rows])

    def test_persistent_schedules_create_threads_and_drop_old_runs(self) -> None:
        migrate.up(target=46, quiet=True)
        with db.transaction() as cur:
            cur.execute(
                "INSERT INTO schedules"
                " (id, name, message, cadence, interval_minutes, agent_runtime, model, effort,"
                " next_run_at, created_at, updated_at) VALUES"
                " (11, 'Agent', 'Review work', 'interval', 60, 'codex',"
                " 'gpt-5.6-terra', 'high', '2026-09-03T09:00:00Z',"
                " '2026-09-02T09:00:00Z', '2026-09-02T09:00:00Z'),"
                " (12, 'Script', '/mnt/kern-agent/agent-home/job.sh', 'interval', 60,"
                " 'script', 'bash', 'fixed', '2026-09-03T09:00:00Z',"
                " '2026-09-02T09:00:00Z', '2026-09-02T09:00:00Z')"
            )
            cur.execute(
                "INSERT INTO schedule_runs"
                " (schedule_id, thread_id, message, agent_runtime, model, effort, status, scheduled_for)"
                " VALUES"
                " (11, 'schedule-11-run-1', 'Review work', 'codex',"
                " 'gpt-5.6-terra', 'high', 'succeeded', '2026-09-02T08:00:00Z'),"
                " (12, 'schedule-12-run-2', '/mnt/kern-agent/agent-home/job.sh',"
                " 'script', 'bash', 'fixed', 'succeeded', '2026-09-02T08:00:00Z'),"
                " (11, 'schedule-11-run-3', '/mnt/kern-agent/agent-home/old-job.sh',"
                " 'script', 'bash', 'fixed', 'succeeded', '2026-09-02T07:00:00Z'),"
                " (12, 'schedule-12-run-4', 'Old model work', 'codex',"
                " 'gpt-5.6-terra', 'high', 'succeeded', '2026-09-02T07:00:00Z')"
            )
            cur.execute(
                "INSERT INTO thread_sessions"
                " (agent_runtime, thread_id, model, effort) VALUES"
                " ('codex', 'schedule-11-run-1', 'gpt-5.6-terra', 'high'),"
                " ('script', 'schedule-12-run-2', 'bash', 'fixed'),"
                " ('script', 'schedule-11-run-3', 'bash', 'fixed'),"
                " ('codex', 'schedule-12-run-4', 'gpt-5.6-terra', 'high'),"
                " ('codex', 'schedule-99-run-99', 'gpt-5.6-terra', 'high')"
            )
            cur.execute(
                "INSERT INTO agent_events"
                " (created_at, event_type, thread_id, message, source) VALUES"
                " ('2026-09-02T08:00:00Z', 'thread.message',"
                "  'schedule-11-run-1', 'old model run', 'user'),"
                " ('2026-09-02T08:00:00Z', 'thread.message',"
                "  'schedule-12-run-2', 'old script run', 'user'),"
                " ('2026-09-02T07:00:00Z', 'thread.message',"
                "  'schedule-11-run-3', 'old script run after kind change', 'user'),"
                " ('2026-09-02T07:00:00Z', 'thread.message',"
                "  'schedule-12-run-4', 'old model run after kind change', 'user'),"
                " ('2026-09-02T06:00:00Z', 'thread.message',"
                "  'schedule-99-run-99', 'old retained run without metadata', 'agent')"
            )

        self.assertEqual(migrate.up(target=50, quiet=True), [47, 48, 49, 50])
        with db.transaction() as cur:
            cur.execute("SELECT id, thread_id FROM schedules ORDER BY id")
            self.assertEqual(
                cur.fetchall(),
                [(11, "schedule-11"), (12, "schedule-12")],
            )
            cur.execute("SELECT thread_id, name FROM chat_threads ORDER BY thread_id")
            self.assertEqual(cur.fetchall(), [])
            cur.execute(
                "SELECT conname FROM pg_constraint"
                " WHERE conrelid = 'schedules'::regclass"
                " AND conname IN"
                " ('schedules_thread_id_check', 'schedules_thread_id_fkey')"
            )
            self.assertEqual(cur.fetchall(), [("schedules_thread_id_check",)])
            cur.execute("SELECT to_regclass('public.schedule_runs')")
            self.assertEqual(cur.fetchone(), (None,))
            cur.execute("SELECT thread_id FROM thread_sessions ORDER BY thread_id")
            self.assertEqual(cur.fetchall(), [])
            cur.execute(
                "SELECT thread_id, message FROM agent_events ORDER BY thread_id"
            )
            self.assertEqual(cur.fetchall(), [])
            cur.execute(
                "INSERT INTO thread_sessions"
                " (agent_runtime, thread_id, model, effort)"
                " VALUES ('script', 'schedule-12', 'bash', 'fixed')"
            )
            cur.execute(
                "INSERT INTO agent_events"
                " (created_at, event_type, thread_id, message, source)"
                " VALUES ('2026-09-02T09:00:00Z', 'thread.message',"
                " 'schedule-12', 'This is an automated trigger.', 'user')"
            )
            cur.execute(
                "INSERT INTO workspace_seen"
                " (item_kind, item_id, message_seq, revision)"
                " VALUES ('chat', 'schedule-11', 7, 0)"
            )

    def test_connection_profiles_preserve_oauth_and_enable_only_approvals(self) -> None:
        migrate.up(target=44, quiet=True)
        with db.transaction() as cur:
            cur.execute(
                "INSERT INTO tool_credentials"
                " (tool_id, account_id, account_label, account_scopes, secret, metadata)"
                " VALUES ('gmail', 'google-sub', 'person@example.com', '[]'::jsonb,"
                " 'encrypted', '{}'::jsonb)"
            )
            for tool_id in ("gmail", "reddit"):
                cur.execute(
                    "INSERT INTO tool_approvals"
                    " (tool_id, action_id, status, summary, payload, check_token, created_at)"
                    " VALUES (%s, 'publish', 'pending', 'Publish.', '{}'::jsonb, %s, 1)",
                    (tool_id, f"{tool_id}-" + "x" * 32),
                )

        self.assertEqual(migrate.up(target=45, quiet=True), [45])
        with db.transaction() as cur:
            cur.execute(
                "SELECT tool_id, connection_id, account_id, account_label"
                " FROM tool_approvals ORDER BY tool_id"
            )
            approvals = cur.fetchall()

        self.assertEqual(
            approvals,
            [
                ("gmail", "default", "", ""),
                ("reddit", "", "", ""),
            ],
        )

    def test_web_app_agent_settings_backfill_linked_sessions_and_require_values(self) -> None:
        self.assertEqual(migrate.up(target=47, quiet=True), list(range(1, 48)))
        with db.transaction() as cur:
            cur.execute(
                "INSERT INTO web_apps"
                " (app_id, name, archived, revision, created_at, updated_at) VALUES"
                " ('app-1', 'Linked', FALSE, 0, '2026-01-01T00:00:00Z',"
                "  '2026-01-01T00:00:00Z'),"
                " ('app-2', 'Sessionless', TRUE, 0, '2026-01-01T00:00:00Z',"
                "  '2026-01-01T00:00:00Z')"
            )
            cur.execute(
                "INSERT INTO thread_sessions"
                " (agent_runtime, thread_id, model, effort) VALUES"
                " ('codex', 'app-1', 'gpt-5.6-terra', 'max')"
            )

        self.assertEqual(migrate.up(target=48, quiet=True), [48])
        with db.transaction() as cur:
            cur.execute(
                "SELECT app_id, agent_runtime, agent_model, agent_effort"
                " FROM web_apps ORDER BY app_id"
            )
            self.assertEqual(
                cur.fetchall(),
                [
                    ("app-1", "codex", "gpt-5.6-terra", "max"),
                    ("app-2", "codex", "gpt-5.6-sol", "high"),
                ],
            )
            cur.execute(
                "SELECT column_name, is_nullable FROM information_schema.columns"
                " WHERE table_schema = 'public' AND table_name = 'web_apps'"
                " AND column_name IN ('agent_runtime', 'agent_model', 'agent_effort')"
                " ORDER BY column_name"
            )
            self.assertEqual(
                cur.fetchall(),
                [
                    ("agent_effort", "NO"),
                    ("agent_model", "NO"),
                    ("agent_runtime", "NO"),
                ],
            )

    def test_onboarding_dismissal_admits_a_single_row(self) -> None:
        self.assertEqual(migrate.up(target=41, quiet=True), list(range(1, 42)))
        with db.transaction() as cur:
            # A host that has never dismissed the checklist starts with no row,
            # and the singleton key admits only one.
            cur.execute("SELECT count(*) FROM workspace_onboarding_dismissal")
            self.assertEqual(cur.fetchone()[0], 0)
            for _ in range(2):
                cur.execute(
                    "INSERT INTO workspace_onboarding_dismissal (singleton)"
                    " VALUES (TRUE) ON CONFLICT (singleton) DO NOTHING"
                )
            cur.execute("SELECT count(*) FROM workspace_onboarding_dismissal")
            self.assertEqual(cur.fetchone()[0], 1)

    def test_agent_history_counters_seed_retained_state(self) -> None:
        self.assertEqual(migrate.up(target=29, quiet=True), list(range(1, 30)))
        with db.transaction() as cur:
            cur.execute(
                "INSERT INTO thread_sessions"
                " (agent_runtime, thread_id, model, effort) VALUES"
                " ('codex', 'thread-1', 'gpt-5.6-terra', 'high'),"
                " ('claude_code', 'thread-2', 'sonnet', 'max')"
            )
            cur.execute(
                "INSERT INTO agent_events"
                " (created_at, event_type, thread_id, message, source, activity) VALUES"
                " ('2026-08-08T00:00:00Z', 'thread.message', 'thread-1', 'one', 'user', NULL),"
                " ('2026-08-08T00:00:01Z', 'thread.message', 'thread-1', 'two', 'agent', NULL),"
                " ('2026-08-08T00:00:02Z', 'thread.activity', 'thread-2', NULL, NULL, '{}'::jsonb),"
                " ('2026-08-08T00:00:03Z', 'thread.error', 'thread-2', NULL, NULL, NULL)"
            )

        self.assertEqual(migrate.up(target=30, quiet=True), [30])
        with db.transaction() as cur:
            cur.execute(
                "SELECT name, value FROM counters"
                " WHERE name LIKE 'agent_history_%' ORDER BY name"
            )
            self.assertEqual(
                cur.fetchall(),
                [
                    ("agent_history_activities", 1),
                    ("agent_history_messages", 2),
                    ("agent_history_threads", 2),
                ],
            )

    def test_agent_stats_split_user_messages_from_agent_activity(self) -> None:
        self.assertEqual(migrate.up(target=29, quiet=True), list(range(1, 30)))
        with db.transaction() as cur:
            cur.execute(
                "INSERT INTO thread_sessions"
                " (agent_runtime, thread_id, model, effort) VALUES"
                " ('codex', 'thread-1', 'gpt-5.6-terra', 'high')"
            )
            cur.execute(
                "INSERT INTO agent_events"
                " (created_at, event_type, thread_id, message, source, activity) VALUES"
                " ('2026-08-08T00:00:00Z', 'thread.message', 'thread-1', 'one', 'user', NULL),"
                " ('2026-08-08T00:00:01Z', 'thread.message', 'thread-1', 'two', 'user', NULL),"
                " ('2026-08-08T00:00:02Z', 'thread.message', 'thread-1', 'reply', 'agent', NULL),"
                " ('2026-08-08T00:00:03Z', 'thread.activity', 'thread-1', NULL, NULL, '{}'::jsonb),"
                " ('2026-08-08T00:00:04Z', 'thread.error', 'thread-1', NULL, NULL, NULL)"
            )

        self.assertEqual(migrate.up(target=31, quiet=True), [30, 31])
        with db.transaction() as cur:
            cur.execute(
                "SELECT name, value FROM counters"
                " WHERE name IN ('agent_history_messages', 'agent_history_activities')"
                " ORDER BY name"
            )
            self.assertEqual(
                cur.fetchall(),
                [("agent_history_activities", 1), ("agent_history_messages", 3)],
            )

        self.assertEqual(migrate.up(target=32, quiet=True), [32])
        with db.transaction() as cur:
            cur.execute(
                "SELECT name, value FROM counters"
                " WHERE name IN ('agent_history_messages', 'agent_history_activities')"
                " ORDER BY name"
            )
            self.assertEqual(
                cur.fetchall(),
                [("agent_history_activities", 2), ("agent_history_messages", 2)],
            )

    def test_product_thread_id_migration_drops_old_sessions_and_events(self) -> None:
        self.assertEqual(migrate.up(target=33, quiet=True), list(range(1, 34)))
        with db.transaction() as cur:
            cur.execute(
                "INSERT INTO thread_sessions"
                " (agent_runtime, thread_id, model, effort) VALUES"
                " ('codex', 'thread-retained', 'gpt-5.6-terra', 'high'),"
                " ('codex', 'admin-owned', 'gpt-5.6-terra', 'high'),"
                " ('codex', %s, 'gpt-5.6-terra', 'high')",
                ("thread-" + "x" * 58,),
            )
            cur.execute(
                "INSERT INTO agent_events"
                " (created_at, event_type, thread_id, message, source) VALUES"
                " ('2026-08-10T00:00:00Z', 'thread.message',"
                "  'thread-retained', 'keep', 'user'),"
                " ('2026-08-10T00:00:01Z', 'thread.message',"
                "  'admin-owned', 'drop session history', 'agent'),"
                " ('2026-08-10T00:00:02Z', 'thread.message',"
                "  'orphaned-old-id', 'drop orphaned history', 'agent'),"
                " ('2026-08-10T00:00:03Z', 'agent_runtime.started',"
                "  NULL, NULL, NULL)"
            )

        self.assertEqual(migrate.up(target=34, quiet=True), [34])
        with db.transaction() as cur:
            cur.execute("SELECT thread_id FROM thread_sessions ORDER BY thread_id")
            self.assertEqual(cur.fetchall(), [("thread-retained",)])
            cur.execute("SELECT thread_id FROM agent_events ORDER BY seq")
            self.assertEqual(cur.fetchall(), [("thread-retained",), (None,)])

    def test_global_resource_migration_is_bounded_deterministic_and_drops_unconfigured_schedules(self) -> None:
        self.assertEqual(migrate.up(target=26, quiet=True), list(range(1, 27)))
        with db.transaction() as cur:
            cur.execute(
                "INSERT INTO web_apps"
                " (app_id, name, archived, created_at, updated_at) VALUES"
                " ('app-1', 'One', FALSE, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'),"
                " ('app-2', 'Two', TRUE, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'),"
                " ('app-3', 'Three', FALSE, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'),"
                " ('app-4', 'Four', FALSE, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'),"
                " ('app-10', 'Ten', FALSE, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
            )
            cur.execute(
                "INSERT INTO thread_sessions"
                " (agent_runtime, thread_id, model, effort) VALUES"
                " ('codex', 'app-1', 'gpt-5.6-terra', 'high'),"
                " ('claude_code', 'app-2', 'sonnet', 'max'),"
                " ('claude_code', 'app-4', 'sonnet', 'max')"
            )
            cur.execute(
                "INSERT INTO web_app_memories"
                " (app_id, name, description, body_md, updated_by, created_at, updated_at)"
                " VALUES"
                " ('app-1', 'shared', 'older', 'old', 'agent',"
                "  '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'),"
                " ('app-2', 'shared', %s, %s, 'user',"
                "  '2026-01-02T00:00:00Z', '2026-01-03T00:00:00Z'),"
                " ('app-10', 'shared', 'lexical loser', 'wrong body', 'user',"
                "  '2026-01-02T00:00:00Z', '2026-01-03T00:00:00Z'),"
                " ('app-3', 'other', 'other page', 'other body', 'user',"
                "  '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')",
                ("winner\n" + "d" * 120, "x" * 1200),
            )
            cur.execute(
                "INSERT INTO web_app_schedules"
                " (id, app_id, name, message, cadence, interval_minutes, daily_time,"
                "  enabled, created_by, last_run_at, next_run_at, created_at, updated_at)"
                " VALUES"
                " (11, 'app-1', E'Run\\nOne', 'do one', 'interval', 60, NULL,"
                "  TRUE, 'user', NULL, '2026-01-01T00:00:00Z',"
                "  '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'),"
                " (12, 'app-2', 'Run Two', 'do two', 'daily', NULL, '09:30',"
                "  TRUE, 'agent', NULL, '2026-01-01T00:00:00Z',"
                "  '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'),"
                " (13, 'app-3', 'No runtime', 'drop me', 'interval', 30, NULL,"
                "  TRUE, 'user', NULL, '2026-01-01T00:00:00Z',"
                "  '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'),"
                " (14, 'app-4', 'Retired runtime', 'configure me', 'interval', 30, NULL,"
                "  FALSE, 'user', NULL, '2026-01-01T00:00:00Z',"
                "  '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
            )
            cur.execute(
                "INSERT INTO web_app_history"
                " (app_id, kind, actor, ui_revision, data_version, entry_json, created_at)"
                " VALUES"
                " ('app-1', 'memory', 'user', 0, 0, '{}', '2026-01-01T00:00:00Z'),"
                " ('app-1', 'ui', 'user', 0, 0, '{}', '2026-01-01T00:00:00Z')"
            )

        self.assertEqual(migrate.up(target=27, quiet=True), [27])

        with db.transaction() as cur:
            cur.execute(
                "SELECT page_id, description, content, revision, created_by, updated_by"
                " FROM memory_pages ORDER BY page_id"
            )
            pages = cur.fetchall()
            self.assertEqual(pages[0], ("other", "other page", "other body", 1, "migration", "migration"))
            self.assertEqual(pages[1][0], "shared")
            self.assertEqual(pages[1][1], ("winner " + "d" * 120)[:100])
            self.assertEqual(pages[1][2], "x" * 1000)
            self.assertEqual(pages[1][3:], (1, "migration", "migration"))
            cur.execute(
                "SELECT page_id, revision, actor FROM memory_page_revisions"
                " ORDER BY page_id"
            )
            self.assertEqual(
                cur.fetchall(),
                [("other", 1, "migration"), ("shared", 1, "migration")],
            )
            cur.execute(
                "SELECT id, name, message, agent_runtime, model, effort,"
                " deleted_at IS NOT NULL, next_run_at"
                " FROM schedules ORDER BY id"
            )
            self.assertEqual(
                cur.fetchall(),
                [
                    (11, "Run One", "Target Web App: app-1\n\ndo one", "codex", "gpt-5.6-terra", "high", False, "2026-01-01T00:00:00Z"),
                    (12, "Run Two", "Target Web App: app-2\n\ndo two", "claude_code", "sonnet", "max", True, "2026-01-01T00:00:00Z"),
                    (14, "Retired runtime", "Target Web App: app-4\n\nconfigure me", "claude_code", "sonnet", "max", True, "2026-01-01T00:00:00Z"),
                ],
            )
            cur.execute("SELECT schedule_id, revision, actor FROM schedule_revisions ORDER BY schedule_id")
            self.assertEqual(
                cur.fetchall(),
                [(11, 1, "migration"), (12, 1, "migration"), (14, 1, "migration")],
            )
            cur.execute("SELECT last_value, is_called FROM schedule_runs_id_seq")
            self.assertEqual(cur.fetchone(), (1, False))
            cur.execute("SELECT last_value, is_called FROM schedules_id_seq")
            self.assertEqual(cur.fetchone(), (15, False))
            cur.execute("SELECT kind FROM web_app_history ORDER BY id")
            self.assertEqual(cur.fetchall(), [("ui",)])
            cur.execute(
                "SELECT to_regclass('public.web_app_memories'),"
                " to_regclass('public.web_app_schedules'),"
                " EXISTS (SELECT 1 FROM information_schema.columns"
                " WHERE table_schema = 'public' AND table_name = 'web_apps'"
                " AND column_name = 'instructions_md')"
            )
            self.assertEqual(cur.fetchone(), (None, None, False))

    def test_workspace_resource_limit_migration_expands_bounds(self) -> None:
        self.assertEqual(migrate.up(target=37, quiet=True), list(range(1, 38)))
        self.assertEqual(migrate.up(target=38, quiet=True), [38])

        with db.transaction() as cur:
            cur.execute(
                "INSERT INTO memory_pages"
                " (page_id, description, content, created_by, updated_by, created_at, updated_at)"
                " VALUES ('boundary', 'Boundary page', %s, 'user', 'user',"
                " '2026-08-18T00:00:00Z', '2026-08-18T00:00:00Z')",
                ("m" * 2000,),
            )
            cur.execute(
                "INSERT INTO memory_page_revisions"
                " (page_id, revision, description, content, deleted, actor, created_at)"
                " VALUES ('boundary', 1, 'Boundary page', %s, FALSE, 'user',"
                " '2026-08-18T00:00:00Z')",
                ("r" * 2000,),
            )
            cur.execute(
                "INSERT INTO schedules"
                " (name, message, cadence, interval_minutes, agent_runtime, model, effort,"
                " next_run_at, created_at, updated_at)"
                " VALUES ('Boundary schedule', %s, 'interval', 60, 'codex',"
                " 'gpt-5.6-terra', 'high', '2026-08-18T01:00:00Z',"
                " '2026-08-18T00:00:00Z', '2026-08-18T00:00:00Z') RETURNING id",
                ("s" * 12000,),
            )
            schedule_id = int(cur.fetchone()[0])
            cur.execute(
                "INSERT INTO schedule_revisions"
                " (schedule_id, revision, name, message, cadence, interval_minutes,"
                " agent_runtime, model, effort, deleted, actor, created_at)"
                " VALUES (%s, 1, 'Boundary schedule', %s, 'interval', 60, 'codex',"
                " 'gpt-5.6-terra', 'high', FALSE, 'user', '2026-08-18T00:00:00Z')",
                (schedule_id, "v" * 12000),
            )
            cur.execute(
                "INSERT INTO schedule_runs"
                " (schedule_id, thread_id, message, agent_runtime, model, effort,"
                " status, scheduled_for)"
                " VALUES (%s, %s, %s, 'codex', 'gpt-5.6-terra', 'high',"
                " 'succeeded', '2026-08-18T01:00:00Z')",
                (schedule_id, f"schedule-{schedule_id}-run-1", "u" * 12000),
            )

    def test_xai_migration_removes_custom_rules_for_the_newly_owned_apexes(self) -> None:
        # Reserving an apex makes any stored custom rule beneath it invalid at
        # parse time, and the proxy answers a policy it cannot parse by denying
        # every request. An upgraded host carrying such a rule would therefore
        # lose all agent egress, not just xAI traffic, so the migration clears
        # them as part of taking ownership.
        xai_version = next(
            item.version for item in migrate.load_migrations()
            if item.name == "xai_integration"
        )
        self.assertEqual(
            migrate.up(target=xai_version - 1, quiet=True),
            list(range(1, xai_version)),
        )
        removed = (
            "x.ai",
            "api.x.ai",
            "grok.com",
            "cli-chat-proxy.grok.com",
            "*.x.ai",
            "*.grok.com",
            "*.ai",
            "*.com",
        )
        retained = ("example.com", "*.example.com", "notx.ai", "mygrok.com")
        with db.transaction() as cur:
            for domain in removed + retained:
                cur.execute("INSERT INTO allowed_domains (domain) VALUES (%s)", (domain,))
                cur.execute(
                    "INSERT INTO domain_methods (domain, position, method)"
                    " VALUES (%s, 0, 'GET')",
                    (domain,),
                )

        self.assertEqual(migrate.up(target=xai_version, quiet=True), [xai_version])
        with db.transaction() as cur:
            cur.execute("SELECT domain FROM allowed_domains ORDER BY domain")
            self.assertEqual([domain for (domain,) in cur.fetchall()], sorted(retained))
            # The cascade takes each deleted domain's method rows with it.
            cur.execute("SELECT domain FROM domain_methods ORDER BY domain")
            self.assertEqual([domain for (domain,) in cur.fetchall()], sorted(retained))

    def test_github_actions_blob_migration_removes_overlapping_custom_domains(self) -> None:
        self.assertEqual(migrate.up(target=2, quiet=True), [1, 2])
        removed = (
            "blob.core.windows.net",
            "productionresultssa17.blob.core.windows.net",
            "*.blob.core.windows.net",
            "*.core.windows.net",
            "*.windows.net",
        )
        retained = ("example.com", "*.example.com", "other.core.windows.net")
        with db.transaction() as cur:
            for domain in removed + retained:
                cur.execute(
                    "INSERT INTO allowed_domains (domain) VALUES (%s)",
                    (domain,),
                )
                cur.execute(
                    "INSERT INTO domain_methods "
                    "(domain, position, method) VALUES (%s, 0, 'GET')",
                    (domain,),
                )

        self.assertEqual(migrate.up(target=3, quiet=True), [3])
        with db.transaction() as cur:
            cur.execute("SELECT domain FROM allowed_domains ORDER BY domain")
            self.assertEqual(
                [domain for (domain,) in cur.fetchall()],
                sorted(retained),
            )
            cur.execute("SELECT domain FROM domain_methods ORDER BY domain")
            self.assertEqual(
                [domain for (domain,) in cur.fetchall()],
                sorted(retained),
            )

    def test_thread_only_migration_moves_task_events_onto_threads(self) -> None:
        self.assertEqual(migrate.up(target=4, quiet=True), [1, 2, 3, 4])
        with db.transaction() as cur:
            cur.execute(
                "INSERT INTO thread_sessions (agent_runtime, thread_id, model, effort)"
                " VALUES ('codex', 'chat', 'gpt-5.6-terra', 'high')"
            )
            cur.execute("INSERT INTO tasks (number, status, thread_id) VALUES (1, 'completed', 'chat')")
            cur.execute(
                "INSERT INTO agent_events (created_at, event_type, task_id) VALUES"
                " ('2026-06-08T00:00:00Z', 'task.started', 'task_1'),"
                " ('2026-06-08T00:00:01Z', 'task.completed', 'task_1'),"
                " ('2026-06-08T00:00:02Z', 'task.message', 'task_99'),"
                " ('2026-06-08T00:00:03Z', 'agent_runtime.started', NULL)"
            )
            cur.execute("INSERT INTO counters (name, value) VALUES ('next_task_number', 2)")

        self.assertEqual(migrate.up(target=5, quiet=True), [5])

        with db.transaction() as cur:
            cur.execute("SELECT event_type, thread_id FROM agent_events ORDER BY seq")
            self.assertEqual(
                cur.fetchall(),
                [
                    ("turn.started", "chat"),
                    ("turn.completed", "chat"),
                    # A pruned task's events stay in the global log,
                    # unattributed to any thread.
                    ("turn.message", None),
                    ("agent_runtime.started", None),
                ],
            )
            cur.execute("SELECT value FROM counters WHERE name = 'next_task_number'")
            self.assertEqual(cur.fetchall(), [])
        tables = self.table_names()
        self.assertNotIn("tasks", tables)
        self.assertNotIn("task_steers", tables)

    def test_thread_event_stream_migration_flattens_history_and_recovers_open_run(self) -> None:
        self.assertEqual(migrate.up(target=5, quiet=True), [1, 2, 3, 4, 5])
        with db.transaction() as cur:
            for thread_id in ("closed", "open"):
                cur.execute(
                    "INSERT INTO thread_sessions"
                    " (agent_runtime, thread_id, model, effort)"
                    " VALUES ('codex', %s, 'gpt-5.6-luna', 'high')",
                    (thread_id,),
                )
            cur.execute(
                "INSERT INTO agent_events"
                " (created_at, event_type, thread_id, message, source) VALUES"
                " ('2026-07-01T00:00:00Z', 'turn.started', 'closed', NULL, NULL)"
                " RETURNING seq"
            )
            first_closed_run = int(cur.fetchone()[0])
            cur.execute(
                "INSERT INTO agent_events"
                " (created_at, event_type, thread_id, message, source) VALUES"
                " ('2026-07-01T00:00:01Z', 'turn.message', 'closed', 'hello', 'user')"
            )
            cur.execute(
                "INSERT INTO agent_events"
                " (created_at, event_type, thread_id, activity) VALUES"
                " ('2026-07-01T00:00:02Z', 'turn.activity', 'closed',"
                " '{\"activity_id\":\"command-1\"}'::jsonb)"
            )
            cur.execute(
                "INSERT INTO agent_events"
                " (created_at, event_type, thread_id) VALUES"
                " ('2026-07-01T00:00:03Z', 'turn.completed', 'closed')"
            )
            cur.execute(
                "INSERT INTO agent_events"
                " (created_at, event_type, thread_id) VALUES"
                " ('2026-07-01T00:00:04Z', 'turn.started', 'closed')"
                " RETURNING seq"
            )
            second_closed_run = int(cur.fetchone()[0])
            cur.execute(
                "INSERT INTO agent_events"
                " (created_at, event_type, thread_id, error_message) VALUES"
                " ('2026-07-01T00:00:05Z', 'turn.failed', 'closed', 'failed')"
            )
            cur.execute(
                "INSERT INTO agent_events"
                " (created_at, event_type, thread_id) VALUES"
                " ('2026-07-01T00:00:06Z', 'turn.started', 'open')"
                " RETURNING seq"
            )
            open_run = int(cur.fetchone()[0])
            cur.execute(
                "INSERT INTO agent_events"
                " (created_at, event_type, thread_id, message, source) VALUES"
                " ('2026-07-01T00:00:07Z', 'turn.message', 'open', 'pending', 'user')"
            )
            cur.execute(
                "INSERT INTO agent_events"
                " (created_at, event_type, thread_id, message, source) VALUES"
                " ('2026-07-01T00:00:08Z', 'turn.message', NULL, 'global', 'agent')"
            )

        self.assertEqual(migrate.up(target=7, quiet=True), [6, 7])

        with db.transaction() as cur:
            cur.execute(
                "SELECT pg_get_indexdef(indexrelid)"
                " FROM pg_index"
                " WHERE indexrelid = 'thread_sessions_recency_page_idx'::regclass"
            )
            index_definition = str(cur.fetchone()[0])
            self.assertIn(
                "(COALESCE(last_used_at, ''::text) DESC, thread_id DESC)",
                index_definition,
            )
            self.assertNotIn("agent_runtime", index_definition)
            cur.execute(
                "SELECT event_type, thread_id, run_number"
                " FROM agent_events ORDER BY seq"
            )
            self.assertEqual(
                cur.fetchall(),
                [
                    ("thread.message", "closed", first_closed_run),
                    ("thread.activity", "closed", first_closed_run),
                    ("thread.error", "closed", second_closed_run),
                    ("thread.message", "open", open_run),
                    ("thread.message", None, None),
                ],
            )
            cur.execute(
                "SELECT thread_id, run_status, run_number"
                " FROM thread_sessions ORDER BY thread_id"
            )
            self.assertEqual(
                cur.fetchall(),
                [
                    ("closed", "idle", second_closed_run),
                    ("open", "running", open_run),
                ],
            )

    def test_workspace_migration_adopts_legacy_ledger_and_direct_ids(self) -> None:
        self.assertEqual(migrate.up(target=12, quiet=True), list(range(1, 13)))
        with db.transaction() as cur:
            cur.execute(
                "INSERT INTO thread_sessions"
                " (agent_runtime, thread_id, provider_session_id, model, effort) VALUES"
                " ('hermes', 'personal_web_app_builder__app-1', 'old-hermes',"
                "  'deepseek.v3.2', 'high'),"
                " ('codex', 'personal_web_app_builder__app-2', 'old-codex',"
                "  'gpt-5.6-terra', 'high'),"
                " ('hermes', 'agent_chat__thread-1', 'old-chat',"
                "  'deepseek.v3.2', 'high')"
            )
            cur.execute(
                "INSERT INTO app_schema_migrations (app_id, version, name) VALUES"
                " ('agent_chat', 1, 'baseline'),"
                " ('agent_chat', 2, 'thread_names'),"
                " ('agent_chat', 3, 'drop_thread_tasks'),"
                " ('personal_web_app_builder', 1, 'app_state'),"
                " ('personal_web_app_builder', 2, 'builder_thread_reset'),"
                " ('personal_web_app_builder', 3, 'multiple_web_apps'),"
                " ('personal_web_app_builder', 4, 'workspace_platform'),"
                " ('personal_web_app_builder', 5, 'remove_archiving'),"
                " ('personal_web_app_builder', 6, 'memory_revision'),"
                " ('alpha_seeker', 1, 'baseline')"
            )

        self.assertEqual(migrate.up(target=14, quiet=True), [13, 14])

        with db.transaction() as cur:
            cur.execute(
                "SELECT thread_id, provider_session_id FROM thread_sessions"
                " ORDER BY thread_id"
            )
            self.assertEqual(
                cur.fetchall(),
                [
                    ("app-1", "old-hermes"),
                    ("app-2", "old-codex"),
                    ("thread-1", "old-chat"),
                ],
            )
            cur.execute(
                "SELECT workspace_kind, version, name FROM workspace_migrations"
                " ORDER BY workspace_kind"
            )
            self.assertEqual(
                cur.fetchall(),
                [
                    ("chat", 1, "baseline"),
                    ("chat", 2, "thread_names"),
                    ("chat", 3, "drop_thread_tasks"),
                    ("web_apps", 1, "app_state"),
                    ("web_apps", 2, "builder_thread_reset"),
                    ("web_apps", 3, "multiple_web_apps"),
                    ("web_apps", 4, "workspace_platform"),
                    ("web_apps", 5, "remove_archiving"),
                    ("web_apps", 6, "memory_revision"),
                ],
            )

    def test_every_partial_workspace_history_upgrades_and_repeat_adoption_is_a_noop(self) -> None:
        migrations = {item.version: item for item in migrate.load_migrations()}
        # Derived, not hard-coded: this asserts that consolidation leaves the
        # ledger complete, which is a statement about every migration on disk
        # rather than about whichever one happened to be last when it was
        # written. Adding a migration must not edit this test.
        latest = max(migrations)
        histories = {
            "chat": (
                "agent_chat",
                (15, 16, 17),
                ("baseline", "thread_names", "drop_thread_tasks"),
            ),
            "web_apps": (
                "personal_web_app_builder",
                (18, 19, 20, 21, 22, 23, 24),
                (
                    "app_state",
                    "builder_thread_reset",
                    "multiple_web_apps",
                    "workspace_platform",
                    "remove_archiving",
                    "memory_revision",
                    "restore_archiving",
                ),
            ),
        }

        cases = [
            (chat_count, 0) for chat_count in range(4)
        ] + [
            (0, web_count) for web_count in range(8)
        ]
        for chat_count, web_count in cases:
            with self.subTest(chat_count=chat_count, web_count=web_count):
                pg_harness.create_database(self.DB_NAME)
                self.assertEqual(
                    migrate.up(target=12, quiet=True), list(range(1, 13))
                )
                with db.transaction() as cur:
                    cur.execute("CREATE SCHEMA app_agent_chat")
                    cur.execute("CREATE SCHEMA app_personal_web_app_builder")
                    for workspace_kind, applied_count in (
                        ("chat", chat_count),
                        ("web_apps", web_count),
                    ):
                        old_workspace_kind, versions, old_names = histories[workspace_kind]
                        for version, old_name in zip(
                            versions[:applied_count], old_names[:applied_count]
                        ):
                            cur.execute(migrations[version].up_sql)
                            cur.execute(
                                "INSERT INTO public.app_schema_migrations"
                                " (app_id, version, name) VALUES (%s, %s, %s)",
                                (old_workspace_kind, version - versions[0] + 1, old_name),
                            )

                self.assertEqual(migrate.up(target=13, quiet=True), [13])
                adoption_sql = _workspace_ledger_adoption_sql()
                with db.transaction() as cur:
                    cur.execute(adoption_sql)
                    # The pre-consolidation helper is safe to repeat too.
                    cur.execute(adoption_sql)
                self.assertEqual(
                    migrate.up(target=25, quiet=True),
                    [
                        version
                        for version in range(14, 26)
                        if version
                        not in {
                            *range(15, 15 + chat_count),
                            *range(18, 18 + web_count),
                        }
                    ],
                )
                with db.transaction() as cur:
                    cur.execute(
                        "INSERT INTO app_agent_chat.threads"
                        " (thread_id, archived, name)"
                        " VALUES ('thread-9', FALSE, 'Preserved chat')"
                    )
                    cur.execute(
                        "INSERT INTO app_personal_web_app_builder.web_apps"
                        " (app_id, name, created_at, updated_at)"
                        " VALUES ('app-9', 'Preserved app',"
                        " '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
                    )
                self.assertEqual(
                    migrate.up(quiet=True),
                    list(range(26, latest + 1)),
                )
                with db.transaction() as cur:
                    # Migration 0026 removed the old ledger; every later
                    # bootstrap must treat adoption as an immediate no-op.
                    cur.execute(adoption_sql)
                    cur.execute(
                        "SELECT version, name FROM public.schema_migrations ORDER BY version"
                    )
                    self.assertEqual(
                        [(int(version), str(name)) for version, name in cur.fetchall()],
                        [
                            (version, migrations[version].name)
                            for version in range(1, latest + 1)
                        ],
                    )
                    cur.execute(
                        "SELECT column_name FROM information_schema.columns"
                        " WHERE table_schema = 'public'"
                        " AND table_name = 'chat_threads'"
                    )
                    self.assertEqual(
                        {str(row[0]) for row in cur.fetchall()},
                        {"thread_id", "archived", "name", "spawned_by_thread_id"},
                    )
                    cur.execute(
                        "SELECT column_name FROM information_schema.columns"
                        " WHERE table_schema = 'public'"
                        " AND table_name = 'web_apps'"
                    )
                    columns = {str(row[0]) for row in cur.fetchall()}
                    self.assertIn("app_id", columns)
                    self.assertNotIn("thread_id", columns)
                    self.assertIn("archived", columns)
                    self.assertIn("revision", columns)
                    self.assertNotIn("data_version", columns)
                    self.assertNotIn("ui_revision", columns)
                    cur.execute(
                        "SELECT schema_name FROM information_schema.schemata"
                        " WHERE schema_name IN"
                        " ('app_agent_chat', 'app_personal_web_app_builder')"
                    )
                    self.assertEqual(cur.fetchall(), [])
                    cur.execute(
                        "SELECT to_regclass('public.workspace_thread_id_migrations')"
                    )
                    self.assertEqual(cur.fetchone(), (None,))
                    cur.execute(
                        "SELECT conname FROM pg_constraint"
                        " WHERE conname IN"
                        " ('chat_threads_id_check', 'web_apps_id_check')"
                        " ORDER BY conname"
                    )
                    self.assertEqual(
                        [row[0] for row in cur.fetchall()],
                        ["chat_threads_id_check", "web_apps_id_check"],
                    )
                    cur.execute(
                        "SELECT"
                        " has_table_privilege('kern-workspace', 'public.chat_threads', 'SELECT'),"
                        " has_table_privilege('kern-workspace', 'public.web_apps', 'UPDATE'),"
                        " has_table_privilege('kern-workspace', 'public.memory_pages', 'INSERT'),"
                        " has_table_privilege('kern-workspace',"
                        " 'public.web_app_revisions', 'SELECT'),"
                        " has_table_privilege('kern-workspace', 'public.provider_accounts', 'SELECT'),"
                        " has_schema_privilege('kern-workspace', 'public', 'CREATE')"
                    )
                    self.assertEqual(
                        cur.fetchone(),
                        (True, True, True, True, False, False),
                    )
                    cur.execute(
                        "SELECT to_regclass('public.schedule_runs'),"
                        " to_regclass('public.schedule_runs_id_seq')"
                    )
                    self.assertEqual(cur.fetchone(), (None, None))
                    cur.execute(
                        "SELECT name FROM public.chat_threads"
                        " WHERE thread_id = 'thread-9'"
                    )
                    self.assertEqual(cur.fetchone(), ("Preserved chat",))
                    cur.execute(
                        "SELECT name FROM public.web_apps WHERE app_id = 'app-9'"
                    )
                    self.assertEqual(cur.fetchone(), ("Preserved app",))

    def test_direct_workspace_id_migration_rejects_cross_table_collisions(self) -> None:
        self.assertEqual(migrate.up(target=13, quiet=True), list(range(1, 14)))
        with db.transaction() as cur:
            cur.execute(
                "INSERT INTO thread_sessions"
                " (agent_runtime, thread_id, model, effort)"
                " VALUES ('codex', 'agent_chat__thread-1',"
                " 'gpt-5.6-terra', 'high')"
            )
            cur.execute(
                "INSERT INTO agent_events"
                " (created_at, event_type, thread_id, message, source)"
                " VALUES ('2026-08-06T00:00:00Z', 'thread.message',"
                " 'thread-1', 'unrelated', 'agent')"
            )

        with self.assertRaises(Exception):
            migrate.up(target=14, quiet=True)
        self.assertEqual(
            [version for version, _name, applied in migrate.status() if applied],
            list(range(1, 14)),
        )

    def test_direct_workspace_id_migration_changes_only_mapped_legacy_ids(self) -> None:
        self.assertEqual(migrate.up(target=13, quiet=True), list(range(1, 14)))
        with db.transaction() as cur:
            for thread_id in (
                "agent_chat__thread-1",
                "personal_web_app_builder__app-1",
                "agent-chat--foo",
                "personal-web-app-builder--foo",
                "thread-99",
                "app-99",
            ):
                cur.execute(
                    "INSERT INTO thread_sessions"
                    " (agent_runtime, thread_id, model, effort)"
                    " VALUES ('codex', %s, 'gpt-5.6-terra', 'high')",
                    (thread_id,),
                )
                cur.execute(
                    "INSERT INTO agent_events"
                    " (created_at, event_type, thread_id, message, source)"
                    " VALUES ('2026-08-06T00:00:00Z', 'thread.message',"
                    " %s, 'message', 'agent')",
                    (thread_id,),
                )

        self.assertEqual(migrate.up(target=14, quiet=True), [14])
        with db.transaction() as cur:
            cur.execute("SELECT thread_id FROM thread_sessions ORDER BY thread_id")
            self.assertEqual(
                [row[0] for row in cur.fetchall()],
                [
                    "agent-chat--foo",
                    "app-1",
                    "app-99",
                    "personal-web-app-builder--foo",
                    "thread-1",
                    "thread-99",
                ],
            )


if __name__ == "__main__":
    unittest.main()
