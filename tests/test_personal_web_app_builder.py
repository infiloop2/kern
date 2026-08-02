"""Agentic Web App contract and backend validation tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
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

    def test_ui_is_a_fullscreen_canvas_with_an_admin_overlay(self) -> None:
        index = (APP_DIR / "ui" / "index.html").read_text()
        source = (APP_DIR / "ui" / "personal_web_app_builder.js").read_text()
        css = (APP_DIR / "ui" / "personal_web_app_builder.css").read_text()
        for element_id in (
            'id="home-view"', 'id="app-view"', 'id="admin-overlay"',
            'id="admin-open"', 'id="admin-close"', 'id="admin-app-title"',
            'id="workspace-panel"', 'id="workspace-list"',
            'id="workspace-new-app"', 'id="workspace-panel-open"',
            'id="new-app"', 'id="apps"', 'id="rename-app"',
            'id="panel-chat"', 'id="panel-schedules"', 'id="panel-memory"',
            'id="panel-history"',
            'id="schedule-form"', 'id="schedule-cadence"',
            'id="instructions-editor"', 'id="memory-editor"',
            'id="app-history-list"', 'id="checkpoint-save"',
            'id="history-loader"', 'id="load-earlier"',
            'id="composer-running"', 'id="stop-turn"',
            'id="attach-file"', 'id="attachments"',
            'id="agent-settings-idle-note"', 'id="agent-session-change-warning"',
        ):
            self.assertIn(element_id, index)
        # The old sidebar/drawer chrome is gone: the canvas is the product.
        self.assertNotIn('id="chat-drawer"', index)
        self.assertNotIn('id="sidebar-open"', index)
        self.assertNotIn('id="open-chat"', index)
        self.assertIn(
            ".agent-settings.active-locked:hover #agent-settings-idle-note",
            css,
        )
        self.assertIn('/v1/apps/agent_chat/ui/rich_text.js', index)
        self.assertIn('/v1/apps/agent_chat/ui/rich_text.css', index)
        self.assertIn("app-dot", source)
        self.assertNotIn("archive", index.lower())
        self.assertNotIn("archived", source)
        self.assertIn("selectedAppId = null", source)
        self.assertIn("INITIAL_CONVERSATION_EVENT_PAGES = 3", source)
        self.assertIn("conversationViewStates = new Map()", source)
        self.assertIn("KernRichText.renderMarkdown(entryData.message)", source)
        self.assertIn("stopRunningTurn()", source)
        self.assertIn("sessionConfigurationChanged()", source)
        self.assertIn("!fromGeneratedApp && sessionConfigurationChanged()", source)
        self.assertIn('classList.toggle("sending", composerSending)', source)
        self.assertIn(".send-button.sending::after", css)
        self.assertIn('showChatStatus("Stopping…")', source)
        self.assertIn('"kern-app-upload-file"', source)
        self.assertIn("[User-uploaded file: ${attachment.file.path}]", source)
        self.assertIn('query.push("activity=false")', source)
        self.assertIn("KernRichText.compactActivityEvents(ordered)", source)
        self.assertIn("loadOlderConversationEvents()", source)
        self.assertIn("refreshSequence !== appsRefreshSequence", source)
        self.assertIn("COMPOSER_DRAFTS_STORAGE_KEY", source)
        self.assertIn("localStorage.setItem", source)
        self.assertIn('entry.checkpoint_type === "manual"', source)
        self.assertIn("entry.revert_prompt || \"Revert this change?\"", source)
        self.assertIn("/checkpoints/${historyId}/revert", source)
        self.assertIn("@media (max-width: 720px)", css)
        self.assertIn("@media (prefers-reduced-motion: reduce)", css)
        self.assertIn("env(safe-area-inset-bottom)", css)
        self.assertEqual(
            (backend.SEND_BUSY_RETRIES - 1) * backend.SEND_BUSY_RETRY_DELAY_SECONDS,
            10,
        )

    def test_bridge_reply_listener_checks_the_parent_source(self) -> None:
        # APP-003 parity: the frame accepts *-result bridge replies only from
        # its own parent window so a sibling frame cannot forge one.
        source = (APP_DIR / "ui" / "personal_web_app_builder.js").read_text()
        handler = source.split('addEventListener("message"', 1)[1].split(
            "\nfunction ", 1
        )[0]
        self.assertIn("event.source !== parent", handler)
        self.assertLess(
            handler.index("event.source !== parent"),
            handler.index("pendingApi.get(message.request_id)"),
        )
    def test_frame_strips_the_injected_context_from_user_bubbles(self) -> None:
        source = (APP_DIR / "ui" / "personal_web_app_builder.js").read_text()
        self.assertIn('const CONTEXT_OPEN = "[Workspace context]"', source)
        self.assertIn('const CONTEXT_CLOSE = "[/Workspace context]"', source)
        self.assertIn("displayedUserMessage(payload.message)", source)
        # Only the block directly after the provenance line is host-composed.
        self.assertIn("lines[1] === CONTEXT_OPEN", source)

    def test_generated_worker_is_pinned_to_its_workspace(self) -> None:
        source = (APP_DIR / "ui" / "personal_web_app_builder.js").read_text()
        self.assertIn("new Worker(", source)
        self.assertIn("selectedAppId !== run.threadId", source)
        self.assertIn("stopCapabilityWorker()", source)
        self.assertIn("encodeURIComponent(run.threadId)", source)
        self.assertIn("void sendMessage(message.message.trim(), run.threadId)", source)
        self.assertIn(
            "conversationResponse.session || listedSession || snapshot.session",
            source,
        )
        self.assertIn("MAX_WORKER_MUTATIONS_PER_TURN = 16", source)
        self.assertIn('"fetch", "XMLHttpRequest", "WebSocket"', source)
        self.assertNotIn("window.open", source)
        self.assertNotIn("location.href", source)

    def test_worker_turns_use_split_counters_and_deny_timers(self) -> None:
        source = (APP_DIR / "ui" / "personal_web_app_builder.js").read_text()
        self.assertIn("expected_data_version: run.dataVersion", source)
        self.assertIn('"setTimeout", "setInterval", "clearTimeout", "clearInterval", "setImmediate"', source)
        self.assertIn('"MessageChannel", "MessagePort"', source)
        # The armed worker only promotes when nothing moved underneath it.
        self.assertIn("armedWorker.uiRevision === app.ui_revision", source)
        self.assertIn("armedWorker.dataVersion === app.data_version", source)
        self.assertIn("armCapabilityWorker", source)
        self.assertIn("armed.timer = setTimeout(discard, WORKER_TURN_TIMEOUT_MS)", source)
        self.assertIn("clearTimeout(armed.timer);\n    armed.run = run", source)
        self.assertIn(
            "if (hasBundle && !workerRun) {\n"
            "      renderedDataVersion = next.app.data_version;\n"
            "      runCapabilityWorker();",
            source,
        )
        # The bundle's blob URL survives across turns for one UI revision.
        self.assertIn("bundleUrl.uiRevision === uiRevision", source)
        self.assertIn("URL.revokeObjectURL", source)
        # Renders patch the shadow tree instead of replacing it wholesale.
        self.assertIn("function patchNode(", source)
        self.assertIn("sanitizeCssCached", source)
        # Drag state stays in the trusted frame and only bounded plain values
        # enter the worker event payload.
        self.assertIn('lower === "data-drag-value"', source)
        self.assertIn('lower === "data-drop-action"', source)
        self.assertIn('lower === "data-drop-value"', source)
        self.assertIn('event.dataTransfer.clearData()', source)
        self.assertIn('event.dataTransfer.setData("text/plain", "")', source)
        self.assertIn('draggedValue: clipEncodedText(', source)
        self.assertIn('generatedRoot.addEventListener("drop", generatedDrop)', source)
        # Enter cannot bypass attachment/session validation represented by the
        # disabled composer action.
        self.assertIn('if (!fromGeneratedApp && $("send-message").disabled) return;', source)

    def test_agent_instructions_are_terse_and_current(self) -> None:
        instructions = (APP_DIR / "agent.md").read_text()
        self.assertIn("This thread belongs\npermanently to this workspace", instructions)
        self.assertIn("app.askAgent(message)", instructions)
        self.assertIn('data-drag-value="item-id"', instructions)
        self.assertIn('data-drop-action="name"', instructions)
        self.assertIn('data-drop-value="target-id"', instructions)
        self.assertIn("draggedValue", instructions)
        self.assertIn('"action":"replace_ui","expected_ui_revision"', instructions)
        self.assertIn('"action":"set","expected_data_version"', instructions)
        self.assertIn("/agent/instructions", instructions)
        self.assertIn("/agent/memories", instructions)
        self.assertIn("/agent/schedules", instructions)
        self.assertIn("Requested by schedule:", instructions)
        self.assertNotIn("replace_app", instructions)
        self.assertNotIn("replace_data", instructions)

    def test_workspace_platform_migration_shape(self) -> None:
        migration = (APP_DIR / "migrations" / "0004_workspace_platform.sql").read_text()
        self.assertIn("RENAME COLUMN revision TO ui_revision", migration)
        self.assertIn("ADD COLUMN data_version", migration)
        self.assertIn("ADD COLUMN instructions_md", migration)
        self.assertIn("CREATE TABLE web_app_history", migration)
        self.assertIn("CREATE TABLE web_app_schedules", migration)
        self.assertIn("CREATE TABLE web_app_memories", migration)
        # Existing workspaces receive the same restore anchors a new one gets.
        self.assertIn("INSERT INTO web_app_history", migration)


class AgentActionValidationTests(unittest.TestCase):
    def test_whole_document_replaces_are_gone(self) -> None:
        for action in ("replace_app", "replace_data"):
            with self.subTest(action=action), self.assertRaises(backend.AppError) as error:
                backend.apply_agent_action(
                    {"action": action, "expected_ui_revision": 0}, "app-1"
                )
            self.assertEqual(error.exception.status, HTTPStatus.UNPROCESSABLE_ENTITY)

    def test_replace_ui_validates_fields_and_returns_slim_counters(self) -> None:
        action = {
            "action": "replace_ui",
            "expected_ui_revision": 4,
            "html": "<main>Hello</main>",
            "css": "main { display: grid; }",
            "javascript": "app.on('save', () => app.notify('saved'));",
        }
        state = {"ui_revision": 5, "data_version": 2, "data": {"kept": True}}
        with patch.object(backend, "_replace_ui_bundle", return_value=state) as replace:
            self.assertEqual(
                backend.apply_agent_action(action, "app-7"),
                {"ok": True, "ui_revision": 5, "data_version": 2},
            )
        self.assertEqual(replace.call_args.args[:2], ("app-7", 4))
        self.assertEqual(replace.call_args.kwargs, {"actor": "agent"})

    def test_agent_action_rejects_extra_fields_and_dynamic_imports(self) -> None:
        base = {
            "action": "replace_ui",
            "expected_ui_revision": 0,
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

    def test_agent_data_action_uses_the_workspace_data_path(self) -> None:
        action = {
            "action": "set",
            "expected_data_version": 4,
            "path": ["status"],
            "value": "done",
        }
        state = {"ui_revision": 1, "data_version": 5, "data": {"status": "done"}}
        with patch.object(backend, "_apply_data_action", return_value=state) as apply:
            self.assertEqual(
                backend.apply_agent_action(action, "app-2"),
                {"ok": True, "ui_revision": 1, "data_version": 5},
            )
        apply.assert_called_once_with(action, "app-2", actor="agent")

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
        state = {"ui_revision": 0, "data_version": 0}
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

    def test_workspace_admin_routes_are_dispatched(self) -> None:
        with (
            patch.object(backend, "load_instructions", return_value={"ok": 1}) as instructions,
            patch.object(backend, "list_memories", return_value={"memories": []}) as memories,
            patch.object(backend, "list_schedules", return_value={"schedules": []}) as schedules,
            patch.object(backend, "list_checkpoints", return_value={"checkpoints": []}) as checkpoints,
            patch.object(backend, "save_workspace_checkpoint", return_value={"id": 2}) as save_checkpoint,
            patch.object(backend, "_workspace_lock", return_value=MagicMock()),
            patch.object(backend, "revert_workspace_checkpoint", return_value={"ok": True}) as revert,
        ):
            backend.route_browser("GET", "/apps/app-5/instructions", None)
            backend.route_browser("GET", "/apps/app-5/memories", None)
            backend.route_browser("GET", "/apps/app-5/schedules", None)
            backend.route_browser("GET", "/apps/app-5/checkpoints", None)
            backend.route_browser("POST", "/apps/app-5/checkpoints", {})
            backend.route_browser("POST", "/apps/app-5/checkpoints/3/revert", {})
        instructions.assert_called_once_with("app-5")
        memories.assert_called_once_with("app-5", {})
        schedules.assert_called_once_with("app-5")
        checkpoints.assert_called_once_with("app-5")
        save_checkpoint.assert_called_once_with("app-5")
        revert.assert_called_once_with("app-5", 3)
        with self.assertRaises(backend.AppError) as hidden_history:
            backend.route_browser("GET", "/apps/app-5/history", None)
        self.assertEqual(hidden_history.exception.status, HTTPStatus.NOT_FOUND)

    def test_stop_route_verifies_the_workspace_and_proxies_to_the_host(self) -> None:
        with (
            patch.object(backend, "_require_web_app") as require,
            patch.object(backend, "call_admin_api", return_value={"status": "accepted"}) as host,
        ):
            result = backend.route_browser("POST", "/apps/app-8/stop", {})
        self.assertEqual(result, {"status": "accepted"})
        require.assert_called_once_with("app-8")
        host.assert_called_once_with("POST", "/v1/threads/app-8/stop", {})

    def test_agent_thread_is_resolved_to_the_exact_workspace(self) -> None:
        with (
            patch.object(backend, "_require_web_app") as require,
            patch.object(backend, "load_app_state", return_value={"ui_revision": 2}) as load,
        ):
            response = backend.route_agent("GET", "/agent/state", None, "app-9")
        self.assertEqual(response["app"]["ui_revision"], 2)
        require.assert_called_once_with("app-9", agent=True)
        load.assert_called_once_with("app-9")

    def test_revert_is_not_an_agent_route(self) -> None:
        # Reverting agent changes is a human control; the agent namespace must
        # not gain a revert verb.
        with (
            patch.object(backend, "_require_web_app"),
            self.assertRaises(backend.AppError) as error,
        ):
            backend.route_agent("POST", "/agent/checkpoints/1/revert", {}, "app-9")
        self.assertEqual(error.exception.status, HTTPStatus.NOT_FOUND)

    def test_agent_memory_and_schedule_routes_are_dispatched(self) -> None:
        with (
            patch.object(backend, "_require_web_app"),
            patch.object(backend, "list_memories", return_value={"memories": []}) as memories,
            patch.object(backend, "save_memory", return_value={"name": "m"}) as save,
            patch.object(backend, "list_schedules", return_value={"schedules": []}) as schedules,
            patch.object(backend, "create_schedule", return_value={"id": 1}) as create,
        ):
            backend.route_agent("GET", "/agent/memories", None, "app-9", {"q": ["x"]})
            backend.route_agent(
                "PUT", "/agent/memories/prefs",
                {"description": "d", "body_md": "b"}, "app-9",
            )
            backend.route_agent("GET", "/agent/schedules", None, "app-9")
            backend.route_agent(
                "POST", "/agent/schedules",
                {"name": "n", "message": "m", "cadence": "interval", "interval_minutes": 30},
                "app-9",
            )
        memories.assert_called_once_with("app-9", {"q": ["x"]}, verify=False)
        self.assertEqual(save.call_args.args[:2], ("app-9", "prefs"))
        self.assertEqual(save.call_args.kwargs["actor"], "agent")
        schedules.assert_called_once_with("app-9", verify=False)
        self.assertEqual(create.call_args.kwargs["actor"], "agent")


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
            "&event_type=thread.message&event_type=thread.activity&event_type=thread.error"
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
                "&event_type=thread.message&event_type=thread.activity&event_type=thread.error"
                "&event_type=thread.stopped",
            ),
        )
        self.assertEqual(
            host.call_args_list[1].args,
            (
                "GET",
                "/v1/threads/app-6/events?before=5&limit=6&message_bytes=122880"
                "&event_type=thread.message&event_type=thread.activity&event_type=thread.error"
                "&event_type=thread.stopped",
            ),
        )

    def test_conversation_events_can_page_without_activity(self) -> None:
        events = {"events": [{"seq": 5, "event_type": "thread.message"}]}
        with (
            patch.object(backend, "_require_web_app"),
            patch.object(backend, "call_admin_api", return_value=events) as host,
        ):
            self.assertEqual(
                backend.browser_conversation_events(
                    "app-6", {"activity": ["false"], "before": ["5"]}
                ),
                events,
            )
        host.assert_called_once_with(
            "GET",
            "/v1/threads/app-6/events?before=5&limit=6&message_bytes=122880"
            "&event_type=thread.message&event_type=thread.error"
            "&event_type=thread.stopped",
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
            patch.object(backend, "_workspace_context", return_value=""),
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

    def test_message_creation_injects_the_workspace_context_block(self) -> None:
        context = (
            f"{backend.CONTEXT_OPEN}\n"
            "Always-on instructions:\nBe terse.\n"
            f"{backend.CONTEXT_CLOSE}\n"
        )
        with (
            patch.object(backend, "_require_web_app"),
            patch.object(backend, "_workspace_context", return_value=context),
            patch.object(
                backend, "call_admin_api", return_value={"status": "accepted"}
            ) as host,
        ):
            backend.create_message(
                {"content": "Morning check."},
                requested_by="schedule",
                thread_id="app-5",
            )
        self.assertEqual(
            host.call_args.args[2]["message"],
            f"Requested by schedule:\n{context}Morning check.",
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
                    patch.object(backend, "_workspace_context", return_value=""),
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
            patch.object(backend, "_workspace_context", return_value=""),
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
            patch.object(backend, "_workspace_context", return_value=""),
            patch.object(backend, "call_admin_api", return_value={"status": "bogus"}),
            self.assertRaises(backend.AppError) as error,
        ):
            backend.create_message(
                {"content": "More."}, requested_by="user", thread_id="app-5"
            )
        self.assertEqual(error.exception.status, HTTPStatus.BAD_GATEWAY)


class ScheduleValidationTests(unittest.TestCase):
    def test_cadence_fields_are_mutually_exclusive_and_bounded(self) -> None:
        valid = backend._validated_schedule_fields(
            {"name": "n", "message": "m", "cadence": "interval", "interval_minutes": 60}
        )
        self.assertEqual(valid["interval_minutes"], 60)
        self.assertIsNone(valid["daily_time"])
        invalid_requests: list[dict[str, Any]] = [
            {"name": "n", "message": "m", "cadence": "interval", "interval_minutes": 4},
            {"name": "n", "message": "m", "cadence": "interval", "interval_minutes": 60, "daily_time": "09:00"},
            {"name": "n", "message": "m", "cadence": "daily", "daily_time": "25:00"},
            {"name": "n", "message": "m", "cadence": "daily", "daily_time": "09:00", "interval_minutes": 60},
            {"name": "n", "message": "m", "cadence": "hourly"},
            {"name": "a\nb", "message": "m", "cadence": "interval", "interval_minutes": 60},
        ]
        for request in invalid_requests:
            with self.subTest(request=request), self.assertRaises(backend.AppError) as error:
                backend._validated_schedule_fields(request)
            self.assertEqual(error.exception.status, HTTPStatus.BAD_REQUEST)

    def test_next_cadence_run_math(self) -> None:
        after = datetime(2026, 7, 31, 10, 30, tzinfo=timezone.utc)
        self.assertEqual(
            backend._next_cadence_run("interval", 90, None, after),
            datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(
            backend._next_cadence_run("daily", None, "11:15", after),
            datetime(2026, 7, 31, 11, 15, tzinfo=timezone.utc),
        )
        self.assertEqual(
            backend._next_cadence_run("daily", None, "09:00", after),
            datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc),
        )

    def test_running_thread_defers_and_missing_session_skips(self) -> None:
        now = datetime(2026, 7, 31, 10, 0, tzinfo=timezone.utc)
        schedule = {
            "message": "msg", "cadence": "interval",
            "interval_minutes": 60, "daily_time": None,
        }
        with (
            patch.object(backend, "_thread_status", return_value="running"),
            patch.object(backend, "_current_due_schedule", return_value=schedule),
            patch.object(backend, "_reschedule") as reschedule,
            patch.object(backend, "create_message") as create,
        ):
            fired = backend._fire_schedule("app-1", 3, now)
        self.assertEqual(fired, 0)
        create.assert_not_called()
        self.assertEqual(
            reschedule.call_args.kwargs,
            {"next_run_at": "2026-07-31T10:05:00Z", "ran_at": None},
        )
        with (
            patch.object(backend, "_thread_status", return_value=None),
            patch.object(backend, "_current_due_schedule", return_value=schedule),
            patch.object(backend, "_reschedule") as reschedule,
            patch.object(backend, "create_message") as create,
        ):
            fired = backend._fire_schedule("app-1", 3, now)
        self.assertEqual(fired, 0)
        create.assert_not_called()
        self.assertEqual(
            reschedule.call_args.kwargs,
            {"next_run_at": "2026-07-31T11:00:00Z", "ran_at": None},
        )

    def test_idle_thread_fires_with_schedule_provenance(self) -> None:
        now = datetime(2026, 7, 31, 10, 0, tzinfo=timezone.utc)
        schedule = {
            "message": "msg", "cadence": "daily",
            "interval_minutes": None, "daily_time": "09:00",
        }
        with (
            patch.object(backend, "_thread_status", return_value="idle"),
            patch.object(backend, "_current_due_schedule", return_value=schedule),
            patch.object(backend, "_reschedule") as reschedule,
            patch.object(backend, "create_message", return_value={"status": "accepted"}) as create,
        ):
            fired = backend._fire_schedule("app-1", 3, now)
        self.assertEqual(fired, 1)
        create.assert_called_once_with(
            {"content": "msg"}, requested_by="schedule", thread_id="app-1"
        )
        self.assertEqual(
            reschedule.call_args.kwargs,
            {"next_run_at": "2026-08-01T09:00:00Z", "ran_at": "2026-07-31T10:00:00Z"},
        )

    def test_schedule_rechecks_idle_status_inside_workspace_lock(self) -> None:
        now = datetime(2026, 7, 31, 10, 0, tzinfo=timezone.utc)
        schedule = {
            "message": "msg", "cadence": "interval",
            "interval_minutes": 60, "daily_time": None,
        }
        lock_held = False

        class RecordingLock:
            def __enter__(self) -> None:
                nonlocal lock_held
                if lock_held:
                    raise AssertionError("workspace lock entered twice")
                lock_held = True

            def __exit__(self, *_args: object) -> None:
                nonlocal lock_held
                lock_held = False

        status_checks = 0

        def status(_thread_id: str) -> str:
            nonlocal status_checks
            status_checks += 1
            self.assertEqual(lock_held, status_checks == 2)
            return "idle" if status_checks == 1 else "running"

        with (
            patch.object(backend, "_workspace_lock", return_value=RecordingLock()),
            patch.object(backend, "_thread_status", side_effect=status),
            patch.object(backend, "_current_due_schedule", return_value=schedule),
            patch.object(backend, "_reschedule") as reschedule,
            patch.object(backend, "create_message") as create,
        ):
            self.assertEqual(backend._fire_schedule("app-1", 3, now), 0)
        self.assertFalse(lock_held)
        self.assertEqual(status_checks, 2)
        create.assert_not_called()
        self.assertEqual(
            reschedule.call_args.kwargs,
            {"next_run_at": "2026-07-31T10:05:00Z", "ran_at": None},
        )


class RuntimeDataActionTests(unittest.TestCase):
    def test_set_delete_and_append_follow_typed_paths(self) -> None:
        data = {"items": [{"name": "one", "done": False}], "tags": []}
        backend._mutate_data(data, "set", ["items", 0, "done"], True)
        backend._mutate_data(data, "append", ["tags"], "new")
        backend._mutate_data(data, "delete", ["items", 0, "name"], None)
        self.assertEqual(data, {"items": [{"done": True}], "tags": ["new"]})

    def test_data_version_conflict_is_checked_inside_the_workspace_row_lock(self) -> None:
        cursor = MagicMock()
        cursor.fetchone.return_value = (2, 7, "", "", "", '{"count":1}', "now")
        transaction = MagicMock()
        transaction.__enter__.return_value = cursor
        with (
            patch.object(backend, "_require_web_app"),
            patch.object(backend.db, "transaction", return_value=transaction),
            self.assertRaises(backend.AppError) as conflict,
        ):
            backend.apply_runtime_action(
                {"action": "set", "expected_data_version": 6, "path": ["count"], "value": 2},
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

    def test_mock_allocates_independent_ids_and_lists_every_app(self) -> None:
        first = self._create_app()
        second = self._create_app()
        third = self._create_app()

        self.assertEqual(
            [first["thread_id"], second["thread_id"], third["thread_id"]],
            ["app-1", "app-2", "app-3"],
        )
        self.assertEqual(
            {app["thread_id"] for app in builder_mock._route_app_api("GET", "apps", None)["apps"]},
            {"app-1", "app-2", "app-3"},
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
        self.assertEqual(first_state["ui_revision"], 1)
        self.assertEqual(second_state["ui_revision"], 0)
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
        self.assertTrue(
            any(
                str(event["payload"].get("message", "")).startswith(
                    "Requested by user:\n[Workspace context]\n"
                )
                and str(event["payload"].get("message", "")).endswith("Add a chart.")
                for event in events
            )
        )
        self.assertEqual(
            [
                event["event_type"]
                for event in events
                if event["event_type"] != "thread.activity"
            ],
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
            and event["payload"]["activity"]["title"] == "Agent provider changed"
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
                "expected_data_version": 1,
                "path": ["count"],
                "value": 7,
            },
        )

        self.assertEqual(changed["app"]["data"]["count"], 7)
        self.assertEqual(changed["app"]["data_version"], 2)
        # Data writes never echo the bundle back to the frame.
        self.assertNotIn("html", changed["app"])
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

    def test_mock_workspace_context_is_injected_and_indexed(self) -> None:
        self._create_app()
        builder_mock._route_app_api(
            "PUT", "apps/app-1/instructions", {"instructions_md": "Stay terse."}
        )
        builder_mock._route_app_api(
            "PUT",
            "apps/app-1/memories/prefs",
            {"description": "Weekly cadence", "body_md": "Mondays 9am."},
        )
        self._send("app-1", "Build it.")
        message = builder_mock.WORKSPACES["app-1"]["events"][0]["payload"]["message"]
        self.assertTrue(message.startswith("Requested by user:\n[Workspace context]\n"))
        self.assertIn("Stay terse.", message)
        self.assertIn("- prefs: Weekly cadence", message)
        self.assertIn("[/Workspace context]\nBuild it.", message)

    def test_mock_checkpoint_updates_one_daily_slot_and_restores_everything(self) -> None:
        self._create_app()
        self._send("app-1", "Build it.")
        builder_mock.TURN_DEADLINES["app-1"] = 0
        builder_mock._route_app_api("GET", "apps/app-1/state", None)
        builder_mock._route_app_api(
            "PUT", "apps/app-1/instructions", {"instructions_md": "Keep this."}
        )
        first = builder_mock._route_app_api(
            "POST", "apps/app-1/checkpoints", {}
        )["checkpoint"]
        second = builder_mock._route_app_api(
            "POST", "apps/app-1/checkpoints", {}
        )["checkpoint"]
        self.assertEqual(first["id"], second["id"])
        builder_mock._route_app_api(
            "POST",
            "apps/app-1/runtime/actions",
            {"action": "set", "expected_data_version": 1, "path": ["count"], "value": 9},
        )
        builder_mock._route_app_api(
            "PUT", "apps/app-1/instructions", {"instructions_md": "Discard this."}
        )
        checkpoints = builder_mock._route_app_api(
            "GET", "apps/app-1/checkpoints", None
        )["checkpoints"]
        self.assertEqual(len([c for c in checkpoints if c["checkpoint_type"] == "manual"]), 1)
        reverted = builder_mock._route_app_api(
            "POST", f"apps/app-1/checkpoints/{first['id']}/revert", {}
        )
        self.assertTrue(reverted["ok"])
        restored = builder_mock.WORKSPACES["app-1"]["app"]
        self.assertEqual(restored["data"]["count"], 2)
        self.assertEqual(restored["data_version"], 3)
        self.assertEqual(restored["ui_revision"], 2)
        self.assertEqual(
            builder_mock.WORKSPACES["app-1"]["instructions"]["instructions_md"],
            "Keep this.",
        )

    def test_mock_enforces_stop_workspace_boundaries(self) -> None:
        self._create_app()
        self._create_app()
        self._send("app-1", "Build it.")

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
        self.assertEqual(state["ui_revision"], 0)


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

    def _replace_ui(self, thread_id: str, revision: int, html: str) -> dict[str, Any]:
        return backend.apply_agent_action(
            {
                "action": "replace_ui",
                "expected_ui_revision": revision,
                "html": html,
                "css": "",
                "javascript": "",
            },
            thread_id,
        )

    def _set(self, thread_id: str, version: int, path: list[Any], value: Any) -> dict[str, Any]:
        return backend.apply_agent_action(
            {
                "action": "set",
                "expected_data_version": version,
                "path": path,
                "value": value,
            },
            thread_id,
        )

    def _history(self, thread_id: str) -> list[dict[str, Any]]:
        return backend.list_history(thread_id, {})["entries"]

    def test_counters_split_and_conflict_independently(self) -> None:
        backend.create_web_app()
        self._replace_ui("app-1", 0, "<p>First</p>")
        changed = self._set("app-1", 0, ["count"], 1)
        self.assertEqual(changed, {"ok": True, "ui_revision": 1, "data_version": 1})
        state = backend.load_app_state("app-1")
        self.assertEqual(state["ui_revision"], 1)
        self.assertEqual(state["data_version"], 1)
        self.assertEqual(state["data"], {"count": 1})
        # A data write does not move the UI revision, and vice versa.
        with self.assertRaises(backend.AppError) as ui_conflict:
            self._replace_ui("app-1", 0, "<p>Stale</p>")
        self.assertEqual(ui_conflict.exception.status, HTTPStatus.CONFLICT)
        with self.assertRaises(backend.AppError) as data_conflict:
            self._set("app-1", 0, ["count"], 2)
        self.assertEqual(data_conflict.exception.status, HTTPStatus.CONFLICT)

    def test_apps_have_independent_chains_and_fixed_threads(self) -> None:
        first = backend.create_web_app()
        second = backend.create_web_app()
        self.assertEqual((first["thread_id"], second["thread_id"]), ("app-1", "app-2"))
        self._set("app-1", 0, ["count"], 1)
        self._set("app-2", 0, ["count"], 9)
        self.assertEqual(backend.load_app_state("app-1")["data"], {"count": 1})
        self.assertEqual(backend.load_app_state("app-2")["data"], {"count": 9})

    def test_rename_keeps_the_same_workspace_id(self) -> None:
        created = backend.create_web_app()
        renamed = backend.rename_web_app(created["thread_id"], {"name": "Meal planner"})
        self.assertEqual(renamed["name"], "Meal planner")
        self.assertEqual(renamed["thread_id"], "app-1")

    def test_app_index_joins_host_session_and_running_status(self) -> None:
        backend.create_web_app()
        backend.create_web_app()
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
            apps = backend.list_web_apps({})["apps"]
        self.assertEqual([app["thread_id"] for app in apps], ["app-1", "app-2"])
        self.assertEqual(apps[0]["session"]["agent_runtime"], "codex")
        self.assertEqual(apps[0]["status"], "running")
        self.assertEqual(apps[0]["ui_revision"], 0)
        self.assertEqual(apps[1]["status"], "idle")

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

    def test_unknown_agent_thread_cannot_read_another_workspace(self) -> None:
        backend.create_web_app()
        with self.assertRaises(backend.AppError) as error:
            backend.route_agent("GET", "/agent/state", None, "app-99")
        self.assertEqual(error.exception.status, HTTPStatus.UNAUTHORIZED)

    def test_history_records_every_write_with_snapshots_on_cadence(self) -> None:
        backend.create_web_app()
        self._replace_ui("app-1", 0, "<p>UI</p>")
        with patch.object(backend, "HISTORY_SNAPSHOT_EVERY", 3):
            for index in range(4):
                self._set("app-1", index, ["step"], index)
        entries = self._history("app-1")
        kinds = [entry["kind"] for entry in entries]
        # Newest first: the 4th set, the cadence snapshot after the 3rd set,
        # sets 3..1, the UI replace, and the two creation anchors.
        self.assertEqual(entries[0]["summary"], "Set step")
        self.assertIn("snapshot", kinds)
        self.assertEqual(kinds.count("ui"), 2)
        self.assertEqual(entries[0]["actor"], "agent")
        self.assertEqual(entries[-1]["kind"], "ui")

    def test_restore_rewinds_data_and_ui_as_a_forward_write(self) -> None:
        backend.create_web_app()
        self._replace_ui("app-1", 0, "<p>v1</p>")
        self._set("app-1", 0, ["count"], 1)
        self._set("app-1", 1, ["count"], 2)
        entries = self._history("app-1")
        target = next(entry for entry in entries if entry["summary"] == "Set count" and entry["data_version"] == 1)
        restored = backend.restore_workspace("app-1", {"history_id": target["id"]})["app"]
        self.assertEqual(restored["data"], {"count": 1})
        self.assertEqual(restored["html"], "<p>v1</p>")
        # A restore is a new forward write on both counters.
        self.assertEqual(restored["ui_revision"], 2)
        self.assertEqual(restored["data_version"], 3)
        newest = self._history("app-1")[0]
        self.assertEqual(newest["restored_from"], target["id"])
        # An in-flight agent write against the pre-restore version conflicts.
        with self.assertRaises(backend.AppError) as stale:
            self._set("app-1", 2, ["count"], 5)
        self.assertEqual(stale.exception.status, HTTPStatus.CONFLICT)

    def test_restore_scope_can_target_data_only(self) -> None:
        backend.create_web_app()
        self._replace_ui("app-1", 0, "<p>v1</p>")
        self._set("app-1", 0, ["count"], 1)
        self._replace_ui("app-1", 1, "<p>v2</p>")
        entries = self._history("app-1")
        target = next(entry for entry in entries if entry["summary"] == "Set count")
        restored = backend.restore_workspace(
            "app-1", {"history_id": target["id"], "scope": "data"}
        )["app"]
        self.assertEqual(restored["html"], "<p>v2</p>")
        self.assertEqual(restored["ui_revision"], 2)
        self.assertEqual(restored["data"], {"count": 1})

    def test_restore_scope_can_target_ui_without_reconstructing_data(self) -> None:
        backend.create_web_app()
        seeded_ui = next(
            entry for entry in reversed(self._history("app-1"))
            if entry["kind"] == "ui"
        )
        self._set("app-1", 0, ["count"], 1)
        with patch.object(
            backend, "_data_at", side_effect=AssertionError("data must not be read")
        ):
            restored = backend.restore_workspace(
                "app-1", {"history_id": seeded_ui["id"], "scope": "ui"}
            )["app"]
        self.assertEqual(restored["ui_revision"], 1)
        self.assertEqual(restored["data_version"], 1)
        self.assertEqual(restored["data"], {"count": 1})

    def test_history_prunes_to_a_window_that_stays_restorable(self) -> None:
        backend.create_web_app()
        self._replace_ui("app-1", 0, "<p>UI</p>")
        with (
            patch.object(backend, "HISTORY_SNAPSHOT_EVERY", 3),
            patch.object(backend, "HISTORY_RETAINED_ENTRIES", 6),
        ):
            for index in range(20):
                self._set("app-1", index, ["step"], index)
        entries = self._history("app-1")
        self.assertLess(len(entries), 20)
        # The single UI anchor survives pruning so old restore points keep
        # their bundle, and the oldest snapshot-reachable entry restores
        # without a missing-anchor conflict.
        self.assertEqual(sum(1 for entry in entries if entry["kind"] == "ui"), 1)
        oldest_reachable = next(
            entry for entry in reversed(entries)
            if entry["kind"] not in {"ui", "checkpoint"}
        )
        restored = backend.restore_workspace(
            "app-1", {"history_id": oldest_reachable["id"]}
        )
        self.assertIn("app", restored)

    def test_instructions_and_memories_round_trip_with_bounds(self) -> None:
        backend.create_web_app()
        saved = backend.save_instructions(
            "app-1", {"instructions_md": "Stay terse."}, actor="agent"
        )
        self.assertEqual(saved["updated_by"], "agent")
        self.assertEqual(
            backend.load_instructions("app-1")["instructions_md"], "Stay terse."
        )
        with self.assertRaises(backend.AppError) as oversized:
            backend.save_instructions(
                "app-1",
                {"instructions_md": "x" * (backend.MAX_INSTRUCTIONS_BYTES + 1)},
                actor="user",
            )
        self.assertEqual(oversized.exception.status, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)

        memory = backend.save_memory(
            "app-1", "prefs",
            {"description": "Weekly cadence", "body_md": "Mondays 9am."},
            actor="agent",
        )
        self.assertEqual(memory["updated_by"], "agent")
        self.assertEqual(
            backend.load_memory("app-1", "prefs")["body_md"], "Mondays 9am."
        )
        hits = backend.list_memories("app-1", {"q": ["monday"]})["memories"]
        self.assertEqual([hit["name"] for hit in hits], ["prefs"])
        misses = backend.list_memories("app-1", {"q": ["tuesday"]})["memories"]
        self.assertEqual(misses, [])
        backend.delete_memory("app-1", "prefs")
        with self.assertRaises(backend.AppError):
            backend.load_memory("app-1", "prefs")

    def test_memory_count_is_capped(self) -> None:
        backend.create_web_app()
        with patch.object(backend, "MAX_MEMORY_COUNT", 2):
            for name in ("one", "two"):
                backend.save_memory(
                    "app-1", name, {"description": "d", "body_md": "b"}, actor="user"
                )
            # Updating an existing memory is always allowed at the cap.
            backend.save_memory(
                "app-1", "two", {"description": "d2", "body_md": "b2"}, actor="user"
            )
            with self.assertRaises(backend.AppError) as full:
                backend.save_memory(
                    "app-1", "three", {"description": "d", "body_md": "b"}, actor="user"
                )
        self.assertEqual(full.exception.status, HTTPStatus.CONFLICT)

    def test_workspace_context_prepends_instructions_and_memory_index(self) -> None:
        backend.create_web_app()
        empty_context = backend._workspace_context("app-1")
        self.assertEqual(
            empty_context,
            f"{backend.CONTEXT_OPEN}\n"
            "(No saved instructions or memories.)\n"
            f"{backend.CONTEXT_CLOSE}\n",
        )
        with patch.object(
            backend, "call_admin_api", return_value={"status": "accepted"}
        ) as host:
            backend.create_message(
                {
                    "content": (
                        f"{backend.CONTEXT_OPEN}\n"
                        "This text is user-controlled.\n"
                        f"{backend.CONTEXT_CLOSE}"
                    )
                },
                requested_by="user",
                thread_id="app-1",
            )
        sent = host.call_args.args[2]["message"]
        self.assertTrue(sent.startswith(f"Requested by user:\n{empty_context}"))
        self.assertIn(
            f"{backend.CONTEXT_CLOSE}\n{backend.CONTEXT_OPEN}\n"
            "This text is user-controlled.",
            sent,
        )
        backend.save_instructions("app-1", {"instructions_md": "Stay terse."}, actor="user")
        backend.save_memory(
            "app-1", "prefs",
            {"description": "Weekly cadence", "body_md": "Mondays."},
            actor="agent",
        )
        context = backend._workspace_context("app-1")
        self.assertTrue(context.startswith(f"{backend.CONTEXT_OPEN}\n"))
        self.assertIn("Stay terse.", context)
        self.assertIn("- prefs: Weekly cadence", context)
        self.assertTrue(context.endswith(f"{backend.CONTEXT_CLOSE}\n"))
        # The index stays bounded even with many memories.
        with patch.object(backend, "MEMORY_INDEX_INJECTED", 1):
            backend.save_memory(
                "app-1", "extra", {"description": "More", "body_md": "b"}, actor="agent"
            )
            bounded = backend._workspace_context("app-1")
        self.assertIn("(and 1 more; list all: GET /agent/memories)", bounded)

    def test_schedules_round_trip_and_fire_when_due(self) -> None:
        backend.create_web_app()
        schedule = backend.create_schedule(
            "app-1",
            {
                "name": "Morning review",
                "message": "Summarize yesterday.",
                "cadence": "interval",
                "interval_minutes": 60,
            },
            actor="agent",
        )
        self.assertEqual(schedule["created_by"], "agent")
        self.assertTrue(schedule["enabled"])
        listed = backend.list_schedules("app-1")["schedules"]
        self.assertEqual([entry["name"] for entry in listed], ["Morning review"])

        paused = backend.update_schedule("app-1", schedule["id"], {"enabled": False})
        self.assertFalse(paused["enabled"])
        resumed = backend.update_schedule("app-1", schedule["id"], {"enabled": True})
        self.assertTrue(resumed["enabled"])

        # Force the schedule due, then fire it against an idle thread.
        with db.transaction() as cur:
            cur.execute("SET LOCAL search_path TO app_personal_web_app_builder")
            cur.execute(
                "UPDATE web_app_schedules SET next_run_at = %s WHERE id = %s",
                ("2020-01-01T00:00:00Z", schedule["id"]),
            )
        sent: list[dict[str, Any]] = []

        def admin(method: str, path: str, body: Any = None) -> dict[str, Any]:
            if method == "GET":
                return {"thread": {"thread_id": "app-1", "status": "idle",
                                   "agent_runtime": "codex", "model": "gpt-5.6-terra",
                                   "effort": "high"}}
            sent.append({"path": path, "body": body})
            return {"status": "accepted"}

        with patch.object(backend, "call_admin_api", side_effect=admin):
            fired = backend.run_due_schedules(datetime(2026, 7, 31, 10, 0, tzinfo=timezone.utc))
        self.assertEqual(fired, 1)
        self.assertEqual(sent[0]["path"], "/v1/threads/app-1/messages")
        self.assertTrue(
            sent[0]["body"]["message"].startswith("Requested by schedule:\n")
        )
        self.assertNotIn("agent_runtime", sent[0]["body"])
        after = backend.list_schedules("app-1")["schedules"][0]
        self.assertEqual(after["last_run_at"], "2026-07-31T10:00:00Z")
        self.assertEqual(after["next_run_at"], "2026-07-31T11:00:00Z")

        backend.delete_schedule("app-1", schedule["id"])
        self.assertEqual(backend.list_schedules("app-1")["schedules"], [])

    def test_schedule_paused_during_thread_lookup_is_not_sent(self) -> None:
        backend.create_web_app()
        schedule = backend.create_schedule(
            "app-1",
            {
                "name": "n", "message": "stale", "cadence": "interval",
                "interval_minutes": 60,
            },
            actor="user",
        )
        with db.transaction() as cur:
            cur.execute("SET LOCAL search_path TO app_personal_web_app_builder")
            cur.execute(
                "UPDATE web_app_schedules SET next_run_at = %s WHERE id = %s",
                ("2020-01-01T00:00:00Z", schedule["id"]),
            )

        def pause_while_checking(_thread_id: str) -> str:
            backend.update_schedule("app-1", schedule["id"], {"enabled": False})
            return "idle"

        with (
            patch.object(backend, "_thread_status", side_effect=pause_while_checking),
            patch.object(backend, "create_message") as create,
        ):
            fired = backend.run_due_schedules(
                datetime(2026, 7, 31, 10, 0, tzinfo=timezone.utc)
            )
        self.assertEqual(fired, 0)
        create.assert_not_called()

    def test_manual_checkpoint_updates_one_daily_slot_and_restores_whole_workspace(self) -> None:
        backend.create_web_app()
        backend.rename_web_app("app-1", {"name": "Saved name"})
        self._replace_ui("app-1", 0, "<main>saved</main>")
        self._set("app-1", 0, ["count"], 2)
        backend.save_instructions(
            "app-1", {"instructions_md": "Saved instructions"}, actor="user"
        )
        backend.save_memory(
            "app-1", "saved-memory",
            {"description": "Saved description", "body_md": "Saved body"},
            actor="user",
        )
        schedule = backend.create_schedule(
            "app-1",
            {
                "name": "Saved schedule", "message": "Saved message",
                "cadence": "interval", "interval_minutes": 60,
            },
            actor="user",
        )
        first = backend.save_workspace_checkpoint("app-1")
        second = backend.save_workspace_checkpoint("app-1")
        self.assertEqual(first["id"], second["id"])
        checkpoints = backend.list_checkpoints("app-1")["checkpoints"]
        self.assertEqual(
            sum(entry["checkpoint_type"] == "manual" for entry in checkpoints), 1
        )

        backend.rename_web_app("app-1", {"name": "Wrong name"})
        self._replace_ui("app-1", 1, "<main>wrong</main>")
        self._set("app-1", 1, ["count"], 99)
        backend.save_instructions(
            "app-1", {"instructions_md": "Wrong instructions"}, actor="agent"
        )
        backend.delete_memory("app-1", "saved-memory")
        backend.update_schedule("app-1", schedule["id"], {"name": "Wrong schedule"})

        restored = backend.revert_workspace_checkpoint("app-1", first["id"])
        self.assertTrue(restored["ok"])
        state = backend.load_app_state("app-1")
        self.assertEqual(state["html"], "<main>saved</main>")
        self.assertEqual(state["data"], {"count": 2})
        self.assertEqual(backend.load_instructions("app-1")["instructions_md"], "Saved instructions")
        self.assertEqual(backend.load_memory("app-1", "saved-memory")["body_md"], "Saved body")
        restored_schedules = backend.list_schedules("app-1")["schedules"]
        self.assertEqual(restored_schedules[0]["name"], "Saved schedule")
        self.assertEqual(restored_schedules[0]["id"], schedule["id"])
        with db.transaction() as cur:
            cur.execute("SET LOCAL search_path TO app_personal_web_app_builder")
            cur.execute("SELECT name FROM web_apps WHERE thread_id = 'app-1'")
            name_row = cur.fetchone()
        self.assertEqual(name_row, ("Saved name",))

    def test_daily_snapshots_are_idempotent_and_retain_seven_days(self) -> None:
        backend.create_web_app()
        # create_web_app() snapshots at the real current date, so anchor the
        # window one day later: the first run then always has a new date to
        # create, whatever day the suite runs on.
        start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        ) + timedelta(days=1)
        self.assertEqual(backend.run_daily_workspace_snapshots(start), 1)
        self.assertEqual(backend.run_daily_workspace_snapshots(start), 0)
        for offset in range(1, 9):
            backend.run_daily_workspace_snapshots(start + timedelta(days=offset))
        automatic = [
            entry for entry in backend.list_checkpoints("app-1")["checkpoints"]
            if entry["checkpoint_type"] == "automatic"
        ]
        self.assertEqual(len(automatic), backend.CHECKPOINT_RETAIN_DAYS)
        self.assertEqual(
            automatic[-1]["checkpoint_date"],
            (start + timedelta(days=2)).date().isoformat(),
        )
        self.assertEqual(
            automatic[0]["checkpoint_date"],
            (start + timedelta(days=8)).date().isoformat(),
        )

    def test_side_system_changes_are_recorded_for_internal_audit(self) -> None:
        backend.create_web_app()
        backend.save_instructions("app-1", {"instructions_md": "Be kind."}, actor="agent")
        backend.save_memory(
            "app-1", "prefs", {"description": "d", "body_md": "b"}, actor="agent"
        )
        schedule = backend.create_schedule(
            "app-1",
            {"name": "n", "message": "m", "cadence": "interval", "interval_minutes": 60},
            actor="agent",
        )
        summaries = [entry["summary"] for entry in self._history("app-1")]
        self.assertIn("Edited always-on instructions", summaries)
        self.assertIn("Added memory prefs", summaries)
        self.assertIn("Added schedule n", summaries)

        entries = self._history("app-1")
        memory_entry = next(e for e in entries if e["summary"] == "Added memory prefs")
        self.assertEqual(memory_entry["resource_label"], "Memory")
        self.assertIsNone(memory_entry["revert_mode"])
        self.assertIsNone(memory_entry["revert_prompt"])
        self.assertEqual(backend.load_memory("app-1", "prefs")["body_md"], "b")
        self.assertEqual(backend.list_schedules("app-1")["schedules"][0]["id"], schedule["id"])

    def test_schedule_count_is_capped(self) -> None:
        backend.create_web_app()
        with patch.object(backend, "MAX_SCHEDULES_PER_APP", 1):
            backend.create_schedule(
                "app-1",
                {"name": "a", "message": "m", "cadence": "interval", "interval_minutes": 60},
                actor="user",
            )
            with self.assertRaises(backend.AppError) as full:
                backend.create_schedule(
                    "app-1",
                    {"name": "b", "message": "m", "cadence": "interval", "interval_minutes": 60},
                    actor="user",
                )
        self.assertEqual(full.exception.status, HTTPStatus.CONFLICT)
