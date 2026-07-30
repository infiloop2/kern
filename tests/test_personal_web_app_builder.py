"""Agentic Web App contract and backend validation tests."""

from __future__ import annotations

from http import HTTPStatus
import json
from pathlib import Path
from typing import Any
import unittest
from unittest.mock import MagicMock, call, patch

import pg_harness

from host.apps.personal_web_app_builder import backend
from host.runtime.core import app_platform
from host.runtime.core import db
from host.runtime.deploy import app_migrate, migrate
from tests.apps.personal_web_app_builder import smoke as builder_mock


REPO_ROOT = Path(__file__).resolve().parents[1]
APP_DIR = REPO_ROOT / "host" / "apps" / "personal_web_app_builder"


class AgenticWebAppContractTests(unittest.TestCase):
    def test_manifest_keeps_identity_and_rebrands_the_product(self) -> None:
        app = app_platform.app_by_id(backend.APP_ID)
        assert app is not None
        self.assertEqual(backend.APP_ID, "personal_web_app_builder")
        self.assertEqual(app.title, "Agentic Web App")
        self.assertEqual(app.allocation.port_offset, 6)
        self.assertEqual(app.db_schema, "app_personal_web_app_builder")
        self.assertEqual(app.release_stage, "stable")
        self.assertTrue(app.agent_api)
        self.assertTrue(app.capability_worker)

    def test_ui_uses_the_agent_chat_app_selector_model(self) -> None:
        index = (APP_DIR / "ui" / "index.html").read_text()
        source = (APP_DIR / "ui" / "personal_web_app_builder.js").read_text()
        css = (APP_DIR / "ui" / "personal_web_app_builder.css").read_text()
        self.assertIn('id="new-app"', index)
        self.assertIn('id="archived-toggle"', index)
        self.assertIn('id="apps"', index)
        self.assertIn('id="rename-app"', index)
        self.assertIn('class="app-rename-button"', index)
        self.assertIn('id="archive-app"', index)
        self.assertIn('id="history-loader"', index)
        self.assertIn('id="load-earlier"', index)
        self.assertIn('id="composer-running"', index)
        self.assertIn('id="stop-turn"', index)
        self.assertIn('id="attach-file"', index)
        self.assertIn('id="attachments"', index)
        self.assertIn('id="agent-settings-idle-note"', index)
        self.assertIn('id="agent-session-change-warning"', index)
        self.assertNotIn('id="agent-settings-help"', index)
        self.assertNotIn('id="first-run-guidance"', index)
        self.assertIn(
            ".agent-settings.active-locked:hover #agent-settings-idle-note",
            css,
        )
        self.assertNotIn(
            ".agent-settings.active-locked:hover .agent-settings-idle-note",
            css,
        )
        self.assertIn('/v1/apps/agent_chat/ui/rich_text.js', index)
        self.assertIn('/v1/apps/agent_chat/ui/rich_text.css', index)
        self.assertIn("app-dot", source)
        self.assertIn('"/apps?archived=true"', source)
        self.assertIn("selectedAppId = null", source)
        self.assertIn("INITIAL_CONVERSATION_EVENT_PAGES = 3", source)
        self.assertIn("conversationViewStates = new Map()", source)
        self.assertIn("KernRichText.renderMarkdown(entryData.message)", source)
        self.assertIn("stopRunningTurn()", source)
        self.assertIn("sessionConfigurationChanged()", source)
        self.assertIn("!fromGeneratedApp && sessionConfigurationChanged()", source)
        self.assertNotIn('showChatStatus("Sending…")', source)
        self.assertIn('classList.toggle("sending", composerSending)', source)
        self.assertIn(".send-button.sending::after", css)
        self.assertIn('showChatStatus("Stopping…")', source)
        self.assertNotIn("Waiting for agent to start", source)
        self.assertIn('"kern-app-upload-file"', source)
        self.assertIn("[User-uploaded file: ${attachment.file.path}]", source)
        self.assertNotIn("chat-task-meta", source)
        self.assertIn("/conversation/events?before=${before}", source)
        self.assertIn("loadOlderConversationEvents()", source)
        self.assertIn("refreshSequence !== appsRefreshSequence", source)
        self.assertIn("if (selectedAppId === threadId) clearSelectedApp()", source)
        self.assertIn("COMPOSER_DRAFTS_STORAGE_KEY", source)
        self.assertIn("localStorage.setItem", source)
        self.assertNotIn("conversation/events?since=0", source)
        self.assertNotIn("reset-app", index)
        self.assertNotIn('api("POST", "/reset")', source)
        self.assertIn("@media (max-width: 720px)", css)
        self.assertEqual(
            (backend.SEND_BUSY_RETRIES - 1) * backend.SEND_BUSY_RETRY_DELAY_SECONDS,
            10,
        )

    def test_generated_worker_is_pinned_to_its_workspace(self) -> None:
        source = (APP_DIR / "ui" / "personal_web_app_builder.js").read_text()
        self.assertIn("new Worker(url)", source)
        self.assertIn("threadId,", source)
        self.assertIn("selectedAppId !== run.threadId", source)
        self.assertIn("stopCapabilityWorker()", source)
        self.assertIn("encodeURIComponent(run.threadId)", source)
        self.assertIn("void sendMessage(message.message.trim(), run.threadId)", source)
        self.assertIn(
            "conversationResponse.session || listedSession || snapshot.session",
            source,
        )
        self.assertIn("generated-host\").classList.toggle(\"readonly\"", source)
        self.assertIn("MAX_WORKER_MUTATIONS_PER_TURN = 16", source)
        self.assertIn('"fetch", "XMLHttpRequest", "WebSocket"', source)
        self.assertNotIn("window.open", source)
        self.assertNotIn("location.href", source)

    def test_agent_instructions_describe_one_thread_per_workspace(self) -> None:
        instructions = (APP_DIR / "agent.md").read_text()
        self.assertIn("This thread belongs permanently to this workspace", instructions)
        self.assertIn("app.askAgent(message)", instructions)
        self.assertIn("app.set(path, value)", instructions)
        self.assertIn('`{"action":"set","expected_revision":3', instructions)
        self.assertIn("Always register `app.onLoad`", instructions)

    def test_new_migration_replaces_singleton_with_clean_workspace_rows(self) -> None:
        migration = (APP_DIR / "migrations" / "0003_multiple_web_apps.sql").read_text()
        self.assertIn("DROP TABLE app_state", migration)
        self.assertIn("CREATE TABLE web_apps", migration)
        for column in (
            "thread_id TEXT PRIMARY KEY",
            "name TEXT NOT NULL",
            "archived BOOLEAN",
            "revision BIGINT",
            "html TEXT",
            "css TEXT",
            "javascript TEXT",
            "data_json TEXT",
        ):
            self.assertIn(column, migration)


