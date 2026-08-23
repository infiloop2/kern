"""Tests for the SQL migration runner (host.runtime.deploy.migrate).

These run against a dedicated database on the scratch cluster so migrating
down never disturbs the schema the other tests share.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pg_harness

from host.runtime.deploy import migrate
from host.runtime.core import db


def _write(directory: Path, name: str, up: str, down: str = "") -> None:
    (directory / name).write_text(f"-- migrate:up\n{up}\n\n-- migrate:down\n{down}\n")


def _workspace_ledger_adoption_sql() -> str:
    """Return the exact SQL bootstrap executes between its two migration runs."""
    bootstrap = (
        Path(__file__).resolve().parents[1] / "host" / "bootstrap" / "bootstrap.sh"
    ).read_text()
    function = bootstrap.split("adopt_workspace_migration_history() {", 1)[1]
    heredoc = function.split("<<'SQL'\n", 1)[1]
    return heredoc.split("\nSQL", 1)[0]


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

    def table_names(self) -> set[str]:
        with db.transaction() as cur:
            cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            return {row[0] for row in cur.fetchall()}

    def test_up_applies_pending_migrations_in_order_and_records_them(self) -> None:
        _write(self.migrations, "0001_first.sql", "CREATE TABLE first (id INT);", "DROP TABLE first;")
        _write(
            self.migrations,
            "0002_second.sql",
            "CREATE TABLE second (first_like INT); INSERT INTO second SELECT 1 FROM first;",
            "DROP TABLE second;",
        )

        applied = migrate.up(directory=self.migrations, quiet=True)

        self.assertEqual(applied, [1, 2])
        self.assertLessEqual({"first", "second", "schema_migrations"}, self.table_names())
        status = migrate.status(directory=self.migrations)
        self.assertEqual(status, [(1, "first", True), (2, "second", True)])

    def test_up_is_idempotent_and_applies_only_new_versions(self) -> None:
        _write(self.migrations, "0001_first.sql", "CREATE TABLE first (id INT);", "DROP TABLE first;")
        self.assertEqual(migrate.up(directory=self.migrations, quiet=True), [1])
        self.assertEqual(migrate.up(directory=self.migrations, quiet=True), [])
        _write(self.migrations, "0002_second.sql", "CREATE TABLE second (id INT);", "DROP TABLE second;")
        self.assertEqual(migrate.up(directory=self.migrations, quiet=True), [2])

    def test_a_failing_migration_rolls_back_and_leaves_the_previous_version(self) -> None:
        _write(self.migrations, "0001_first.sql", "CREATE TABLE first (id INT);", "DROP TABLE first;")
        migrate.up(directory=self.migrations, quiet=True)
        _write(self.migrations, "0002_broken.sql", "CREATE TABLE second (id INT); SELECT no_such_column;")

        with self.assertRaises(Exception):
            migrate.up(directory=self.migrations, quiet=True)

        self.assertNotIn("second", self.table_names())
        self.assertEqual(migrate.status(directory=self.migrations)[0], (1, "first", True))

    def test_down_reverts_the_newest_and_to_reverts_everything_above_the_target(self) -> None:
        _write(self.migrations, "0001_first.sql", "CREATE TABLE first (id INT);", "DROP TABLE first;")
        _write(self.migrations, "0002_second.sql", "CREATE TABLE second (id INT);", "DROP TABLE second;")
        _write(self.migrations, "0003_third.sql", "CREATE TABLE third (id INT);", "DROP TABLE third;")
        migrate.up(directory=self.migrations, quiet=True)

        self.assertEqual(migrate.down(directory=self.migrations, quiet=True), [3])
        self.assertNotIn("third", self.table_names())
        self.assertEqual(migrate.down(target=0, directory=self.migrations, quiet=True), [2, 1])
        self.assertNotIn("first", self.table_names())
        self.assertNotIn("second", self.table_names())
        self.assertEqual(
            migrate.status(directory=self.migrations),
            [(1, "first", False), (2, "second", False), (3, "third", False)],
        )

    def test_down_refuses_a_version_with_no_file_or_empty_down_section(self) -> None:
        _write(self.migrations, "0001_first.sql", "CREATE TABLE first (id INT);")
        migrate.up(directory=self.migrations, quiet=True)
        with self.assertRaises(migrate.MigrationError):
            migrate.down(directory=self.migrations, quiet=True)

    def test_malformed_migration_files_are_rejected(self) -> None:
        (self.migrations / "0001_missing_markers.sql").write_text("CREATE TABLE first (id INT);")
        with self.assertRaises(migrate.MigrationError):
            migrate.load_migrations(self.migrations)
        (self.migrations / "0001_missing_markers.sql").unlink()

        (self.migrations / "not_versioned.sql").write_text("-- migrate:up\nSELECT 1;\n-- migrate:down\n")
        with self.assertRaises(migrate.MigrationError):
            migrate.load_migrations(self.migrations)

    def test_repo_migrations_apply_and_roll_back_cleanly(self) -> None:
        # The real migration history must always migrate a fresh database up
        # and back down; this is the guardrail for every future migration.
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

        with db.transaction() as cur:
            cur.execute(
                "INSERT INTO oauth_logins"
                " (runtime, status, login_url, expires_at, device_code, login_id)"
                " VALUES ('grok', 'awaiting_login', 'https://accounts.x.ai/device',"
                " '2099-01-01T00:00:00Z', 'CODE', 'login-1')"
            )
        reverted = migrate.down(target=13, quiet=True)
        with db.transaction() as cur:
            cur.execute(
                "SELECT workspace_kind, version, name"
                " FROM workspace_migrations"
                " ORDER BY workspace_kind, version"
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
                    ("web_apps", 7, "restore_archiving"),
                ],
            )
        reverted += migrate.down(target=0, quiet=True)
        self.assertEqual(reverted, list(reversed(applied)))
        self.assertEqual(self.table_names(), {"schema_migrations"})
        with db.transaction() as cur:
            cur.execute(
                "SELECT schema_name FROM information_schema.schemata"
                " WHERE schema_name IN"
                " ('app_agent_chat', 'app_personal_web_app_builder')"
            )
            self.assertEqual(cur.fetchall(), [])

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

    def test_agent_history_counters_seed_retained_state_and_roll_back_cleanly(self) -> None:
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

        self.assertEqual(migrate.down(target=29, quiet=True), [30])
        with db.transaction() as cur:
            cur.execute("SELECT name FROM counters WHERE name LIKE 'agent_history_%'")
            self.assertEqual(cur.fetchall(), [])

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

        self.assertEqual(migrate.down(target=31, quiet=True), [32])
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

    def test_workspace_resource_limit_migration_expands_and_restores_bounds(self) -> None:
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

        self.assertEqual(migrate.down(target=37, quiet=True), [38])
        with db.transaction() as cur:
            cur.execute("SELECT char_length(content) FROM memory_pages WHERE page_id = 'boundary'")
            self.assertEqual(cur.fetchone(), (1000,))
            cur.execute(
                "SELECT char_length(content) FROM memory_page_revisions"
                " WHERE page_id = 'boundary'"
            )
            self.assertEqual(cur.fetchone(), (1000,))
            cur.execute("SELECT char_length(message) FROM schedules WHERE id = %s", (schedule_id,))
            self.assertEqual(cur.fetchone(), (4000,))
            cur.execute(
                "SELECT char_length(message) FROM schedule_revisions WHERE schedule_id = %s",
                (schedule_id,),
            )
            self.assertEqual(cur.fetchone(), (4000,))
            cur.execute(
                "SELECT char_length(message) FROM schedule_runs WHERE schedule_id = %s",
                (schedule_id,),
            )
            self.assertEqual(cur.fetchone(), (4000,))

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
                        {"thread_id", "archived", "name"},
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
                        " has_table_privilege('kern-workspace', 'public.schedule_runs', 'UPDATE'),"
                        " has_table_privilege('kern-workspace',"
                        " 'public.web_app_revisions', 'SELECT'),"
                        " has_sequence_privilege('kern-workspace',"
                        " 'public.schedule_runs_id_seq', 'USAGE'),"
                        " has_table_privilege('kern-workspace', 'public.provider_accounts', 'SELECT'),"
                        " has_schema_privilege('kern-workspace', 'public', 'CREATE')"
                    )
                    self.assertEqual(
                        cur.fetchone(),
                        (True, True, True, True, True, True, False, False),
                    )
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

    def test_direct_workspace_id_rollback_changes_only_mapped_legacy_ids(self) -> None:
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

        self.assertEqual(migrate.down(target=13, quiet=True), [14])
        expected = [
            "agent-chat--foo",
            "agent_chat__thread-1",
            "app-99",
            "personal-web-app-builder--foo",
            "personal_web_app_builder__app-1",
            "thread-99",
        ]
        with db.transaction() as cur:
            cur.execute("SELECT thread_id FROM thread_sessions ORDER BY thread_id")
            self.assertEqual([row[0] for row in cur.fetchall()], expected)
            cur.execute("SELECT thread_id FROM agent_events ORDER BY thread_id")
            self.assertEqual([row[0] for row in cur.fetchall()], expected)
            cur.execute(
                "SELECT to_regclass('public.workspace_thread_id_migrations')"
            )
            self.assertEqual(cur.fetchone(), (None,))

    def test_workspace_storage_rollback_restores_legacy_service_access(self) -> None:
        self.assertEqual(migrate.up(target=26, quiet=True), list(range(1, 27)))
        with db.transaction() as cur:
            cur.execute('CREATE ROLE "kern-ux-surface"')

        try:
            self.assertEqual(migrate.down(target=25, quiet=True), [26])
            with db.transaction() as cur:
                cur.execute(
                    "SELECT"
                    " has_schema_privilege('kern-ux-surface',"
                    " 'app_agent_chat', 'USAGE'),"
                    " has_table_privilege('kern-ux-surface',"
                    " 'app_agent_chat.threads', 'SELECT'),"
                    " has_table_privilege('kern-ux-surface',"
                    " 'app_personal_web_app_builder.web_apps', 'UPDATE'),"
                    " has_sequence_privilege('kern-ux-surface',"
                    " 'app_personal_web_app_builder.web_app_history_id_seq',"
                    " 'USAGE')"
                )
                self.assertEqual(cur.fetchone(), (True, True, True, True))
        finally:
            # Reapplying 0026 explicitly revokes the restored grants, leaving
            # the synthetic legacy role safe to remove.
            migrate.up(quiet=True)
            with db.transaction() as cur:
                cur.execute('DROP ROLE IF EXISTS "kern-ux-surface"')



if __name__ == "__main__":
    unittest.main()
