"""Contracts for Kern's unified Chat and Web Apps Workspace service."""

from __future__ import annotations

from http import HTTPStatus
import unittest
from pathlib import Path
from unittest.mock import patch

from host.constants import SERVICE_ACCOUNTS
from host.runtime.workspace import service
from host.runtime.workspace.chat import backend as chat
from host.runtime.workspace.web_apps import backend as web_apps


class WorkspaceTests(unittest.TestCase):
    def test_one_fixed_service_identity_and_direct_product_ids(self) -> None:
        self.assertIsNotNone(chat.THREAD_ID_RE.fullmatch("thread-1"))
        self.assertIsNotNone(web_apps.APP_ID_RE.fullmatch("app-1"))
        self.assertEqual(SERVICE_ACCOUNTS["kern-workspace"], 47750)
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
                "workspace-scheduler",
                "workspace-maintenance",
                "tcp-serve",
            ],
        )

    def test_workspace_service_owns_workspace_storage_maintenance(self) -> None:
        with (
            patch.object(service.memory, "prune_deleted") as prune_memory,
            patch.object(service.schedules, "prune_retained") as prune_schedules,
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
