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

from host.runtime.workspace.web_apps import backend
from host.runtime.workspace import seen
from host.runtime.core import db
from host.runtime.deploy import migrate
from tests.workspaces.web_apps import smoke as builder_mock


REPO_ROOT = Path(__file__).resolve().parents[1]
APP_DIR = REPO_ROOT / "host" / "runtime" / "workspace" / "web_apps"
MIGRATIONS_DIR = REPO_ROOT / "host" / "migrations"


class AgenticWebAppContractTests(unittest.TestCase):
    def test_app_summary_forwards_the_latest_host_event_and_message_sequences(self) -> None:
        summary = backend._web_app_summary(
            (
                "app-1",
                "Release notes",
                3,
                "2026-08-18T10:00:00Z",
                "2026-08-18T10:01:00Z",
                False,
                False,
                "codex",
                "gpt-5.6-terra",
                "high",
            ),
            {
                "status": "idle",
                "agent_runtime": "codex",
                "model": "gpt-5.6-terra",
                "effort": "high",
                "last_used_at": "2026-08-18T10:02:00Z",
                "latest_event_seq": 42,
                "latest_message_seq": 40,
            },
        )

        self.assertEqual(summary["latest_event_seq"], 42)
        self.assertEqual(summary["latest_message_seq"], 40)
        self.assertEqual(
            summary["agent_settings"],
            {"agent_runtime": "codex", "model": "gpt-5.6-terra", "effort": "high"},
        )

    def test_agent_settings_migration_backfills_and_requires_complete_values(self) -> None:
        migration = (MIGRATIONS_DIR / "0048_web_app_agent_settings.sql").read_text()
        self.assertIn("ADD COLUMN agent_runtime TEXT", migration)
        self.assertIn("ADD COLUMN agent_model TEXT", migration)
        self.assertIn("ADD COLUMN agent_effort TEXT", migration)
        self.assertIn("FROM thread_sessions AS session", migration)
        self.assertIn("WHERE session.thread_id = app.app_id", migration)
        self.assertIn("agent_model = 'gpt-5.6-sol'", migration)
        self.assertIn("ALTER COLUMN agent_runtime SET NOT NULL", migration)

    def test_app_summary_never_exposes_nullable_agent_settings(self) -> None:
        with self.assertRaises(backend.WorkspaceError) as error:
            backend._agent_settings_from_values(None, None, None)
        self.assertEqual(error.exception.status, HTTPStatus.INTERNAL_SERVER_ERROR)

    def test_app_default_prefers_an_active_runtime_and_named_model(self) -> None:
        expected_models = {
            "claude_code": "claude-opus-5",
            "grok": "grok-4.6",
            "hermes": "moonshotai.kimi-k2.5",
        }
        for runtime, model in expected_models.items():
            with self.subTest(runtime=runtime), patch.object(
                backend, "active_agent_runtimes", return_value=[runtime]
            ):
                self.assertEqual(
                    backend.default_app_agent_settings(),
                    {
                        "agent_runtime": runtime,
                        "model": model,
                        "effort": "high",
                    },
                )
        with patch.object(backend, "active_agent_runtimes", return_value=[]):
            self.assertEqual(
                backend.default_app_agent_settings(),
                {
                    "agent_runtime": "codex",
                    "model": "gpt-5.6-sol",
                    "effort": "high",
                },
            )

    def test_fixed_workspace_identity(self) -> None:
        self.assertFalse((APP_DIR / "agent.md").exists())

    def test_ui_script_has_an_isolated_lexical_scope(self) -> None:
        source = (APP_DIR / "ui" / "personal_web_app_builder.js").read_text()
        self.assertTrue(source.startswith('(() => {\n"use strict";'))
        self.assertTrue(source.rstrip().endswith("})();"))

    def test_capability_worker_supports_opt_in_targeted_data(self) -> None:
        source = (APP_DIR / "ui" / "personal_web_app_builder.js").read_text()
        self.assertIn('read(path) { return read(path); }', source)
        self.assertIn('options.data === "targeted"', source)
        self.assertIn('/runtime/data/read', source)
        self.assertIn('/state/ui', source)
        self.assertIn('/state/data', source)
        self.assertIn('JSON.parse(JSON.stringify(message.value))', source)
        self.assertEqual(source.count("Object.defineProperty(parent, leaf"), 2)

    def test_capability_worker_supports_collection_queries(self) -> None:
        source = (APP_DIR / "ui" / "personal_web_app_builder.js").read_text()
        self.assertIn("query(collection, request) { return query(collection, request); }", source)
        self.assertIn('type: "collection-query"', source)
        self.assertIn("/runtime/collections/${encodeURIComponent(message.collection)}/query", source)
        self.assertIn('message.type === "collection-query-result"', source)
        self.assertIn("response.collection.revision !== run.revision", source)

    def test_ui_is_an_app_first_canvas_with_one_command_surface(self) -> None:
        index = (APP_DIR / "ui" / "index.html").read_text()
        source = (APP_DIR / "ui" / "personal_web_app_builder.js").read_text()
        css = (APP_DIR / "ui" / "personal_web_app_builder.css").read_text()
        composer_css = (APP_DIR.parent / "ui" / "composer.css").read_text()
        for element_id in (
            'id="app-view"', 'id="app-update-veil"', 'id="archived-app-veil"',
            'id="app-refresh"', 'id="settings-open"', 'id="recovery-open"',
            'id="lock-agent-updates"', 'id="history-toggle"',
            'id="agent-command-surface"', 'id="rename-app"',
            'id="app-subtitle"', 'id="composer-running"', 'id="chat-status"',
            'id="app-history-list"', 'id="recovery-drawer"',
            'id="chat-history"', 'id="chat-history-scroll"',
            'id="chat-history-list"', 'id="chat-history-more"',
            'id="chat-announcer"',
            'id="stop-turn"',
            'id="attach-file"', 'id="attachments"',
            'id="agent-settings-idle-note"',
        ):
            self.assertIn(element_id, index)
        self.assertNotIn('id="admin-overlay"', index)
        self.assertNotIn('id="chat-drawer"', index)
        self.assertNotIn('id="sidebar-open"', index)
        self.assertNotIn('id="open-chat"', index)
        self.assertNotIn('id="home-view"', index)
        self.assertNotIn('id="workspace-panel"', index)
        self.assertNotIn('id="latest-agent-card"', index)
        self.assertNotIn('id="latest-agent-message"', index)
        self.assertLess(index.index('id="chat-history"'), index.index('id="chat-composer"'))
        self.assertIn('class="chat-composer workspace-composer"', index)
        self.assertNotIn('id="revision-label"', index)
        self.assertIn(
            ".agent-settings.active-locked:hover #agent-settings-idle-note",
            css,
        )
        self.assertNotIn("<script", index)
        self.assertNotIn("<link", index)
        self.assertIn('id="archive-app"', index)
        self.assertIn("selectedAppOutsideActiveIndex", source)
        self.assertIn("function appWritesBlocked()", source)
        self.assertIn("selectedAppId = null", source)
        self.assertIn("INITIAL_CONVERSATION_EVENT_PAGES = 1", source)
        self.assertIn("conversationViewStates = new Map()", source)
        self.assertIn('$("composer-running").hidden = !running', source)
        self.assertIn('$("agent-command-surface").hidden = !hasApp || readOnly || !historyMode', source)
        self.assertIn('`${runtimeLabel(runtime)} · ${modelLabel(runtime, model)} · ${optionLabel(effort)}`', source)
        self.assertIn("stopRunningTurn()", source)
        self.assertIn("sessionConfigurationChanged()", source)
        self.assertIn("|| sessionConfigurationChanged()", source)
        self.assertIn('classList.toggle("sending", composerSending)', source)
        self.assertIn(".send-button.sending::after", composer_css)
        self.assertNotIn(".send-button", css)
        self.assertNotIn('id="agent-session-change-warning"', index)
        self.assertIn('closest(".md-open-file")', source)
        self.assertIn('sender.className = "chat-history-sender"', source)
        self.assertIn(".attach-button,\n  .send-button", composer_css)
        self.assertIn("const persistFallback = Boolean(", source)
        self.assertNotIn("conversationResponse.agent_settings", source)
        self.assertIn("selectedAgentSettings = app.agent_settings;", source)
        self.assertIn("selectedAgentSettings = response.app.agent_settings;", source)
        self.assertIn("runtimeRunnable(savedSettings.agent_runtime)", source)
        self.assertIn('codex: "gpt-5.6-sol"', source)
        self.assertIn('claude_code: "claude-opus-5"', source)
        self.assertIn('hermes: "moonshotai.kimi-k2.5"', source)
        self.assertIn("await agentSettingsSaveQueue", source)
        send_message = source.split("async function sendMessage", 1)[1].split(
            "\nasync function stopRunningTurn", 1
        )[0]
        self.assertLess(
            send_message.index("const submittedSettings = currentAgentSettings();"),
            send_message.index("await agentSettingsSaveQueue;"),
        )
        self.assertLess(
            send_message.index("const includeSubmittedSettings ="),
            send_message.index("await agentSettingsSaveQueue;"),
        )
        self.assertIn(
            "sameAgentSettings(settingsFailure.settings, submittedSettings)",
            send_message,
        )
        self.assertIn("body.agent_runtime = submittedSettings.agent_runtime", send_message)
        self.assertIn("agentSettingsSaveFailures.get(appId)", source)
        self.assertIn("pendingAgentSettingsByApp.get(appId) || null", source)
        self.assertIn("appsRefreshSequence += 1;", source)
        settings_save = source.split("function persistAgentSettings", 1)[1].split(
            "\nfunction setSessionOptions", 1
        )[0]
        self.assertLess(
            settings_save.index("appsRefreshSequence += 1;"),
            settings_save.index("pendingAgentSettingsByApp.delete(appId);"),
        )
        self.assertIn(
            "if (selectedAppId === appId) selectedRefreshSequence += 1;",
            settings_save,
        )
        self.assertNotIn("await window.KernHost.refreshNavigation()", settings_save)
        self.assertIn("void window.KernHost.refreshNavigation().catch(() => {});", source)
        self.assertIn("selectedAppOutsideActiveIndex || snapshot.status", source)
        self.assertIn("const settingsKey = selectedAgentSettings", source)
        activation = source.split("async function refreshRuntimeActivation", 1)[1].split(
            "\nasync function refresh()", 1
        )[0]
        self.assertIn("setSessionOptions(opening.model, opening.effort);\n    persistAgentSettings();", activation)
        self.assertIn('showChatStatus("Stopping…")', source)
        self.assertIn("window.KernHost.chooseFiles", source)
        self.assertIn("window.KernHost.apiUpload", source)
        upload = source.split("async function requestFileUpload", 1)[1].split(
            "\nfunction capabilityWorkerBootstrap", 1
        )[0]
        self.assertLess(
            upload.index("await window.KernHost.apiUpload(file)"),
            upload.rindex("localFiles.delete(selectionId)"),
        )
        self.assertIn("[User-uploaded file: ${attachment.file.path}]", source)
        self.assertIn('query.push("activity=false")', source)
        self.assertIn("function renderConversationHistory", source)
        self.assertIn("function loadOlderConversationEvents", source)
        self.assertIn("message.textContent = entry.message", source)
        self.assertIn("historyRenderedAppId !== selectedAppId", source)
        self.assertIn("entryKey !== historyRenderedEntryKey", source)
        self.assertIn('scrollTop <= 80', source)
        self.assertIn("button.app-toolbar-button.history-toggle.active", css)
        command_surface = css.split(".agent-command-surface {", 1)[1].split("}", 1)[0]
        self.assertIn("z-index: 13", command_surface)
        load_older = source.split("async function loadOlderConversationEvents", 1)[1].split(
            "\nfunction setHistoryMode", 1
        )[0]
        self.assertLess(
            load_older.index("const response = await api("),
            load_older.index("const previousHeight = scroll.scrollHeight"),
        )
        self.assertIn(
            "scroll.scrollTop = previousTop + scroll.scrollHeight - previousHeight",
            load_older,
        )
        self.assertIn("KernRichText.compactActivityEvents(ordered)", source)
        self.assertIn("Number(listed?.seen_message_seq) || 0", source)
        self.assertNotIn("const renderedMessageSeq = conversationEvents.reduce", source)
        self.assertIn("renderConversationHistory(true);\n    markSelectedAppSeen();", source)
        self.assertIn('id="chat-announcer" class="sr-only" role="status"', index)
        self.assertIn("newestAgentSeq > previousNewestAgentSeq", source)
        self.assertIn('$("chat-announcer").textContent = `Agent:', source)
        self.assertIn(".sr-only {", css)
        self.assertIn("refreshSequence !== appsRefreshSequence", source)
        self.assertIn("let appSelectionSequence = 0;", source)
        self.assertIn("selectionSequence !== appSelectionSequence", source)
        self.assertIn("if (!createAppPromise)", source)
        self.assertIn("selectedAppId && !selectedAppOutsideActiveIndex", source)
        self.assertIn("if (!selectedAppId || appWritesBlocked()) return;", source)
        self.assertIn("if (focused && typeof focused.blur", source)
        self.assertIn("stopCapabilityWorker();", source)
        self.assertIn("COMPOSER_DRAFTS_STORAGE_KEY", source)
        self.assertIn("localStorage.setItem", source)
        self.assertIn("/revisions/${revision}/restore", source)
        self.assertIn("applyAppVersion(response.app);", source)
        self.assertIn("@media (max-width: 720px)", css)
        self.assertIn("@media (prefers-reduced-motion: reduce)", css)
        self.assertEqual(
            (backend.SEND_BUSY_RETRIES - 1) * backend.SEND_BUSY_RETRY_DELAY_SECONDS,
            10,
        )

    def test_capability_worker_uses_one_trusted_networkless_broker(self) -> None:
        source = (APP_DIR / "ui" / "capability_worker_sandbox.js").read_text()
        builder = (APP_DIR / "ui" / "personal_web_app_builder.js").read_text()
        index = (APP_DIR / "ui" / "index.html").read_text()
        self.assertIn('new Worker("/workspace/capability-worker-sandbox.js")', builder)
        self.assertIn('this.bridge.postMessage({ type: "create", source })', builder)
        self.assertIn(
            "`data:application/javascript;charset=utf-8,${encodeURIComponent(message.source)}`",
            source,
        )
        self.assertNotIn("Blob", source)
        self.assertNotIn("iframe", index)

    def test_generated_apps_use_shared_safe_navigation_and_copy_fallback(self) -> None:
        source = (APP_DIR / "ui" / "personal_web_app_builder.js").read_text()
        self.assertNotIn("function safeXReplyIntentHref(value)", source)
        self.assertIn("KernRichText.safeNavigationHref(rawHref)", source)
        self.assertIn("KernRichText.safeHref(rawHref)", source)
        self.assertIn('clean.setAttribute("title", navigationHref)', source)
        self.assertIn('clean.setAttribute("target", "_blank")', source)
        self.assertIn('clean.setAttribute("rel", "noopener noreferrer")', source)
        self.assertIn('clean.setAttribute("data-kern-copy-href", copyHref)', source)
        self.assertIn('event.target.closest("button[data-kern-copy-href]")', source)
        self.assertIn('showRuntimeStatus("Link copied", "success")', source)

    def test_frame_renders_recorded_messages_without_rewriting_them(self) -> None:
        source = (APP_DIR / "ui" / "personal_web_app_builder.js").read_text()
        self.assertNotIn("[Workspace context]", source)
        self.assertNotIn("displayedUserMessage", source)
        self.assertIn('id="chat-history"', (APP_DIR / "ui" / "index.html").read_text())
        self.assertIn("message.textContent = entry.message", source)
        self.assertNotIn("Requested by user:", source)

    def test_generated_worker_is_pinned_to_its_workspace(self) -> None:
        source = (APP_DIR / "ui" / "personal_web_app_builder.js").read_text()
        sandbox = (APP_DIR / "ui" / "capability_worker_sandbox.js").read_text()
        self.assertIn("new SandboxedCapabilityWorker", source)
        self.assertIn('new Worker("/workspace/capability-worker-sandbox.js")', source)
        self.assertIn("new Worker(", sandbox)
        self.assertIn("selectedAppId !== run.appId", source)
        self.assertIn("stopCapabilityWorker()", source)
        self.assertIn("encodeURIComponent(run.appId)", source)
        self.assertIn("void sendMessage(message.message.trim(), run.appId)", source)
        self.assertIn(
            "conversationResponse.session || listedSession || snapshot.session",
            source,
        )
        self.assertIn("MAX_WORKER_MUTATIONS_PER_TURN = 16", source)
        self.assertIn('"fetch", "XMLHttpRequest", "WebSocket"', source)
        self.assertNotIn("window.open", source)
        self.assertNotIn("location.href", source)

    def test_worker_turns_use_one_revision_and_deny_timers(self) -> None:
        source = (APP_DIR / "ui" / "personal_web_app_builder.js").read_text()
        self.assertIn("const WORKER_START_TIMEOUT_MS = 15 * 1000", source)
        self.assertIn("const WORKER_TURN_TIMEOUT_MS = 3000", source)
        self.assertIn("expected_revision: run.revision", source)
        self.assertIn('"setTimeout", "setInterval", "clearTimeout", "clearInterval", "setImmediate"', source)
        self.assertIn('"MessageChannel", "MessagePort"', source)
        # The armed worker only promotes when nothing moved underneath it.
        self.assertIn("armedWorker.revision === app.revision", source)
        self.assertIn("if (this.finished) return;", source)
        self.assertIn("const current = workerRun === this;", source)
        self.assertIn('if (current && reason === "timeout")', source)
        self.assertIn("This app action took too long and was stopped.", source)
        self.assertIn('else if (current && reason === "error")', source)
        self.assertIn("This app action failed.", source)
        self.assertNotIn("Generated behavior stopped safely", source)
        self.assertIn("function clearRuntimeStatus()", source)
        self.assertIn("showRuntimeStatus(message, level = \"info\", persistent = false)", source)
        self.assertIn("if (persistent) return;", source)
        self.assertIn("This app could not start its live interface.", source)
        worker_run = source.split("async function runCapabilityWorker", 1)[1]
        self.assertLess(
            worker_run.index("clearRuntimeStatus();"),
            worker_run.index("if (appWritesBlocked()"),
        )
        self.assertIn("armCapabilityWorker", source)
        self.assertIn("armed.timer = setTimeout(discard, WORKER_TURN_TIMEOUT_MS)", source)
        self.assertIn("clearTimeout(armed.timer);", source)
        self.assertIn("armed.run = run;", source)
        self.assertIn("armed.timer = setTimeout(discard, WORKER_START_TIMEOUT_MS)", source)
        self.assertGreaterEqual(
            source.count(
                'run.timer = setTimeout(() => run.finish("timeout", "execution"), '
                "WORKER_TURN_TIMEOUT_MS)"
            ),
            2,
        )
        self.assertIn("let pendingApp = null", source)
        self.assertIn("function applyPendingAppVersion()", source)
        self.assertIn("pendingApp = next.app", source)
        self.assertIn("pendingApp = null", source)
        self.assertIn(
            "function appMutationInFlight(appId, currentRevision, observedRevision)",
            source,
        )
        self.assertIn("observedRevision === currentRevision + 1", source)
        refresh = source.split("async function refreshSelectedApp", 1)[1].split(
            "\nasync function refresh()", 1
        )[0]
        self.assertIn(
            "if (!appMutationInFlight(appId, currentApp.revision, next.app.revision))",
            refresh,
        )
        self.assertLess(
            refresh.index(
                "if (!appMutationInFlight(appId, currentApp.revision, next.app.revision))"
            ),
            refresh.index("pendingApp = next.app"),
        )
        mutation = source.split("async function handleWorkerDataAction", 1)[1].split(
            "\nfunction validDataPath", 1
        )[0]
        failure = mutation.split("} catch (_error) {", 1)[1]
        self.assertLess(
            failure.index("run.mutationPending = false"),
            failure.index("await refreshSelectedApp(run.appId)"),
        )
        preview_failure = source.split("function applyAppVersion(app)", 1)[1].split(
            "function applyPendingAppVersion", 1
        )[0]
        self.assertIn("catch (_error) {", preview_failure)
        self.assertIn("clearGenerated();", preview_failure)
        # The bundle source survives across turns for one App revision.
        self.assertIn("bundleUrl.revision === revision", source)
        sandbox = (APP_DIR / "ui" / "capability_worker_sandbox.js").read_text()
        self.assertIn("data:application/javascript", sandbox)
        self.assertNotIn("URL.createObjectURL", sandbox)
        # Renders patch the shadow tree instead of replacing it wholesale.
        self.assertIn("function patchNode(", source)
        self.assertIn("sanitizeCssCached", source)
        self.assertIn("generatedRoot.adoptedStyleSheets = [generatedStyleSheet]", source)
        self.assertIn("generatedStyleSheet.replaceSync(styleText)", source)
        self.assertIn('generatedStyleLink.rel = "stylesheet"', source)
        self.assertIn('new Blob([styleText], { type: "text/css" })', source)
        self.assertIn("generatedMobileTextControlCss", source)
        self.assertIn("font-size:max(16px,1em)!important", source)
        self.assertIn("${safeCss}${generatedMobileTextControlCss}", source)
        self.assertGreaterEqual(source.count("current === generatedStyleLink"), 2)
        self.assertIn("parent.insertBefore(want, generatedStyleLink)", source)
        self.assertIn("MAX_RENDER_NODES = 5000", source)
        self.assertIn("MAX_RENDER_DEPTH = 128", source)
        self.assertIn("MAX_CSS_RULES = 4096", source)
        self.assertIn("MAX_CSS_RULE_DEPTH = 16", source)
        self.assertNotIn('document.createElement("style")', source)
        # Drag state stays in the trusted frame and only bounded plain values
        # enter the worker event payload.
        self.assertIn('lower === "data-drag-value"', source)
        self.assertIn('lower === "data-drop-action"', source)
        self.assertIn('lower === "data-drop-value"', source)
        self.assertIn('lower === "data-enter-action"', source)
        self.assertIn('event.dataTransfer.clearData()', source)
        self.assertIn('event.dataTransfer.setData("text/plain", "")', source)
        self.assertIn('draggedValue: clipEncodedText(', source)
        self.assertIn('generatedRoot.addEventListener("drop", generatedDrop)', source)
        self.assertIn('generatedRoot.addEventListener("keydown", generatedEnterInteraction)', source)
        # Enter cannot bypass read-only App state or attachment/session
        # validation represented by the disabled composer action.
        self.assertIn(
            "if (selectedAppOutsideActiveIndex || "
            '(!fromGeneratedApp && $("send-message").disabled)) return;',
            source,
        )

    def test_growing_workspace_editors_reflow_when_their_width_changes(self) -> None:
        source = (
            REPO_ROOT / "host" / "runtime" / "workspace" / "ui" / "workspace.js"
        ).read_text()
        self.assertIn('const growingEditorIds = ["memory-content", "schedule-message"]', source)
        self.assertIn("new ResizeObserver(entries =>", source)
        self.assertIn("observedWidths.get(entry.target) === width", source)
        self.assertIn("resizeTextarea(entry.target.id)", source)

    def test_memory_ui_separates_swarm_and_individual_pages(self) -> None:
        source = (
            REPO_ROOT / "host" / "runtime" / "workspace" / "ui" / "workspace.js"
        ).read_text()
        markup = (
            REPO_ROOT / "host" / "runtime" / "workspace" / "ui" / "index.html"
        ).read_text()
        self.assertIn('data-memory-scope="swarm"', markup)
        self.assertIn('data-memory-scope="individual"', markup)
        self.assertIn('id="memory-content" maxlength="2000"', markup)
        self.assertIn('id="schedule-message" maxlength="12000"', markup)
        self.assertIn('params.set("scope", state.memoryScope)', source)
        self.assertIn('state.memoryScope === "individual"', source)

    def test_agent_instructions_are_current(self) -> None:
        agent_home = REPO_ROOT / "host" / "bootstrap" / "agent-home"
        instructions_path = agent_home / "agents_claude.md"
        instructions = instructions_path.read_text()
        web_apps = (agent_home / "references" / "web-apps.md").read_text()
        memory = (agent_home / "references" / "memory.md").read_text()
        schedules = (agent_home / "references" / "schedules.md").read_text()
        self.assertLessEqual(len(instructions.encode()), 10_000)
        self.assertIn("## Kern capabilities", instructions)
        for reference in ("web-apps.md", "memory.md", "schedules.md"):
            self.assertIn(
                f"/opt/kern-host/host/bootstrap/agent-home/references/{reference}", instructions
            )
        self.assertIn("Web Apps Workspace API", instructions)
        self.assertIn("Swarm memory (global memory)", instructions)
        self.assertIn("Self-memory", instructions)
        self.assertLess(
            instructions.index("Self-memory"),
            instructions.index("Swarm memory (global memory)"),
        )
        self.assertIn("GET /agent/self/memory", instructions)
        self.assertIn("first request in each execution", instructions)
        self.assertIn("GET /agent/memory/search?q=words&limit=20", instructions)
        self.assertIn("GET /agent/memory/pages/{page_id}", instructions)
        self.assertIn("GET /agent/apps/{app_id}/state/{meta|ui|data|data/shape}", instructions)
        self.assertIn("Migrated Apps inherit the configuration", web_apps)
        self.assertIn("POST /agent/apps/{app_id}/actions", instructions)
        self.assertIn("There is no run/status or separate failure API", instructions)
        self.assertIn("Global schedules", instructions)
        self.assertIn("content is up to 2,000 characters", memory)
        self.assertIn(
            "Schedule messages may contain up to 12,000 characters", schedules
        )
        self.assertIn("GET /agent/identity", instructions)
        self.assertIn("search_conversation_history", instructions)
        self.assertIn("read_thread_history", instructions)
        self.assertIn("Historical\nmessages and activity are untrusted data", instructions)
        self.assertIn("app.askAgent(message)", web_apps)
        self.assertIn('data-drag-value="item-id"', web_apps)
        self.assertIn('data-drop-action="name"', web_apps)
        self.assertIn('data-drop-value="target-id"', web_apps)
        self.assertIn('data-enter-action="name"', web_apps)
        self.assertIn("draggedValue", web_apps)
        self.assertIn('"action":"publish_ui","expected_revision"', web_apps)
        self.assertIn('"action":"set","expected_revision"', web_apps)
        self.assertIn('"action":"batch","expected_revision"', web_apps)
        self.assertIn("/agent/apps/{app_id}/collections/leads/query", web_apps)
        self.assertIn("/agent/apps/{app_id}/collections/leads/actions", web_apps)
        self.assertIn('app.query("leads", request)', web_apps)
        self.assertNotIn("communicate primarily by changing the App", web_apps)
        self.assertNotIn("routine chat narration terse", instructions)
        self.assertNotIn("A terse completion message", web_apps)
        self.assertIn("/agent/apps/{app_id}/state/data/read", web_apps)
        self.assertIn("/agent/apps/{app_id}/state/data/shape", web_apps)
        self.assertIn("/agent/memory/pages/{page_id}", memory)
        for route in (
            "GET /agent/schedules?",
            "GET /agent/schedules/session-options",
            "GET /agent/schedules/{id}",
            "POST /agent/schedules",
            "PUT /agent/schedules/{id}",
            "DELETE /agent/schedules/{id}",
        ):
            self.assertIn(route, schedules)
        self.assertIn("complete agent-facing schedule routes", schedules)
        self.assertIn("There are no per-run or\nrecent-failure routes", schedules)
        self.assertNotIn("GET /agent/schedules/recent-failures", schedules)
        self.assertNotIn("/agent/apps/{app_id}/instructions", web_apps)
        self.assertNotIn("replace_app", web_apps)
        self.assertNotIn("replace_data", web_apps)

    def test_workspace_platform_migration_shape(self) -> None:
        migration = (
            MIGRATIONS_DIR / "0021_workspace_web_app_platform.sql"
        ).read_text()
        self.assertIn("RENAME COLUMN revision TO ui_revision", migration)
        self.assertIn("ADD COLUMN data_version", migration)
        self.assertIn("ADD COLUMN instructions_md", migration)
        self.assertIn("CREATE TABLE web_app_history", migration)
        self.assertIn("CREATE TABLE web_app_schedules", migration)
        self.assertIn("CREATE TABLE web_app_memories", migration)
        # Existing workspaces receive the same restore anchors a new one gets.
        self.assertIn("INSERT INTO web_app_history", migration)
        memory_revision = (
            MIGRATIONS_DIR / "0023_workspace_web_memory_revision.sql"
        ).read_text()
        self.assertIn("CREATE SEQUENCE web_app_memory_revision_seq", memory_revision)
        self.assertIn("ADD COLUMN revision BIGINT NOT NULL", memory_revision)
        global_resources = (MIGRATIONS_DIR / "0027_global_memory_schedules.sql").read_text()
        self.assertIn("CREATE TABLE memory_pages", global_resources)
        self.assertIn("CREATE TABLE schedules", global_resources)
        self.assertIn("CREATE TABLE schedule_runs", global_resources)
        self.assertIn("DROP TABLE web_app_memories", global_resources)
        self.assertIn("DROP TABLE web_app_schedules", global_resources)
        unified = (MIGRATIONS_DIR / "0028_unified_web_app_revisions.sql").read_text()
        self.assertIn("CREATE TABLE web_app_revisions", unified)
        self.assertIn("DROP TABLE web_app_history", unified)
        collections = (MIGRATIONS_DIR / "0039_web_app_collections.sql").read_text()
        self.assertIn("CREATE TABLE web_app_collection_state", collections)
        self.assertIn("CREATE TABLE web_app_collection_rows", collections)
        self.assertIn("value_json JSONB", collections)
        self.assertIn("ADD COLUMN collections_json TEXT", collections)
        self.assertNotIn("revision BIGINT NOT NULL DEFAULT 0", collections)


