"""Global Workspace memory and schedule behavior."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
import json
from pathlib import Path
import tempfile
from typing import Any
import unittest
from unittest.mock import patch

import pg_harness

from host.runtime.core import db
from host.runtime.workspace import agent_api, getting_started, memory, schedules
from host.runtime.workspace.host_api import WorkspaceError
from host.runtime.workspace.web_apps import backend as web_apps


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

    def test_onboarding_status_is_derived_from_live_resources(self) -> None:
        active = patch.object(
            getting_started, "active_agent_runtimes", return_value=["codex"]
        )
        with active:
            status = getting_started.completion_status()
        self.assertEqual(
            status,
            {
                "provider_ready": True,
                "chat_created": False,
                "app_created": False,
                "schedule_created": False,
                "dismissed": False,
            },
        )

        # Chat writes this row while sending, and it is the Workspace-owned
        # signal the checklist is allowed to read.
        with db.transaction() as cur:
            cur.execute(
                "INSERT INTO chat_threads (thread_id, archived)"
                " VALUES ('thread-7', FALSE)"
            )

        web_apps.create_web_app()
        schedules.create_schedule(
            {
                "name": "Daily plan",
                "message": "Review priorities",
                "cadence": "daily",
                "interval_minutes": None,
                "daily_time": "09:00",
                **SESSION,
            },
            actor="user",
        )

        with active:
            self.assertEqual(
                getting_started.completion_status(),
                {
                    "provider_ready": True,
                    "chat_created": True,
                    "app_created": True,
                    "schedule_created": True,
                    "dismissed": False,
                },
            )

        # Nothing is latched: deactivating every provider takes the first step
        # back to incomplete rather than leaving a stale tick behind.
        with patch.object(getting_started, "active_agent_runtimes", return_value=[]):
            self.assertIs(
                getting_started.completion_status()["provider_ready"], False
            )

        with active:
            self.assertIs(getting_started.dismiss()["dismissed"], True)
            # Dismissing twice stays a no-op rather than raising on the key.
            self.assertIs(getting_started.dismiss()["dismissed"], True)

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

    def test_memory_links_follow_source_updates_deletes_and_restores(self) -> None:
        def stored_links() -> list[tuple[str, str]]:
            with db.transaction() as cur:
                cur.execute(
                    "SELECT source_page_id, target_page_id FROM memory_page_links"
                    " ORDER BY source_page_id, target_page_id"
                )
                return [(str(source), str(target)) for source, target in cur.fetchall()]

        for page_id in ("target-a", "target-b"):
            memory.save_page(
                page_id,
                {
                    "description": f"Target {page_id}",
                    "content": "Target content.",
                    "expected_revision": 0,
                },
                actor="agent",
            )
        source = memory.save_page(
            "source",
            {
                "description": "Link source",
                "content": "See [[target-a]].",
                "expected_revision": 0,
            },
            actor="agent",
        )
        self.assertEqual(memory.load_page("target-a")["backlinks"], ["source"])
        self.assertEqual(stored_links(), [("source", "target-a")])

        source = memory.save_page(
            "source",
            {
                "description": "Link source",
                "content": "Now see [[target-b]].",
                "expected_revision": source["revision"],
            },
            actor="agent",
        )
        self.assertEqual(memory.load_page("target-a")["backlinks"], [])
        self.assertEqual(memory.load_page("target-b")["backlinks"], ["source"])
        self.assertEqual(stored_links(), [("source", "target-b")])

        memory.delete_page(
            "source",
            {"expected_revision": [str(source["revision"])]},
            actor="agent",
        )
        self.assertEqual(memory.load_page("target-b")["backlinks"], [])
        self.assertEqual(stored_links(), [])

        restored = memory.restore_revision("source", 1, {"expected_revision": 3})
        self.assertEqual(memory.load_page("target-a")["backlinks"], ["source"])
        self.assertEqual(memory.load_page("target-b")["backlinks"], [])
        self.assertEqual(stored_links(), [("source", "target-a")])

        # Restoring the deleted revision clears outgoing rows again.
        memory.restore_revision(
            "source", 3, {"expected_revision": restored["revision"]}
        )
        self.assertEqual(memory.load_page("target-a")["backlinks"], [])
        self.assertEqual(stored_links(), [])

    def test_target_delete_preserves_dangling_link_for_restore(self) -> None:
        target = memory.save_page(
            "target",
            {
                "description": "Link target",
                "content": "Target content.",
                "expected_revision": 0,
            },
            actor="agent",
        )
        memory.save_page(
            "source",
            {
                "description": "Link source",
                "content": "See [[target]].",
                "expected_revision": 0,
            },
            actor="agent",
        )
        memory.delete_page(
            "target",
            {"expected_revision": [str(target["revision"])]},
            actor="agent",
        )
        with db.transaction() as cur:
            cur.execute(
                "SELECT source_page_id, target_page_id FROM memory_page_links"
            )
            self.assertEqual(cur.fetchall(), [("source", "target")])

        memory.restore_revision("target", 1, {"expected_revision": 2})
        self.assertEqual(memory.load_page("target")["backlinks"], ["source"])

    def test_agent_memory_search_combines_semantic_and_lexical_matches(self) -> None:
        first = memory.save_page(
            "identity-provider",
            {
                "description": "OAuth callback troubleshooting",
                "content": "Check the redirect URI and client registration.",
                "expected_revision": 0,
            },
            actor="agent",
        )
        memory.save_page(
            "deployment",
            {
                "description": "Production deployment checklist",
                "content": "Verify the release before shifting traffic.",
                "expected_revision": 0,
            },
            actor="agent",
        )
        pending = memory._unembedded_memory_pages("test-model", 10)
        self.assertEqual(
            {page_id for page_id, _revision, _description, _content in pending},
            {"identity-provider", "deployment"},
        )
        identity_vector = [1.0] + [0.0] * 383
        deployment_vector = [0.0, 1.0] + [0.0] * 382
        memory._store_memory_page_embeddings(
            "test-model",
            [
                ("identity-provider", first["revision"], identity_vector),
                ("deployment", 1, deployment_vector),
            ],
        )

        with (
            patch.object(memory.embedding_client, "MODEL_NAME", "test-model"),
            patch.object(
                memory.embedding_client,
                "embed_texts",
                return_value=[identity_vector],
            ),
        ):
            response = memory.route_agent(
                "GET",
                "/agent/memory/search",
                None,
                {"q": ["unable to sign in"]},
            )

        self.assertEqual(response["search_mode"], "hybrid")
        self.assertEqual(response["pages"][0]["page_id"], "identity-provider")

        memory.save_page(
            "identity-provider",
            {
                "description": "Updated identity notes",
                "content": "The old callback advice no longer applies.",
                "expected_revision": first["revision"],
            },
            actor="agent",
        )
        self.assertEqual(
            [row[0] for row in memory._unembedded_memory_pages("test-model", 10)],
            ["identity-provider"],
        )

    def test_agent_memory_search_drops_an_unrelated_replacement_from_the_old_query(
        self,
    ) -> None:
        original_query = [1.0] + [0.0] * 383
        # Cosine similarity to original_query is 0.44: close to the 0.438
        # measured for the stale real-host pair, high enough to pass the old
        # 0.35 cutoff, but below the calibrated retrieval threshold. The
        # vector remains normalized.
        replacement_query = [0.44, 0.897997772825746] + [0.0] * 382
        first = memory.save_page(
            "replacement-probe",
            {
                "description": "Deployment recovery procedure",
                "content": (
                    "If the deployment fails, restore the previous release artifact "
                    "and redirect traffic to it."
                ),
                "expected_revision": 0,
            },
            actor="agent",
        )
        memory._store_memory_page_embeddings(
            "test-model",
            [("replacement-probe", first["revision"], original_query)],
        )

        def embed_query(texts: list[str], *, kind: str) -> list[list[float]]:
            self.assertEqual(kind, "query")
            self.assertEqual(len(texts), 1)
            if texts[0] == "How can a guest get into the building?":
                return [replacement_query]
            return [original_query]

        with (
            patch.object(memory.embedding_client, "MODEL_NAME", "test-model"),
            patch.object(memory.embedding_client, "embed_texts", side_effect=embed_query),
        ):
            initial = memory.search_swarm_pages(
                {"q": ["How should we undo a broken launch?"]}
            )
        self.assertEqual(
            [page["page_id"] for page in initial["pages"]],
            ["replacement-probe"],
        )

        updated = memory.save_page(
            "replacement-probe",
            {
                "description": "Office access procedure",
                "content": (
                    "Visitors must obtain a temporary badge from the reception desk "
                    "before entering."
                ),
                "expected_revision": first["revision"],
            },
            actor="agent",
        )
        memory._store_memory_page_embeddings(
            "test-model",
            [("replacement-probe", updated["revision"], replacement_query)],
        )

        with (
            patch.object(memory.embedding_client, "MODEL_NAME", "test-model"),
            patch.object(memory.embedding_client, "embed_texts", side_effect=embed_query),
        ):
            old_topic = memory.search_swarm_pages(
                {"q": ["How should we undo a broken launch?"]}
            )
            new_topic = memory.search_swarm_pages(
                {"q": ["How can a guest get into the building?"]}
            )

        self.assertEqual(old_topic["pages"], [])
        # Popular suggestions are intentionally query-independent and remain a
        # separate response field; callers must not flatten them into matches.
        self.assertIn(
            "replacement-probe",
            [page["page_id"] for page in old_topic["popular_pages"]],
        )
        self.assertEqual(
            [page["page_id"] for page in new_topic["pages"]],
            ["replacement-probe"],
        )
        self.assertEqual(new_topic["pages"][0]["revision"], updated["revision"])

    def test_agent_memory_search_falls_back_to_bounded_lexical_search(self) -> None:
        memory.save_page(
            "rollback",
            {
                "description": "Production rollback procedure",
                "content": "Return to the previous release.",
                "expected_revision": 0,
            },
            actor="agent",
        )
        with patch.object(
            memory.embedding_client,
            "embed_texts",
            side_effect=memory.embedding_client.EmbeddingError("offline"),
        ):
            response = memory.route_agent(
                "GET",
                "/agent/memory/search",
                None,
                {"q": ["rollback"], "limit": ["1"]},
            )

        self.assertEqual(response["search_mode"], "lexical_fallback")
        self.assertEqual(response["pages"][0]["page_id"], "rollback")

    def test_description_substring_matches_page_beyond_the_exact_window(self) -> None:
        # "auth" is not a full-text token of "OAuth", so these pages are only
        # reachable through the substring channel. More than EXACT_CANDIDATES of
        # them must still all be reachable, rather than stopping at that
        # booster's bound with no cursor to continue.
        total = memory.EXACT_CANDIDATES + 10
        expected = {f"oauth-note-{index:03d}" for index in range(total)}
        for page_id in sorted(expected):
            memory.save_page(
                page_id,
                {
                    "description": f"OAuth callback note {page_id}",
                    "content": "unrelated body",
                    "expected_revision": 0,
                },
                actor="agent",
            )

        seen: set[str] = set()
        with patch.object(
            memory.embedding_client,
            "embed_texts",
            side_effect=memory.embedding_client.EmbeddingError("offline"),
        ):
            query: dict[str, list[str]] = {"q": ["auth"], "limit": ["20"]}
            response = memory.search_swarm_pages(query)
            seen.update(page["page_id"] for page in response["pages"])
            pages = 1
            while "next_cursor" in response:
                response = memory.search_swarm_pages(
                    {**query, "cursor": [response["next_cursor"]]}
                )
                seen.update(page["page_id"] for page in response["pages"])
                pages += 1
                self.assertLessEqual(pages, 20, "pagination did not terminate")

        self.assertEqual(response["search_mode"], "lexical_fallback")
        self.assertEqual(expected - seen, set())

    def test_reciprocal_links_do_not_consume_the_neighbor_budget(self) -> None:
        # A page that links back to the seed appears in both halves of the edge
        # union. Numbering each copy separately would spend two of the seed's
        # neighbour slots on one page and push valid neighbours out.
        per_seed = memory.GRAPH_NEIGHBORS_PER_SEED
        neighbours = [f"n{index:02d}" for index in range(per_seed + 4)]
        reciprocal = set(neighbours[:5])
        for page_id in neighbours:
            memory.save_page(
                page_id,
                {
                    "description": f"Neighbour {page_id}",
                    "content": "Links [[seed]]." if page_id in reciprocal else "Leaf.",
                    "expected_revision": 0,
                },
                actor="agent",
            )
        memory.save_page(
            "seed",
            {
                "description": "Seed page",
                "content": " ".join(f"[[{page_id}]]" for page_id in neighbours),
                "expected_revision": 0,
            },
            actor="agent",
        )

        rows = memory._search_pages_graph(
            ["seed"],
            scope="swarm",
            limit=memory.GRAPH_SEEDS * per_seed,
        )
        found = {str(row[0]) for row in rows}
        self.assertNotIn("seed", found)
        self.assertEqual(len(found), per_seed)

    def test_search_restarts_when_pages_change_between_candidate_queries(self) -> None:
        # The candidate channels each run in their own transaction. A page
        # removed after its channel ran must not be reported from the stale
        # tuple the fusion is still holding.
        memory.save_page(
            "deployment",
            {
                "description": "Production deployment checklist",
                "content": "Verify the release before shifting traffic.",
                "expected_revision": 0,
            },
            actor="agent",
        )
        real_exact = memory._search_pages_exact

        def delete_after_exact(*args: Any, **kwargs: Any) -> list[tuple[Any, ...]]:
            rows = real_exact(*args, **kwargs)
            memory.delete_page(
                "deployment", {"expected_revision": ["1"]}, actor="agent"
            )
            return rows

        with (
            patch.object(memory, "_search_pages_exact", side_effect=delete_after_exact),
            patch.object(
                memory.embedding_client,
                "embed_texts",
                side_effect=memory.embedding_client.EmbeddingError("offline"),
            ),
            self.assertRaises(WorkspaceError) as raised,
        ):
            memory.search_swarm_pages({"q": ["deployment"]})

        self.assertEqual(raised.exception.status, HTTPStatus.CONFLICT)
        self.assertIn("restart pagination", raised.exception.message)

    def test_restoring_a_deleted_revision_drops_the_embedding(self) -> None:
        first = memory.save_page(
            "deployment",
            {
                "description": "Production checklist",
                "content": "Verify the release.",
                "expected_revision": 0,
            },
            actor="agent",
        )
        memory.delete_page(
            "deployment",
            {"expected_revision": [str(first["revision"])]},
            actor="agent",
        )
        restored = memory.restore_revision(
            "deployment", first["revision"], {"expected_revision": 2}
        )
        memory._store_memory_page_embeddings(
            "test-model",
            [("deployment", restored["revision"], [1.0] + [0.0] * 383)],
        )

        # Restoring back onto the deleted revision must clear the vector; the
        # index loop skips deleted pages, so nothing else would remove it.
        memory.restore_revision(
            "deployment", 2, {"expected_revision": restored["revision"]}
        )
        with db.transaction() as cur:
            cur.execute(
                "SELECT count(*) FROM memory_page_embeddings WHERE page_id = 'deployment'"
            )
            self.assertEqual(cur.fetchone()[0], 0)

    def test_current_memory_embedding_replaces_old_revision(self) -> None:
        vector = [1.0] + [0.0] * 383
        first = memory.save_page(
            "page-a",
            {
                "description": "Original notes",
                "content": "Stored context.",
                "expected_revision": 0,
            },
            actor="agent",
        )
        memory._store_memory_page_embeddings(
            "test-model", [("page-a", first["revision"], vector)]
        )
        updated = memory.save_page(
            "page-a",
            {
                "description": "Replacement notes",
                "content": "Current context.",
                "expected_revision": first["revision"],
            },
            actor="agent",
        )

        self.assertEqual(
            [row[0] for row in memory._unembedded_memory_pages("test-model", 10)],
            ["page-a"],
        )
        memory._store_memory_page_embeddings(
            "test-model", [("page-a", updated["revision"], vector)]
        )
        with db.transaction() as cur:
            cur.execute(
                "SELECT revision FROM memory_page_embeddings WHERE page_id = 'page-a'"
            )
            self.assertEqual(cur.fetchall(), [(2,)])

    def test_exact_identity_and_one_hop_graph_improve_agent_search(self) -> None:
        memory.save_page(
            "deployment-runbook",
            {
                "description": "Production release procedure",
                "content": "Read [[rollback-plan]] before shipping.",
                "expected_revision": 0,
            },
            actor="agent",
        )
        memory.save_page(
            "rollback-plan",
            {
                "description": "Emergency recovery",
                "content": "Restore the previous release.",
                "expected_revision": 0,
            },
            actor="agent",
        )
        memory.save_page(
            "other-page",
            {
                "description": "Production release procedure notes",
                "content": "A secondary exact-term match.",
                "expected_revision": 0,
            },
            actor="agent",
        )

        with patch.object(
            memory.embedding_client,
            "embed_texts",
            side_effect=memory.embedding_client.EmbeddingError("offline"),
        ):
            response = memory.search_swarm_pages(
                {"q": ["deployment-runbook"], "limit": ["3"]}
            )

        self.assertEqual(response["pages"][0]["page_id"], "deployment-runbook")
        self.assertIn("rollback-plan", [page["page_id"] for page in response["pages"]])
        with db.transaction() as cur:
            cur.execute(
                "SELECT source_page_id, target_page_id FROM memory_page_links"
            )
            self.assertEqual(cur.fetchall(), [("deployment-runbook", "rollback-plan")])

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

        with patch.object(
            memory.embedding_client,
            "embed_texts",
            side_effect=memory.embedding_client.EmbeddingError("offline"),
        ):
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

        with patch.object(
            memory.embedding_client,
            "embed_texts",
            side_effect=memory.embedding_client.EmbeddingError("offline"),
        ):
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

    def test_agent_memory_weak_search_treats_or_as_a_literal_acronym(self) -> None:
        memory.save_page(
            "operating-room",
            {
                "description": "Surgical suite procedure",
                "content": "Consult the OR scheduling desk before access.",
                "expected_revision": 0,
            },
            actor="agent",
        )

        # Exercise the weak query directly with another term that does not
        # match. PostgreSQL may recover a standalone OR as a lexeme, while an
        # unquoted OR in this expression is ambiguous with the Boolean operator.
        with db.transaction() as cur:
            rows = memory._weak_search_rows(cur, "missing OR", scope="swarm")

        self.assertEqual([row[0] for row in rows], ["operating-room"])

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


class MemorySearchPagingTests(unittest.TestCase):
    """Hybrid paging depth, without a database."""

    @staticmethod
    def _row(index: int) -> tuple[Any, ...]:
        return (
            f"page-{index:04d}",
            f"Description {index}",
            "content",
            1,
            None,
            "agent",
            "2026-07-01T00:00:00Z",
            "2026-07-01T00:00:00Z",
            1.0 - index / 10000,
        )

    def test_weak_search_ignores_stopwords(self) -> None:
        self.assertEqual(
            memory._weak_search_tokens("How should we undo a broken launch?"),
            ["undo", "broken", "launch"],
        )
        self.assertEqual(
            memory._weak_search_tokens("Reset IT password for US CAN MAY"),
            ["reset", "it", "password", "us", "can", "may"],
        )

    def test_paging_reaches_lexical_matches_below_the_fusion_window(self) -> None:
        # 500 lexical matches: a fixed 200-row fusion window would strand every
        # match past the 200th with no cursor to reach it.
        total = 500
        depths: list[int] = []

        def lexical(_needle: str, limit: int, offset: int, **_kwargs: Any) -> list[tuple[Any, ...]]:
            return [self._row(i) for i in range(offset, min(offset + limit, total))]

        # Depth past the fusion window is carried by the id-only tail, which
        # reaches the requested page without selecting page content.
        def lexical_tail(_needle: str, limit: int, offset: int, **_kwargs: Any) -> list[str]:
            depths.append(offset + limit)
            return [f"page-{i:04d}" for i in range(offset, min(offset + limit, total))]

        seen: list[str] = []
        with (
            patch.object(memory, "_search_pages_lexical", side_effect=lexical),
            patch.object(memory, "_lexical_page_id_tail", side_effect=lexical_tail),
            patch.object(memory, "_search_pages_exact", return_value=[]),
            patch.object(memory, "_search_pages_graph", return_value=[]),
            patch.object(
                memory,
                "_current_page_rows",
                side_effect=lambda page_ids, *, scope: [
                    self._row(int(page_id.split("-")[1]))[:8] for page_id in page_ids
                ],
            ),
            patch.object(memory, "_record_memory_top_hit"),
            patch.object(
                memory.embedding_client,
                "embed_texts",
                side_effect=memory.embedding_client.EmbeddingError("offline"),
            ),
        ):
            query: dict[str, list[str]] = {"q": ["release"], "limit": ["50"]}
            response = memory.search_swarm_pages(query)
            seen.extend(page["page_id"] for page in response["pages"])
            while "next_cursor" in response:
                response = memory.search_swarm_pages(
                    {**query, "cursor": [response["next_cursor"]]}
                )
                self.assertTrue(response["pages"], "paged into an empty result page")
                seen.extend(page["page_id"] for page in response["pages"])
                self.assertLessEqual(len(seen), total, "pagination did not terminate")

        self.assertEqual(len(seen), total)
        self.assertEqual(seen[-1], f"page-{total - 1:04d}")
        self.assertEqual(len(seen), len(set(seen)))
        # Depth grows to cover the requested page instead of staying pinned.
        self.assertGreater(max(depths), memory.SEMANTIC_CANDIDATES)
        self.assertLessEqual(max(depths), memory.MAX_PAGES)

    def test_a_deleted_top_hit_backfills_from_the_next_candidates(self) -> None:
        # With limit=1 and the top candidate deleted between ranking and
        # revalidation, the response must fall through to the next valid
        # candidate rather than returning nothing and dropping to the weak
        # fallback while strong candidates remain.
        total = 5

        def lexical(_needle: str, limit: int, offset: int, **_kwargs: Any) -> list[tuple[Any, ...]]:
            return [self._row(i) for i in range(offset, min(offset + limit, total))]

        def current(page_ids: list[str], **_kwargs: Any) -> list[tuple[Any, ...]]:
            return [
                self._row(int(page_id.split("-")[1]))[:8]
                for page_id in page_ids
                if page_id != "page-0000"
            ]

        with (
            patch.object(memory, "_search_pages_lexical", side_effect=lexical),
            patch.object(memory, "_search_pages_exact", return_value=[]),
            patch.object(memory, "_search_pages_graph", return_value=[]),
            patch.object(memory, "_current_page_rows", side_effect=current),
            patch.object(memory, "_record_memory_top_hit"),
            patch.object(memory, "_memory_search_fallback") as fallback,
            patch.object(
                memory.embedding_client,
                "embed_texts",
                side_effect=memory.embedding_client.EmbeddingError("offline"),
            ),
        ):
            response = memory.search_swarm_pages({"q": ["release"], "limit": ["1"]})

        fallback.assert_not_called()
        self.assertEqual(
            [page["page_id"] for page in response["pages"]], ["page-0001"]
        )

    def test_deleted_deep_tail_slice_backfills_and_keeps_its_cursor(self) -> None:
        total = 300

        def lexical(
            _needle: str, limit: int, offset: int, **_kwargs: Any
        ) -> list[tuple[Any, ...]]:
            return [self._row(i) for i in range(offset, min(offset + limit, total))]

        def lexical_tail(
            _needle: str, limit: int, offset: int, **_kwargs: Any
        ) -> list[str]:
            return [f"page-{i:04d}" for i in range(offset, min(offset + limit, total))]

        deleted = {"page-0250", "page-0251"}

        def current(page_ids: list[str], **_kwargs: Any) -> list[tuple[Any, ...]]:
            return [
                self._row(int(page_id.split("-")[1]))[:8]
                for page_id in page_ids
                if page_id not in deleted
            ]

        with (
            patch.object(memory, "_search_pages_lexical", side_effect=lexical),
            patch.object(memory, "_lexical_page_id_tail", side_effect=lexical_tail),
            patch.object(memory, "_search_pages_exact", return_value=[]),
            patch.object(memory, "_search_pages_graph", return_value=[]),
            patch.object(memory, "_current_page_rows", side_effect=current),
            patch.object(memory, "_record_memory_top_hit"),
            patch.object(
                memory.embedding_client,
                "embed_texts",
                side_effect=memory.embedding_client.EmbeddingError("offline"),
            ),
        ):
            response = memory.search_swarm_pages(
                {
                    "q": ["release"],
                    "limit": ["2"],
                    "cursor": [
                        memory._encode_semantic_offset_cursor(
                            250,
                            "fallback",
                            memory._memory_search_fingerprint("release", "swarm"),
                            memory._memory_search_generation(),
                        )
                    ],
                }
            )

        self.assertEqual(
            [page["page_id"] for page in response["pages"]],
            ["page-0252", "page-0253"],
        )
        self.assertIn("next_cursor", response)

    def test_deeper_pages_do_not_rerank_the_consumed_prefix(self) -> None:
        # A page that is both a semantic hit and a deep lexical match must not
        # gain a lexical RRF score once a later request fetches past its rank:
        # that would move it into the already-consumed prefix and lose it.
        total = 400
        window = memory.SEMANTIC_CANDIDATES
        # The candidate ranks last among a full semantic set, so on its own it
        # sorts below the whole lexical window; its lexical rank sits past the
        # window, so only a deeper fetch can hand it a second RRF score.
        deep = self._row(240)
        semantic = [self._row(i) for i in range(window - 1)] + [deep]

        def lexical(_needle: str, limit: int, offset: int, **_kwargs: Any) -> list[tuple[Any, ...]]:
            return [self._row(i) for i in range(offset, min(offset + limit, total))]

        def lexical_tail(_needle: str, limit: int, offset: int, **_kwargs: Any) -> list[str]:
            return [f"page-{i:04d}" for i in range(offset, min(offset + limit, total))]

        seen: list[str] = []
        with (
            patch.object(memory, "_search_pages_lexical", side_effect=lexical),
            patch.object(memory, "_lexical_page_id_tail", side_effect=lexical_tail),
            patch.object(memory, "_search_pages_exact", return_value=[]),
            patch.object(memory, "_search_pages_graph", return_value=[]),
            patch.object(
                memory,
                "_current_page_rows",
                side_effect=lambda page_ids, *, scope: [
                    self._row(int(page_id.split("-")[1]))[:8] for page_id in page_ids
                ],
            ),
            patch.object(memory, "_record_memory_top_hit"),
            patch.object(memory.embedding_client, "embed_texts", return_value=[[0.0] * 384]),
            patch.object(memory, "_search_pages_semantic", return_value=semantic),
        ):
            query: dict[str, list[str]] = {"q": ["release"], "limit": ["50"]}
            response = memory.search_swarm_pages(query)
            seen.extend(page["page_id"] for page in response["pages"])
            while "next_cursor" in response:
                response = memory.search_swarm_pages(
                    {**query, "cursor": [response["next_cursor"]]}
                )
                seen.extend(page["page_id"] for page in response["pages"])
                self.assertLessEqual(len(seen), total + 1, "pagination did not terminate")

        self.assertIn(deep[0], seen)
        self.assertEqual(len(seen), len(set(seen)), "a page was returned twice")
        self.assertEqual(set(seen), {f"page-{i:04d}" for i in range(total)})


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
