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

from host.runtime.deploy import app_migrate, migrate
from host.runtime.core import app_platform, db


def _write(directory: Path, name: str, up: str, down: str = "") -> None:
    (directory / name).write_text(f"-- migrate:up\n{up}\n\n-- migrate:down\n{down}\n")


def _app_up(app_id: str) -> list[int]:
    """The production app-migration loop (bootstrap shells pending, then
    apply-sql as the app role and record as admin per version), driven
    in-process for tests. No advisory lock: tests are single-process."""
    app = app_platform.migration_app_by_id(app_id)
    assert app is not None
    applied = []
    for version in app_migrate.pending(app_id):
        app_migrate.apply_sql(app_id, version, connection_user=app.db_role)
        app_migrate.record(app_id, version)
        applied.append(version)
    return applied


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
        reverted = migrate.down(target=0, quiet=True)
        self.assertEqual(reverted, list(reversed(applied)))
        self.assertEqual(self.table_names(), {"schema_migrations"})

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


class AppMigrationTests(unittest.TestCase):
    DB_NAME = "kern_app_migrate_test"

    def setUp(self) -> None:
        pg_harness.create_database(self.DB_NAME)
        self.env_patch = patch.dict("os.environ", {"KERN_DB_NAME": self.DB_NAME})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        # Close pooled connections to this class's database before the env
        # restore, so no later test checks one out against the wrong database.
        self.addCleanup(db.close_pool)
        migrate.up(quiet=True)
        with db.transaction() as cur:
            cur.execute(
                """
                DO $$
                BEGIN
                  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'kern-app-0') THEN
                    CREATE ROLE "kern-app-0" LOGIN;
                  END IF;
                END
                $$;
                """
            )
            cur.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
            cur.execute('CREATE SCHEMA app_agent_chat AUTHORIZATION "kern-app-0"')

    def test_app_migration_runs_in_app_schema_and_records_host_version(self) -> None:
        self.assertEqual(_app_up("agent_chat"), [1, 2, 3])
        self.assertEqual(_app_up("agent_chat"), [])

        with db.transaction() as cur:
            cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'app_agent_chat'")
            self.assertEqual({row[0] for row in cur.fetchall()}, {"threads"})
            cur.execute(
                """
                SELECT column_name, data_type
                FROM information_schema.columns
                WHERE table_schema = 'app_agent_chat' AND table_name = 'threads'
                ORDER BY ordinal_position
                """
            )
            self.assertEqual(
                cur.fetchall(),
                [
                    ("thread_id", "text"),
                    ("archived", "boolean"),
                    ("name", "text"),
                ],
            )
            cur.execute(
                "SELECT app_id, version, name FROM app_schema_migrations"
                " ORDER BY app_id, version"
            )
            self.assertEqual(
                cur.fetchall(),
                [
                    ("agent_chat", 1, "baseline"),
                    ("agent_chat", 2, "thread_names"),
                    ("agent_chat", 3, "drop_thread_tasks"),
                ],
            )
            cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename = 'preferences'")
            self.assertEqual(cur.fetchall(), [])

    def test_app_migration_recovers_when_sql_commits_before_host_record(self) -> None:
        # The baseline's CREATE ... IF NOT EXISTS statements make a re-applied,
        # never-recorded version idempotent: the loop reapplies and records it.
        app_migrate.apply_sql("agent_chat", 1, connection_user="kern-app-0")

        self.assertEqual(_app_up("agent_chat"), [1, 2, 3])

        with db.transaction() as cur:
            cur.execute(
                "SELECT app_id, version, name FROM app_schema_migrations"
                " ORDER BY app_id, version"
            )
            self.assertEqual(
                cur.fetchall(),
                [
                    ("agent_chat", 1, "baseline"),
                    ("agent_chat", 2, "thread_names"),
                    ("agent_chat", 3, "drop_thread_tasks"),
                ],
            )
            cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'app_agent_chat'")
            self.assertEqual({row[0] for row in cur.fetchall()}, {"threads"})

    def test_deprecated_app_migrations_drop_all_app_tables(self) -> None:
        apps = {app.id: app for app in app_platform.migration_apps()}
        for app_id in ("alpha_seeker", "mission_pursuit", "social_marketer",
                       "software_builder", "virality_machine"):
            app = apps[app_id]
            with self.subTest(app_id=app_id):
                with db.transaction() as cur:
                    cur.execute(
                        "SELECT 1 FROM pg_roles WHERE rolname = %s",
                        (app.db_role,),
                    )
                    if cur.fetchone() is None:
                        cur.execute(f'CREATE ROLE "{app.db_role}" LOGIN')
                    cur.execute(
                        f'CREATE SCHEMA {app.db_schema} AUTHORIZATION "{app.db_role}"'
                    )
                self.assertTrue(app.deprecated)
                self.assertEqual(_app_up(app_id), [1, 2])
                with db.transaction() as cur:
                    cur.execute(
                        "SELECT tablename FROM pg_tables WHERE schemaname = %s",
                        (app.db_schema,),
                    )
                    self.assertEqual(cur.fetchall(), [])
                    cur.execute(
                        "SELECT version, name FROM app_schema_migrations "
                        "WHERE app_id = %s ORDER BY version",
                        (app_id,),
                    )
                    self.assertEqual(
                        cur.fetchall(),
                        [(1, "baseline"), (2, "drop_deprecated_state")],
                    )

    def test_app_migration_cannot_reset_back_to_host_role(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            migrations = Path(temp_dir) / "migrations"
            migrations.mkdir()
            _write(
                migrations,
                "0001_escape.sql",
                "RESET ROLE; CREATE TABLE public.host_escape_attempt (id INT);",
            )
            app = app_platform.AppManifest(
                id="agent_chat",
                title="Agent Chat",
                release_stage="stable",
                package_dir=Path(temp_dir),
                backend_entrypoint=Path(temp_dir) / "backend.py",
                migrations_dir=migrations,
                ui_dir=Path(temp_dir),
                allocation=app_platform.AppAllocation(uid=48000, gid=48000, port_offset=0),
                agent_instructions="Test app instructions.",
            )
            with patch("host.runtime.core.app_platform.migration_app_by_id", return_value=app):
                with self.assertRaises(Exception):
                    _app_up("agent_chat")

        with db.transaction() as cur:
            cur.execute("SELECT to_regclass('public.host_escape_attempt')")
            self.assertEqual(cur.fetchone(), (None,))
            cur.execute(
                "SELECT app_id, version, name FROM app_schema_migrations"
                " ORDER BY app_id, version"
            )
            self.assertEqual(cur.fetchall(), [])


if __name__ == "__main__":
    unittest.main()