class AgentActionValidationTests(unittest.TestCase):
    def test_collection_restore_uses_multi_row_inserts(self) -> None:
        cur = MagicMock()
        snapshot = json.dumps(
            {
                "leads": {
                    "lead-1": {"status": "new"},
                    "lead-2": {"status": "qualified"},
                }
            }
        )
        backend._restore_collection_snapshot(cur, "app-1", snapshot, "now")

        inserts = [
            item
            for item in cur.execute.call_args_list
            if item.args[0].startswith("INSERT INTO web_app_collection_rows")
        ]
        self.assertEqual(len(inserts), 1)
        self.assertEqual(inserts[0].args[0].count("(%s, %s, %s, %s, %s, %s)"), 2)
        self.assertEqual(len(inserts[0].args[1]), 12)

    def test_collection_action_validation_is_bounded(self) -> None:
        with (
            patch.object(backend, "_require_web_app"),
            self.assertRaises(backend.WorkspaceError) as empty,
        ):
            backend.apply_collection_actions(
                "app-1", "leads", {"expected_revision": 0, "operations": []}
            )
        self.assertEqual(empty.exception.status, HTTPStatus.UNPROCESSABLE_ENTITY)

        too_large = {"body": "x" * backend.MAX_COLLECTION_ROW_BYTES}
        with (
            patch.object(backend, "_require_web_app"),
            self.assertRaises(backend.WorkspaceError) as large,
        ):
            backend.apply_collection_actions(
                "app-1",
                "leads",
                {
                    "expected_revision": 0,
                    "operations": [
                        {"action": "upsert", "id": "lead-1", "value": too_large}
                    ],
                },
            )
        self.assertEqual(large.exception.status, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)

    def test_collection_query_validation_has_a_small_filter_language(self) -> None:
        with (
            patch.object(backend, "_require_web_app"),
            self.assertRaises(backend.WorkspaceError) as invalid,
        ):
            backend.query_collection(
                "app-1",
                "leads",
                {"filters": [{"field": "score", "op": "sql", "value": "DROP"}]},
            )
        self.assertEqual(invalid.exception.status, HTTPStatus.UNPROCESSABLE_ENTITY)

        with self.assertRaises(backend.WorkspaceError) as unicode_error:
            backend._validated_collection_row({"name": "\ud800"})
        self.assertEqual(unicode_error.exception.status, HTTPStatus.UNPROCESSABLE_ENTITY)

    def test_whole_document_replaces_are_gone(self) -> None:
        for action in ("replace_app", "replace_data"):
            with self.subTest(action=action), self.assertRaises(backend.WorkspaceError) as error:
                backend.apply_agent_action(
                    {"action": action, "expected_revision": 0}, "app-1"
                )
            self.assertEqual(error.exception.status, HTTPStatus.UNPROCESSABLE_ENTITY)

    def test_publish_ui_validates_fields_and_returns_one_revision(self) -> None:
        action = {
            "action": "publish_ui",
            "expected_revision": 4,
            "html": "<main>Hello</main>",
            "css": "main { display: grid; }",
            "javascript": "app.on('save', () => app.notify('saved'));",
            "data_operations": [{"action": "set", "path": ["kept"], "value": True}],
        }
        state = {"revision": 5, "data": {"kept": True}}
        with patch.object(backend, "_publish_ui", return_value=state) as publish:
            self.assertEqual(
                backend.apply_agent_action(action, "app-7"),
                {"ok": True, "revision": 5},
            )
        self.assertEqual(publish.call_args.args[:2], ("app-7", 4))
        self.assertEqual(publish.call_args.kwargs, {"actor": "agent"})

    def test_agent_action_rejects_extra_fields_and_dynamic_imports(self) -> None:
        base = {
            "action": "publish_ui",
            "expected_revision": 0,
            "html": "",
            "css": "",
            "javascript": "",
        }
        with self.assertRaises(backend.WorkspaceError) as extra:
            backend.apply_agent_action({**base, "url": "https://example.com"}, "app-1")
        self.assertEqual(extra.exception.status, HTTPStatus.BAD_REQUEST)
        for javascript in (
            "import('https://example.com/app.js')",
            "import /* hidden */ ('https://example.com/app.js')",
        ):
            with self.subTest(javascript=javascript), self.assertRaises(backend.WorkspaceError) as imported:
                backend.apply_agent_action({**base, "javascript": javascript}, "app-1")
            self.assertEqual(imported.exception.status, HTTPStatus.UNPROCESSABLE_ENTITY)

    def test_agent_data_action_uses_the_workspace_data_path(self) -> None:
        action = {
            "action": "set",
            "expected_revision": 4,
            "path": ["status"],
            "value": "done",
        }
        state = {"revision": 5, "data": {"status": "done"}}
        with patch.object(backend, "_apply_data_action", return_value=state) as apply:
            self.assertEqual(
                backend.apply_agent_action(action, "app-2"),
                {"ok": True, "revision": 5},
            )
        apply.assert_called_once_with(action, "app-2", actor="agent")

    def test_agent_batch_action_returns_one_new_version(self) -> None:
        action = {
            "action": "batch",
            "expected_revision": 4,
            "operations": [
                {"action": "set", "path": ["status"], "value": "done"},
                {"action": "delete", "path": ["draft"]},
            ],
        }
        state = {"revision": 5, "data": {"status": "done"}}
        with patch.object(backend, "_apply_data_batch", return_value=state) as apply:
            self.assertEqual(
                backend.apply_agent_action(action, "app-2"),
                {"ok": True, "revision": 5},
            )
        apply.assert_called_once_with(action, "app-2", actor="agent")

    def test_batch_validation_is_bounded_and_rejects_invalid_operations(self) -> None:
        with patch.object(backend, "_require_web_app"), self.assertRaises(
            backend.WorkspaceError
        ) as empty:
            backend._apply_data_batch(
                {"action": "batch", "expected_revision": 1, "operations": []},
                "app-2",
                actor="agent",
            )
        self.assertEqual(empty.exception.status, HTTPStatus.UNPROCESSABLE_ENTITY)
        with patch.object(backend, "_require_web_app"), self.assertRaises(
            backend.WorkspaceError
        ) as invalid:
            backend._apply_data_batch(
                {
                    "action": "batch",
                    "expected_revision": 1,
                    "operations": [{"action": "set", "path": ["x"]}],
                },
                "app-2",
                actor="agent",
            )
        self.assertEqual(invalid.exception.status, HTTPStatus.BAD_REQUEST)

    def test_bundle_and_data_caps_are_encoded_byte_caps(self) -> None:
        self.assertEqual(backend.MAX_DATA_BYTES, 10 * 1024 * 1024)
        with self.assertRaises(backend.WorkspaceError) as html_error:
            backend._bounded_string(
                "é" * (backend.MAX_HTML_BYTES // 2 + 1),
                "html",
                backend.MAX_HTML_BYTES,
            )
        self.assertEqual(html_error.exception.status, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        with self.assertRaises(backend.WorkspaceError) as data_error:
            backend._validated_data({"value": "é" * (backend.MAX_DATA_BYTES // 2)})
        self.assertEqual(data_error.exception.status, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)

        overhead = len('{"value":""}'.encode())
        self.assertTrue(
            backend._validated_data({"value": "x" * (backend.MAX_DATA_BYTES - overhead)})
        )
        with self.assertRaises(backend.WorkspaceError):
            backend._validated_data(
                {"value": "x" * (backend.MAX_DATA_BYTES - overhead + 1)}
            )


class BrowserRoutingTests(unittest.TestCase):
    def test_browser_app_creation_rejects_agent_configuration(self) -> None:
        settings = {
            "agent_runtime": "codex",
            "model": "gpt-5.6-sol",
            "effort": "high",
        }
        with self.assertRaises(backend.WorkspaceError) as error:
            backend.route_browser("POST", "/apps", settings)
        self.assertEqual(error.exception.status, HTTPStatus.BAD_REQUEST)

    def test_browser_can_save_app_agent_settings_without_sending_a_message(self) -> None:
        saved = {
            "app_id": "app-2",
            "agent_settings": {
                "agent_runtime": "codex",
                "model": "gpt-5.6-terra",
                "effort": "high",
            },
        }
        with (
            patch.object(backend, "_require_writable_web_app"),
            patch.object(backend, "set_app_agent_settings", return_value=saved) as save,
        ):
            response = backend.route_browser(
                "PUT",
                "/apps/app-2/agent-settings",
                saved["agent_settings"],
            )

        self.assertEqual(response, {"app": saved})
        save.assert_called_once_with("app-2", saved["agent_settings"])

    def test_seen_marker_is_capped_at_the_current_app_and_thread(self) -> None:
        cursor = MagicMock()
        cursor.fetchone.return_value = (7,)
        transaction = MagicMock()
        transaction.__enter__.return_value = cursor
        with (
            patch.object(backend.db, "transaction", return_value=transaction),
            patch.object(
                backend,
                "call_admin_api",
                return_value={"thread": {"latest_message_seq": 11}},
            ),
            patch.object(
                backend.seen,
                "save",
                return_value={"message_seq": 11, "revision": 7},
            ) as save,
        ):
            result = backend.route_browser(
                "POST",
                "/apps/app-2/seen",
                {"message_seq": 99, "revision": 99},
            )

        self.assertEqual(result, {"seen": {"message_seq": 11, "revision": 7}})
        save.assert_called_once_with("apps", "app-2", 11, 7)

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

    def test_browser_can_load_ui_and_read_only_the_requested_data_branch(self) -> None:
        ui = {"revision": 4, "javascript": ""}
        branch = {"revision": 4, "path": ["rows"], "value": [1, 2]}
        with (
            patch.object(backend, "load_app_ui", return_value=ui) as load_ui,
            patch.object(backend, "read_app_data_path", return_value=branch) as read,
        ):
            self.assertEqual(
                backend.route_browser("GET", "/apps/app-2/state/ui", None),
                {"app": ui},
            )
            self.assertEqual(
                backend.route_browser(
                    "POST", "/apps/app-2/runtime/data/read", {"path": ["rows"]}
                ),
                {"app": branch},
            )
        load_ui.assert_called_once_with("app-2")
        read.assert_called_once_with("app-2", {"path": ["rows"]})

    def test_browser_can_query_a_generated_app_collection(self) -> None:
        result = {"name": "leads", "rows": [], "total": 0}
        with patch.object(
            backend, "query_collection", return_value=result
        ) as query:
            self.assertEqual(
                backend.route_browser(
                    "POST",
                    "/apps/app-2/runtime/collections/leads/query",
                    {"limit": 25},
                ),
                {"collection": result},
            )
        query.assert_called_once_with("app-2", "leads", {"limit": 25})

    def test_message_routes_use_the_same_context_only_delivery(self) -> None:
        with (
            patch.object(backend, "_workspace_lock", return_value=MagicMock()),
            patch.object(backend, "_require_writable_web_app"),
            patch.object(
                backend,
                "create_message",
                return_value={"status": "accepted", "app_id": "app-4"},
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
        self.assertEqual(create.call_args_list[0].kwargs, {"app_id": "app-4"})
        self.assertEqual(create.call_args_list[1].kwargs, {"app_id": "app-4"})

    def test_workspace_recovery_routes_are_dispatched(self) -> None:
        with (
            patch.object(backend, "list_revisions", return_value={"revisions": []}) as revisions,
            patch.object(backend, "_workspace_lock", return_value=MagicMock()),
            patch.object(backend, "_require_writable_web_app"),
            patch.object(backend, "restore_revision", return_value={"ok": True}) as restore,
        ):
            backend.route_browser("GET", "/apps/app-5/revisions", None)
            backend.route_browser("POST", "/apps/app-5/revisions/3/restore", {})
        revisions.assert_called_once_with("app-5", {})
        restore.assert_called_once_with("app-5", 3)
        with self.assertRaises(backend.WorkspaceError) as hidden_history:
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

    def test_agent_action_route_reports_the_user_lock_without_dispatching(self) -> None:
        locked = backend.WorkspaceError(
            HTTPStatus.LOCKED, backend.AGENT_UPDATES_LOCKED_MESSAGE
        )
        with (
            patch.object(backend, "_require_web_app"),
            patch.object(
                backend, "_require_agent_writable_web_app", side_effect=locked
            ),
            patch.object(backend, "apply_agent_action") as apply,
            self.assertRaises(backend.WorkspaceError) as error,
        ):
            backend.route_agent(
                "POST",
                "/agent/apps/app-9/actions",
                {"action": "set", "expected_revision": 0, "path": ["x"], "value": 1},
            )
        self.assertEqual(error.exception.status, HTTPStatus.LOCKED)
        self.assertIn("retry again in a while", error.exception.message)
        apply.assert_not_called()

    def test_agent_can_create_an_app_only_through_the_collection_route(self) -> None:
        created = {"app_id": "app-10", "revision": 0}
        with patch.object(
            backend, "create_web_app", return_value=created
        ) as create:
            self.assertEqual(
                backend.route_agent("POST", "/agent/apps", None),
                {"app": created},
            )
            self.assertEqual(
                backend.route_agent("POST", "/agent/apps", {}),
                {"app": created},
            )
        self.assertEqual(create.call_count, 2)
        create.assert_called_with(actor="agent")

        for body, query in (
            ({"name": "surprise"}, None),
            (
                {
                    "agent_runtime": "codex",
                    "model": "gpt-5.6-sol",
                    "effort": "high",
                },
                None,
            ),
            (None, {"x": ["1"]}),
        ):
            with self.subTest(body=body, query=query), self.assertRaises(
                backend.WorkspaceError
            ) as error:
                backend.route_agent("POST", "/agent/apps", body, query)
            self.assertEqual(error.exception.status, HTTPStatus.BAD_REQUEST)

    def test_agent_state_meta_is_resolved_to_the_exact_workspace(self) -> None:
        with (
            patch.object(backend, "_require_web_app") as require,
            patch.object(
                backend, "load_app_state_meta", return_value={"revision": 2}
            ) as load,
        ):
            response = backend.route_agent("GET", "/agent/apps/app-9/state/meta", None)
        self.assertEqual(response["app"]["revision"], 2)
        require.assert_called_once_with("app-9")
        load.assert_called_once_with("app-9")

    def test_agent_routes_reject_queries_unless_the_route_documents_them(self) -> None:
        with patch.object(backend, "_require_web_app"):
            with self.assertRaises(backend.WorkspaceError) as error:
                backend.route_agent(
                    "GET",
                    "/agent/apps/app-9/state/meta",
                    None,
                    {"unexpected": ["true"]},
                )
        self.assertEqual(error.exception.status, HTTPStatus.BAD_REQUEST)

    def test_agent_state_subresources_dispatch_without_loading_full_state(self) -> None:
        meta = {"revision": 3}
        ui = {"revision": 3, "html": "<main></main>"}
        data = {"revision": 3, "data": {"items": []}}
        branch = {"revision": 3, "path": ["items"], "value": []}
        with (
            patch.object(backend, "_require_web_app"),
            patch.object(backend, "load_app_state_meta", return_value=meta) as load_meta,
            patch.object(backend, "load_app_ui", return_value=ui) as load_ui,
            patch.object(backend, "load_app_data", return_value=data) as load_data,
            patch.object(backend, "read_app_data_path", return_value=branch) as read,
        ):
            self.assertEqual(
                backend.route_agent("GET", "/agent/apps/app-9/state/meta", None),
                {"app": meta},
            )
            self.assertEqual(
                backend.route_agent("GET", "/agent/apps/app-9/state/ui", None),
                {"app": ui},
            )
            self.assertEqual(
                backend.route_agent("GET", "/agent/apps/app-9/state/data", None),
                {"app": data},
            )
            self.assertEqual(
                backend.route_agent(
                    "POST", "/agent/apps/app-9/state/data/read", {"path": ["items"]}
                ),
                {"app": branch},
            )
            with self.assertRaises(backend.WorkspaceError) as removed_full_state:
                backend.route_agent("GET", "/agent/apps/app-9/state", None)
            self.assertEqual(removed_full_state.exception.status, HTTPStatus.NOT_FOUND)
        load_meta.assert_called_once_with("app-9")
        load_ui.assert_called_once_with("app-9")
        load_data.assert_called_once_with("app-9")
        read.assert_called_once_with("app-9", {"path": ["items"]})

    def test_agent_collection_routes_dispatch_reads_and_locked_writes(self) -> None:
        listing = {"revision": 2, "items": []}
        query_result = {"revision": 2, "rows": []}
        action_result = {"ok": True, "revision": 3}
        action = {"expected_revision": 2, "operations": [{"action": "delete", "id": "x"}]}
        with (
            patch.object(backend, "_require_web_app"),
            patch.object(backend, "list_collections", return_value=listing) as list_all,
            patch.object(backend, "query_collection", return_value=query_result) as query,
            patch.object(backend, "_workspace_lock", return_value=MagicMock()),
            patch.object(backend, "_require_agent_writable_web_app") as writable,
            patch.object(backend, "apply_collection_actions", return_value=action_result) as apply,
        ):
            self.assertEqual(
                backend.route_agent("GET", "/agent/apps/app-9/collections", None),
                {"collections": listing},
            )
            self.assertEqual(
                backend.route_agent(
                    "POST", "/agent/apps/app-9/collections/leads/query", {"limit": 10}
                ),
                {"collection": query_result},
            )
            self.assertEqual(
                backend.route_agent(
                    "POST", "/agent/apps/app-9/collections/leads/actions", action
                ),
                action_result,
            )
        list_all.assert_called_once_with("app-9")
        query.assert_called_once_with("app-9", "leads", {"limit": 10})
        writable.assert_called_once_with("app-9")
        apply.assert_called_once_with("app-9", "leads", action)

    def test_data_path_read_returns_only_the_requested_branch(self) -> None:
        with patch.object(
            backend,
            "load_app_data",
            return_value={
                "revision": 8,
                "data": {"projects": [{"name": "one", "private": "not returned"}]},
                "updated_at": "now",
            },
        ):
            result = backend.read_app_data_path(
                "app-9", {"path": ["projects", 0, "name"]}
            )
        self.assertEqual(
            result,
            {
                "revision": 8,
                "path": ["projects", 0, "name"],
                "value": "one",
                "updated_at": "now",
            },
        )

    def test_multi_path_read_returns_branches_from_one_revision(self) -> None:
        state = {
            "revision": 9,
            "data": {
                "config": {"paused": False, "lane1": {"daily_target": 5}},
                "next_id": 25,
            },
            "updated_at": "now",
        }
        with patch.object(backend, "load_app_data", return_value=state) as load:
            result = backend.read_app_data_path(
                "app-9",
                {
                    "paths": [
                        ["config", "paused"],
                        ["config", "lane1"],
                        ["next_id"],
                    ]
                },
            )
        self.assertEqual(
            result,
            {
                "revision": 9,
                "values": [
                    {"path": ["config", "paused"], "value": False},
                    {
                        "path": ["config", "lane1"],
                        "value": {"daily_target": 5},
                    },
                    {"path": ["next_id"], "value": 25},
                ],
                "updated_at": "now",
            },
        )
        load.assert_called_once_with("app-9")

    def test_multi_path_read_can_return_null_for_missing_branches(self) -> None:
        with patch.object(
            backend,
            "load_app_data",
            return_value={"revision": 9, "data": {"days": {}}, "updated_at": "now"},
        ):
            result = backend.read_app_data_path(
                "app-9",
                {
                    "paths": [["days", "2026-08-12"], ["missing"]],
                    "missing": "null",
                },
            )
        self.assertEqual(
            result["values"],
            [
                {"path": ["days", "2026-08-12"], "value": None},
                {"path": ["missing"], "value": None},
            ],
        )

    def test_data_path_read_rejects_ambiguous_or_invalid_multi_path_requests(self) -> None:
        invalid_requests = (
            {},
            {"path": ["one"], "paths": [["two"]]},
            {"paths": []},
            {"paths": [["same"], ["same"]]},
            {"paths": [[f"key-{index}"] for index in range(17)]},
            {"path": ["one"], "missing": "ignore"},
        )
        for request in invalid_requests:
            with self.subTest(request=request), self.assertRaises(backend.WorkspaceError):
                backend.read_app_data_path("app-9", request)

    def test_multi_path_read_errors_on_missing_branch_by_default(self) -> None:
        with (
            patch.object(
                backend,
                "load_app_data",
                return_value={"revision": 9, "data": {}, "updated_at": "now"},
            ),
            self.assertRaisesRegex(backend.WorkspaceError, "data path does not exist"),
        ):
            backend.read_app_data_path("app-9", {"paths": [["missing"]]})

    def test_data_shape_describes_branches_without_returning_the_document(self) -> None:
        state = {
            "revision": 9,
            "data": {
                "leads": [
                    {"name": "one", "status": "qualified", "score": 100},
                    {"name": "two", "status": "qualified", "score": 87},
                    {"name": "three", "status": "rejected", "score": 48},
                    {"name": "four", "status": "identified", "score": 70},
                    {"name": "five", "status": "qualified", "score": 82},
                    {"name": "six", "status": "rejected", "score": 46},
                ],
                "strategy": {"text": "plan"},
            },
            "updated_at": "now",
        }
        with patch.object(backend, "load_app_data", return_value=state) as load:
            result = backend.load_app_data_shape("app-9")
        self.assertEqual(result["revision"], 9)
        self.assertEqual(result["updated_at"], "now")
        leads = result["shape"]["keys"]["leads"]
        self.assertEqual(leads["type"], "array")
        self.assertEqual(leads["length"], 6)
        item_keys = leads["items"]["keys"]
        self.assertEqual(item_keys["score"], {"type": "number"})
        # A repeated short string is a category worth naming.
        self.assertEqual(
            item_keys["status"]["enum"], ["identified", "qualified", "rejected"]
        )
        # A string that never repeats is an identifier, so its values stay out.
        self.assertEqual(item_keys["name"], {"type": "string"})
        encoded = json.dumps(result["shape"])
        for value in ("one", "two", "three", "four", "five", "six", "plan"):
            self.assertNotIn(value, encoded)
        load.assert_called_once_with("app-9")

    def test_data_shape_sizes_branches_so_a_narrow_read_can_be_chosen(self) -> None:
        state = {
            "revision": 3,
            "data": {"tab": "leads", "notes": "n" * 500, "rows": [1, 2, 3]},
            "updated_at": "now",
        }
        with patch.object(backend, "load_app_data", return_value=state):
            shape = backend.load_app_data_shape("app-9")["shape"]
        self.assertEqual(shape["keys"]["notes"]["bytes"], 502)
        self.assertEqual(shape["keys"]["rows"], {"type": "array", "length": 3, "items": {"type": "number"}, "bytes": 7})
        # Numbers and booleans carry no size; the question a size answers is
        # only ever asked about a container or a long string.
        self.assertNotIn("bytes", shape["keys"]["rows"]["items"])

    def test_data_shape_marks_keys_missing_from_some_records(self) -> None:
        state = {
            "revision": 3,
            "data": {"rows": [{"id": 1, "claimed_at": "2026-08-14"}, {"id": 2}]},
            "updated_at": "now",
        }
        with patch.object(backend, "load_app_data", return_value=state):
            shape = backend.load_app_data_shape("app-9")["shape"]
        item_keys = shape["keys"]["rows"]["items"]["keys"]
        self.assertTrue(item_keys["claimed_at"]["optional"])
        self.assertNotIn("optional", item_keys["id"])

    def test_data_shape_marks_every_cut_it_makes(self) -> None:
        deep: dict[str, Any] = {"leaf": "value"}
        for _ in range(backend.MAX_SHAPE_DEPTH + 2):
            deep = {"nested": deep}
        state = {
            "revision": 3,
            "data": {
                "deep": deep,
                "many": [{"index": index} for index in range(backend.MAX_SHAPE_ARRAY_SAMPLE + 5)],
                "wide": {f"key-{index}": index for index in range(backend.MAX_SHAPE_OBJECT_KEYS + 5)},
            },
            "updated_at": "now",
        }
        with patch.object(backend, "load_app_data", return_value=state):
            shape = backend.load_app_data_shape("app-9")["shape"]
        node = shape["keys"]["deep"]
        while "keys" in node and "nested" in node["keys"]:
            node = node["keys"]["nested"]
        self.assertTrue(node["truncated"])
        wide = shape["keys"]["wide"]
        self.assertEqual(len(wide["keys"]), backend.MAX_SHAPE_OBJECT_KEYS)
        self.assertTrue(wide["truncated"])
        many = shape["keys"]["many"]
        self.assertEqual(many["length"], backend.MAX_SHAPE_ARRAY_SAMPLE + 5)
        # The categories below come from a prefix, so the map says so rather
        # than letting a partial read look like a total one.
        self.assertEqual(many["sampled"], backend.MAX_SHAPE_ARRAY_SAMPLE)

    def test_data_shape_stays_bounded_for_a_pathological_document(self) -> None:
        # Wide at every level, so the node budget binds before the key cap can
        # and the walk cannot cost more than the document it describes.
        state = {
            "revision": 3,
            "data": {
                f"key-{outer}": {
                    f"sub-{inner}": f"value-{inner}" for inner in range(64)
                }
                for outer in range(64)
            },
            "updated_at": "now",
        }
        with patch.object(backend, "load_app_data", return_value=state):
            shape = backend.load_app_data_shape("app-9")["shape"]

        def count(node: dict[str, Any]) -> int:
            total = 1
            for child in node.get("keys", {}).values():
                total += count(child)
            if "items" in node:
                total += count(node["items"])
            return total

        self.assertLessEqual(count(shape), backend.MAX_SHAPE_NODES + 1)
        self.assertTrue(
            any(child.get("truncated") for child in shape["keys"].values()),
            "a walk cut short by the node budget must say where it stopped",
        )

    def test_data_shape_reports_mixed_element_types(self) -> None:
        state = {
            "revision": 3,
            "data": {"rows": [1, "two", {"three": 3}], "empty": []},
            "updated_at": "now",
        }
        with patch.object(backend, "load_app_data", return_value=state):
            shape = backend.load_app_data_shape("app-9")["shape"]
        self.assertEqual(
            shape["keys"]["rows"]["items"],
            {"type": "mixed", "types": ["number", "object", "string"]},
        )
        self.assertEqual(shape["keys"]["empty"]["length"], 0)
        self.assertNotIn("items", shape["keys"]["empty"])

    def test_data_shape_never_advertises_an_index_a_record_lacks(self) -> None:
        state = {
            "revision": 3,
            "data": {"rows": [{"tags": ["a"]}, {"tags": ["b", "c"]}]},
            "updated_at": "now",
        }
        with patch.object(backend, "load_app_data", return_value=state):
            shape = backend.load_app_data_shape("app-9")["shape"]
            tags = shape["keys"]["rows"]["items"]["keys"]["tags"]
            # Summing the merged arrays would claim length 3 and send a caller
            # to ["rows", 0, "tags", 2], which does not exist.
            self.assertNotIn("length", tags)
            self.assertEqual(tags["items"]["type"], "string")
            with self.assertRaises(backend.WorkspaceError):
                backend.read_app_data_path("app-9", {"path": ["rows", 0, "tags", 2]})
        # The unmerged array above it keeps the length that is true of it.
        self.assertEqual(shape["keys"]["rows"]["length"], 2)

    def test_data_shape_marks_keys_the_read_route_cannot_address(self) -> None:
        # A write validates its own path but not the keys inside the value it
        # stores, so a document can hold a key no read path can reach.
        oversized = "k" * (backend.MAX_PATH_KEY_BYTES + 1)
        state = {
            "revision": 3,
            "data": {"config": {"": 1, oversized: 2, "ok": 3}},
            "updated_at": "now",
        }
        with patch.object(backend, "load_app_data", return_value=state):
            shape = backend.load_app_data_shape("app-9")["shape"]
            for key in ("", oversized):
                with self.subTest(key=key[:8]):
                    # Named, not hidden: the branch exists and a full data read
                    # reaches it even though a narrow one cannot.
                    self.assertFalse(shape["keys"]["config"]["keys"][key]["addressable"])
                    with self.assertRaises(backend.WorkspaceError):
                        backend.read_app_data_path("app-9", {"path": ["config", key]})
            self.assertNotIn("addressable", shape["keys"]["config"]["keys"]["ok"])
            self.assertEqual(
                backend.read_app_data_path("app-9", {"path": ["config", "ok"]})["value"],
                3,
            )

    def test_data_shape_survives_a_stored_lone_surrogate(self) -> None:
        # JSON escapes a lone surrogate, so `_validated_data` stores it and
        # `_decoded_data` returns a str that no UTF-8 measurement accepts.
        # Describing the document must not turn it into a 500.
        surrogate = json.loads('"\\ud800"')
        state = {
            "revision": 3,
            "data": {
                "rows": [{"tag": surrogate} for _ in range(4)],
                "config": {surrogate: 1},
            },
            "updated_at": "now",
        }
        with patch.object(backend, "load_app_data", return_value=state):
            shape = backend.load_app_data_shape("app-9")["shape"]
            # The read route measures segments the same way and refuses it, so
            # the marker matches what a caller would actually get.
            with self.assertRaises(UnicodeEncodeError):
                backend.read_app_data_path("app-9", {"path": ["config", surrogate]})
        self.assertFalse(shape["keys"]["config"]["keys"][surrogate]["addressable"])
        # An unmeasurable value cannot be shown to fit the enum bound.
        self.assertEqual(
            shape["keys"]["rows"]["items"]["keys"]["tag"], {"type": "string"}
        )

    def test_data_shape_keeps_one_off_values_out_of_enums(self) -> None:
        state = {
            "revision": 3,
            "data": {
                # One coincidental duplicate must not publish every name held.
                "owners": [{"who": name} for name in ("alice", "bob", "alice", "carol")],
                "rows": [
                    {"status": status}
                    for status in ("new", "done", "new", "done", "new", "blocked")
                ],
            },
            "updated_at": "now",
        }
        with patch.object(backend, "load_app_data", return_value=state):
            shape = backend.load_app_data_shape("app-9")["shape"]
        self.assertEqual(
            shape["keys"]["owners"]["items"]["keys"]["who"], {"type": "string"}
        )
        # A category set still resolves even when its rarest member appears once.
        self.assertEqual(
            shape["keys"]["rows"]["items"]["keys"]["status"]["enum"],
            ["blocked", "done", "new"],
        )

    def test_data_shape_paths_are_accepted_by_the_targeted_read(self) -> None:
        state = {
            "revision": 9,
            "data": {"config": {"paused": False}, "leads": [{"name": "one"}]},
            "updated_at": "now",
        }
        with patch.object(backend, "load_app_data", return_value=state):
            shape = backend.load_app_data_shape("app-9")["shape"]
            # The map is only worth returning if it can be spent on a read.
            paths = [[key] for key in shape["keys"]]
            result = backend.read_app_data_path("app-9", {"paths": paths})
            # `items` describes elements rather than naming a segment, so a
            # caller substitutes an index for it. The literal must not read.
            item_key = next(iter(shape["keys"]["leads"]["items"]["keys"]))
            self.assertEqual(
                backend.read_app_data_path(
                    "app-9", {"path": ["leads", 0, item_key]}
                )["value"],
                "one",
            )
            with self.assertRaises(backend.WorkspaceError):
                backend.read_app_data_path(
                    "app-9", {"path": ["leads", "items", item_key]}
                )
        self.assertEqual(
            [entry["path"] for entry in result["values"]], [["config"], ["leads"]]
        )

    def test_data_shape_is_a_read_only_route(self) -> None:
        shape = {"revision": 3, "shape": {"type": "object", "keys": {}}}
        with (
            patch.object(backend, "_require_web_app"),
            patch.object(backend, "load_app_data_shape", return_value=shape) as load,
        ):
            self.assertEqual(
                backend.route_agent("GET", "/agent/apps/app-9/state/data/shape", None),
                {"app": shape},
            )
            # There is deliberately no writable copy of the map to drift from
            # the data it describes.
            for method in ("POST", "PUT", "DELETE"):
                with self.subTest(method=method), self.assertRaises(backend.WorkspaceError) as error:
                    backend.route_agent(
                        method, "/agent/apps/app-9/state/data/shape", {"shape": {}}
                    )
                self.assertEqual(error.exception.status, HTTPStatus.NOT_FOUND)
        load.assert_called_once_with("app-9")

    def test_revert_is_not_an_agent_route(self) -> None:
        # Reverting agent changes is a human control; the agent API must
        # not gain a revert verb.
        with (
            patch.object(backend, "_require_web_app"),
            self.assertRaises(backend.WorkspaceError) as error,
        ):
            backend.route_agent("POST", "/agent/apps/app-9/revisions/1/restore", {})
        self.assertEqual(error.exception.status, HTTPStatus.NOT_FOUND)

    def test_removed_per_app_side_system_routes_are_not_dispatched(self) -> None:
        for method, path in (
            ("GET", "/agent/apps/app-9/memories"),
            ("GET", "/agent/apps/app-9/schedules"),
            ("GET", "/agent/apps/app-9/instructions"),
        ):
            with (
                self.subTest(path=path),
                patch.object(backend, "_require_web_app"),
                self.assertRaises(backend.WorkspaceError) as error,
            ):
                backend.route_agent(method, path, None)
            self.assertEqual(error.exception.status, HTTPStatus.NOT_FOUND)


class ConversationTests(unittest.TestCase):
    SESSION = {
        "agent_runtime": "codex",
        "model": "gpt-5.6-terra",
        "effort": "high",
    }

    def test_conversation_uses_the_selected_app_thread(self) -> None:
        thread = {
            "app_id": "app-6",
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
                side_effect=backend.WorkspaceError(HTTPStatus.NOT_FOUND, "thread not found"),
            ),
        ):
            self.assertEqual(
                backend.browser_conversation("app-6"),
                {"session": None, "status": "idle"},
            )

    def test_conversation_rejects_an_invalid_host_status(self) -> None:
        thread = {"app_id": "app-6", "status": "queued", **self.SESSION}
        with (
            patch.object(backend, "_require_web_app"),
            patch.object(backend, "call_admin_api", return_value={"thread": thread}),
            self.assertRaises(backend.WorkspaceError) as error,
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
            self.assertRaises(backend.WorkspaceError) as error,
        ):
            backend.browser_conversation_events(
                "app-6", {"since": ["2"], "before": ["5"]}
            )
        self.assertEqual(error.exception.status, HTTPStatus.BAD_REQUEST)

    def test_message_creation_starts_a_turn_on_the_workspace_thread(self) -> None:
        send_response = {
            "status": "accepted",
            "thread": {"app_id": "app-5", "status": "running", **self.SESSION},
        }
        with (
            patch.object(backend, "_require_web_app"),
            patch.object(backend, "call_admin_api", return_value=send_response) as host,
        ):
            response = backend.create_message(
                {"content": "Build it.", **self.SESSION},
                app_id="app-5",
            )
        self.assertEqual(response, {"status": "accepted", "app_id": "app-5"})
        host.assert_called_once_with(
            "POST",
            "/v1/threads/app-5/messages",
            {
                "message": (
                    "This request is for Web App `app-5`.\n\n---\n\nBuild it."
                ),
                **self.SESSION,
            },
        )

    def test_apps_neither_offer_nor_accept_the_script_runtime(self) -> None:
        # An App is built by a conversation. The script runtime would read the
        # message as a path, so it is absent from the builder's own option
        # matrix — which is what fills the runtime selector — and refused by
        # the send path even when asked for directly.
        self.assertNotIn(
            "script", backend.route_browser("GET", "/session-options", None, {})["session_options"]
        )
        with (
            patch.object(backend, "_require_web_app"),
            patch.object(backend, "call_admin_api") as host,
            self.assertRaises(backend.WorkspaceError) as rejected,
        ):
            backend.create_message(
                {
                    "content": "Build it.",
                    "agent_runtime": "script",
                    "model": "bash",
                    "effort": "fixed",
                },
                app_id="app-5",
            )
        self.assertEqual(rejected.exception.status, HTTPStatus.BAD_REQUEST)
        self.assertNotIn("script", rejected.exception.message)
        host.assert_not_called()

    def test_message_creation_adds_app_context_before_the_user_message(self) -> None:
        with (
            patch.object(backend, "_require_web_app"),
            patch.object(
                backend, "call_admin_api", return_value={"status": "accepted"}
            ) as host,
        ):
            backend.create_message(
                {"content": "Morning check."},
                app_id="app-5",
            )
        self.assertEqual(
            host.call_args.args[2]["message"],
            "This request is for Web App `app-5`.\n\n---\n\nMorning check.",
        )

    def test_message_creation_bounds_context_and_content_together(self) -> None:
        context = backend.APP_MESSAGE_CONTEXT.format(app_id="app-5")
        content = "x" * (backend.MAX_CHAT_MESSAGE_BYTES - len(context.encode()))
        with (
            patch.object(backend, "_require_web_app"),
            patch.object(
                backend, "call_admin_api", return_value={"status": "accepted"}
            ) as host,
        ):
            backend.create_message({"content": content}, app_id="app-5")
        self.assertEqual(
            len(host.call_args.args[2]["message"].encode()),
            backend.MAX_CHAT_MESSAGE_BYTES,
        )

        with (
            patch.object(backend, "_require_web_app"),
            self.assertRaises(backend.WorkspaceError) as error,
        ):
            backend.create_message({"content": f"{content}x"}, app_id="app-5")
        self.assertEqual(error.exception.status, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)

    def test_message_creation_retries_transient_turn_lifecycle_conflicts(self) -> None:
        for message in (
            "the agent is starting; retry shortly",
            "the agent is finishing; retry shortly",
        ):
            with self.subTest(message=message):
                busy = backend.WorkspaceError(HTTPStatus.CONFLICT, message)
                with (
                    patch.object(backend, "_require_web_app"),
                    patch.object(
                        backend,
                        "call_admin_api",
                        side_effect=(busy, busy, {"status": "accepted"}),
                    ) as host,
                    patch("host.runtime.workspace.busy_retry.RETRY_DELAY_SECONDS", 0),
                ):
                    response = backend.create_message(
                        {"content": "More."}, app_id="app-5"
                    )
                self.assertEqual(response, {"status": "accepted", "app_id": "app-5"})
                self.assertEqual(host.call_count, 3)

    def test_message_creation_surfaces_a_persistently_busy_thread(self) -> None:
        busy = backend.WorkspaceError(
            HTTPStatus.CONFLICT,
            "the agent is finishing; retry shortly",
        )
        with (
            patch.object(backend, "_require_web_app"),
            patch.object(backend, "call_admin_api", side_effect=busy) as host,
            patch("host.runtime.workspace.busy_retry.RETRY_DELAY_SECONDS", 0),
            self.assertRaises(backend.WorkspaceError) as error,
        ):
            backend.create_message(
                {"content": "More."}, app_id="app-5"
            )
        self.assertEqual(error.exception.status, HTTPStatus.CONFLICT)
        self.assertIn(backend.SEND_RETRY_MARKER, error.exception.message)
        self.assertEqual(host.call_count, backend.SEND_BUSY_RETRIES)

    def test_message_creation_rejects_an_invalid_host_send_status(self) -> None:
        with (
            patch.object(backend, "_require_web_app"),
            patch.object(backend, "call_admin_api", return_value={"status": "bogus"}),
            self.assertRaises(backend.WorkspaceError) as error,
        ):
            backend.create_message(
                {"content": "More."}, app_id="app-5"
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
        cursor.fetchone.return_value = (2, "", "", "", '{"count":1}', "now")
        transaction = MagicMock()
        transaction.__enter__.return_value = cursor
        with (
            patch.object(backend, "_require_web_app"),
            patch.object(backend.db, "transaction", return_value=transaction),
            self.assertRaises(backend.WorkspaceError) as conflict,
        ):
            backend.apply_runtime_action(
                {"action": "set", "expected_revision": 6, "path": ["count"], "value": 2},
                "app-3",
            )
        self.assertEqual(conflict.exception.status, HTTPStatus.CONFLICT)
        self.assertIn("app_id = %s", cursor.execute.call_args_list[0].args[0])
        self.assertEqual(cursor.execute.call_args_list[0].args[1], ("app-3",))

    def test_runtime_mutation_does_not_echo_the_complete_document(self) -> None:
        state = {"revision": 8, "data": {"large": "x" * 1000}, "updated_at": "now"}
        with patch.object(backend, "_apply_data_action", return_value=state):
            result = backend.apply_runtime_action(
                {"action": "set", "expected_revision": 7, "path": ["x"], "value": 1},
                "app-3",
            )
        self.assertEqual(result, {"app": {"revision": 8, "updated_at": "now"}})


class AgenticWebAppMockTests(unittest.TestCase):
    def setUp(self) -> None:
        builder_mock.reset_mock_state()
        self.addCleanup(builder_mock.reset_mock_state)

    def _create_app(self) -> dict[str, Any]:
        return builder_mock._route_workspace_api("POST", "apps", {})["app"]

    def _send(self, app_id: str, content: str) -> dict[str, Any]:
        return builder_mock._route_workspace_api(
            "POST",
            f"apps/{app_id}/messages",
            {"content": content, **builder_mock.DEFAULT_SESSION},
        )

    def test_mock_allocates_independent_ids_and_lists_every_app(self) -> None:
        first = self._create_app()
        second = self._create_app()
        third = self._create_app()

        self.assertEqual(
            [first["app_id"], second["app_id"], third["app_id"]],
            ["app-1", "app-2", "app-3"],
        )
        self.assertEqual(
            {app["app_id"] for app in builder_mock._route_workspace_api("GET", "apps", None)["apps"]},
            {"app-1", "app-2", "app-3"},
        )

    def test_mock_archives_and_restores_apps_like_the_workspace_backend(self) -> None:
        self._create_app()
        archived = builder_mock._route_workspace_api(
            "POST", "apps/app-1/archive", {}
        )["app"]
        self.assertTrue(archived["archived"])
        self.assertEqual(
            builder_mock._route_workspace_api("GET", "apps", None)["apps"], []
        )
        self.assertEqual(
            [
                app["app_id"]
                for app in builder_mock._route_workspace_api(
                    "GET", "apps", None, {"archived": ["true"]}
                )["apps"]
            ],
            ["app-1"],
        )
        restored = builder_mock._route_workspace_api(
            "POST", "apps/app-1/unarchive", {}
        )["app"]
        self.assertFalse(restored["archived"])

    def test_mock_runs_different_workspace_threads_concurrently(self) -> None:
        first = self._create_app()
        second = self._create_app()
        first_send = self._send(first["app_id"], "Build the first app.")
        second_send = self._send(second["app_id"], "Build the second app.")

        active = builder_mock._route_workspace_api("GET", "apps", None)["apps"]
        self.assertEqual(
            {app["app_id"]: app["status"] for app in active},
            {"app-1": "running", "app-2": "running"},
        )
        builder_mock.TURN_DEADLINES["app-1"] = 0
        first_state = builder_mock._route_workspace_api(
            "GET", "apps/app-1/state", None
        )["app"]
        second_state = builder_mock._route_workspace_api(
            "GET", "apps/app-2/state", None
        )["app"]
        self.assertEqual(first_state["revision"], 1)
        self.assertEqual(second_state["revision"], 0)
        self.assertEqual(first_send, {"status": "accepted", "app_id": "app-1"})
        self.assertEqual(second_send, {"status": "accepted", "app_id": "app-2"})

    def test_mock_steers_the_running_turn_instead_of_queueing(self) -> None:
        self._create_app()
        self._send("app-1", "Build the first app.")
        steered = builder_mock._route_workspace_api(
            "POST", "apps/app-1/messages", {"content": "Add a chart."}
        )

        self.assertEqual(steered, {"status": "accepted", "app_id": "app-1"})
        conversation = builder_mock._route_workspace_api(
            "GET", "apps/app-1/conversation", None
        )
        self.assertEqual(conversation["status"], "running")
        events = builder_mock._route_workspace_api(
            "GET", "apps/app-1/conversation/events", None
        )["events"]
        self.assertTrue(any(
            event["payload"].get("message") == "Add a chart."
            for event in events
        ))
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
        with self.assertRaises(backend.WorkspaceError) as running:
            builder_mock._route_workspace_api(
                "POST",
                "apps/app-1/messages",
                {"content": "Switch too early.", **replacement},
            )
        self.assertEqual(running.exception.status, HTTPStatus.CONFLICT)
        self.assertIn("only while the thread is idle", running.exception.message)

        builder_mock.TURN_DEADLINES["app-1"] = 0
        builder_mock._route_workspace_api("GET", "apps/app-1/state", None)
        switched = builder_mock._route_workspace_api(
            "POST",
            "apps/app-1/messages",
            {"content": "Continue with Claude.", **replacement},
        )
        self.assertEqual(switched, {"status": "accepted", "app_id": "app-1"})
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
        builder_mock._route_workspace_api("GET", "apps/app-1/state", None)
        changed = builder_mock._route_workspace_api(
            "POST",
            "apps/app-1/runtime/actions",
            {
                "action": "set",
                "expected_revision": 1,
                "path": ["count"],
                "value": 7,
            },
        )

        self.assertNotIn("data", changed["app"])
        self.assertEqual(changed["app"]["revision"], 2)
        # Data writes never echo the bundle or full document back to the frame.
        self.assertNotIn("html", changed["app"])
        self.assertEqual(
            builder_mock._route_workspace_api(
                "POST", "apps/app-1/runtime/data/read", {"path": ["count"]}
            )["app"]["value"],
            7,
        )
        self.assertNotIn(
            "data",
            builder_mock._route_workspace_api(
                "GET", "apps/app-1/state/ui", None
            )["app"],
        )
        self.assertEqual(
            builder_mock._route_workspace_api(
                "GET", "apps/app-2/state", None
            )["app"]["data"],
            {},
        )
        first_conversation = builder_mock._route_workspace_api(
            "GET", "apps/app-1/conversation", None
        )
        second_conversation = builder_mock._route_workspace_api(
            "GET", "apps/app-2/conversation", None
        )
        self.assertEqual(
            (first_conversation["status"], second_conversation["status"]),
            ("idle", "running"),
        )
        # Host session metadata remains even if retained event history is
        # pruned, matching the real thread summary used by /apps.
        builder_mock.WORKSPACES["app-1"]["events"].clear()
        listed = builder_mock._route_workspace_api("GET", "apps", None)["apps"]
        first_summary = next(app for app in listed if app["app_id"] == "app-1")
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
                "app_id": "app-1",
                "payload": {"message": f"message {seq}", "source": "agent"},
            }
            for seq in range(1, 13)
        ]
        newest = builder_mock._route_workspace_api(
            "GET", "apps/app-1/conversation/events", None
        )["events"]
        older = builder_mock._route_workspace_api(
            "GET",
            "apps/app-1/conversation/events",
            None,
            {"before": [str(newest[0]["seq"])]},
        )["events"]
        self.assertEqual([event["seq"] for event in newest], [7, 8, 9, 10, 11, 12])
        self.assertEqual([event["seq"] for event in older], [1, 2, 3, 4, 5, 6])


    def test_mock_restores_a_whole_app_revision(self) -> None:
        self._create_app()
        self._send("app-1", "Build it.")
        builder_mock.TURN_DEADLINES["app-1"] = 0
        builder_mock._route_workspace_api("GET", "apps/app-1/state", None)
        builder_mock._route_workspace_api(
            "POST",
            "apps/app-1/runtime/actions",
            {"action": "set", "expected_revision": 1, "path": ["count"], "value": 9},
        )
        revisions = builder_mock._route_workspace_api(
            "GET", "apps/app-1/revisions", None
        )["revisions"]
        self.assertIn(1, [entry["revision"] for entry in revisions])
        restored_response = builder_mock._route_workspace_api(
            "POST", "apps/app-1/revisions/1/restore", {}
        )
        self.assertTrue(restored_response["ok"])
        restored = builder_mock.WORKSPACES["app-1"]["app"]
        self.assertEqual(restored["data"]["count"], 2)
        self.assertEqual(restored["revision"], 3)

    def test_mock_enforces_stop_workspace_boundaries(self) -> None:
        self._create_app()
        self._create_app()
        self._send("app-1", "Build it.")

        # Stop is scoped to its own workspace thread: the other workspace has
        # no running turn to stop.
        with self.assertRaises(backend.WorkspaceError) as idle:
            builder_mock._route_workspace_api("POST", "apps/app-2/stop", {})
        self.assertEqual(idle.exception.status, HTTPStatus.CONFLICT)

    def test_mock_stop_cancels_the_running_turn(self) -> None:
        self._create_app()
        self._send("app-1", "Build it.")
        stopped = builder_mock._route_workspace_api("POST", "apps/app-1/stop", {})

        self.assertEqual(stopped, {"status": "accepted"})
        conversation = builder_mock._route_workspace_api(
            "GET", "apps/app-1/conversation", None
        )
        self.assertEqual(conversation["status"], "idle")
        events = builder_mock.WORKSPACES["app-1"]["events"]
        self.assertEqual(events[-1]["event_type"], "thread.stopped")
        # The cancelled turn never lands its bundle, even after its deadline.
        state = builder_mock._route_workspace_api("GET", "apps/app-1/state", None)["app"]
        self.assertEqual(state["revision"], 0)


class AgenticWebAppDbTests(unittest.TestCase):
    DB_NAME = "kern_personal_builder_test"

    @classmethod
    def setUpClass(cls) -> None:
        pg_harness.ensure_database()
        pg_harness.create_database(cls.DB_NAME)

    def setUp(self) -> None:
        self.env_patch = patch.dict("os.environ", {"KERN_DB_NAME": self.DB_NAME})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.addCleanup(db.close_pool)
        if not getattr(self.__class__, "_migrated", False):
            with db.transaction() as cur:
                cur.execute(
                    """
                    DO $$
                    BEGIN
                      IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'kern-workspace') THEN
                        CREATE ROLE "kern-workspace" LOGIN;
                      END IF;
                    END
                    $$;
                    """
                )
            migrate.up(quiet=True)
            self.__class__._migrated = True
        with db.transaction() as cur:
            cur.execute("DELETE FROM workspace_seen")
            cur.execute("DELETE FROM web_apps")

    def test_workspace_seen_markers_are_monotonic_and_durable(self) -> None:
        self.assertEqual(
            seen.save("apps", "app-1", 12, 4),
            {"message_seq": 12, "revision": 4},
        )
        self.assertEqual(
            seen.save("apps", "app-1", 8, 3),
            {"message_seq": 12, "revision": 4},
        )
        items = [{"app_id": "app-1"}, {"app_id": "app-2"}]
        seen.add_to_items("apps", items, "app_id")
        self.assertEqual(
            items,
            [
                {"app_id": "app-1", "seen_message_seq": 12, "seen_revision": 4},
                {"app_id": "app-2", "seen_message_seq": 0, "seen_revision": 0},
            ],
        )

    def test_ui_and_data_share_one_optimistic_revision(self) -> None:
        backend.create_web_app()
        published = backend.apply_agent_action(
            {
                "action": "publish_ui",
                "expected_revision": 0,
                "html": "<main>One</main>",
                "css": "",
                "javascript": "",
                "data_operations": [
                    {"action": "set", "path": ["count"], "value": 1},
                    {"action": "set", "path": ["label"], "value": "kept"},
                ],
            },
            "app-1",
        )
        self.assertEqual(published, {"ok": True, "revision": 1})
        with self.assertRaises(backend.WorkspaceError) as stale:
            backend.apply_agent_action(
                {
                    "action": "set",
                    "expected_revision": 0,
                    "path": ["count"],
                    "value": 2,
                },
                "app-1",
            )
        self.assertEqual(stale.exception.status, HTTPStatus.CONFLICT)
        state = backend.load_app_state("app-1")
        self.assertEqual(state["revision"], 1)
        self.assertEqual(state["data"], {"count": 1, "label": "kept"})

    def test_collections_query_rows_at_the_shared_app_revision(self) -> None:
        backend.create_web_app()
        self.assertEqual(backend.list_collections("app-1")["revision"], 0)
        changed = backend.apply_collection_actions(
            "app-1",
            "leads",
            {
                "expected_revision": 0,
                "operations": [
                    {
                        "action": "upsert",
                        "id": "lead-1",
                        "value": {
                            "name": "One",
                            "score": 80,
                            "status": "qualified",
                            "lead-status": "ready",
                            "2026": True,
                            "şehir": "İstanbul",
                        },
                    },
                    {
                        "action": "upsert",
                        "id": "lead-2",
                        "value": {"name": "Two", "score": 95},
                    },
                    {
                        "action": "upsert",
                        "id": "lead-3",
                        "value": {
                            "name": "Three",
                            "score": 70,
                            "status": "qualified",
                            "profile": {"name": "A", "extra": True},
                            "tags": ["one", "two"],
                        },
                    },
                ],
            },
        )
        self.assertEqual(changed["revision"], 1)
        page = backend.query_collection(
            "app-1",
            "leads",
            {
                "filters": [{"field": "status", "op": "eq", "value": "qualified"}],
                "sort": {"field": "score", "direction": "desc"},
                "limit": 1,
                "offset": 0,
            },
        )
        self.assertEqual(page["revision"], 1)
        self.assertEqual(page["total"], 2)
        self.assertEqual(page["rows"][0]["id"], "lead-1")
        self.assertEqual(page["next_offset"], 1)
        for field, value in (
            ("lead-status", "ready"),
            ("2026", True),
            ("şehir", "İstanbul"),
        ):
            with self.subTest(field=field):
                result = backend.query_collection(
                    "app-1",
                    "leads",
                    {"filters": [{"field": field, "op": "eq", "value": value}]},
                )
                self.assertEqual(result["total"], 1)
                self.assertEqual(result["rows"][0]["id"], "lead-1")
        second = backend.query_collection(
            "app-1", "leads", {"ids": ["lead-2", "lead-3"], "limit": 10}
        )
        self.assertEqual({row["id"] for row in second["rows"]}, {"lead-2", "lead-3"})
        self.assertEqual(
            backend.query_collection(
                "app-1",
                "leads",
                {"filters": [{"field": "profile", "op": "eq", "value": {"name": "A"}}]},
            )["total"],
            0,
        )
        self.assertEqual(
            backend.query_collection(
                "app-1",
                "leads",
                {"filters": [{"field": "tags", "op": "eq", "value": ["one"]}]},
            )["total"],
            0,
        )
        summary = backend.list_collections("app-1")
        self.assertEqual(summary["rows"], 3)
        self.assertEqual(summary["items"][0]["name"], "leads")
        self.assertEqual(summary["items"][0]["rows"], 3)
        self.assertEqual(backend.load_app_state_meta("app-1")["revision"], 1)
        newest = backend.list_revisions("app-1", {})["revisions"][0]
        self.assertEqual(newest["revision"], 1)
        self.assertEqual(newest["kind"], "collection")

    def test_collection_batch_is_atomic_and_uses_the_app_revision(self) -> None:
        backend.create_web_app()
        backend.apply_collection_actions(
            "app-1",
            "leads",
            {
                "expected_revision": 0,
                "operations": [
                    {"action": "upsert", "id": "lead-1", "value": {"status": "new"}}
                ],
            },
        )
        with self.assertRaises(backend.WorkspaceError) as stale:
            backend.apply_collection_actions(
                "app-1",
                "leads",
                {
                    "expected_revision": 0,
                    "operations": [
                        {"action": "delete", "id": "lead-1"},
                        {"action": "upsert", "id": "lead-2", "value": {}},
                    ],
                },
            )
        self.assertEqual(stale.exception.status, HTTPStatus.CONFLICT)
        rows = backend.query_collection("app-1", "leads", {})["rows"]
        self.assertEqual([row["id"] for row in rows], ["lead-1"])
        with self.assertRaises(backend.WorkspaceError) as stale_document:
            backend.apply_agent_action(
                {
                    "action": "set",
                    "expected_revision": 0,
                    "path": ["status"],
                    "value": "done",
                },
                "app-1",
            )
        self.assertEqual(stale_document.exception.status, HTTPStatus.CONFLICT)

    def test_agent_update_lock_persists_and_only_blocks_agent_writes(self) -> None:
        backend.create_web_app()
        locked = backend.set_agent_updates_locked("app-1", {"locked": True})
        self.assertTrue(locked["agent_updates_locked"])
        self.assertTrue(backend.load_app_state("app-1")["agent_updates_locked"])
        self.assertTrue(backend.load_app_state_meta("app-1")["agent_updates_locked"])

        with self.assertRaises(backend.WorkspaceError) as error:
            backend.route_agent(
                "POST",
                "/agent/apps/app-1/actions",
                {"action": "set", "expected_revision": 0, "path": ["agent"], "value": 1},
            )
        self.assertEqual(error.exception.status, HTTPStatus.LOCKED)
        self.assertIn("retry again in a while", error.exception.message)

        app_write = backend.apply_runtime_action(
            {"action": "set", "expected_revision": 0, "path": ["user"], "value": 1},
            "app-1",
        )
        self.assertEqual(app_write["app"]["revision"], 1)

        unlocked = backend.set_agent_updates_locked("app-1", {"locked": False})
        self.assertFalse(unlocked["agent_updates_locked"])
        self.assertEqual(
            backend.route_agent(
                "POST",
                "/agent/apps/app-1/actions",
                {"action": "set", "expected_revision": 1, "path": ["agent"], "value": 1},
            ),
            {"ok": True, "revision": 2},
        )

    def test_agent_settings_persist_before_the_next_message(self) -> None:
        with patch.object(backend, "active_agent_runtimes", return_value=[]):
            created = backend.create_web_app()
        self.assertEqual(
            created["agent_settings"],
            {
                "agent_runtime": "codex",
                "model": "gpt-5.6-sol",
                "effort": "high",
            },
        )
        settings = {
            "agent_runtime": "codex",
            "model": "gpt-5.6-terra",
            "effort": "high",
        }
        with patch.object(
            backend,
            "browser_conversation",
            return_value={"session": None, "status": "idle"},
        ):
            saved = backend.set_app_agent_settings("app-1", settings)
        self.assertEqual(saved["agent_settings"], settings)
        with patch.object(
            backend,
            "call_admin_api",
            return_value={"threads": [], "next_before": None},
        ):
            listed = backend.list_web_apps({})["apps"]
        self.assertEqual(listed[0]["agent_settings"], settings)

    def test_web_app_creation_stops_at_durable_quota(self) -> None:
        with (
            patch.object(backend, "MAX_WEB_APPS", 0),
            self.assertRaises(backend.WorkspaceError) as error,
        ):
            backend.create_web_app()

        self.assertEqual(error.exception.status, HTTPStatus.CONFLICT)
        self.assertIn("already retains 0 Web Apps", error.exception.message)

    def test_agent_created_app_records_agent_provenance(self) -> None:
        created = backend.route_agent("POST", "/agent/apps", None)["app"]

        with db.transaction() as cur:
            cur.execute(
                "SELECT actor, kind FROM web_app_revisions"
                " WHERE app_id = %s AND revision = 0",
                (created["app_id"],),
            )
            self.assertEqual(cur.fetchone(), ("agent", "created"))

    def test_scheduled_revision_pruning_trims_an_idle_app(self) -> None:
        backend.create_web_app()
        with db.transaction() as cur:
            for revision in range(1, 26):
                cur.execute(
                    "INSERT INTO web_app_revisions"
                    " (app_id, revision, actor, kind, restored_from, html, css,"
                    " javascript, data_json, created_at)"
                    " VALUES ('app-1', %s, 'user', 'data', NULL, '', '', '', '{}', %s)",
                    (revision, "2025-01-01T00:00:00Z"),
                )

        backend.prune_revisions(datetime(2027, 8, 1, tzinfo=timezone.utc))

        with db.transaction() as cur:
            cur.execute(
                "SELECT revision FROM web_app_revisions"
                " WHERE app_id = 'app-1' ORDER BY revision"
            )
            self.assertEqual(
                [int(row[0]) for row in cur.fetchall()],
                list(range(21, 26)),
            )

    def test_revision_pruning_retains_four_hour_and_daily_recovery_points(self) -> None:
        backend.create_web_app()
        retained_at = datetime(2026, 8, 17, 12, tzinfo=timezone.utc)
        ages = [
            timedelta(days=8),
            timedelta(hours=150),
            timedelta(hours=126),
            timedelta(hours=102),
            timedelta(hours=78),
            timedelta(hours=54),
            timedelta(hours=30),
            timedelta(hours=21),
            timedelta(hours=17),
            timedelta(hours=13),
            timedelta(hours=9),
            timedelta(hours=5),
            timedelta(hours=1),
            timedelta(minutes=50),
            timedelta(minutes=40),
            timedelta(minutes=30),
            timedelta(minutes=20),
            timedelta(minutes=10),
        ]
        with db.transaction() as cur:
            for revision, age in enumerate(ages, start=1):
                cur.execute(
                    "INSERT INTO web_app_revisions"
                    " (app_id, revision, actor, kind, restored_from, html, css,"
                    " javascript, data_json, created_at)"
                    " VALUES ('app-1', %s, 'user', 'data', NULL, '', '', '', '{}', %s)",
                    (revision, (retained_at - age).strftime(backend.TIME_FORMAT)),
                )

        backend.prune_revisions(retained_at)

        with db.transaction() as cur:
            cur.execute(
                "SELECT revision FROM web_app_revisions"
                " WHERE app_id = 'app-1' ORDER BY revision"
            )
            self.assertEqual(
                [int(row[0]) for row in cur.fetchall()],
                list(range(2, 19)),
            )

    def test_batch_is_atomic_and_advances_one_revision(self) -> None:
        backend.create_web_app()
        changed = backend.apply_agent_action(
            {
                "action": "batch",
                "expected_revision": 0,
                "operations": [
                    {"action": "set", "path": ["items"], "value": []},
                    {"action": "append", "path": ["items"], "value": "one"},
                ],
            },
            "app-1",
        )
        self.assertEqual(changed, {"ok": True, "revision": 1})
        self.assertEqual(backend.load_app_state("app-1")["data"], {"items": ["one"]})
        revisions = backend.list_revisions("app-1", {})["revisions"]
        self.assertEqual([item["revision"] for item in revisions], [1, 0])

    def test_restore_recovers_interface_data_and_collections_as_a_forward_revision(self) -> None:
        backend.create_web_app()
        backend.apply_agent_action(
            {
                "action": "publish_ui",
                "expected_revision": 0,
                "html": "<main>Saved</main>",
                "css": "main { color: green; }",
                "javascript": "",
                "data_operations": [
                    {"action": "set", "path": ["count"], "value": 2},
                ],
            },
            "app-1",
        )
        backend.apply_collection_actions(
            "app-1",
            "leads",
            {
                "expected_revision": 1,
                "operations": [
                    {
                        "action": "upsert",
                        "id": "lead-1",
                        "value": {"status": "saved"},
                    }
                ],
            },
        )
        backend.apply_agent_action(
            {
                "action": "publish_ui",
                "expected_revision": 2,
                "html": "<main>New</main>",
                "css": "",
                "javascript": "",
                "data_operations": [
                    {"action": "set", "path": ["count"], "value": 9},
                ],
            },
            "app-1",
        )
        with db.transaction() as cur:
            cur.execute(
                "SELECT collections_json FROM web_app_revisions"
                " WHERE app_id = 'app-1' AND revision = 3"
            )
            snapshot_row = cur.fetchone()
        assert snapshot_row is not None
        self.assertEqual(
            json.loads(snapshot_row[0]),
            {"leads": {"lead-1": {"status": "saved"}}},
        )
        backend.apply_collection_actions(
            "app-1",
            "leads",
            {
                "expected_revision": 3,
                "operations": [
                    {
                        "action": "upsert",
                        "id": "lead-1",
                        "value": {"status": "changed"},
                    },
                    {
                        "action": "upsert",
                        "id": "lead-2",
                        "value": {"status": "new"},
                    },
                ],
            },
        )
        restored = backend.restore_revision("app-1", 2)["app"]
        self.assertEqual(restored["revision"], 5)
        self.assertEqual(restored["html"], "<main>Saved</main>")
        self.assertEqual(restored["data"], {"count": 2})
        collection = backend.query_collection("app-1", "leads", {})
        self.assertEqual(collection["revision"], 5)
        self.assertEqual(
            collection["rows"],
            [{"id": "lead-1", "value": {"status": "saved"}}],
        )
        newest = backend.list_revisions("app-1", {})["revisions"][0]
        self.assertEqual(newest["kind"], "restore")
        self.assertEqual(newest["restored_from"], 2)
