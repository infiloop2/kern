"""Contracts for Kern's unified Chat and Web Apps Workspace service."""

from __future__ import annotations

from http import HTTPStatus
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from host.constants import SERVICE_ACCOUNTS
from host.runtime.workspace import getting_started, memory, service
from host.runtime.workspace.chat import backend as chat
from host.runtime.workspace.web_apps import backend as web_apps


class WorkspaceTests(unittest.TestCase):
    def test_memory_hybrid_cursor_retries_during_model_failure(self) -> None:
        rows = [
            (
                f"page-{index}",
                f"Page {index}",
                "content",
                1,
                None,
                "agent",
                "2026-07-01T00:00:00Z",
                "2026-07-01T00:00:00Z",
            )
            for index in range(3)
        ]

        def lexical(
            _needle: str,
            limit: int,
            offset: int,
            *,
            scope: str,
        ) -> list[tuple[object, ...]]:
            self.assertEqual(scope, "swarm")
            return rows[offset : offset + limit]

        with (
            patch.object(memory, "_search_pages_exact", return_value=[]),
            patch.object(memory, "_search_pages_lexical", side_effect=lexical) as search,
            patch.object(
                memory, "_search_pages_semantic", return_value=[]
            ) as semantic_search,
            patch.object(memory, "_search_pages_graph", return_value=[]),
            patch.object(
                memory,
                "_current_page_rows",
                side_effect=lambda page_ids, *, scope: [
                    row for row in rows if str(row[0]) in set(page_ids)
                ],
            ),
            patch.object(memory, "_record_memory_top_hit") as record_top_hit,
            patch.object(
                memory.embedding_client,
                "embed_texts",
                side_effect=[
                    [[0.0] * 384],
                    memory.embedding_client.EmbeddingError("offline"),
                    [[0.0] * 384],
                ],
            ),
        ):
            first = memory.route_agent(
                "GET", "/agent/memory/search", None, {"q": ["page"], "limit": ["1"]}
            )
            with self.assertRaises(memory.WorkspaceError) as unavailable:
                memory.route_agent(
                    "GET",
                    "/agent/memory/search",
                    None,
                    {
                        "q": ["page"],
                        "limit": ["1"],
                        "cursor": [first["next_cursor"]],
                    },
                )
            resumed = memory.route_agent(
                "GET",
                "/agent/memory/search",
                None,
                {
                    "q": ["page"],
                    "limit": ["1"],
                    "cursor": [first["next_cursor"]],
                },
            )

        self.assertEqual(first["pages"][0]["page_id"], "page-0")
        self.assertEqual(unavailable.exception.status, HTTPStatus.SERVICE_UNAVAILABLE)
        self.assertEqual(resumed["search_mode"], "hybrid")
        self.assertEqual(resumed["pages"][0]["page_id"], "page-1")
        record_top_hit.assert_called_once_with("page-0")
        self.assertEqual([call.args[2] for call in search.call_args_list], [0, 0, 0])
        self.assertEqual(semantic_search.call_count, 2)

    def test_agent_memory_search_rejects_pre_snapshot_offset_cursor(self) -> None:
        with self.assertRaises(memory.WorkspaceError) as raised:
            memory.route_agent(
                "GET",
                "/agent/memory/search",
                None,
                {"q": ["valid query"], "cursor": [memory._encode_offset_cursor(1)]},
            )

        self.assertEqual(raised.exception.status, HTTPStatus.BAD_REQUEST)

    def test_fallback_memory_cursor_keeps_lexical_ranking_after_recovery(self) -> None:
        fingerprint = memory._memory_search_fingerprint("valid query", "swarm")
        cursor = memory._encode_semantic_offset_cursor(
            1, "fallback", fingerprint, memory._memory_search_generation()
        )
        with (
            patch.object(memory, "_search_pages_exact", return_value=[]),
            patch.object(memory, "_search_pages_lexical", return_value=[]),
            patch.object(memory, "_lexical_page_id_tail", return_value=[]),
            patch.object(memory, "_search_pages_graph", return_value=[]),
            patch.object(memory, "_memory_search_fallback", return_value={"pages": []}),
            patch.object(memory.embedding_client, "embed_texts") as embed,
        ):
            response = memory.search_swarm_pages(
                {"q": ["valid query"], "cursor": [cursor]}
            )

        self.assertEqual(response["search_mode"], "lexical_fallback")
        embed.assert_not_called()

    def test_hybrid_memory_cursor_expires_when_candidates_change(self) -> None:
        fingerprint = memory._memory_search_fingerprint("valid query", "swarm")
        generation = memory._memory_search_generation()
        cursor = memory._encode_semantic_offset_cursor(
            1, "hybrid", fingerprint, generation
        )
        memory._advance_memory_search_generation()

        with self.assertRaises(memory.WorkspaceError) as raised:
            memory.search_swarm_pages(
                {"q": ["valid query"], "cursor": [cursor]}
            )

        self.assertEqual(raised.exception.status, HTTPStatus.CONFLICT)

    def test_hybrid_memory_cursor_is_bound_to_its_query(self) -> None:
        fingerprint = memory._memory_search_fingerprint("valid query", "swarm")
        cursor = memory._encode_semantic_offset_cursor(
            1, "hybrid", fingerprint, memory._memory_search_generation()
        )

        with self.assertRaises(memory.WorkspaceError) as raised:
            memory.search_swarm_pages(
                {"q": ["different query"], "cursor": [cursor]}
            )

        self.assertEqual(raised.exception.status, HTTPStatus.BAD_REQUEST)

    def test_agent_memory_search_rejects_malformed_input_before_database_work(self) -> None:
        for query in ({"q": ["   "]}, {"q": ["bad\x00query"]}, {"q": ["x" * 201]}):
            with self.subTest(query=query), self.assertRaises(memory.WorkspaceError):
                memory.route_agent("GET", "/agent/memory/search", None, query)

        with self.assertRaises(memory.WorkspaceError):
            memory.route_agent(
                "GET",
                "/agent/memory/search",
                None,
                {"q": ["valid query"], "cursor": ["!" * 513]},
            )
        oversized_snapshot = memory._encode_cursor(str(memory.MAX_PAGES + 1))
        with self.assertRaises(memory.WorkspaceError):
            memory.route_agent(
                "GET",
                "/agent/memory/search",
                None,
                {"q": ["valid query"], "cursor": [oversized_snapshot]},
            )

    def test_one_fixed_service_identity_and_direct_product_ids(self) -> None:
        self.assertIsNotNone(chat.THREAD_ID_RE.fullmatch("thread-1"))
        self.assertIsNotNone(web_apps.APP_ID_RE.fullmatch("app-1"))
        self.assertEqual(SERVICE_ACCOUNTS["kern-workspace"], 47750)
        self.assertEqual(SERVICE_ACCOUNTS["kern-embedding"], 47751)
        self.assertNotIn("kern-agent-workspace", SERVICE_ACCOUNTS)
        self.assertFalse(any(name.startswith("kern-app-") for name in SERVICE_ACCOUNTS))

    def test_all_schema_migrations_use_the_one_host_stream(self) -> None:
        migrations = Path("host/migrations")
        self.assertFalse((migrations / "workspace").exists())
        self.assertTrue((migrations / "0015_workspace_chat_baseline.sql").is_file())
        self.assertTrue((migrations / "0025_workspace_web_app_identity.sql").is_file())
        self.assertFalse(Path("host/runtime/deploy/workspace_migrate.py").exists())

    def test_direct_id_migration_moves_events_and_sessions(self) -> None:
        sql = Path("host/migrations/0014_direct_workspace_thread_ids.sql").read_text()
        self.assertIn("UPDATE agent_events", sql)
        self.assertIn("UPDATE thread_sessions", sql)
        self.assertIn("agent_chat__", sql)
        self.assertIn("personal_web_app_builder__", sql)
        self.assertNotIn("provider_session_id", sql)

    def test_browser_mutation_lock_uses_decoded_app_id(self) -> None:
        self.assertEqual(
            web_apps._browser_mutation_app_id("POST", "/apps/%61pp-1/messages"),
            "app-1",
        )
        with self.assertRaises(web_apps.WorkspaceError):
            web_apps._browser_mutation_app_id("POST", "/apps/app%2F1/messages")
        with self.assertRaises(web_apps.WorkspaceError):
            web_apps._browser_mutation_app_id("POST", "/apps/thread-1/messages")

    def test_workspace_backends_enforce_their_product_id_shapes(self) -> None:
        with self.assertRaises(chat.WorkspaceError) as chat_error:
            chat.route_browser("GET", "/threads/app-1/events", None)
        self.assertEqual(chat_error.exception.status, HTTPStatus.BAD_REQUEST)
        with self.assertRaises(chat.WorkspaceError) as send_error:
            chat.send_chat_message(
                {"thread_id": "app-1", "input_message": "cross product"}
            )
        self.assertEqual(send_error.exception.status, HTTPStatus.BAD_REQUEST)

        with self.assertRaises(web_apps.WorkspaceError) as app_error:
            web_apps.route_agent("GET", "/agent/apps/thread-1/state/meta", None)
        self.assertEqual(app_error.exception.status, HTTPStatus.BAD_REQUEST)

    def test_chat_clear_memory_proxies_one_host_route_for_chat_threads_only(self) -> None:
        with (
            patch.object(chat, "_require_chat_thread") as require_thread,
            patch.object(
                chat, "call_admin_api", return_value={"status": "cleared"}
            ) as admin_call,
        ):
            self.assertEqual(
                chat.route_browser("POST", "/threads/thread-3/clear-memory", None),
                {"status": "cleared"},
            )
        admin_call.assert_called_once_with(
            "POST", "/v1/threads/thread-3/clear-memory", None
        )
        # Archived threads are read-only, so the default (active-only) check
        # applies rather than Stop's include_archived form.
        require_thread.assert_called_once_with("thread-3")

        with self.assertRaises(chat.WorkspaceError) as cross_product:
            chat.route_browser("POST", "/threads/app-1/clear-memory", None)
        self.assertEqual(cross_product.exception.status, HTTPStatus.BAD_REQUEST)

    def test_clear_memory_absorbs_the_hosts_retryable_finishing_conflict(self) -> None:
        """Clear sits next to Stop, and a stopped turn stays live briefly.

        The thread already reads as idle then, so forwarding that conflict
        once would show a failure for an action that just needs a moment.
        """
        busy = chat.WorkspaceError(
            HTTPStatus.CONFLICT, "the thread is still finishing; retry shortly"
        )
        with (
            patch.object(chat, "_require_chat_thread"),
            patch.object(chat, "time"),
            patch.object(
                chat,
                "call_admin_api",
                side_effect=[busy, busy, {"status": "cleared"}],
            ) as admin_call,
        ):
            self.assertEqual(
                chat.route_browser("POST", "/threads/thread-3/clear-memory", None),
                {"status": "cleared"},
            )
        self.assertEqual(admin_call.call_count, 3)

        # A conflict the host did not mark retryable is still surfaced.
        running = chat.WorkspaceError(
            HTTPStatus.CONFLICT,
            "working memory can be cleared only while the thread is idle",
        )
        with (
            patch.object(chat, "_require_chat_thread"),
            patch.object(chat, "call_admin_api", side_effect=running) as admin_call,
            self.assertRaises(chat.WorkspaceError) as refused,
        ):
            chat.route_browser("POST", "/threads/thread-3/clear-memory", None)
        self.assertEqual(refused.exception.status, HTTPStatus.CONFLICT)
        self.assertEqual(admin_call.call_count, 1)

    def test_hiding_activity_still_fetches_the_working_memory_boundary(self) -> None:
        """The clear marker is the operator's only confirmation.

        Hiding activity drops thread.activity from the requested types, so the
        marker has its own display type and must survive that filter.
        """
        requested: list[str] = []
        with (
            patch.object(chat, "_require_chat_thread"),
            patch.object(
                chat,
                "call_admin_api",
                side_effect=lambda method, path: requested.append(path) or {"events": []},
            ),
        ):
            chat.route_browser("GET", "/threads/thread-3/events", None, {"activity": ["false"]})
            chat.route_browser("GET", "/threads/thread-3/events", None, {"activity": ["true"]})

        hidden, shown = requested
        self.assertIn("event_type=thread.memory_cleared", hidden)
        self.assertNotIn("event_type=thread.activity", hidden)
        self.assertIn("event_type=thread.memory_cleared", shown)
        self.assertIn("event_type=thread.activity", shown)

    def test_service_binds_both_endpoints_before_background_work(self) -> None:
        events: list[str] = []

        class FakeTcpServer:
            def __init__(self, *_args: object) -> None:
                events.append("tcp-bind")

            def serve_forever(self) -> None:
                events.append("tcp-serve")

        class FakeAgentServer:
            def __init__(self, *_args: object) -> None:
                events.append("agent-bind")

            def serve_forever(self) -> None:
                events.append("agent-serve")

        class FakeThread:
            def __init__(self, *, target, name: str, daemon: bool) -> None:
                self.target = target
                self.name = name

            def start(self) -> None:
                events.append(self.name)

        with (
            patch.object(service, "ThreadingHTTPServer", FakeTcpServer),
            patch.object(service.agent_api, "AgentWorkspaceServer", FakeAgentServer),
            patch.object(service.threading, "Thread", FakeThread),
        ):
            self.assertEqual(service.main(), 0)
        self.assertEqual(
            events,
            [
                "tcp-bind",
                "agent-bind",
                "workspace-agent-api",
                "workspace-memory-embedding-index",
                "workspace-scheduler",
                "workspace-maintenance",
                "tcp-serve",
            ],
        )

    def test_workspace_service_owns_workspace_storage_maintenance(self) -> None:
        with (
            patch.object(service.memory, "prune_deleted") as prune_memory,
            patch.object(service.schedules, "prune_deleted") as prune_schedules,
            patch.object(service.web_apps, "prune_revisions") as prune_apps,
        ):
            service.maintain_storage()

        prune_memory.assert_called_once_with()
        prune_schedules.assert_called_once_with()
        prune_apps.assert_called_once_with()

    def test_tcp_routes_by_path_without_identity_headers(self) -> None:
        handler = object.__new__(service.Handler)
        handler.path = "/chat/threads?archived=false"
        handler.headers = {}
        with (
            patch.object(service.Handler, "_read_body", return_value=None) as read_body,
            patch.object(service.Handler, "_send_json") as send_json,
            patch.object(service.chat, "route_browser", return_value={"threads": []}) as chat_route,
            patch.object(service.web_apps, "route_browser") as apps_route,
        ):
            handler._handle("GET")
        chat_route.assert_called_once_with("GET", "/threads", None, {"archived": ["false"]})
        read_body.assert_called_once_with(chat.MAX_REQUEST_BODY_BYTES)
        apps_route.assert_not_called()
        send_json.assert_called_once_with(HTTPStatus.OK, {"threads": []})

    def test_tcp_uses_the_web_app_body_limit_for_web_app_routes(self) -> None:
        handler = object.__new__(service.Handler)
        handler.path = "/apps/apps"
        handler.headers = {}
        with (
            patch.object(service.Handler, "_read_body", return_value=None) as read_body,
            patch.object(service.Handler, "_send_json"),
            patch.object(service.web_apps, "route_browser", return_value={"apps": []}),
        ):
            handler._handle("GET")
        read_body.assert_called_once_with(web_apps.MAX_REQUEST_BODY_BYTES)

    def test_getting_started_derives_every_step_from_live_state(self) -> None:
        transaction = MagicMock()
        cursor = transaction.__enter__.return_value
        cursor.fetchone.return_value = (True, False, True, False)
        with (
            patch.object(getting_started.db, "transaction", return_value=transaction),
            patch.object(
                getting_started, "active_agent_runtimes", return_value=["codex"]
            ),
        ):
            self.assertEqual(
                getting_started.completion_status(),
                {
                    "provider_ready": True,
                    "chat_created": True,
                    "app_created": False,
                    "schedule_created": True,
                    "dismissed": False,
                },
            )
        query = cursor.execute.call_args_list[0].args[0]
        # Nothing is latched: each step reads the resource it describes.
        self.assertIn("FROM chat_threads", query)
        self.assertIn("FROM web_apps", query)
        self.assertIn("FROM schedules", query)
        self.assertIn("FROM workspace_onboarding_dismissal", query)
        # Every table read here must be Workspace-owned: this role holds no
        # grant on host-owned tables, so reaching for one is a runtime failure.
        self.assertNotIn("thread_sessions", query)

    def test_getting_started_provider_step_follows_live_activation(self) -> None:
        transaction = MagicMock()
        cursor = transaction.__enter__.return_value
        cursor.fetchone.return_value = (True, True, True, False)
        cases = (
            (["codex"], True),
            ([], False),
            # Unknown activation reads as incomplete: the checklist only nudges,
            # so re-showing a step costs less than ticking an unconfirmed one.
            (None, False),
            # Kern runs the script runtime, so it can never stand in for a
            # provider the operator was supposed to connect.
            (["script"], False),
            (["script", "hermes"], True),
        )
        for activation, expected in cases:
            with (
                patch.object(getting_started.db, "transaction", return_value=transaction),
                patch.object(
                    getting_started, "active_agent_runtimes", return_value=activation
                ),
            ):
                status = getting_started.completion_status()
            self.assertIs(status["provider_ready"], expected, activation)

    def test_getting_started_dismissal_is_recorded_for_the_whole_host(self) -> None:
        transaction = MagicMock()
        cursor = transaction.__enter__.return_value
        cursor.fetchone.return_value = (False, False, False, True)
        with (
            patch.object(getting_started.db, "transaction", return_value=transaction),
            patch.object(getting_started, "active_agent_runtimes", return_value=[]),
        ):
            self.assertIs(getting_started.dismiss()["dismissed"], True)
        insert = cursor.execute.call_args_list[0].args[0]
        self.assertIn("INSERT INTO workspace_onboarding_dismissal", insert)
        # Dismissing twice must stay a no-op rather than raising on the key.
        self.assertIn("ON CONFLICT (singleton) DO NOTHING", insert)

    def test_tcp_routes_getting_started_status_without_mutation(self) -> None:
        handler = object.__new__(service.Handler)
        handler.path = "/getting-started"
        handler.headers = {}
        with (
            patch.object(service.Handler, "_read_body", return_value=None) as read_body,
            patch.object(service.Handler, "_send_json") as send_json,
            patch.object(
                service.getting_started,
                "route_browser",
                return_value={"chat_created": False},
            ) as status_route,
        ):
            handler._handle("GET")
        read_body.assert_called_once()
        status_route.assert_called_once_with("GET", "/getting-started", None, {})
        send_json.assert_called_once_with(HTTPStatus.OK, {"chat_created": False})

    def test_tcp_routes_the_getting_started_dismiss_subpath(self) -> None:
        # The browser posts to a subpath of the checklist route. Matching only
        # the exact path 404s every dismissal before the handler is reached.
        handler = object.__new__(service.Handler)
        handler.path = "/getting-started/dismiss"
        handler.headers = {}
        with (
            patch.object(service.Handler, "_read_body", return_value=None),
            patch.object(service.Handler, "_send_json") as send_json,
            patch.object(
                service.getting_started,
                "route_browser",
                return_value={"dismissed": True},
            ) as dismiss_route,
        ):
            handler._handle("POST")
        dismiss_route.assert_called_once_with(
            "POST", "/getting-started/dismiss", None, {}
        )
        send_json.assert_called_once_with(HTTPStatus.OK, {"dismissed": True})

    def test_tcp_rejects_agent_routes(self) -> None:
        handler = object.__new__(service.Handler)
        handler.path = "/agent/apps"
        handler.headers = {}
        with (
            patch.object(service.Handler, "_read_body", return_value=None),
            patch.object(service.Handler, "_send_json") as send_json,
        ):
            handler._handle("GET")
        send_json.assert_called_once_with(
            HTTPStatus.NOT_FOUND, {"error": {"message": "route not found"}}
        )


if __name__ == "__main__":
    unittest.main()
