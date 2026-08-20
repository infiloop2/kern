"""Global Workspace memory and schedule behavior."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pg_harness

from host.runtime.core import db
from host.runtime.workspace import agent_api, memory, schedules
from host.runtime.workspace.host_api import WorkspaceError


SESSION = {
    "agent_runtime": "codex",
    "model": "gpt-5.6-terra",
    "effort": "high",
}

SCHEDULE_FIELDS = (
    "name", "message", "cadence", "interval_minutes", "daily_time",
    "agent_runtime", "model", "effort",
)


def schedule_update(schedule: dict, **changes: object) -> dict:
    body = {field: schedule[field] for field in SCHEDULE_FIELDS}
    body.update(changes)
    body["expected_revision"] = schedule["revision"]
    return body


class WorkspaceGlobalDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        pg_harness.ensure_database()

    def setUp(self) -> None:
        pg_harness.reset_database()
        self.addCleanup(db.close_pool)

    def test_memory_is_global_paged_linked_searchable_and_revision_guarded(self) -> None:
        first = memory.save_page(
            "deployment",
            {
                "description": "Use before changing production",
                "content": "Read [[rollback]] before deploying.",
                "expected_revision": 0,
            },
            actor="agent",
        )
        memory.save_page(
            "rollback",
            {
                "description": "Use when deployment verification fails",
                "content": "Return to the last known good release.",
                "expected_revision": 0,
            },
            actor="user",
        )
        self.assertEqual(first["links"], ["rollback"])
        self.assertEqual(
            memory.load_page("rollback")["backlinks"], ["deployment"]
        )
        page = memory.list_pages({"limit": ["1"]})
        self.assertEqual([item["page_id"] for item in page["pages"]], ["deployment"])
        self.assertIn("next_cursor", page)
        next_page = memory.list_pages(
            {"limit": ["1"], "cursor": [page["next_cursor"]]}
        )
        self.assertEqual([item["page_id"] for item in next_page["pages"]], ["rollback"])
        self.assertEqual(
            [item["page_id"] for item in memory.search_pages({"q": ["production"]})["pages"]],
            ["deployment"],
        )
        with self.assertRaises(WorkspaceError) as conflict:
            memory.save_page(
                "deployment",
                {
                    "description": "stale",
                    "content": "stale",
                    "expected_revision": 0,
                },
                actor="agent",
            )
        self.assertEqual(conflict.exception.status, HTTPStatus.CONFLICT)

    def test_agent_memory_search_falls_back_to_weak_and_popular_pages(self) -> None:
        pages = (
            (
                "popular-one",
                "Use before production deployments",
                "Production deployment checklist.",
            ),
            (
                "popular-two",
                "Use for durable context decisions",
                "Durable context guidance.",
            ),
            (
                "weak-match",
                "Use before running Playwright",
                "Browser testing guidance.",
            ),
            (
                "recent-page",
                "Use for a newly documented workflow",
                "New workflow guidance.",
            ),
        )
        for page_id, description, content in pages:
            memory.save_page(
                page_id,
                {
                    "description": description,
                    "content": content,
                    "expected_revision": 0,
                },
                actor="agent",
            )

        for _ in range(2):
            strong = memory.search_swarm_pages(
                {"q": ["production deployments"]}
            )
            self.assertEqual(strong["pages"][0]["page_id"], "popular-one")
            self.assertNotIn("popular_pages", strong)
        memory.search_swarm_pages({"q": ["durable context"]})
        memory.search_pages({"q": ["production deployments"]})

        with db.transaction() as cur:
            cur.execute(
                "SELECT page_id, strong_top_hit_count FROM memory_pages"
                " WHERE page_id IN ('popular-one', 'popular-two') ORDER BY page_id"
            )
            self.assertEqual(
                cur.fetchall(), [("popular-one", 2), ("popular-two", 1)]
            )
            cur.execute(
                "UPDATE memory_pages SET updated_at = CASE page_id"
                " WHEN 'recent-page' THEN '2026-04-04T00:00:00Z'"
                " WHEN 'weak-match' THEN '2026-04-03T00:00:00Z'"
                " WHEN 'popular-two' THEN '2026-04-02T00:00:00Z'"
                " ELSE '2026-04-01T00:00:00Z' END"
            )

        fallback = memory.search_swarm_pages(
            {"q": ["introspection playwright chromium cleanup"]}
        )
        self.assertEqual(fallback["match_mode"], "weak")
        self.assertEqual(
            [page["page_id"] for page in fallback["pages"]], ["weak-match"]
        )
        self.assertEqual(
            fallback["popular_pages"][0]["page_id"], "popular-one"
        )

    def test_memory_delete_and_operator_restore_are_forward_revisions(self) -> None:
        created = memory.save_page(
            "preferences",
            {
                "description": "Use when formatting reports",
                "content": "Prefer concise tables.",
                "expected_revision": 0,
            },
            actor="user",
        )
        memory.delete_page(
            "preferences", {"expected_revision": [str(created["revision"])]}, actor="agent"
        )
        with self.assertRaises(WorkspaceError) as missing:
            memory.load_page("preferences")
        self.assertEqual(missing.exception.status, HTTPStatus.NOT_FOUND)
        restored = memory.restore_revision(
            "preferences", 1, {"expected_revision": 2}
        )
        self.assertEqual(restored["revision"], 3)
        self.assertFalse(restored["deleted"])
        self.assertEqual(restored["content"], "Prefer concise tables.")

    def test_deleted_memory_expires_after_ninety_days(self) -> None:
        page = memory.save_page(
            "temporary",
            {
                "description": "Temporary durable context",
                "content": "Remove after retention.",
                "expected_revision": 0,
            },
            actor="user",
        )
        memory.delete_page(
            "temporary", {"expected_revision": [str(page["revision"])]}, actor="user"
        )
        with db.transaction() as cur:
            cur.execute(
                "UPDATE memory_pages SET deleted_at = '2026-01-01T00:00:00Z'"
                " WHERE page_id = 'temporary'"
            )
        self.assertEqual(
            memory.prune_deleted(datetime(2026, 4, 2, tzinfo=timezone.utc)), 1
        )
        with self.assertRaises(WorkspaceError):
            memory.load_page("temporary", include_deleted=True)

    def test_memory_bounds_and_agent_history_boundary(self) -> None:
        self.assertEqual(memory.MAX_CONTENT_CHARS, 2_000)
        boundary = memory.save_page(
            "boundary",
            {
                "description": "Maximum-size memory page",
                "content": "x" * memory.MAX_CONTENT_CHARS,
                "expected_revision": 0,
            },
            actor="agent",
        )
        self.assertEqual(len(boundary["content"]), memory.MAX_CONTENT_CHARS)
        with self.assertRaises(WorkspaceError):
            memory.route_agent(
                "PUT",
                "/agent/memory/pages/Bad_ID",
                {"description": "d", "content": "", "expected_revision": 0},
                {},
            )
        with self.assertRaises(WorkspaceError):
            memory.save_page(
                "large",
                {
                    "description": "d",
                    "content": "x" * (memory.MAX_CONTENT_CHARS + 1),
                    "expected_revision": 0,
                },
                actor="agent",
            )
        with self.assertRaises(WorkspaceError) as hidden_history:
            memory.route_agent(
                "GET", "/agent/memory/pages/large/revisions", None, {}
            )
        self.assertEqual(hidden_history.exception.status, HTTPStatus.NOT_FOUND)
        with self.assertRaises(WorkspaceError) as oversized_cursor:
            memory.list_revisions(
                "large", {"before": [str(2**63)], "limit": ["1"]}
            )
        self.assertEqual(oversized_cursor.exception.status, HTTPStatus.BAD_REQUEST)

    def test_individual_memory_is_hidden_from_swarm_agent_routes(self) -> None:
        memory.save_page(
            "thread-7",
            {
                "description": "Use when continuing this thread",
                "content": "Private context",
                "expected_revision": 0,
            },
            actor="agent",
        )
        memory.save_page(
            "shared-context",
            {
                "description": "Use across threads",
                "content": "Private context is not shared.",
                "expected_revision": 0,
            },
            actor="agent",
        )
        memory.save_page(
            "thread-8",
            {
                "description": "Use when continuing another thread",
                "content": "See [[thread-7]].",
                "expected_revision": 0,
            },
            actor="agent",
        )

        self.assertEqual(
            [
                page["page_id"]
                for page in memory.route_agent(
                    "GET", "/agent/memory", None, {}
                )["pages"]
            ],
            ["shared-context"],
        )
        self.assertEqual(
            [
                page["page_id"]
                for page in memory.route_agent(
                    "GET", "/agent/memory/search", None, {"q": ["context"]}
                )["pages"]
            ],
            ["shared-context"],
        )
        for method, body, query in (
            ("GET", None, {}),
            (
                "PUT",
                {
                    "description": "changed",
                    "content": "changed",
                    "expected_revision": 1,
                },
                {},
            ),
            ("DELETE", None, {"expected_revision": ["1"]}),
        ):
            with self.subTest(method=method), self.assertRaises(WorkspaceError) as hidden:
                memory.route_agent(
                    method, "/agent/memory/pages/thread-7", body, query
                )
            self.assertEqual(hidden.exception.status, HTTPStatus.NOT_FOUND)

        self.assertEqual(
            [
                page["page_id"]
                for page in memory.list_pages({"scope": ["individual"]})["pages"]
            ],
            ["thread-7", "thread-8"],
        )
        self.assertEqual(
            [
                page["page_id"]
                for page in memory.search_pages(
                    {"q": ["context"], "scope": ["individual"]}
                )["pages"]
            ],
            ["thread-7"],
        )
        self.assertEqual(
            agent_api.dispatch_call(
                "GET", "/agent/self/memory", None, peer_thread_id="thread-7"
            )["body"]["page"]["backlinks"],
            [],
        )

    def test_individual_memory_is_excluded_from_the_link_graph(self) -> None:
        for page_id, content in (
            ("swarm-source", "See [[swarm-target]] and [[thread-7]]."),
            ("swarm-target", "Shared target."),
            ("thread-7", "See [[swarm-target]] and [[thread-8]]."),
            ("thread-8", "Private target."),
        ):
            memory.save_page(
                page_id,
                {
                    "description": f"Memory page {page_id}",
                    "content": content,
                    "expected_revision": 0,
                },
                actor="agent",
            )

        self.assertEqual(memory.load_page("swarm-source")["links"], ["swarm-target"])
        self.assertEqual(memory.load_page("swarm-target")["backlinks"], ["swarm-source"])
        self.assertEqual(memory.load_page("thread-7")["links"], [])
        self.assertEqual(memory.load_page("thread-7")["backlinks"], [])
        self.assertEqual(memory.load_page("thread-8")["backlinks"], [])

    def test_self_memory_is_resolved_from_peer_identity(self) -> None:
        with self.assertRaises(WorkspaceError) as missing:
            agent_api.dispatch_call(
                "GET", "/agent/self/memory", None, peer_thread_id="thread-7"
            )
        self.assertEqual(missing.exception.status, HTTPStatus.NOT_FOUND)

        created = agent_api.dispatch_call(
            "PUT",
            "/agent/self/memory",
            {
                "description": "Use when continuing this thread",
                "content": "Prefer the bounded approach.",
                "expected_revision": 0,
            },
            peer_thread_id="thread-7",
        )
        self.assertEqual(created["body"]["page"]["page_id"], "thread-7")
        self.assertEqual(
            agent_api.dispatch_call(
                "GET", "/agent/self/memory", None, peer_thread_id="thread-7"
            )["body"]["page"]["content"],
            "Prefer the bounded approach.",
        )

        with self.assertRaises(WorkspaceError) as stale:
            agent_api.dispatch_call(
                "PUT",
                "/agent/self/memory",
                {
                    "description": "stale",
                    "content": "stale",
                    "expected_revision": 0,
                },
                peer_thread_id="thread-7",
            )
        self.assertEqual(stale.exception.status, HTTPStatus.CONFLICT)

    def test_self_memory_accepts_app_and_chat_thread_kinds(self) -> None:
        for peer_thread_id in ("app-3", "thread-7"):
            with self.subTest(peer_thread_id=peer_thread_id):
                created = agent_api.dispatch_call(
                    "PUT",
                    "/agent/self/memory",
                    {
                        "description": "Use when continuing this thread",
                        "content": peer_thread_id,
                        "expected_revision": 0,
                    },
                    peer_thread_id=peer_thread_id,
                )
                self.assertEqual(created["body"]["page"]["page_id"], peer_thread_id)

    def test_self_memory_rejects_missing_schedule_and_unrecognized_identities(self) -> None:
        for peer_thread_id in (None, "schedule-42-run-9", "legacy-thread"):
            with self.subTest(peer_thread_id=peer_thread_id):
                with self.assertRaises(WorkspaceError) as conflict:
                    agent_api.dispatch_call(
                        "GET",
                        "/agent/self/memory",
                        None,
                        peer_thread_id=peer_thread_id,
                    )
                self.assertEqual(conflict.exception.status, HTTPStatus.CONFLICT)

        with self.assertRaises(WorkspaceError) as unsupported:
            agent_api.dispatch_call(
                "DELETE", "/agent/self/memory", None, peer_thread_id="thread-7"
            )
        self.assertEqual(unsupported.exception.status, HTTPStatus.NOT_FOUND)
        with self.assertRaises(WorkspaceError) as caller_identity:
            agent_api.dispatch_call(
                "GET",
                "/agent/self/memory?page_id=someone-else",
                None,
                peer_thread_id="thread-7",
            )
        self.assertEqual(caller_identity.exception.status, HTTPStatus.BAD_REQUEST)

    def test_concurrent_memory_creates_respect_the_global_quota(self) -> None:
        self.assertEqual(memory.MAX_PAGES, 10_000)

        def create(page_id: str) -> str:
            try:
                memory.save_page(
                    page_id,
                    {
                        "description": "durable context",
                        "content": "value",
                        "expected_revision": 0,
                    },
                    actor="agent",
                )
                return "created"
            except WorkspaceError as exc:
                return f"error:{exc.status.value}"

        with (
            patch.object(memory, "MAX_PAGES", 1),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            results = list(executor.map(create, ("first", "second")))
        self.assertEqual(sorted(results), ["created", "error:409"])
        self.assertEqual(len(memory.list_pages({})["pages"]), 1)

    def test_schedule_run_uses_a_fresh_thread_and_snapshotted_configuration(self) -> None:
        schedule = schedules.create_schedule(
            {
                "name": "Morning review",
                "message": "Summarize open work.",
                "cadence": "interval",
                "interval_minutes": 60,
                **SESSION,
            },
            actor="user",
        )
        due = datetime(2026, 8, 7, 10, 0, tzinfo=timezone.utc)
        with db.transaction() as cur:
            cur.execute(
                "UPDATE schedules SET next_run_at = %s WHERE id = %s",
                ("2026-08-07T09:00:00Z", schedule["id"]),
            )
        calls: list[tuple[str, str, object]] = []

        def host(method: str, path: str, body: object = None) -> dict:
            calls.append((method, path, body))
            return {
                "status": "accepted",
                "thread": {"thread_id": "schedule-1-run-1", "status": "running"},
            }

        with patch.object(schedules, "call_admin_api", side_effect=host):
            self.assertEqual(schedules.run_due(due), 1)
        run = schedules.list_runs(schedule["id"], {})["runs"][0]
        self.assertEqual(run["thread_id"], "schedule-1-run-1")
        self.assertEqual(run["status"], "running")
        self.assertNotIn("message", run)
        self.assertEqual(
            schedules.load_run(schedule["id"], run["id"])["message"],
            "Summarize open work.",
        )
        self.assertEqual(
            calls,
            [
                (
                    "POST",
                    "/v1/threads/schedule-1-run-1/messages",
                    {"message": "Summarize open work.", **SESSION},
                )
            ],
        )
        self.assertNotIn(
            "message", schedules.list_schedules({})["schedules"][0]
        )

    def test_schedule_message_limit_accepts_twelve_thousand_characters(self) -> None:
        self.assertEqual(schedules.MAX_MESSAGE_CHARS, 12_000)
        schedule = schedules.create_schedule(
            {
                "name": "Maximum-size prompt",
                "message": "x" * schedules.MAX_MESSAGE_CHARS,
                "cadence": "interval",
                "interval_minutes": 60,
                **SESSION,
            },
            actor="agent",
        )
        self.assertEqual(len(schedule["message"]), schedules.MAX_MESSAGE_CHARS)
        with self.assertRaises(WorkspaceError) as too_long:
            schedules.create_schedule(
                {
                    "name": "Oversized prompt",
                    "message": "x" * (schedules.MAX_MESSAGE_CHARS + 1),
                    "cadence": "interval",
                    "interval_minutes": 60,
                    **SESSION,
                },
                actor="agent",
            )
        self.assertEqual(too_long.exception.status, HTTPStatus.BAD_REQUEST)

    def test_concurrent_schedule_creates_respect_the_global_quota(self) -> None:
        def create(name: str) -> str:
            try:
                schedules.create_schedule(
                    {
                        "name": name,
                        "message": "Do bounded work",
                        "cadence": "interval",
                        "interval_minutes": 60,
                        **SESSION,
                    },
                    actor="agent",
                )
                return "created"
            except WorkspaceError as exc:
                return f"error:{exc.status.value}"

        with (
            patch.object(schedules, "MAX_SCHEDULES", 1),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            results = list(executor.map(create, ("First", "Second")))
        self.assertEqual(sorted(results), ["created", "error:409"])
        self.assertEqual(len(schedules.list_schedules({})["schedules"]), 1)

    def test_schedule_delete_is_the_only_pause_and_restore_reactivates(self) -> None:
        schedule = schedules.create_schedule(
            {
                "name": "Review",
                "message": "Review open work",
                "cadence": "interval",
                "interval_minutes": 60,
                **SESSION,
            },
            actor="user",
        )
        self.assertNotIn("enabled", schedule)
        deleted = schedules.delete_schedule(
            schedule["id"], {"expected_revision": ["1"]}, actor="user"
        )
        self.assertEqual(deleted["revision"], 2)
        with self.assertRaises(WorkspaceError) as hidden:
            schedules.load_schedule(schedule["id"])
        self.assertEqual(hidden.exception.status, HTTPStatus.NOT_FOUND)

        restored = schedules.restore_revision(
            schedule["id"], 1, {"expected_revision": 2}
        )
        self.assertFalse(restored["deleted"])
        self.assertEqual(restored["revision"], 3)
        self.assertNotIn("enabled", restored)

        with self.assertRaises(WorkspaceError) as old_pause_field:
            schedules.update_schedule(
                schedule["id"],
                {**schedule_update(restored), "enabled": False},
                actor="user",
            )
        self.assertEqual(old_pause_field.exception.status, HTTPStatus.BAD_REQUEST)

    def test_active_run_blocks_overlap_while_edits_affect_only_future_runs(self) -> None:
        schedule = schedules.create_schedule(
            {
                "name": "Review",
                "message": "First message",
                "cadence": "interval",
                "interval_minutes": 60,
                **SESSION,
            },
            actor="agent",
        )
        now = datetime(2026, 8, 7, 10, 0, tzinfo=timezone.utc)
        with db.transaction() as cur:
            cur.execute(
                "UPDATE schedules SET next_run_at = %s WHERE id = %s",
                ("2026-08-07T09:00:00Z", schedule["id"]),
            )
        run = schedules._claim_run(schedule["id"], now)
        assert run is not None
        schedules._start_run(run["id"])
        updated = schedules.update_schedule(
            schedule["id"],
            schedule_update(schedule, message="Future message"),
            actor="user",
        )
        self.assertEqual(updated["revision"], 2)
        with db.transaction() as cur:
            cur.execute(
                "UPDATE schedules SET next_run_at = %s WHERE id = %s",
                ("2026-08-07T09:30:00Z", schedule["id"]),
            )
        self.assertIsNone(schedules._claim_run(schedule["id"], now))
        self.assertEqual(run["message"], "First message")
        schedules.delete_schedule(
            schedule["id"], {"expected_revision": ["2"]}, actor="user"
        )
        self.assertEqual(schedules.load_run(schedule["id"], run["id"])["status"], "running")

    def test_active_due_schedules_do_not_starve_later_due_work(self) -> None:
        created = [
            schedules.create_schedule(
                {
                    "name": f"Review {index}",
                    "message": f"Work {index}",
                    "cadence": "interval",
                    "interval_minutes": 60,
                    **SESSION,
                },
                actor="user",
            )
            for index in range(3)
        ]
        with db.transaction() as cur:
            for index, schedule in enumerate(created[:2], start=90):
                cur.execute(
                    "INSERT INTO schedule_runs"
                    " (schedule_id, thread_id, message, agent_runtime, model, effort,"
                    " status, scheduled_for)"
                    " VALUES (%s, %s, %s, %s, %s, %s, 'running', %s)",
                    (
                        schedule["id"], f"schedule-{schedule['id']}-run-{index}",
                        schedule["message"],
                        SESSION["agent_runtime"],
                        SESSION["model"], SESSION["effort"], "2026-08-07T09:00:00Z",
                    ),
                )
            cur.execute("UPDATE schedules SET next_run_at = '2026-08-07T09:00:00Z'")

        accepted = {
            "status": "accepted",
            "thread": {"thread_id": "schedule-3-run-3", "status": "running"},
        }
        with (
            patch.object(schedules, "DUE_BATCH", 2),
            patch.object(schedules, "call_admin_api", return_value=accepted) as host,
        ):
            self.assertEqual(
                schedules.run_due(datetime(2026, 8, 7, 10, 0, tzinfo=timezone.utc)),
                1,
            )
        self.assertEqual(
            host.call_args.args[1], "/v1/threads/schedule-3-run-3/messages"
        )

    def test_active_run_reconciliation_checks_every_bounded_active_run(self) -> None:
        created = [
            schedules.create_schedule(
                {
                    "name": f"Review {index}",
                    "message": f"Work {index}",
                    "cadence": "interval",
                    "interval_minutes": 60,
                    **SESSION,
                },
                actor="user",
            )
            for index in range(3)
        ]
        with db.transaction() as cur:
            for index, schedule in enumerate(created, start=90):
                cur.execute(
                    "INSERT INTO schedule_runs"
                    " (schedule_id, thread_id, message, agent_runtime, model, effort,"
                    " status, scheduled_for)"
                    " VALUES (%s, %s, %s, %s, %s, %s, 'running', %s)",
                    (
                        schedule["id"], f"schedule-{schedule['id']}-run-{index}",
                        schedule["message"],
                        SESSION["agent_runtime"],
                        SESSION["model"], SESSION["effort"], "2026-08-07T09:00:00Z",
                    ),
                )

        with patch.object(
            schedules,
            "call_admin_api",
            return_value={"thread": {"status": "running"}},
        ) as host:
            self.assertEqual(schedules.refresh_active_runs(), 0)
        observed = [call.args[1] for call in host.call_args_list]
        self.assertEqual(observed, [
            "/v1/threads/schedule-1-run-90",
            "/v1/threads/schedule-2-run-91",
            "/v1/threads/schedule-3-run-92",
        ])

    def test_invalid_schedule_configuration_fails_only_when_invoked(self) -> None:
        with self.assertRaises(WorkspaceError) as oversized:
            schedules.create_schedule(
                {
                    "name": "Oversized configuration",
                    "message": "Review open work",
                    "cadence": "interval",
                    "interval_minutes": 60,
                    **{**SESSION, "model": "m" * 101},
                },
                actor="agent",
            )
        self.assertEqual(oversized.exception.status, HTTPStatus.BAD_REQUEST)

        schedule = schedules.create_schedule(
            {
                "name": "Invalid configuration",
                "message": "Review open work",
                "cadence": "interval",
                "interval_minutes": 60,
                "agent_runtime": "retired",
                "model": "retired-model",
                "effort": "retired-effort",
            },
            actor="user",
        )
        with db.transaction() as cur:
            cur.execute(
                "UPDATE schedules SET next_run_at = %s WHERE id = %s",
                ("2026-08-07T09:00:00Z", schedule["id"]),
            )

        def host(method: str, path: str, body: object = None) -> dict:
            del path, body
            if method == "POST":
                raise WorkspaceError(HTTPStatus.BAD_REQUEST, "model is no longer offered")
            raise WorkspaceError(HTTPStatus.NOT_FOUND, "thread not found")

        with patch.object(schedules, "call_admin_api", side_effect=host):
            self.assertEqual(
                schedules.run_due(datetime(2026, 8, 7, 10, 0, tzinfo=timezone.utc)),
                1,
            )
        run = schedules.list_runs(schedule["id"], {})["runs"][0]
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["error_message"], "model is no longer offered")

    def test_agent_recent_failures_are_bounded_paged_and_active_only(self) -> None:
        active = schedules.create_schedule(
            {
                "name": "Active review",
                "message": "Review open work",
                "cadence": "interval",
                "interval_minutes": 60,
                **SESSION,
            },
            actor="agent",
        )
        deleted = schedules.create_schedule(
            {
                "name": "Deleted review",
                "message": "Review old work",
                "cadence": "interval",
                "interval_minutes": 60,
                **SESSION,
            },
            actor="agent",
        )
        schedules.delete_schedule(
            deleted["id"], {"expected_revision": ["1"]}, actor="agent"
        )

        def insert_run(
            schedule_id: int, suffix: str, status: str, error: str | None
        ) -> int:
            with db.transaction() as cur:
                cur.execute(
                    "INSERT INTO schedule_runs"
                    " (schedule_id, thread_id, message, agent_runtime, model, effort,"
                    " status, error_message, scheduled_for, finished_at)"
                    " VALUES (%s, %s, 'private prompt', %s, %s, %s, %s, %s,"
                    " '2026-08-17T10:00:00Z', '2026-08-17T10:01:00Z')"
                    " RETURNING id",
                    (
                        schedule_id,
                        f"schedule-{schedule_id}-run-{suffix}",
                        SESSION["agent_runtime"],
                        SESSION["model"],
                        SESSION["effort"],
                        status,
                        error,
                    ),
                )
                row = cur.fetchone()
                assert row is not None
                return int(row[0])

        older_id = insert_run(active["id"], "1", "failed", "older failure")
        insert_run(active["id"], "2", "succeeded", None)
        latest_id = insert_run(active["id"], "3", "failed", "x" * 600)
        insert_run(deleted["id"], "4", "failed", "deleted failure")

        first = schedules.route_agent(
            "GET", "/agent/schedules/recent-failures", None, {"limit": ["1"]}
        )
        self.assertEqual(len(first["failures"]), 1)
        failure = first["failures"][0]
        self.assertEqual(failure["id"], latest_id)
        self.assertEqual(failure["schedule_id"], active["id"])
        self.assertEqual(failure["schedule_name"], "Active review")
        self.assertEqual(failure["status"], "failed")
        self.assertEqual(len(failure["error_message"]), 500)
        self.assertNotIn("message", failure)
        self.assertEqual(first["next_before"], latest_id)

        second = schedules.route_agent(
            "GET",
            "/agent/schedules/recent-failures",
            None,
            {"limit": ["1"], "before": [str(first["next_before"])]},
        )
        self.assertEqual([item["id"] for item in second["failures"]], [older_id])
        self.assertNotIn("next_before", second)

        with self.assertRaises(WorkspaceError) as invalid_query:
            schedules.route_agent(
                "GET",
                "/agent/schedules/recent-failures",
                None,
                {"status": ["succeeded"]},
            )
        self.assertEqual(invalid_query.exception.status, HTTPStatus.BAD_REQUEST)

    def test_a_script_schedule_carries_a_path_and_rejects_anything_else(self) -> None:
        script_session = {
            "agent_runtime": "script",
            "model": "bash",
            "effort": "fixed",
        }

        def create(name: str, message: str) -> dict:
            return schedules.create_schedule(
                {
                    "name": name,
                    "message": message,
                    "cadence": "interval",
                    "interval_minutes": 60,
                    **script_session,
                },
                actor="agent",
            )

        schedule = create("Nightly backup", "/mnt/kern-agent/agent-home/backup.sh")
        self.assertEqual(schedule["message"], "/mnt/kern-agent/agent-home/backup.sh")

        # The message field means something else for this runtime, so a prompt
        # is rejected while the schedule is being written rather than becoming
        # a failed run an hour later.
        for message in (
            "Summarize open work.",
            "/etc/cron.daily/backup.sh",
            "/mnt/kern-agent/agent-home/../../etc/backup.sh",
            "/mnt/kern-agent/agent-home/backup.sh; rm -rf /",
        ):
            with self.subTest(message=message):
                with self.assertRaises(WorkspaceError) as rejected:
                    create("Bad script", message)
                self.assertEqual(rejected.exception.status, HTTPStatus.BAD_REQUEST)

        # An edit is held to the same contract, in both directions.
        with self.assertRaises(WorkspaceError):
            schedules.update_schedule(
                schedule["id"],
                {
                    "expected_revision": schedule["revision"],
                    "name": schedule["name"],
                    "message": "Summarize open work.",
                    "cadence": "interval",
                    "interval_minutes": 60,
                    **script_session,
                },
                actor="user",
            )
        # ...and a model runtime still takes an ordinary prompt.
        prompted = schedules.update_schedule(
            schedule["id"],
            {
                "expected_revision": schedule["revision"],
                "name": schedule["name"],
                "message": "Summarize open work.",
                "cadence": "interval",
                "interval_minutes": 60,
                **SESSION,
            },
            actor="user",
        )
        self.assertEqual(prompted["message"], "Summarize open work.")

    def test_a_script_run_submits_the_path_as_the_thread_message(self) -> None:
        schedule = schedules.create_schedule(
            {
                "name": "Nightly backup",
                "message": "/mnt/kern-agent/agent-home/scripts/backup.sh",
                "cadence": "interval",
                "interval_minutes": 60,
                "agent_runtime": "script",
                "model": "bash",
                "effort": "fixed",
            },
            actor="user",
        )
        with db.transaction() as cur:
            cur.execute(
                "UPDATE schedules SET next_run_at = %s WHERE id = %s",
                ("2026-08-07T09:00:00Z", schedule["id"]),
            )
        calls: list[tuple[str, str, object]] = []

        def host(method: str, path: str, body: object = None) -> dict:
            calls.append((method, path, body))
            return {
                "status": "accepted",
                "thread": {"thread_id": "schedule-1-run-1", "status": "running"},
            }

        with patch.object(schedules, "call_admin_api", side_effect=host):
            self.assertEqual(
                schedules.run_due(datetime(2026, 8, 7, 10, 0, tzinfo=timezone.utc)), 1
            )
        # A script run is an ordinary schedule run: same fresh thread, same
        # submission shape, with the path where the prompt would be.
        self.assertEqual(
            calls,
            [
                (
                    "POST",
                    "/v1/threads/schedule-1-run-1/messages",
                    {
                        "message": "/mnt/kern-agent/agent-home/scripts/backup.sh",
                        "agent_runtime": "script",
                        "model": "bash",
                        "effort": "fixed",
                    },
                )
            ],
        )

    def test_schedule_admission_failure_is_terminal_without_retry(self) -> None:
        schedule = schedules.create_schedule(
            {
                "name": "Review",
                "message": "Work",
                "cadence": "daily",
                "daily_time": "09:00",
                **SESSION,
            },
            actor="user",
        )
        with db.transaction() as cur:
            cur.execute(
                "UPDATE schedules SET next_run_at = %s WHERE id = %s",
                ("2026-08-07T08:00:00Z", schedule["id"]),
            )
        run = schedules._claim_run(
            schedule["id"], datetime(2026, 8, 7, 10, 0, tzinfo=timezone.utc)
        )
        assert run is not None
        failure = WorkspaceError(HTTPStatus.BAD_GATEWAY, "response lost")
        with patch.object(schedules, "call_admin_api", side_effect=failure) as host:
            schedules._launch_run(run)
        self.assertEqual(host.call_count, 1)
        failed = schedules.load_run(schedule["id"], run["id"])
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error_message"], "response lost")

    def test_run_events_are_fetched_from_the_host_not_copied(self) -> None:
        schedule = schedules.create_schedule(
            {
                "name": "Review",
                "message": "Work",
                "cadence": "interval",
                "interval_minutes": 60,
                **SESSION,
            },
            actor="user",
        )
        with db.transaction() as cur:
            cur.execute(
                "INSERT INTO schedule_runs"
                " (schedule_id, thread_id, message, agent_runtime, model, effort,"
                " status, scheduled_for)"
                " VALUES (%s, 'schedule-1-run-9', 'Work', %s, %s, %s, 'succeeded', %s)"
                " RETURNING id",
                (
                    schedule["id"], SESSION["agent_runtime"], SESSION["model"],
                    SESSION["effort"], "2026-08-07T10:00:00Z",
                ),
            )
            run_id = int(cur.fetchone()[0])
        event = {"seq": 1, "event_type": "thread.message", "payload": {"message": "Done"}}
        with patch.object(schedules, "call_admin_api", return_value={"events": [event]}) as host:
            result = schedules.run_events(schedule["id"], run_id, {})
        self.assertEqual(result, {"events": [event], "retained": True})
        self.assertIn("/v1/threads/schedule-1-run-9/events", host.call_args.args[1])
        with patch.object(schedules, "call_admin_api", return_value={"events": []}):
            self.assertEqual(
                schedules.run_events(schedule["id"], run_id, {}),
                {"events": [], "retained": False},
            )

    def test_large_schedule_history_pages_are_rejected_before_serialization(self) -> None:
        schedule = schedules.create_schedule(
            {
                "name": "Review",
                "message": "x" * schedules.MAX_MESSAGE_CHARS,
                "cadence": "interval",
                "interval_minutes": 60,
                **SESSION,
            },
            actor="user",
        )
        with self.assertRaises(WorkspaceError) as too_large:
            schedules.list_revisions(schedule["id"], {"limit": ["11"]})
        self.assertEqual(too_large.exception.status, HTTPStatus.BAD_REQUEST)

        with self.assertRaises(WorkspaceError) as memory_too_large:
            memory.list_revisions("missing", {"limit": ["51"]})
        self.assertEqual(memory_too_large.exception.status, HTTPStatus.BAD_REQUEST)
        with self.assertRaises(WorkspaceError) as oversized_id:
            schedules.route_agent(
                "GET", f"/agent/schedules/{2**63}", None, {}
            )
        self.assertEqual(oversized_id.exception.status, HTTPStatus.BAD_REQUEST)

    def test_terminal_runs_and_deleted_schedules_expire_together(self) -> None:
        schedule = schedules.create_schedule(
            {
                "name": "Temporary",
                "message": "Do temporary work",
                "cadence": "interval",
                "interval_minutes": 60,
                **SESSION,
            },
            actor="user",
        )
        with db.transaction() as cur:
            cur.execute(
                "INSERT INTO schedule_runs"
                " (schedule_id, thread_id, message, agent_runtime, model, effort,"
                " status, scheduled_for, finished_at)"
                " VALUES (%s, 'schedule-1-run-99', 'Do temporary work', %s, %s, %s, 'succeeded',"
                " '2026-01-01T00:00:00Z',"
                " '2026-01-01T00:00:00Z')",
                (
                    schedule["id"], SESSION["agent_runtime"], SESSION["model"],
                    SESSION["effort"],
                ),
            )
        schedules.delete_schedule(
            schedule["id"], {"expected_revision": ["1"]}, actor="user"
        )
        with db.transaction() as cur:
            cur.execute(
                "UPDATE schedules SET deleted_at = '2026-01-01T00:00:00Z'"
                " WHERE id = %s",
                (schedule["id"],),
            )
        schedules.prune_retained(datetime(2026, 4, 2, tzinfo=timezone.utc))
        with self.assertRaises(WorkspaceError):
            schedules.load_schedule(schedule["id"], include_deleted=True)

    def test_fresh_terminal_run_keeps_its_deleted_schedule_until_retention(self) -> None:
        schedule = schedules.create_schedule(
            {
                "name": "Long running",
                "message": "Finish after deletion",
                "cadence": "interval",
                "interval_minutes": 60,
                **SESSION,
            },
            actor="user",
        )
        with db.transaction() as cur:
            cur.execute(
                "INSERT INTO schedule_runs"
                " (schedule_id, thread_id, message, agent_runtime, model, effort,"
                " status, scheduled_for, finished_at)"
                " VALUES (%s, 'schedule-1-run-100', 'Finish after deletion', %s, %s, %s, 'succeeded',"
                " '2026-01-01T00:00:00Z',"
                " '2026-04-01T00:00:00Z') RETURNING id",
                (
                    schedule["id"], SESSION["agent_runtime"], SESSION["model"],
                    SESSION["effort"],
                ),
            )
            run_row = cur.fetchone()
            assert run_row is not None
            run_id = int(run_row[0])
        schedules.delete_schedule(
            schedule["id"], {"expected_revision": ["1"]}, actor="user"
        )
        with db.transaction() as cur:
            cur.execute(
                "UPDATE schedules SET deleted_at = '2026-01-01T00:00:00Z' WHERE id = %s",
                (schedule["id"],),
            )

        schedules.prune_retained(datetime(2026, 4, 2, tzinfo=timezone.utc))
        self.assertTrue(
            schedules.load_schedule(schedule["id"], include_deleted=True)["deleted"]
        )
        self.assertEqual(
            schedules.load_run(schedule["id"], run_id)["status"], "succeeded"
        )

        schedules.prune_retained(datetime(2026, 7, 1, tzinfo=timezone.utc))
        with self.assertRaises(WorkspaceError):
            schedules.load_schedule(schedule["id"], include_deleted=True)


class WorkspaceIdentityTests(unittest.TestCase):
    def test_managed_claude_settings_disable_native_memory(self) -> None:
        settings_path = (
            Path(__file__).parents[1]
            / "host/bootstrap/agent-home/.claude/settings.json"
        )
        settings = json.loads(settings_path.read_text())
        self.assertIs(settings["autoMemoryEnabled"], False)
        self.assertEqual(settings["env"]["CLAUDE_CODE_DISABLE_AUTO_MEMORY"], "1")

    def test_managed_claude_settings_disable_background_execution(self) -> None:
        settings_path = (
            Path(__file__).parents[1]
            / "host/bootstrap/agent-home/.claude/settings.json"
        )
        settings = json.loads(settings_path.read_text())
        self.assertIs(settings["disableAgentView"], True)
        self.assertEqual(
            settings["env"]["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"], "1"
        )

    def test_identity_comes_from_the_peer_cgroup_and_is_not_caller_supplied(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cgroup = root / "42" / "cgroup"
            cgroup.parent.mkdir()
            cgroup.write_text("0::/kern_agent.slice/kern-agent-thread-app-7.scope\n")
            with patch.object(agent_api, "PROC_ROOT", root):
                self.assertEqual(agent_api._peer_thread_id(42), "app-7")
        self.assertEqual(
            agent_api.dispatch_call(
                "GET", "/agent/identity", None, peer_thread_id="app-7"
            ),
            {"status": 200, "body": {"thread_id": "app-7"}},
        )
        with self.assertRaises(WorkspaceError) as unavailable:
            agent_api.dispatch_call("GET", "/agent/identity", None)
        self.assertEqual(unavailable.exception.status, HTTPStatus.CONFLICT)

    def test_agent_dispatch_exposes_crud_but_not_revision_restore(self) -> None:
        with (
            patch.object(memory, "route_agent", return_value={"pages": []}) as memory_route,
            patch.object(schedules, "route_agent", return_value={"schedules": []}) as schedule_route,
        ):
            agent_api.dispatch_call("GET", "/agent/memory?limit=5", None)
            agent_api.dispatch_call("GET", "/agent/schedules", None)
        memory_route.assert_called_once_with("GET", "/agent/memory", None, {"limit": ["5"]})
        schedule_route.assert_called_once_with("GET", "/agent/schedules", None, {})
        with self.assertRaises(WorkspaceError):
            agent_api.dispatch_call("GET", "/agent/memory?unexpected=", None)

    def test_agent_dispatch_exposes_recent_schedule_failures(self) -> None:
        expected = {"failures": [{"id": 7, "status": "failed"}]}
        with patch.object(
            schedules, "route_agent", return_value=expected
        ) as schedule_route:
            response = agent_api.dispatch_call(
                "GET", "/agent/schedules/recent-failures?limit=5", None
            )
        self.assertEqual(response, {"status": 200, "body": expected})
        schedule_route.assert_called_once_with(
            "GET",
            "/agent/schedules/recent-failures",
            None,
            {"limit": ["5"]},
        )

    def test_agent_can_discover_valid_schedule_session_options(self) -> None:
        response = schedules.route_agent(
            "GET", "/agent/schedules/session-options", None, {}
        )
        self.assertIn("codex", response["session_options"])
        # Schedules are the surface that offers the script runtime, so the
        # agent can discover it here rather than having to know it exists.
        self.assertEqual(response["session_options"]["script"], {"bash": ["fixed"]})
        with self.assertRaises(WorkspaceError):
            schedules.route_agent(
                "GET", "/agent/schedules/session-options", None, {"extra": ["1"]}
            )


if __name__ == "__main__":
    unittest.main()