class AgentActionValidationTests(unittest.TestCase):
    def test_replace_app_validates_every_field_and_updates_one_workspace(self) -> None:
        action = {
            "action": "replace_app",
            "expected_revision": 4,
            "html": "<main>Hello</main>",
            "css": "main { display: grid; }",
            "javascript": "app.on('save', () => app.notify('saved'));",
            "data": {"items": [{"name": "first"}]},
        }
        changed = {**action, "revision": 5, "updated_at": "now"}
        with patch.object(backend, "_update_state", return_value=changed) as update:
            self.assertEqual(
                backend.apply_agent_action(action, "app-7"),
                {"app": changed},
            )
        self.assertEqual(update.call_args.args[0], 4)
        self.assertEqual(update.call_args.args[2], "app-7")
        self.assertEqual(json.loads(update.call_args.args[1]["data_json"]), action["data"])

    def test_agent_action_rejects_extra_fields_and_dynamic_imports(self) -> None:
        base = {
            "action": "replace_ui",
            "expected_revision": 0,
            "html": "",
            "css": "",
            "javascript": "",
        }
        with self.assertRaises(backend.AppError) as extra:
            backend.apply_agent_action({**base, "url": "https://example.com"}, "app-1")
        self.assertEqual(extra.exception.status, HTTPStatus.BAD_REQUEST)
        for javascript in (
            "import('https://example.com/app.js')",
            "import /* hidden */ ('https://example.com/app.js')",
        ):
            with self.subTest(javascript=javascript), self.assertRaises(backend.AppError) as imported:
                backend.apply_agent_action({**base, "javascript": javascript}, "app-1")
            self.assertEqual(imported.exception.status, HTTPStatus.UNPROCESSABLE_ENTITY)

    def test_agent_data_action_can_finish_for_an_archived_workspace(self) -> None:
        action = {
            "action": "set",
            "expected_revision": 4,
            "path": ["status"],
            "value": "done",
        }
        with patch.object(
            backend,
            "apply_runtime_action",
            return_value={"app": {"revision": 5}},
        ) as apply:
            backend.apply_agent_action(action, "app-2")
        apply.assert_called_once_with(action, "app-2", allow_archived=True)

    def test_bundle_and_data_caps_are_encoded_byte_caps(self) -> None:
        with self.assertRaises(backend.AppError) as html_error:
            backend._bounded_string(
                "é" * (backend.MAX_HTML_BYTES // 2 + 1),
                "html",
                backend.MAX_HTML_BYTES,
            )
        self.assertEqual(html_error.exception.status, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        with self.assertRaises(backend.AppError) as data_error:
            backend._validated_data({"value": "é" * (backend.MAX_DATA_BYTES // 2)})
        self.assertEqual(data_error.exception.status, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)


class BrowserRoutingTests(unittest.TestCase):
    def test_browser_routes_are_workspace_scoped(self) -> None:
        state = {"revision": 0}
        idle = {"session": None, "status": "idle"}
        with (
            patch.object(backend, "load_app_state", return_value=state) as load,
            patch.object(backend, "browser_conversation", return_value=idle) as conversation,
        ):
            self.assertEqual(
                backend.route_browser("GET", "/apps/app-2/state", None),
                {"app": state},
            )
            self.assertEqual(
                backend.route_browser("GET", "/apps/app-3/conversation", None),
                idle,
            )
        load.assert_called_once_with("app-2")
        conversation.assert_called_once_with("app-3")

    def test_message_routes_preserve_user_and_generated_app_provenance(self) -> None:
        with (
            patch.object(backend, "_workspace_lock", return_value=MagicMock()),
            patch.object(
                backend,
                "create_message",
                return_value={"status": "accepted", "thread_id": "app-4"},
            ) as create,
        ):
            backend.route_browser(
                "POST",
                "/apps/app-4/messages",
                {"content": "Build it"},
            )
            backend.route_browser(
                "POST",
                "/apps/app-4/runtime/agent-requests",
                {"content": "Refresh it"},
            )
        self.assertEqual(
            create.call_args_list[0].kwargs,
            {"requested_by": "user", "thread_id": "app-4"},
        )
        self.assertEqual(
            create.call_args_list[1].kwargs,
            {"requested_by": "app", "thread_id": "app-4"},
        )

    def test_stop_route_verifies_the_workspace_and_proxies_to_the_host(self) -> None:
        with (
            patch.object(backend, "_require_web_app") as require,
            patch.object(backend, "call_admin_api", return_value={"status": "accepted"}) as host,
        ):
            result = backend.route_browser("POST", "/apps/app-8/stop", {})
        self.assertEqual(result, {"status": "accepted"})
        require.assert_called_once_with("app-8", include_archived=True)
        host.assert_called_once_with("POST", "/v1/threads/app-8/stop", {})

    def test_per_task_routes_are_removed(self) -> None:
        with self.assertRaises(backend.AppError) as error:
            backend.route_browser("POST", "/apps/app-8/tasks/task-2/kill", {})
        self.assertEqual(error.exception.status, HTTPStatus.NOT_FOUND)

    def test_agent_thread_is_resolved_to_the_exact_workspace(self) -> None:
        with (
            patch.object(backend, "_require_web_app") as require,
            patch.object(backend, "load_app_state", return_value={"revision": 2}) as load,
        ):
            response = backend.route_agent("GET", "/agent/state", None, "app-9")
        self.assertEqual(response["app"]["revision"], 2)
        require.assert_called_once_with("app-9", include_archived=True, agent=True)
        load.assert_called_once_with("app-9")


class ConversationTests(unittest.TestCase):
    SESSION = {
        "agent_runtime": "codex",
        "model": "gpt-5.6-terra",
        "effort": "high",
    }

    def test_conversation_uses_the_selected_app_thread(self) -> None:
        thread = {
            "thread_id": "app-6",
            "last_used_at": "2026-07-27T10:00:00Z",
            "status": "running",
            **self.SESSION,
        }
        with (
            patch.object(backend, "_require_web_app"),
            patch.object(backend, "call_admin_api", return_value={"thread": thread}) as host,
        ):
            self.assertEqual(
                backend.browser_conversation("app-6"),
                {"session": self.SESSION, "status": "running"},
            )
        host.assert_called_once_with("GET", "/v1/threads/app-6")

    def test_conversation_is_idle_before_the_host_thread_exists(self) -> None:
        # The host thread appears with the first message; an unconfigured
        # workspace reads as an idle conversation with no session yet.
        with (
            patch.object(backend, "_require_web_app"),
            patch.object(
                backend,
                "call_admin_api",
                side_effect=backend.AppError(HTTPStatus.NOT_FOUND, "thread not found"),
            ),
        ):
            self.assertEqual(
                backend.browser_conversation("app-6"),
                {"session": None, "status": "idle"},
            )

    def test_conversation_rejects_an_invalid_host_status(self) -> None:
        thread = {"thread_id": "app-6", "status": "queued", **self.SESSION}
        with (
            patch.object(backend, "_require_web_app"),
            patch.object(backend, "call_admin_api", return_value={"thread": thread}),
            self.assertRaises(backend.AppError) as error,
        ):
            backend.browser_conversation("app-6")
        self.assertEqual(error.exception.status, HTTPStatus.BAD_GATEWAY)

    def test_conversation_events_are_scoped_and_bounded(self) -> None:
        events = {"events": [{"seq": 5, "event_type": "thread.message"}]}
        with (
            patch.object(backend, "_require_web_app"),
            patch.object(backend, "call_admin_api", return_value=events) as host,
        ):
            self.assertEqual(
                backend.browser_conversation_events("app-6", {"since": ["2"]}),
                events,
            )
        host.assert_called_once_with(
            "GET",
            "/v1/threads/app-6/events?since=2&limit=6&message_bytes=122880"
            "&event_type=thread.message&event_type=thread.error"
            "&event_type=thread.stopped",
        )

    def test_conversation_events_open_at_tail_and_page_backward(self) -> None:
        events = {"events": [{"seq": 5, "event_type": "thread.message"}]}
        with (
            patch.object(backend, "_require_web_app"),
            patch.object(backend, "call_admin_api", return_value=events) as host,
        ):
            self.assertEqual(backend.browser_conversation_events("app-6", {}), events)
            self.assertEqual(
                backend.browser_conversation_events("app-6", {"before": ["5"]}),
                events,
            )
        self.assertEqual(
            host.call_args_list[0].args,
            (
                "GET",
                "/v1/threads/app-6/events?limit=6&message_bytes=122880"
                "&event_type=thread.message&event_type=thread.error"
                "&event_type=thread.stopped",
            ),
        )
        self.assertEqual(
            host.call_args_list[1].args,
            (
                "GET",
                "/v1/threads/app-6/events?before=5&limit=6&message_bytes=122880"
                "&event_type=thread.message&event_type=thread.error"
                "&event_type=thread.stopped",
            ),
        )

    def test_conversation_events_reject_mixed_cursors(self) -> None:
        with (
            patch.object(backend, "_require_web_app"),
            self.assertRaises(backend.AppError) as error,
        ):
            backend.browser_conversation_events(
                "app-6", {"since": ["2"], "before": ["5"]}
            )
        self.assertEqual(error.exception.status, HTTPStatus.BAD_REQUEST)

    def test_message_creation_starts_a_turn_on_the_workspace_thread(self) -> None:
        send_response = {
            "status": "accepted",
            "thread": {"thread_id": "app-5", "status": "running", **self.SESSION},
        }
        with (
            patch.object(backend, "_require_web_app"),
            patch.object(backend, "call_admin_api", return_value=send_response) as host,
        ):
            response = backend.create_message(
                {"content": "Build it.", **self.SESSION},
                requested_by="user",
                thread_id="app-5",
            )
        self.assertEqual(response, {"status": "accepted", "thread_id": "app-5"})
        host.assert_called_once_with(
            "POST",
            "/v1/threads/app-5/messages",
            {
                "message": "Requested by user:\nBuild it.",
                **self.SESSION,
            },
        )

    def test_message_creation_retries_transient_turn_lifecycle_conflicts(self) -> None:
        for message in (
            "the agent is starting; retry shortly",
            "the agent is finishing; retry shortly",
        ):
            with self.subTest(message=message):
                busy = backend.AppError(HTTPStatus.CONFLICT, message)
                with (
                    patch.object(backend, "_require_web_app"),
                    patch.object(
                        backend,
                        "call_admin_api",
                        side_effect=(busy, busy, {"status": "accepted"}),
                    ) as host,
                    patch.object(backend, "SEND_BUSY_RETRY_DELAY_SECONDS", 0),
                ):
                    response = backend.create_message(
                        {"content": "More."}, requested_by="user", thread_id="app-5"
                    )
                self.assertEqual(response, {"status": "accepted", "thread_id": "app-5"})
                self.assertEqual(host.call_count, 3)

    def test_message_creation_surfaces_a_persistently_busy_thread(self) -> None:
        busy = backend.AppError(
            HTTPStatus.CONFLICT,
            "the agent is finishing; retry shortly",
        )
        with (
            patch.object(backend, "_require_web_app"),
            patch.object(backend, "call_admin_api", side_effect=busy) as host,
            patch.object(backend, "SEND_BUSY_RETRY_DELAY_SECONDS", 0),
            self.assertRaises(backend.AppError) as error,
        ):
            backend.create_message(
                {"content": "More."}, requested_by="user", thread_id="app-5"
            )
        self.assertEqual(error.exception.status, HTTPStatus.CONFLICT)
        self.assertIn(backend.SEND_RETRY_MARKER, error.exception.message)
        self.assertEqual(host.call_count, backend.SEND_BUSY_RETRIES)

    def test_message_creation_rejects_an_invalid_host_send_status(self) -> None:
        with (
            patch.object(backend, "_require_web_app"),
            patch.object(backend, "call_admin_api", return_value={"status": "bogus"}),
            self.assertRaises(backend.AppError) as error,
        ):
            backend.create_message(
                {"content": "More."}, requested_by="user", thread_id="app-5"
            )
        self.assertEqual(error.exception.status, HTTPStatus.BAD_GATEWAY)


class RuntimeDataActionTests(unittest.TestCase):
    def test_set_delete_and_append_follow_typed_paths(self) -> None:
        data = {"items": [{"name": "one", "done": False}], "tags": []}
        backend._mutate_data(data, "set", ["items", 0, "done"], True)
        backend._mutate_data(data, "append", ["tags"], "new")
        backend._mutate_data(data, "delete", ["items", 0, "name"], None)
        self.assertEqual(data, {"items": [{"done": True}], "tags": ["new"]})

    def test_revision_conflict_is_checked_inside_the_workspace_row_lock(self) -> None:
        cursor = MagicMock()
        cursor.fetchone.return_value = (7, "", "", "", '{"count":1}', "now")
        transaction = MagicMock()
        transaction.__enter__.return_value = cursor
        with (
            patch.object(backend, "_require_web_app"),
            patch.object(backend.db, "transaction", return_value=transaction),
            self.assertRaises(backend.AppError) as conflict,
        ):
            backend.apply_runtime_action(
                {"action": "set", "expected_revision": 6, "path": ["count"], "value": 2},
                "app-3",
            )
        self.assertEqual(conflict.exception.status, HTTPStatus.CONFLICT)
        self.assertIn("thread_id = %s", cursor.execute.call_args_list[1].args[0])
        self.assertEqual(cursor.execute.call_args_list[1].args[1], ("app-3",))


class AgenticWebAppMockTests(unittest.TestCase):
    def setUp(self) -> None:
        builder_mock.reset_mock_state()
        self.addCleanup(builder_mock.reset_mock_state)

    def _create_app(self) -> dict[str, Any]:
        return builder_mock._route_app_api("POST", "apps", {})["app"]

    def _send(self, thread_id: str, content: str) -> dict[str, Any]:
        return builder_mock._route_app_api(
            "POST",
            f"apps/{thread_id}/messages",
            {"content": content, **builder_mock.DEFAULT_SESSION},
        )

    def test_mock_allocates_independent_ids_and_never_reuses_archived_apps(self) -> None:
        first = self._create_app()
        second = self._create_app()
        builder_mock._route_app_api(
            "POST", f"apps/{second['thread_id']}/archive", {}
        )
        third = self._create_app()

        self.assertEqual(
            [first["thread_id"], second["thread_id"], third["thread_id"]],
            ["app-1", "app-2", "app-3"],
        )
        self.assertEqual(
            [
                app["thread_id"]
                for app in builder_mock._route_app_api(
                    "GET", "apps", None, {"archived": ["true"]}
                )["apps"]
            ],
            ["app-2"],
        )

    def test_mock_runs_different_workspace_threads_concurrently(self) -> None:
        first = self._create_app()
        second = self._create_app()
        first_send = self._send(first["thread_id"], "Build the first app.")
        second_send = self._send(second["thread_id"], "Build the second app.")

        active = builder_mock._route_app_api("GET", "apps", None)["apps"]
        self.assertEqual(
            {app["thread_id"]: app["status"] for app in active},
            {"app-1": "running", "app-2": "running"},
        )
        builder_mock.TURN_DEADLINES["app-1"] = 0
        first_state = builder_mock._route_app_api(
            "GET", "apps/app-1/state", None
        )["app"]
        second_state = builder_mock._route_app_api(
            "GET", "apps/app-2/state", None
        )["app"]
        self.assertEqual(first_state["revision"], 1)
        self.assertEqual(second_state["revision"], 0)
        self.assertEqual(first_send, {"status": "accepted", "thread_id": "app-1"})
        self.assertEqual(second_send, {"status": "accepted", "thread_id": "app-2"})

    def test_mock_steers_the_running_turn_instead_of_queueing(self) -> None:
        self._create_app()
        self._send("app-1", "Build the first app.")
        steered = builder_mock._route_app_api(
            "POST", "apps/app-1/messages", {"content": "Add a chart."}
        )

        self.assertEqual(steered, {"status": "accepted", "thread_id": "app-1"})
        conversation = builder_mock._route_app_api(
            "GET", "apps/app-1/conversation", None
        )
        self.assertEqual(conversation["status"], "running")
        events = builder_mock._route_app_api(
            "GET", "apps/app-1/conversation/events", None
        )["events"]
        self.assertIn(
            "Requested by user:\nAdd a chart.",
            [event["payload"].get("message") for event in events],
        )
        self.assertEqual(
            [event["event_type"] for event in events],
            ["thread.message", "thread.message", "thread.message"],
        )

    def test_mock_switches_an_idle_session_and_rejects_a_running_switch(self) -> None:
        self._create_app()
        self._send("app-1", "Build the first app.")
        replacement = {
            "agent_runtime": "claude_code",
            "model": "claude-sonnet-5",
            "effort": "high",
        }
        with self.assertRaises(backend.AppError) as running:
            builder_mock._route_app_api(
                "POST",
                "apps/app-1/messages",
                {"content": "Switch too early.", **replacement},
            )
        self.assertEqual(running.exception.status, HTTPStatus.CONFLICT)
        self.assertIn("only while the thread is idle", running.exception.message)

        builder_mock.TURN_DEADLINES["app-1"] = 0
        builder_mock._route_app_api("GET", "apps/app-1/state", None)
        switched = builder_mock._route_app_api(
            "POST",
            "apps/app-1/messages",
            {"content": "Continue with Claude.", **replacement},
        )
        self.assertEqual(switched, {"status": "accepted", "thread_id": "app-1"})
        self.assertEqual(
            builder_mock.WORKSPACES["app-1"]["session"],
            replacement,
        )
        activities = [
            event["payload"]["activity"]
            for event in builder_mock.WORKSPACES["app-1"]["events"]
            if event["event_type"] == "thread.activity"
        ]
        self.assertEqual(len(activities), 1)
        self.assertEqual(activities[0]["title"], "Agent provider changed")
        self.assertIn("Claude Code · claude-sonnet-5 · high", activities[0]["detail"])

    def test_mock_keeps_data_conversations_and_sessions_per_workspace(self) -> None:
        self._create_app()
        self._create_app()
        self._send("app-1", "Build the first app.")
        self._send("app-2", "Build the second app.")
        builder_mock.TURN_DEADLINES["app-1"] = 0
        builder_mock._route_app_api("GET", "apps/app-1/state", None)
        changed = builder_mock._route_app_api(
            "POST",
            "apps/app-1/runtime/actions",
            {
                "action": "set",
                "expected_revision": 1,
                "path": ["count"],
                "value": 7,
            },
        )

        self.assertEqual(changed["app"]["data"]["count"], 7)
        self.assertEqual(
            builder_mock._route_app_api(
                "GET", "apps/app-2/state", None
            )["app"]["data"],
            {},
        )
        first_conversation = builder_mock._route_app_api(
            "GET", "apps/app-1/conversation", None
        )
        second_conversation = builder_mock._route_app_api(
            "GET", "apps/app-2/conversation", None
        )
        self.assertEqual(
            (first_conversation["status"], second_conversation["status"]),
            ("idle", "running"),
        )
        first_messages = [
            event["payload"].get("message")
            for event in builder_mock.WORKSPACES["app-1"]["events"]
        ]
        second_messages = [
            event["payload"].get("message")
            for event in builder_mock.WORKSPACES["app-2"]["events"]
        ]
        self.assertTrue(any("first app" in str(message) for message in first_messages))
        self.assertFalse(any("second app" in str(message) for message in first_messages))
        self.assertTrue(any("second app" in str(message) for message in second_messages))

        # Host session metadata remains even if retained event history is
        # pruned, matching the real thread summary used by /apps.
        builder_mock.WORKSPACES["app-1"]["events"].clear()
        listed = builder_mock._route_app_api("GET", "apps", None)["apps"]
        first_summary = next(app for app in listed if app["thread_id"] == "app-1")
        self.assertEqual(first_summary["session"], builder_mock.DEFAULT_SESSION)
        self.assertEqual(first_summary["status"], "idle")

    def test_mock_conversation_events_page_from_newest_then_backward(self) -> None:
        self._create_app()
        workspace = builder_mock.WORKSPACES["app-1"]
        workspace["events"] = [
            {
                "seq": seq,
                "timestamp": f"2026-07-27T00:00:{seq:02d}Z",
                "event_id": f"event_app-1_{seq}",
                "event_type": "thread.message",
                "thread_id": "app-1",
                "payload": {"message": f"message {seq}", "source": "agent"},
            }
            for seq in range(1, 13)
        ]
        newest = builder_mock._route_app_api(
            "GET", "apps/app-1/conversation/events", None
        )["events"]
        older = builder_mock._route_app_api(
            "GET",
            "apps/app-1/conversation/events",
            None,
            {"before": [str(newest[0]["seq"])]},
        )["events"]
        self.assertEqual([event["seq"] for event in newest], [7, 8, 9, 10, 11, 12])
        self.assertEqual([event["seq"] for event in older], [1, 2, 3, 4, 5, 6])

    def test_mock_enforces_archive_and_stop_workspace_boundaries(self) -> None:
        self._create_app()
        self._create_app()
        send = self._send("app-1", "Build it.")
        builder_mock._route_app_api("POST", "apps/app-1/archive", {})

        with self.assertRaises(backend.AppError) as archived:
            builder_mock._route_app_api(
                "POST", "apps/app-1/messages", {"content": "Blocked"}
            )
        self.assertEqual(archived.exception.status, HTTPStatus.NOT_FOUND)

        # Archive does not revoke work that already started.
        builder_mock.TURN_DEADLINES[send["thread_id"]] = 0
        archived_state = builder_mock._route_app_api(
            "GET", "apps/app-1/state", None
        )["app"]
        self.assertEqual(archived_state["revision"], 1)

        # Stop is scoped to its own workspace thread: the other workspace has
        # no running turn to stop.
        with self.assertRaises(backend.AppError) as idle:
            builder_mock._route_app_api("POST", "apps/app-2/stop", {})
        self.assertEqual(idle.exception.status, HTTPStatus.CONFLICT)

    def test_mock_stop_cancels_the_running_turn(self) -> None:
        self._create_app()
        self._send("app-1", "Build it.")
        stopped = builder_mock._route_app_api("POST", "apps/app-1/stop", {})

        self.assertEqual(stopped, {"status": "accepted"})
        conversation = builder_mock._route_app_api(
            "GET", "apps/app-1/conversation", None
        )
        self.assertEqual(conversation["status"], "idle")
        events = builder_mock.WORKSPACES["app-1"]["events"]
        self.assertEqual(events[-1]["event_type"], "thread.stopped")
        # The cancelled turn never lands its bundle, even after its deadline.
        state = builder_mock._route_app_api("GET", "apps/app-1/state", None)["app"]
        self.assertEqual(state["revision"], 0)


class AgenticWebAppDbTests(unittest.TestCase):
    DB_NAME = "kern_personal_builder_test"
    _initialized = False

    @classmethod
    def setUpClass(cls) -> None:
        pg_harness.ensure_database()
        pg_harness.create_database(cls.DB_NAME)
        cls._initialized = True

    def setUp(self) -> None:
        self.env_patch = patch.dict("os.environ", {"KERN_DB_NAME": self.DB_NAME})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.addCleanup(db.close_pool)
        if not getattr(self.__class__, "_migrated", False):
            migrate.up(quiet=True)
            with db.transaction() as cur:
                cur.execute(
                    """
                    DO $$
                    BEGIN
                      IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'kern-app-6') THEN
                        CREATE ROLE "kern-app-6" LOGIN;
                      END IF;
                    END
                    $$;
                    """
                )
                cur.execute(
                    'CREATE SCHEMA IF NOT EXISTS app_personal_web_app_builder '
                    'AUTHORIZATION "kern-app-6"'
                )
            app = app_platform.app_by_id(backend.APP_ID)
            assert app is not None
            for version in app_migrate.pending(app.id):
                app_migrate.apply_sql(app.id, version, connection_user=app.db_role)
                app_migrate.record(app.id, version)
            self.__class__._migrated = True
        with db.transaction() as cur:
            cur.execute("SET LOCAL search_path TO app_personal_web_app_builder")
            cur.execute("DELETE FROM web_apps")

    def test_apps_have_independent_revision_chains_and_fixed_threads(self) -> None:
        first = backend.create_web_app()
        second = backend.create_web_app()
        self.assertEqual((first["thread_id"], second["thread_id"]), ("app-1", "app-2"))

        backend.apply_agent_action(
            {
                "action": "replace_app",
                "expected_revision": 0,
                "html": "<p>First</p>",
                "css": "",
                "javascript": "",
                "data": {"count": 1},
            },
            "app-1",
        )
        backend.apply_agent_action(
            {
                "action": "replace_data",
                "expected_revision": 0,
                "data": {"count": 9},
            },
            "app-2",
        )
        self.assertEqual(backend.load_app_state("app-1")["data"], {"count": 1})
        self.assertEqual(backend.load_app_state("app-2")["data"], {"count": 9})

    def test_rename_archive_and_unarchive_keep_the_same_workspace_id(self) -> None:
        created = backend.create_web_app()
        renamed = backend.rename_web_app(created["thread_id"], {"name": "Meal planner"})
        archived = backend.set_web_app_archived(created["thread_id"], archived=True)
        unarchived = backend.set_web_app_archived(created["thread_id"], archived=False)
        self.assertEqual(renamed["name"], "Meal planner")
        self.assertTrue(archived["archived"])
        self.assertFalse(unarchived["archived"])
        self.assertEqual(
            {renamed["thread_id"], archived["thread_id"], unarchived["thread_id"]},
            {"app-1"},
        )

    def test_app_index_joins_host_session_and_running_status(self) -> None:
        backend.create_web_app()
        backend.create_web_app()
        backend.set_web_app_archived("app-2", archived=True)
        host_threads = {
            "threads": [
                {
                    "thread_id": "app-1",
                    "agent_runtime": "codex",
                    "model": "gpt-5.6-terra",
                    "effort": "high",
                    "last_used_at": "2026-07-27T10:00:00Z",
                    "status": "running",
                },
                {
                    "thread_id": "app-2",
                    "agent_runtime": "claude_code",
                    "model": "claude-opus-5",
                    "effort": "high",
                    "last_used_at": "2026-07-27T09:00:00Z",
                    "status": "idle",
                },
            ]
        }
        with patch.object(backend, "call_admin_api", return_value=host_threads):
            active = backend.list_web_apps({})["apps"]
            archived = backend.list_web_apps({"archived": ["true"]})["apps"]
        self.assertEqual(active[0]["thread_id"], "app-1")
        self.assertEqual(active[0]["session"]["agent_runtime"], "codex")
        self.assertEqual(active[0]["status"], "running")
        self.assertEqual(archived[0]["thread_id"], "app-2")
        self.assertEqual(archived[0]["status"], "idle")

    def test_app_index_drains_scoped_host_thread_pages(self) -> None:
        first = {
            "threads": [{"thread_id": "app-1"}],
            "next_before": "next/token",
        }
        second = {"threads": [{"thread_id": "app-2"}]}
        with patch.object(
            backend,
            "call_admin_api",
            side_effect=(first, second),
        ) as host:
            self.assertEqual(
                backend._host_thread_summaries(),
                [{"thread_id": "app-1"}, {"thread_id": "app-2"}],
            )
        self.assertEqual(
            host.call_args_list,
            [
                call("GET", "/v1/threads?limit=100"),
                call("GET", "/v1/threads?limit=100&before=next%2Ftoken"),
            ],
        )

    def test_archived_apps_are_browser_read_only_but_existing_agent_can_finish(self) -> None:
        backend.create_web_app()
        backend.set_web_app_archived("app-1", archived=True)
        with self.assertRaises(backend.AppError) as browser:
            backend.apply_runtime_action(
                {"action": "set", "expected_revision": 0, "path": ["done"], "value": True},
                "app-1",
            )
        self.assertEqual(browser.exception.status, HTTPStatus.NOT_FOUND)

        changed = backend.route_agent(
            "POST",
            "/agent/actions",
            {
                "action": "set",
                "expected_revision": 0,
                "path": ["done"],
                "value": True,
            },
            "app-1",
        )
        self.assertEqual(changed["app"]["data"], {"done": True})

    def test_unknown_agent_thread_cannot_read_another_workspace(self) -> None:
        backend.create_web_app()
        with self.assertRaises(backend.AppError) as error:
            backend.route_agent("GET", "/agent/state", None, "app-99")
        self.assertEqual(error.exception.status, HTTPStatus.UNAUTHORIZED)
