"""Database-free admin UI and shared-dispatch contract tests."""

from __future__ import annotations

import base64
from http import HTTPStatus
import json
from pathlib import Path
from typing import Any
import unittest
from unittest.mock import patch

import test_admin_api as admin_api_tests

from host.runtime.admin_api import service as admin_api
from host.runtime.admin_api import workspace_api as workspace_admin_api
from host.runtime.core import pgclient


class AdminUiStaticTests(unittest.TestCase):
    def setUp(self) -> None:
        snapshot = patch.object(
            admin_api.state,
            "conversation_search_snapshot",
            return_value=(1, 10**12, 1, 20),
        )
        snapshot.start()
        self.addCleanup(snapshot.stop)
        retention = patch.object(
            admin_api.state,
            "conversation_search_retention",
            return_value=(1, 1),
        )
        retention.start()
        self.addCleanup(retention.stop)

    def test_admin_api_reference_documents_every_oauth_runtime(self) -> None:
        # The reference is the contract API clients follow, and a runtime added
        # without it is undiscoverable: Grok shipped its routes, its reset
        # value, and its account fields before this doc caught up. Derive the
        # expectations from the service so the next runtime cannot repeat it.
        reference = (
            Path(__file__).parents[1] / "docs/api/AdminAPI.md"
        ).read_text()
        for runtime_type, flow in admin_api._OAUTH_LOGIN_FLOWS.items():
            route = f"/v1/agent-runtime/{flow.oauth_key}-oauth-login"
            self.assertIn(f"POST {route}", reference, runtime_type)
            self.assertIn(f"GET  {route}", reference, runtime_type)
            self.assertIn(f"| `POST` | `{route}` |", reference, runtime_type)
            self.assertIn(f"| `GET` | `{route}` |", reference, runtime_type)

        # Every runtime reset-linked-account accepts has to appear in the row
        # documenting what it accepts.
        reset_row = [
            line for line in reference.splitlines()
            if line.startswith("| `POST` | `/v1/agent-runtime/reset-linked-account`")
        ]
        self.assertEqual(len(reset_row), 1)
        for runtime_type in admin_api.OAUTH_RUNTIME_TYPES:
            self.assertIn(f'"{runtime_type}"', reset_row[0], runtime_type)

        # The account response enumerates its runtimes and providers.
        for runtime_type in admin_api.OAUTH_RUNTIME_TYPES:
            self.assertIn(f"`{runtime_type}`", reference, runtime_type)
        for usage_key in admin_api._RUNTIME_USAGE_KEYS.values():
            self.assertIn(f"`accounts[].{usage_key}`", reference, usage_key)
        for provider in ("openai", "claude", "xai", "bedrock"):
            self.assertIn(f"`{provider}`", reference, provider)

    def test_database_free_admin_ui_contract(self) -> None:
        # The database-backed integration-test class is skipped when local PostgreSQL is
        # unavailable, but this method reads static assets only. Run the same
        # assertions here so exact UI-copy and domain-list contracts are always
        # exercised before CI.
        admin_api_tests.AdminApiIntegrationTests.test_admin_ui_has_activity_and_diagnostic_views(self)

    def test_session_activity_is_centralized_and_excludes_background_polls(self) -> None:
        runtime = Path(__file__).parents[1] / "host/runtime/admin_api/admin_ui"
        api = (runtime / "api.js").read_text()
        app = (runtime / "app.js").read_text()
        self.assertIn('const SESSION_ACTIVITY_HEADER = "X-Kern-Session-Activity"', api)
        self.assertIn("Date.now() - lastOperatorActivityAt <= RECENT_OPERATOR_ACTIVITY_MS", api)
        self.assertIn('"pointerdown"', api)
        self.assertIn('"keydown"', api)
        self.assertIn('"touchstart"', api)
        self.assertIn("if (event && !event.isTrusted) return;", api)
        self.assertNotIn("setInterval", api)
        self.assertNotIn("markSessionActivity", app)

    def test_workspace_mounts_are_single_flight_and_query_the_whole_shadow_tree(self) -> None:
        root = Path(__file__).parents[1]
        app = (root / "host/runtime/admin_api/admin_ui/app.js").read_text()
        admin_css = (root / "host/runtime/admin_api/admin_ui/admin_ui.css").read_text()
        chat_css = (
            root / "host/runtime/workspace/chat/ui/agent_chat.css"
        ).read_text()
        web_apps_css = (
            root
            / "host/runtime/workspace/web_apps/ui/personal_web_app_builder.css"
        ).read_text()
        self.assertIn("const workspaceMounts = new Map();", app)
        self.assertIn("mounting = performWorkspaceMount(name, panelId, htmlPath);", app)
        self.assertIn("window.KernWorkspaceRoots[name] = shadow;", app)
        self.assertNotIn("window.KernWorkspaceRoots[name] = root;", app)
        # Each shadow host and root must be allowed to shrink below its
        # intrinsic content height. Otherwise a long conversation expands the
        # whole mounted surface instead of scrolling inside its own pane.
        self.assertIn(":host {", chat_css)
        self.assertIn(":host {", web_apps_css)
        self.assertRegex(chat_css, r"\.chat-app\s*\{[^}]*min-height:\s*0;[^}]*overflow:\s*hidden;")
        self.assertRegex(web_apps_css, r"\.builder-shell\s*\{[^}]*min-height:\s*0;[^}]*overflow:\s*hidden;")
        self.assertNotIn(".workspace-panel > *", admin_css)
        self.assertIn(
            "body.viewport-panel-open .workspace-panel {\n    height: 100%;",
            admin_css,
        )
        self.assertIn("workspace-input-focused", app)
        self.assertIn("isWorkspaceKeyboardInput(deepActiveElement())", app)
        self.assertIn("while (active?.shadowRoot?.activeElement)", app)
        self.assertIn("function isWorkspaceKeyboardInput(target)", app)
        self.assertIn(
            '["text", "search", "email", "tel", "url", "password", "number"]',
            app,
        )
        self.assertIn("focusedWorkspaceInput.isConnected", app)
        self.assertIn(
            "focusedWorkspaceInputObserver.observe(target.getRootNode(),",
            app,
        )
        self.assertIn(
            "body.viewport-panel-open.workspace-input-focused .runtime-overview",
            admin_css,
        )
        self.assertIn("keepLatestMessageAboveComposer", (
            root / "host/runtime/workspace/chat/ui/agent_chat.js"
        ).read_text())

    def test_workspace_navigation_fences_stale_fetches_and_actions(self) -> None:
        app = (
            Path(__file__).parents[1] / "host/runtime/admin_api/admin_ui/app.js"
        ).read_text()
        web_apps = (
            Path(__file__).parents[1]
            / "host/runtime/workspace/web_apps/ui/personal_web_app_builder.js"
        ).read_text()
        self.assertIn("let workspaceNavigationRefreshSequence = 0;", app)
        self.assertIn("let workspaceNavigationActionSequence = 0;", app)
        self.assertIn("const sequence = ++workspaceNavigationRefreshSequence;", app)
        self.assertIn("sequence !== workspaceNavigationRefreshSequence", app)
        self.assertIn("const actionSequence = ++workspaceNavigationActionSequence;", app)
        self.assertIn("actionSequence !== workspaceNavigationActionSequence", app)
        self.assertIn("backToHome(actionSequence)", app)
        self.assertEqual(app.count("backToHome(actionSequence)"), 3)
        self.assertRegex(
            app,
            r"function backToHome\(workspaceActionSequence = null\) \{\s*"
            r"if \(\s*workspaceActionSequence !== null\s*"
            r"&& workspaceActionSequence !== workspaceNavigationActionSequence\s*"
            r"\) return false;\s*if \(history\.state",
        )
        self.assertIn("const workspacePendingMutations = new Set();", app)
        self.assertIn("button.disabled = pending;", app)
        self.assertIn("const initializationPromise = initialize();", web_apps)
        self.assertIn("return initializationPromise.then(() => action(...args));", web_apps)
        self.assertIn("create: (...args) => afterInitialization(createApp, ...args)", web_apps)
        self.assertIn("open: (...args) => afterInitialization(showApp, ...args)", web_apps)
        self.assertNotIn("initialize();\n})();", web_apps)

    def test_workspace_navigation_tracks_seen_chat_activity_and_app_revisions(self) -> None:
        root = Path(__file__).parents[1]
        app = (root / "host/runtime/admin_api/admin_ui/app.js").read_text()
        last_seen = (
            root / "host/runtime/admin_api/admin_ui/workspace_last_seen.js"
        ).read_text()
        admin_css = (root / "host/runtime/admin_api/admin_ui/admin_ui.css").read_text()
        chat = (root / "host/runtime/workspace/chat/ui/agent_chat.js").read_text()
        web_apps = (
            root / "host/runtime/workspace/web_apps/ui/personal_web_app_builder.js"
        ).read_text()

        self.assertIn('const STORAGE_KEY = "kern.workspace-last-seen.v2";', last_seen)
        self.assertIn(
            'workspaceLastSeen.initialize("chat", chatNavItems, chatNavArchived);',
            app,
        )
        self.assertIn(
            'workspaceLastSeen.initialize("apps", webAppNavItems, webAppsNavArchived);',
            app,
        )
        initialize_seen = last_seen.split("function initialize(kind", 1)[1].split(
            "function initializeArchived", 1
        )[0]
        self.assertIn("state.active[kind]", initialize_seen)
        self.assertNotIn("chatNavArchived || webAppsNavArchived", initialize_seen)
        mark_seen = last_seen.split("function markSeen", 1)[1].split(
            'window.addEventListener("storage"', 1
        )[0]
        self.assertNotIn("state.active", mark_seen)
        self.assertIn("Number(item.latest_message_seq)", last_seen)
        self.assertNotIn("Number(item.latest_event_seq)", last_seen)
        self.assertIn("current.activity > (Number(seen.activity) || 0)", last_seen)
        self.assertIn("current.revision > (Number(seen.revision) || 0)", last_seen)
        self.assertIn("state = mergeState(loadState(), state)", last_seen)
        self.assertIn('workspaceLastSeen.initializeArchived("chat", chatNavItems, chatNavArchived)', app)
        self.assertIn('workspaceLastSeen.initializeArchived("apps", webAppNavItems, webAppsNavArchived)', app)
        self.assertIn('dot.setAttribute("aria-label", "New activity")', app)
        self.assertIn("document.visibilityState !== \"visible\"", last_seen)
        open_chat = app.split("async function openWorkspaceChat", 1)[1].split(
            "async function findWebAppNavItem", 1
        )[0]
        self.assertNotIn('markWorkspaceSeen("chat"', open_chat)
        self.assertIn('window.KernHost.markWorkspaceSeen("chat", {', chat)
        self.assertIn("latest_message_seq: acknowledgedMessageSeq", chat)
        self.assertIn(
            '["thread.message", "thread.memory_cleared"].includes(event.event_type)',
            chat,
        )
        self.assertIn("markSelectedThreadSeen({ thread_id: threadId });", chat)
        self.assertIn("const refreshedThreadId = selectedThreadId;", chat)
        self.assertIn("if (rendered && selectedThreadId === refreshedThreadId && visibleThread)", chat)
        self.assertIn('window.KernHost.markWorkspaceSeen("apps", {', web_apps)
        self.assertIn("Number(listed?.latest_message_seq) || 0", web_apps)
        self.assertIn("renderedMessageSeq", web_apps)
        self.assertIn('event.event_type === "thread.message"', web_apps)
        self.assertIn("revision: renderedRevision", web_apps)
        self.assertIn(".workspace-nav-unseen {", admin_css)

    def test_admin_passkey_ui_keeps_password_as_factor_one(self) -> None:
        runtime = Path(__file__).parents[1] / "host/runtime/admin_api"
        html = (runtime / "admin_ui/index.html").read_text()
        app = (runtime / "admin_ui/app.js").read_text()
        passkeys = (runtime / "admin_ui/passkeys.js").read_text()
        self.assertIn('id="passkey-setup"', html)
        self.assertIn('id="passkey-status-control"', html)
        self.assertIn('id="login-passkey-status"', html)
        self.assertIn("Two-factor authentication enabled", html)
        self.assertIn("Your admin password remains unchanged.", html)
        self.assertIn("finishPasskeyLogin(result.publicKey)", app)
        self.assertIn("refreshLoginPasskeyStatus()", app)
        self.assertIn('"show-passkey-guidance": () => openPasskeyGuidance()', app)
        self.assertIn('showTab("home")', app)
        self.assertIn("setupPasskey();", app)
        self.assertIn('"/v1/login/status"', passkeys)
        self.assertIn('"/v1/login/passkey"', passkeys)
        self.assertIn('"/v1/admin-passkeys/register/options"', passkeys)
        self.assertIn("navigator.credentials.create", passkeys)
        self.assertIn("navigator.credentials.get", passkeys)
        self.assertIn("control.hidden = !available", passkeys)
        self.assertIn('control.classList.toggle("passkey-protected", configured)', passkeys)
        self.assertIn('control.classList.toggle("passkey-setup-needed", !configured)', passkeys)
        self.assertIn("banner.scrollIntoView", passkeys)
        setup_flow = passkeys.split("export async function setupPasskey()", 1)[1]
        self.assertLess(setup_flow.index("try {"), setup_flow.index("requireWebAuthn();"))

    def test_agent_chat_uses_one_backend_authoritative_composer(self) -> None:
        script = (
            Path(__file__).parents[1] / "host/runtime/workspace/chat/ui/agent_chat.js"
        ).read_text()
        stylesheet = (
            Path(__file__).parents[1] / "host/runtime/workspace/chat/ui/agent_chat.css"
        ).read_text()
        send = script.split("async function sendMessageUnlocked()", 1)[1].split(
            "\nasync function", 1
        )[0]
        self.assertIn('"POST",\n    "/messages",', send)
        self.assertIn("MESSAGE_DELIVERY_TIMEOUT_MS,", send)
        self.assertIn("DELIVERY_TIMEOUT_MESSAGE,", send)
        self.assertIn("void Promise.all([refresh(), window.KernHost.refreshNavigation()])", send)
        self.assertIn(
            '["thread.message", "thread.activity", "thread.error", "thread.stopped",\n'
            '      "thread.memory_cleared"].includes(event.event_type)',
            script,
        )
        # A clear made while scrolled up must bring its own confirmation into
        # view; the marker is the only signal the action took effect.
        self.assertIn(
            "await api(\"POST\", `/threads/${encodeURIComponent(selectedThreadId)}/clear-memory`);",
            script,
        )
        self.assertIn("forceScrollBottom = true;\n  await refresh();", script)
        self.assertIn('let showingActivity = false;', script)
        self.assertIn('classList.toggle("activity-hidden", !showingActivity)', script)
        self.assertIn('"--activity-anchor-space"', script)
        self.assertIn("toggleSequence !== activityToggleSequence", script)
        self.assertIn("const atTail = distanceFromBottom <= 1;", script)
        self.assertNotIn("activityAnchorState", script)
        clear_selected = script.split("function clearSelectedThread()", 1)[1].split(
            "function startNewThread()", 1
        )[0]
        self.assertIn("activityToggleSequence += 1;", clear_selected)
        self.assertIn("clearActivityAnchorSpace();", clear_selected)
        self.assertIn("var(--activity-anchor-space, 0px)", stylesheet)
        activity_markup = (
            Path(__file__).parents[1] / "host/runtime/workspace/chat/ui/index.html"
        ).read_text()
        self.assertIn('id="activity-toggle"', activity_markup)
        self.assertIn('aria-checked="false" title="Show agent activity"', activity_markup)
        self.assertIn(".chat-app.activity-hidden .thread-activity", stylesheet)
        self.assertIn('class="thread-entry thread-activity"', script)
        self.assertIn('querySelectorAll(".thread-entry:not(.thread-activity)")', script)
        self.assertIn(
            'button.activity-switch[aria-checked="true"] .activity-switch-track',
            stylesheet,
        )
        self.assertIn("background: var(--ok);", stylesheet)
        self.assertIn(
            "background: color-mix(in srgb, var(--ok) 82%, white);",
            stylesheet,
        )
        self.assertNotIn("hydrateCompletePrompts", script)
        self.assertNotIn("completePromptByTask", script)
        self.assertNotIn('"task.created"', script)
        self.assertNotIn('"task.updated"', script)
        self.assertNotIn("task-steer-input", script)
        self.assertNotIn("task.output_message", script)
        self.assertIn(
            "`/threads/${encodeURIComponent(threadId)}/events${suffix}`",
            script,
        )
        self.assertIn('query.push("activity=false")', script)
        self.assertIn('threadEventPath(threadId, pageState, "before", before)', script)
        self.assertIn(
            'threadEventPath(threadId, pageState, "since", pageState.newestSeq)',
            script,
        )
        self.assertIn("const INITIAL_EVENT_PAGES = 3", script)
        self.assertIn("const threadViewStates = new Map()", script)
        self.assertIn("saveSelectedThreadView();", script)
        self.assertIn("restoreThreadView(threadId);", script)
        self.assertIn('<span class="activity-phase">Started</span>', script)
        self.assertNotIn(".activity-card.started .activity-icon", stylesheet)
        self.assertIn('"/threads?archived=true"', script)
        self.assertIn('"unarchive"', script)

    def test_workspace_use_host_owned_file_and_api_helpers(self) -> None:
        runtime = Path(__file__).parents[1] / "host/runtime/admin_api/admin_ui"
        app = (runtime / "app.js").read_text()
        api = (runtime / "api.js").read_text()
        files = (runtime / "files.js").read_text()
        chat = (
            Path(__file__).parents[1] / "host/runtime/workspace/chat/ui/agent_chat.js"
        ).read_text()
        web_apps = (
            Path(__file__).parents[1]
            / "host/runtime/workspace/web_apps/ui/personal_web_app_builder.js"
        ).read_text()
        self.assertIn("window.KernHost", app)
        self.assertIn('input.type = "file"', app)
        self.assertIn("input.multiple = maximum > 1", app)
        self.assertNotIn("files.slice(0, maximum)", app)
        self.assertIn("chooseFiles", app)
        self.assertIn("apiUpload", app)
        self.assertIn("refreshNavigation()", app)
        self.assertIn('openAgentFile(path, fallbackPath = "")', app)
        self.assertIn("openLinkedAgentFile(path, fallbackPath)", app)
        self.assertLess(
            files.index("showFileDownload(path);"),
            files.index("const blob = await apiBlob"),
        )
        self.assertLess(
            files.index("prepareFileViewer(path);"),
            files.index("showFileDownload(path);"),
        )
        self.assertIn("let fileActionSequence = 0", files)
        self.assertIn("requestSequence !== fileActionSequence", files)
        self.assertGreaterEqual(files.count("requestIsStale()"), 3)
        self.assertIn("await Promise.all([", files)
        self.assertIn("loadAgentFiles(parentPath(filePath), true, actionSequence)", files)
        self.assertIn(
            'readAgentFile(filePath, actionSequence, String(fallbackPath || ""))',
            files,
        )
        self.assertIn("error.status === 404 && fallbackPath", files)
        self.assertIn("const downloadPath = currentViewerPath", files)
        self.assertIn("encodeURIComponent(downloadPath)", files)
        self.assertIn('downloadPath.split("/").pop()', files)
        self.assertIn("upload failed (${response.status})", api)
        self.assertIn("/v1/agent-files/upload?filename=", api)
        self.assertIn("window.KernHost.chooseFiles", chat)
        self.assertIn("window.KernHost.refreshNavigation()", chat)
        self.assertIn("window.KernHost.openAgentFile", chat)
        self.assertIn("window.KernHost.chooseFiles", web_apps)
        self.assertIn("window.KernHost.refreshNavigation()", web_apps)
        self.assertNotIn("postMessage", chat)
        self.assertNotIn("kern-app-upload-file", web_apps)
        self.assertIn("const ATTACHMENT_LIMIT = 10", chat)
        self.assertIn("const ATTACHMENT_MAX_BYTES = 25 * 1024 * 1024", chat)
        self.assertIn(
            "attachment.size_bytes > ATTACHMENT_MAX_BYTES",
            chat,
        )
        self.assertIn(
            "for (const [index, attachment] of pendingAttachments.entries())",
            chat,
        )

    def test_connection_guide_preserves_network_and_data_disclosure_contracts(self) -> None:
        runtime = Path(__file__).parents[1] / "host/runtime/admin_api"
        catalog = (runtime / "admin_ui" / "integration_catalog.js").read_text()
        guide = (runtime / "admin_ui" / "connection_guide.js").read_text()
        html = (runtime / "admin_ui/index.html").read_text()

        for required_text in (
            "GraphQL denied",
            "LFS uploads denied",
            "github.com",
            "api.github.com",
            "uploads.github.com",
            "codeload.github.com",
            "objects.githubusercontent.com",
            "github-cloud.githubusercontent.com",
            "raw.githubusercontent.com",
            "release-assets.githubusercontent.com",
            "pypi.org",
            "files.pythonhosted.org",
            "nodejs.org",
            "registry.npmjs.org",
        ):
            self.assertIn(required_text, catalog)
        self.assertIn("query parameters", catalog)
        self.assertIn("renderDataSummary", guide)
        self.assertIn("action.input_schema || {}", guide)
        self.assertIn("action.output_schema || {}", guide)
        # Every manifest object is closed now, so the guide states what an
        # action returns instead of warning about undeclared extras.
        self.assertNotIn("permits additional output fields", guide)
        self.assertIn("This action queues an approval; the outcome is a message, not data fields.", guide)
        self.assertIn("This action returns a file into the agent workspace, not data fields.", guide)
        # A union is a type wherever it appears, so a nullable field renders as
        # one instead of falling through to "unspecified".
        self.assertIn('if (Array.isArray(schema.oneOf)) return schema.oneOf.map(schemaTypeLabel).join(" or ");', guide)
        self.assertNotIn("Complete JSON schemas", guide)
        self.assertNotIn('.guide-capability:has(> .guide-action-contract[open])', (
            runtime / "admin_ui" / "admin_ui.css"
        ).read_text())
        self.assertNotIn("renderDataFlows", guide)
        self.assertIn("What happens to your data", guide)
        self.assertIn("Technical notes", guide)
        self.assertIn("renderGuide(selected)", guide)
        self.assertNotIn("guide.connection", guide)
        self.assertNotIn("guides.map(renderGuide)", guide)
        self.assertNotIn("scrollIntoView", guide)
        # Opening a panel resets scroll, but `html` scrolls smoothly, and an
        # animation in flight outlives a plain "instant" scroll — the reset has
        # to cancel it rather than issue a competing one.
        app_js = (runtime / "admin_ui" / "app.js").read_text()
        reset = app_js.split("function resetPageScroll()")[1].split("\n}")[0]
        self.assertIn('scrollBehavior = "auto"', reset)
        self.assertIn('behavior: "instant"', reset)
        # The other async scroll the panel reset has to beat: pushState with
        # the browser's default restoration also restores an offset, after the
        # panel has already reset itself.
        self.assertIn('history.scrollRestoration = "manual"', app_js)
        self.assertIn(
            "scroll-behavior: smooth",
            (runtime / "admin_ui" / "admin_ui.css").read_text(),
        )
        self.assertIn('id="home-integration-groups"', html)
        self.assertIn("Integration guide", html)
        self.assertNotIn('id="panel-connection-guide"', html)
        self.assertNotIn("What each integration enables", html)

    def test_xai_ui_copy_and_no_web_search_toggle_contract(self) -> None:
        runtime = Path(__file__).parents[1] / "host/runtime/admin_api"
        catalog = (runtime / "admin_ui" / "integration_catalog.js").read_text()
        network = (runtime / "admin_ui" / "network.js").read_text()
        app = (runtime / "admin_ui" / "app.js").read_text()
        helpers = (runtime / "admin_ui" / "helpers.js").read_text()
        connection_guide = (runtime / "admin_ui" / "connection_guide.js").read_text()

        self.assertIn('xai: {\n    label: "Grok"', catalog)
        self.assertIn('["openai", "claude", "xai", "bedrock"]', connection_guide)
        # The two hosts the guard opens, and the two it deliberately does not,
        # are the operator-facing point of this integration.
        self.assertIn("auth.x.ai", catalog)
        self.assertIn("cli-chat-proxy.grok.com", catalog)
        self.assertIn("metered developer API stays blocked", catalog)
        self.assertIn("Run Grok Build chats and tasks", catalog)
        self.assertIn("Creates and resumes Grok Build sessions", catalog)
        self.assertIn("accepts live steering", catalog)
        for url in (
            "https://console.x.ai/",
            "https://docs.x.ai/build/modes-and-commands#core-tui-commands",
            "https://docs.x.ai/developers/tools/x-search",
            "https://docs.x.ai/developers/tools/image-generation",
            "https://docs.x.ai/developers/faq/security#does-xai-train-on-customers-api-requests",
            "https://docs.x.ai/build/enterprise#privacy--data-lifecycle",
            "https://grok.com/?_s=data",
            "https://x.com/settings/grok_settings",
            "https://x.ai/legal/faq#how-do-i-select-whether-my-content-is-used-for-model-training",
        ):
            self.assertIn(url, catalog)

        # The shared web-search control stays parameterised by provider, but
        # Grok is not one of its providers: xAI ships no toggle at all.
        self.assertIn("setProviderWebSearch", network)
        self.assertIn("setProviderWebSearch(button.dataset.provider, true)", app)
        self.assertIn("setProviderWebSearch(button.dataset.provider, false)", app)
        self.assertNotIn("setClaudeWebSearch", network + app)
        self.assertNotIn("enable-claude-web-search", network + app)
        disclosures = network.split("WEB_SEARCH_DISCLOSURE")[1][:600]
        self.assertIn("claude: {", disclosures)
        self.assertNotIn("xai: {", disclosures)
        self.assertIn('if (name === "claude" && enabled)', network)

        # The operator-facing reason web search is absent, rather than silence
        # about a capability Grok has everywhere else.
        self.assertIn("Grok's server-side web search is not available on this host", catalog)
        self.assertIn("Web search is blocked because Grok's cannot be narrowed", catalog)
        self.assertIn("without that request ever passing this host's network policy", catalog)
        self.assertIn("shapes that stay on xAI/X infrastructure", catalog)
        self.assertIn("xAI executes keyword, semantic, user, and thread search", catalog)
        self.assertIn("this host does not contact x.com or a third-party search provider", catalog)
        self.assertIn("Grok Build 1.0.5 does not emit either declaration", catalog)
        self.assertIn("media generation is not yet usable from the Grok runtime", catalog)
        # Nothing in the xAI entry may still offer the removed control. The
        # capability block described it as optional after the toggle was gone,
        # which is the shape this regression takes.
        xai_entry = catalog.split('xai: {\n    label: "Grok"')[1].split("\n  bedrock: {")[0]
        for advertised in (
            "Web search (optional",
            "off by default",
            "unless you enable it",
            "when you enable it",
        ):
            self.assertNotIn(advertised.lower(), xai_entry.lower(), advertised)
        self.assertIn("Web search (not available)", xai_entry)

        # The same drift hid in prose the catalog check cannot see. These
        # phrases only make sense if an xAI search control exists, so they are
        # the ones that must not come back anywhere the integration is
        # described. Claude's toggle is discussed in the same documents, so
        # this pins the claims rather than banning the word.
        integration_doc = (
            Path(__file__).parents[1] / "docs/architecture/xai-integration.md"
        ).read_text()
        controls_doc = (
            Path(__file__).parents[1] / "docs/api/NetworkControls.md"
        ).read_text()
        architecture_controls_doc = (
            Path(__file__).parents[1] / "docs/architecture/network-controls.md"
        ).read_text()
        xai_manifest = (
            Path(__file__).parents[1]
            / "host/network_integrations/xai/manifest.py"
        ).read_text()
        xai_guard = (
            Path(__file__).parents[1]
            / "host/network_integrations/xai/guard.py"
        ).read_text()
        # Every document that describes this integration, not just the two that
        # describe it at length: the drift that survived two passes was one
        # clause in the architecture index.
        index_doc = (
            Path(__file__).parents[1] / "docs/architecture/index.md"
        ).read_text()
        xai_index_row = [
            line for line in index_doc.splitlines() if "xai-integration.md" in line
        ]
        self.assertEqual(len(xai_index_row), 1)
        self.assertNotIn("web-search toggle", xai_index_row[0])
        self.assertNotIn("routing metadata", xai_index_row[0])
        self.assertIn("bearer-token account pinning", xai_index_row[0])
        for claim in (
            "when search is enabled",
            "opting into live search",
            "`web_search` requires `enabled`",
            "the operator opted into",
            "with the toggle on",
            "control is shared with claude",
        ):
            self.assertNotIn(claim.lower(), integration_doc.lower(), claim)
            self.assertNotIn(claim.lower(), controls_doc.lower(), claim)
        self.assertIn(
            "Grok's server-side web search is not available on this host",
            integration_doc,
        )
        self.assertIn("the integration has no options", controls_doc.lower())
        self.assertIn("data-flow audit for the allowed tools", integration_doc.lower())
        self.assertIn("xai/x only", integration_doc.lower())
        self.assertIn("fixed allowlist", controls_doc.lower())
        for obsolete_header_claim in (
            "optional account header",
            "identity headers must agree",
            "every one it does send must match",
            "matching header and `sub`",
        ):
            for document in (
                architecture_controls_doc,
                integration_doc,
                xai_manifest,
                xai_guard,
            ):
                self.assertNotIn(obsolete_header_claim, document.lower())

        # Grok has a real account/login card, runtime status, and task-session
        # selection through the ACP adapter.
        self.assertIn('name === "openai" || name === "claude" || name === "xai"', network)
        self.assertIn('grok: { label: "Grok", provider: "xai"', helpers)
        self.assertIn('const XAI_INTEGRATION = "xai";', network)
        self.assertIn('typeof account.zdr_enabled === "boolean"', network)
        self.assertIn('` &middot; ZDR ${account.zdr_enabled ? "active" : "inactive"}`', network)
        self.assertIn(
            'typeof account.coding_data_retention_opt_out === "boolean"', network
        )
        self.assertIn(
            '` &middot; coding-data opt-out ${account.coding_data_retention_opt_out ? "active" : "inactive"}`',
            network,
        )
        self.assertIn("renderPolicyPointContent(point)", connection_guide)
        self.assertIn('{ url: "https://console.x.ai/", label: "xAI Console" }', catalog)
        self.assertIn(
            '{ url: "https://grok.com/?_s=data", label: "Grok.com data controls" }',
            catalog,
        )
        self.assertIn(
            '{ url: "https://x.com/settings/grok_settings", label: "X Grok settings" }',
            catalog,
        )

    def test_bedrock_ui_copy_and_toolbar_contract(self) -> None:
        runtime = Path(__file__).parents[1] / "host/runtime/admin_api"
        catalog = (runtime / "admin_ui" / "integration_catalog.js").read_text()
        guide = (runtime / "admin_ui" / "connection_guide.js").read_text()
        health = (runtime / "admin_ui" / "health.js").read_text()
        network = (runtime / "admin_ui" / "network.js").read_text()
        css = (runtime / "admin_ui/admin_ui.css").read_text()
        combined = "\n".join((catalog, guide, health, network, css))

        self.assertIn('label: "Hermes (AWS Bedrock)"', catalog)
        self.assertNotIn("bedrock-usage-runtime", combined)
        self.assertNotIn("AWS Bedrock AI inference", combined)
        # One live usage box for Hermes in both the toolbar and provider panel;
        # the Cost Explorer display is gone with the polling it required.
        self.assertIn("bedrockUsage(account)", health)
        self.assertIn("runtime-summary-bedrock", combined)
        self.assertIn("bedrock-usage-box", network)
        self.assertIn("MTD est.", health)
        self.assertNotIn("Cost Explorer", combined)
        self.assertNotIn("bedrock_spend", combined)
        self.assertNotIn("ce:GetCostAndUsage", combined)
        self.assertNotIn("bedrock-toolbar-lag", combined)
        self.assertIn("runtime-running-badge", combined)
        self.assertIn('id="bedrock-region-', network)
        self.assertIn('"region": region', network)
        self.assertNotIn("setBedrockRegion", combined)
        self.assertIn("credentials required", network)
        self.assertIn("bedrock:InvokeModel", catalog)
        self.assertIn("bedrock:InvokeModelWithResponseStream", catalog)
        self.assertIn("guide-step-code", combined)
        self.assertNotIn("Kern rechecks the connected key", catalog)
        self.assertNotIn("The first task is the live check", catalog)
        self.assertNotIn("Kern immediately verifies", catalog)
        self.assertNotIn("Kern verifies the credential", catalog)

    def test_custom_domain_ui_exposes_websocket_opt_in(self) -> None:
        network = (
            Path(__file__).parents[1] / "host/runtime/admin_api/admin_ui/network.js"
        ).read_text()
        html = (Path(__file__).parents[1] / "host/runtime/admin_api/admin_ui/index.html").read_text()
        self.assertIn('id="policy-allow-websocket"', html)
        self.assertIn("if (allowWebsocket) rule.allow_websocket = true", network)

    def test_connection_guide_screenshots_are_local_png_assets(self) -> None:
        repo = Path(__file__).parents[1]
        asset_dir = repo / "host/tools/shared/guide_assets/google"
        mock = (repo / "tests/smoke-ui/run_admin_ui_mock.py").read_text()
        for name in (
            "google-auth-app-information.png",
            "google-auth-data-access.png",
            "google-auth-web-client.png",
        ):
            asset = asset_dir / name
            self.assertTrue(asset.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))
            self.assertIn(f"/guide-assets/{name}", admin_api.UI_ASSETS)
        self.assertIn('route = f"/guide-assets/{asset.name}"', mock)
        self.assertFalse((repo / "host/runtime/admin_ui/guide_assets").exists())

    def test_mock_capability_worker_is_networkless(self) -> None:
        mock = (
            Path(__file__).parents[1] / "tests/smoke-ui/run_admin_ui_mock.py"
        ).read_text()
        handler = mock.split("def _send_capability_worker", 1)[1].split(
            "def _send", 1
        )[0]
        self.assertIn("connect-src 'none'", handler)
        self.assertIn("worker-src data:", handler)
        self.assertIn("script-src 'none'", handler)
        self.assertIn('"X-Frame-Options", "DENY"', handler)

    def test_disabled_integrations_omit_irrelevant_connection_state(self) -> None:
        runtime = Path(__file__).parents[1] / "host/runtime/admin_api"
        network_js = (runtime / "admin_ui" / "network.js").read_text()
        tools_js = (runtime / "admin_ui" / "tools.js").read_text()
        css = (runtime / "admin_ui/admin_ui.css").read_text()

        self.assertIn('if (!enabled) {\n      setHtml(node, "");', network_js)
        self.assertIn('const summary = !enabled && !linked', network_js)
        self.assertEqual(
            network_js.count(
                'const record = runtimeRecords().find(entry => entry.type === runtime) || '
                '{ status: account.status || "loading" };'
            ),
            2,
        )
        self.assertIn('tool.connection === "oauth" && (tool.enabled || connected)', tools_js)
        self.assertIn('${tool.enabled ? "" : " disabled"}>Reconnect</button>', tools_js)
        self.assertIn('${tool.enabled ? "" : " disabled"}>${connections.length', tools_js)
        self.assertIn(".connection-summary {", css)
        self.assertIn("color: var(--text-dim);", css)

    def test_mobile_navigation_uses_an_accessible_drawer(self) -> None:
        runtime = Path(__file__).parents[1] / "host/runtime/admin_api"
        html = (runtime / "admin_ui/index.html").read_text()
        app_js = (runtime / "admin_ui" / "app.js").read_text()
        css = (runtime / "admin_ui/admin_ui.css").read_text()

        self.assertIn('id="mobile-nav-toggle"', html)
        self.assertIn('aria-controls="sidebar"', html)
        self.assertIn('id="nav-backdrop"', html)
        self.assertIn("function setMobileNavOpen(open, restoreFocus = false)", app_js)
        self.assertIn('sidebar.inert = mobile && !mobileNavOpen', app_js)
        self.assertIn('document.querySelector(".topbar").inert = mobileNavOpen', app_js)
        self.assertIn('document.querySelector("main").inert = mobileNavOpen', app_js)
        self.assertIn(".sidebar.mobile-open { transform: translateX(0); }", css)

    def test_iphone_standalone_edges_do_not_navigate_browser_history(self) -> None:
        runtime = Path(__file__).parents[1] / "host/runtime/admin_api"
        app_js = (runtime / "admin_ui" / "app.js").read_text()
        css = (runtime / "admin_ui/admin_ui.css").read_text()

        self.assertIn("function bindIPhoneStandaloneSidebarSwipe()", app_js)
        self.assertIn('if (!isIPhoneStandalone()) return;', app_js)
        self.assertIn('document.addEventListener("touchstart"', app_js)
        self.assertIn('document.addEventListener("touchmove"', app_js)
        self.assertIn('touch.clientX >= window.innerWidth - edgeWidth ? "right"', app_js)
        self.assertNotIn("interactiveTarget", app_js)
        self.assertIn("if (event.cancelable) event.preventDefault();", app_js)
        self.assertIn("setMobileNavOpen(true);", app_js)
        self.assertIn("html.iphone-standalone, html.iphone-standalone body {", css)
        self.assertIn("overscroll-behavior-x: none;", css)
        self.assertIn("function syncIPhoneStandaloneViewport()", app_js)
        self.assertIn("workspaceKeyboardOwnsViewport()", app_js)
        self.assertIn("function visualViewportIsContracted()", app_js)
        self.assertIn("workspaceKeyboardViewportBaselineHeight - 80", app_js)
        self.assertIn("workspaceKeyboardViewportBaselineHeight = 0", app_js)
        self.assertIn("Math.abs(layout.width - iPhoneStandaloneViewportBaseline.width) > 80", app_js)
        self.assertIn("keyboardOwnsViewport\n    ? layout.height", app_js)
        self.assertIn('style.setProperty("--kern-viewport-height"', app_js)
        self.assertIn("height: var(--kern-viewport-height, 100dvh);", css)
        self.assertNotIn('visualViewport.addEventListener("scroll"', app_js)

    def test_provider_usage_rings_have_warning_and_critical_thresholds(self) -> None:
        runtime = Path(__file__).parents[1] / "host/runtime/admin_api"
        health_js = (runtime / "admin_ui" / "health.js").read_text()
        css = (runtime / "admin_ui/admin_ui.css").read_text()

        self.assertIn('percent > 90 ? " usage-critical" : percent > 80 ? " usage-warning"', health_js)
        # The rings use the muted usage-* palette so they read quietly in the
        # dark top bar; ok/warning/critical stay visually distinct.
        self.assertIn(".usage-ring.usage-warning .usage-ring-value { stroke: var(--usage-warn); }", css)
        self.assertIn(".usage-ring.usage-critical .usage-ring-value { stroke: var(--usage-critical); }", css)

    def test_runtime_usage_is_legible_without_hover_magnification(self) -> None:
        runtime = Path(__file__).parents[1] / "host/runtime/admin_api"
        css = (runtime / "admin_ui/admin_ui.css").read_text()
        health_js = (runtime / "admin_ui" / "health.js").read_text()

        self.assertNotIn("button.runtime-summary:hover > .runtime-usage", css)
        self.assertNotIn("transform: scale(1.5);", css)
        self.assertIn('viewBox="0 0 20 20"', health_js)
        self.assertIn("font-size: 0.56rem;", css)
        self.assertIn("font-weight: 650;", css)
        self.assertIn("stroke-width: 1.25;", css)
        self.assertIn(".usage-window { font-size: 0.44rem;", css)
        self.assertIn(
            ".runtime-overview.expanded .runtime-stat-value { font-size: 0.64rem; }",
            css,
        )
        self.assertIn(
            ".runtime-overview.expanded .runtime-stat-label { font-size: 0.5rem; }",
            css,
        )

    def test_provider_usage_rings_show_compact_reset_countdowns(self) -> None:
        runtime = Path(__file__).parents[1] / "host/runtime/admin_api"
        health_js = (runtime / "admin_ui" / "health.js").read_text()
        css = (runtime / "admin_ui/admin_ui.css").read_text()

        self.assertIn("function resetCountdown(value, now = Date.now())", health_js)
        self.assertIn("current_session_resets_at", health_js)
        self.assertNotIn("resets_at_text", health_js)
        # The countdown shares the single window-label line under the ring so
        # the top bar keeps a constant height.
        self.assertIn('const display = available ? `${Math.round(percent)}` : "--";', health_js)
        self.assertNotIn('`${Math.round(percent)}%`', health_js)
        self.assertIn('${esc(label)}${countdown ? ` · ${countdown}` : ""}', health_js)
        self.assertNotIn("usage-reset", health_js)
        self.assertNotIn(".usage-reset {", css)
        self.assertIn(".usage-window {", css)

    def test_grok_usage_maps_every_normalized_billing_period(self) -> None:
        runtime = Path(__file__).parents[1] / "host/runtime/admin_api"
        health_js = (runtime / "admin_ui" / "health.js").read_text()

        self.assertIn('daily: { label: "day", summary: "daily" }', health_js)
        self.assertIn('weekly: { label: "wk", summary: "weekly" }', health_js)
        self.assertIn('monthly: { label: "mo", summary: "monthly" }', health_js)

    def test_upgrade_notice_is_descriptive_and_shown_with_home_version(self) -> None:
        runtime = Path(__file__).parents[1] / "host/runtime/admin_api"
        html = (runtime / "admin_ui/index.html").read_text()
        health_js = (runtime / "admin_ui" / "health.js").read_text()
        css = (runtime / "admin_ui/admin_ui.css").read_text()

        self.assertNotIn('id="upgrade-notice"', html)
        self.assertNotIn('id="upgrade-popover"', html)
        self.assertNotIn('href="https://github.com/infiloop2/kern', html)
        self.assertIn("renderVersion(health.version)", health_js)
        self.assertIn("renderHomeUpgrade(health.upgrade)", health_js)
        self.assertIn('"stat-wide version-tile"', health_js)
        self.assertNotIn("renderUpgradeNotice", health_js)
        self.assertIn("Use your operator plane to upgrade.", health_js)
        self.assertNotIn('class="muted">runtime', health_js)
        self.assertNotIn('class="muted">state', health_js)
        self.assertNotIn(".upgrade-notice {", css)
        self.assertIn(".home-upgrade-notice {", css)
        self.assertIn(".upgrade-popover {", css)
        self.assertNotIn(".upgrade-notice:hover .upgrade-popover", css)

    def test_home_getting_started_checklist_uses_durable_progress_and_chat_prompts(self) -> None:
        root = Path(__file__).parents[1]
        runtime = root / "host/runtime/admin_api"
        html = (runtime / "admin_ui/index.html").read_text()
        app = (runtime / "admin_ui/app.js").read_text()
        checklist = (runtime / "admin_ui/getting_started.js").read_text()
        chat = (root / "host/runtime/workspace/chat/ui/agent_chat.js").read_text()
        css = (runtime / "admin_ui/admin_ui.css").read_text()

        self.assertLess(html.index('id="getting-started"'), html.index("Host health"))
        self.assertIn('/v1/workspace/getting-started', checklist)
        # All four steps read the same server-derived payload; none of them is
        # recomputed in the browser from cached runtime records.
        for step in ("provider_ready", "chat_created", "app_created", "schedule_created"):
            self.assertIn(f"workspaceStatus.{step} === true", checklist)
        self.assertNotIn("RUNTIME_PROVIDERS", checklist)
        # Dismissal is a host decision, not a per-browser one, so it must go to
        # the server rather than to local storage.
        self.assertNotIn("localStorage", checklist)
        self.assertIn(
            'api("POST", "/v1/workspace/getting-started/dismiss")',
            checklist,
        )
        self.assertIn("workspaceStatus.dismissed === true", checklist)
        self.assertIn(
            'workspaceStatus = await api("GET", "/v1/workspace/getting-started")',
            checklist,
        )
        self.assertIn('data-guide="openai"', checklist)
        self.assertIn('data-guide="claude"', checklist)
        self.assertIn('data-guide="bedrock"', checklist)
        self.assertIn('data-action="getting-started-prompt"', checklist)
        self.assertIn("Ask your agent to create an app", checklist)
        self.assertIn("Ask your agent to create a schedule", checklist)
        self.assertIn("Create a daily 09:00 UTC schedule", checklist)
        self.assertNotIn("Create a weekday", checklist)
        self.assertIn("refreshGettingStarted()", app)
        self.assertIn("window.KernChat.newThread(prompt)", app)
        self.assertIn('newThread(prompt = "")', chat)
        # A starter prompt replaces an unsent draft outright; no confirmation.
        self.assertNotIn("Replace your unsent new-chat draft", chat)
        self.assertIn("saveComposerDraft();", chat)
        self.assertIn(".getting-started-progress", css)
        self.assertIn(".getting-started-step.complete", css)
        self.assertNotIn("style=", checklist)

    def test_icons_have_intrinsic_sizes_and_share_the_favicon_asset(self) -> None:
        runtime = Path(__file__).parents[1] / "host/runtime/admin_api"
        html = (runtime / "admin_ui/index.html").read_text()
        css = (runtime / "admin_ui/admin_ui.css").read_text()
        app_js = (runtime / "admin_ui" / "app.js").read_text()

        favicon_src = '/favicon.svg'
        self.assertIn(f'<img class="brand-mark" width="30" height="30" src="{favicon_src}" alt="">', html)
        self.assertIn(f'<img class="login-mark" width="44" height="44" src="{favicon_src}" alt="">', html)
        # Home owns integration and diagnostic navigation. Memory and
        # Schedules remain first-class Workspace destinations.
        self.assertEqual(html.count('<svg width="19" height="19" viewBox="0 0 20 20"'), 3)
        self.assertIn('/favicon.svg', html)
        self.assertIn('/favicon.ico', html)
        self.assertIn('/admin_ui.css', html)
        self.assertIn('<script type="module" src="/admin_ui/app.js"></script>', html)
        self.assertIn('<link rel="manifest" href="/manifest.webmanifest">', html)
        self.assertIn('<link rel="apple-touch-icon" sizes="180x180" href="/icons/kern-180.png">', html)
        self.assertIn(".brand-mark { display: block; flex: 0 0 30px; height: 30px; width: 30px; }", css)
        self.assertIn(".login-mark { display: inline-block; height: 44px; margin-bottom: 0.4rem; width: 44px; }", css)
        self.assertIn(".tab-button svg { display: block; height: 19px; width: 19px; }", css)
        self.assertIn(".memory-swap-values", css)
        self.assertIn("button.icon-button svg", css)
        self.assertEqual(html.count('<span class="home-card-icon'), 6)
        self.assertEqual(html.count('<svg viewBox="0 0 20 20" aria-hidden="true">'), 6)
        self.assertIn(".home-card-icon svg {", css)
        self.assertIn("const runtime = button.dataset.runtime;", app_js)
        self.assertIn('"start-login": () => startLogin(runtime)', app_js)
        for name, size in (
            ("kern-180.png", 180), ("kern-192.png", 192),
            ("kern-512.png", 512), ("kern-maskable-512.png", 512),
        ):
            data = (runtime / "admin_ui" / "icons" / name).read_bytes()
            self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
            self.assertEqual(int.from_bytes(data[16:20], "big"), size)
            self.assertEqual(int.from_bytes(data[20:24], "big"), size)
            # Installed-app surfaces choose their own icon mask. Keeping the
            # source PNGs opaque prevents a white page background from showing
            # through the artwork's rounded corners.
            self.assertEqual(data[25], 2)
        self.assertNotIn("animation: panel-in", css)
        self.assertIn("position: fixed", css)
        self.assertNotIn('id="tab-processes"', html)
        self.assertIn('data-action="open-home-view" data-view="processes"', html)
        self.assertIn('id="processes"', html)
        self.assertIn('id="file-image" class="file-image" alt="" hidden', html)
        self.assertIn("img-src 'self' data: blob:", admin_api.SECURITY_HEADERS["Content-Security-Policy"])
        self.assertIn("style-src 'self' blob:", admin_api.SECURITY_HEADERS["Content-Security-Policy"])
        self.assertIn('id="chat-nav-items"', html)
        self.assertIn('id="web-apps-nav-items"', html)
        self.assertIn("window.KernHost", app_js)
        self.assertIn("attachShadow", app_js)
        files_js = (runtime / "admin_ui" / "files.js").read_text()
        self.assertNotIn(".innerHTML", files_js)
        self.assertIn("button.textContent", files_js)
        logos_js = (runtime / "admin_ui" / "connection_guide.js").read_text()
        self.assertIn('openai: `<svg viewBox="0 0 512 512">', logos_js)
        self.assertNotIn('openai: `<svg viewBox="0 0 32 32"><g', logos_js)
        self.assertIn('"tool:ibkr": `<svg viewBox="0 0 775 1511">', logos_js)
        self.assertNotIn('m6 18 5.4-5', logos_js)
        self.assertIn("video.src = activeFileUrl", files_js)
        self.assertIn("image.src = activeFileUrl", files_js)
        self.assertIn('["image/jpeg", "image/png", "image/webp"]', files_js)
        self.assertNotIn("window.open", files_js)
        self.assertNotIn("location.", files_js)

    def test_workspace_auth_uses_one_fixed_peer_uid(self) -> None:
        class User:
            pw_uid = 12345

        with patch("host.runtime.admin_api.workspace_api.pwd.getpwnam", return_value=User()):
            self.assertEqual(workspace_admin_api._workspace_uid(), 12345)

    def test_workspace_auth_rejects_a_non_service_peer(self) -> None:
        class Request:
            pass

        class Handler:
            request = Request()

        with (
            patch("host.runtime.admin_api.workspace_api._peer_uid", return_value=12346),
            patch("host.runtime.admin_api.workspace_api._workspace_uid", return_value=12345),
            self.assertRaises(admin_api.ApiError) as error,
        ):
            workspace_admin_api.Handler._authenticate_workspace(Handler())  # type: ignore[arg-type]

        self.assertEqual(error.exception.status, HTTPStatus.UNAUTHORIZED)

    def test_workspace_route_allowlist_contains_thread_and_history_operations(self) -> None:
        allowed = [
            ("GET", "/v1/threads"),
            ("GET", "/v1/threads/thread-1"),
            ("POST", "/v1/threads/thread-1/messages"),
            ("POST", "/v1/threads/thread-1/stop"),
            ("POST", "/v1/threads/thread-1/clear-memory"),
            ("GET", "/v1/threads/thread-1/events"),
            ("POST", "/v1/conversation-history/search"),
            ("POST", "/v1/conversation-history/read"),
        ]

        for method, path in allowed:
            with self.subTest(method=method, path=path):
                workspace_admin_api._require_workspace_route(method, path)

    def test_workspace_route_allowlist_rejects_host_admin_and_removed_task_routes(self) -> None:
        denied = [
            ("POST", "/v1/tasks"),
            ("GET", "/v1/tasks"),
            ("GET", "/v1/tasks/task_1"),
            ("PUT", "/v1/tasks/task_1"),
            ("POST", "/v1/tasks/task_1/cancel"),
            ("POST", "/v1/tasks/task_1/kill"),
            ("POST", "/v1/tasks/task_1/steer"),
            ("GET", "/v1/threads/thread-1/tasks"),
            ("GET", "/v1/health"),
            ("GET", "/v1/network/policy"),
            ("PUT", "/v1/network/policy"),
            ("GET", "/v1/agent-files"),
            ("GET", "/v1/tools"),
            ("POST", "/v1/tools/brave_search/enable"),
            ("GET", "/v1/tools/brave_search/approvals"),
            ("GET", "/v1/network-tools/github-credential"),
            ("GET", "/v1/agent-runtime/account"),
            ("GET", "/v1/events"),
        ]

        for method, path in denied:
            with self.subTest(method=method, path=path):
                with self.assertRaises(admin_api.ApiError) as error:
                    workspace_admin_api._require_workspace_route(method, path)
                self.assertEqual(error.exception.status, HTTPStatus.FORBIDDEN)

    def test_workspace_thread_ids_pass_through_unchanged(self) -> None:
        with patch(
            "host.runtime.admin_api.workspace_api.admin_api.route",
            return_value={"thread": {"thread_id": "thread-1", "status": "running"}},
        ) as route:
            response = workspace_admin_api.route_workspace_request(
                "GET", "/v1/threads/thread-1", {}, None
            )
        route.assert_called_once_with(
            "GET", "/v1/threads/thread-1", {}, None,
            principal=admin_api.WorkspacePrincipal(),
        )
        self.assertEqual(response["thread"]["thread_id"], "thread-1")

    def test_workspace_thread_detail_and_stop_use_direct_thread_path(self) -> None:
        with patch(
            "host.runtime.admin_api.workspace_api.admin_api.route",
            return_value={"thread": {"thread_id": "thread-1", "status": "idle"}},
        ) as route:
            response = workspace_admin_api.route_workspace_request(
                "GET", "/v1/threads/thread-1", {}, None
            )

        route.assert_called_once_with(
            "GET",
            "/v1/threads/thread-1",
            {},
            None,
            principal=admin_api.WorkspacePrincipal(),
        )
        self.assertEqual(response["thread"], {"thread_id": "thread-1", "status": "idle"})

        with patch(
            "host.runtime.admin_api.workspace_api.admin_api.route",
            return_value={"status": "accepted"},
        ) as stop_route:
            response = workspace_admin_api.route_workspace_request(
                "POST", "/v1/threads/thread-1/stop", {}, None
            )

        stop_route.assert_called_once_with(
            "POST",
            "/v1/threads/thread-1/stop",
            {},
            None,
            principal=admin_api.WorkspacePrincipal(),
        )
        self.assertEqual(response, {"status": "accepted"})

    def test_workspace_thread_events_use_direct_thread_path(self) -> None:
        with patch(
            "host.runtime.admin_api.workspace_api.admin_api.route",
            return_value={"events": [{"seq": 4, "thread_id": "thread-1", "event_type": "thread.message"}]},
        ) as route:
            response = workspace_admin_api.route_workspace_request(
                "GET", "/v1/threads/thread-1/events", {"since": ["2"]}, None
            )

        route.assert_called_once_with(
            "GET",
            "/v1/threads/thread-1/events",
            {"since": ["2"]},
            None,
            principal=admin_api.WorkspacePrincipal(),
        )
        self.assertEqual(response["events"][0]["seq"], 4)
        self.assertEqual(response["events"][0]["thread_id"], "thread-1")

    def test_thread_events_bound_page_and_message_bytes_before_workspace_proxying(self) -> None:
        message_bytes = 12 * 1024
        events = [
            {
                "seq": seq,
                "thread_id": "thread-1",
                "event_type": "thread.message",
                "payload": {
                    "message": "\x01" * (message_bytes + 1),
                    "error_message": "\x01" * (message_bytes + 1),
                    "source": "agent",
                },
            }
            for seq in range(1, 6)
        ]
        with patch(
            "host.runtime.admin_api.service.state.page_thread_events",
            return_value=events,
        ) as page:
            response = admin_api.thread_route(
                "GET",
                "/v1/threads/thread-1/events",
                {
                    "since": ["2"],
                    "limit": ["5"],
                    "message_bytes": [str(message_bytes)],
                },
                None,
            )

        page.assert_called_once_with("thread-1", 2, 5, before=None)
        payload = response["events"][0]["payload"]
        self.assertLessEqual(len(payload["message"].encode()), message_bytes)
        self.assertTrue(payload["message"].endswith("… (truncated)"))
        self.assertLessEqual(len(payload["error_message"].encode()), message_bytes)
        self.assertLess(
            len(json.dumps(response, sort_keys=True).encode()),
            admin_api.MAX_REQUEST_BODY_BYTES,
        )

    def test_thread_events_support_before_but_reject_combined_cursors(self) -> None:
        with patch(
            "host.runtime.admin_api.service.state.page_thread_events",
            return_value=[],
        ) as page:
            self.assertEqual(
                admin_api.thread_route(
                    "GET",
                    "/v1/threads/thread-1/events",
                    {"before": ["42"], "limit": ["5"]},
                    None,
                ),
                {"events": []},
            )

        page.assert_called_once_with("thread-1", None, 5, before=42)
        with self.assertRaises(admin_api.ApiError) as error:
            admin_api.thread_route(
                "GET",
                "/v1/threads/thread-1/events",
                {"since": ["2"], "before": ["42"]},
                None,
            )
        self.assertEqual(error.exception.status, HTTPStatus.BAD_REQUEST)

    def test_thread_events_can_filter_to_display_event_types(self) -> None:
        with patch(
            "host.runtime.admin_api.service.state.page_thread_events",
            return_value=[],
        ) as page:
            self.assertEqual(
                admin_api.thread_route(
                    "GET",
                    "/v1/threads/thread-1/events",
                    {
                        "limit": ["6"],
                        "event_type": [
                            "thread.message",
                            "thread.error",
                            "thread.stopped",
                        ],
                    },
                    None,
                ),
                {"events": []},
            )

        page.assert_called_once_with(
            "thread-1",
            None,
            6,
            before=None,
            event_types=(
                "thread.message",
                "thread.error",
                "thread.stopped",
            ),
        )
        with self.assertRaises(admin_api.ApiError) as error:
            admin_api.thread_route(
                "GET",
                "/v1/threads/thread-1/events",
                {"event_type": ["turn.started"]},
                None,
            )
        self.assertEqual(error.exception.status, HTTPStatus.BAD_REQUEST)

    def test_thread_events_bound_nested_activity_output_without_dropping_event(self) -> None:
        message_bytes = 120 * 1024
        events = [{
            "seq": 1,
            "thread_id": "thread-1",
            "event_type": "thread.activity",
            "payload": {
                "activity": {
                    "activity_id": "command-1",
                    "kind": "command",
                    "phase": "completed",
                    "title": "large command",
                    "detail": "d" * (40 * 1024),
                    "output": "o" * (200 * 1024),
                }
            },
        }]
        with patch(
            "host.runtime.admin_api.service.state.page_thread_events",
            return_value=events,
        ):
            response = admin_api.thread_route(
                "GET",
                "/v1/threads/thread-1/events",
                {"limit": ["1"], "message_bytes": [str(message_bytes)]},
                None,
            )

        activity = response["events"][0]["payload"]["activity"]
        self.assertEqual(activity["activity_id"], "command-1")
        self.assertTrue(activity["detail"].endswith("… (truncated)"))
        self.assertTrue(activity["output"].endswith("… (truncated)"))
        self.assertLess(len(json.dumps(response).encode()), admin_api.MAX_REQUEST_BODY_BYTES)

    def test_six_large_activity_events_leave_room_for_wire_metadata(self) -> None:
        message_bytes = 120 * 1024
        events = [
            {
                "seq": seq,
                "thread_id": "thread-1",
                "event_type": "thread.activity",
                "payload": {
                    "activity": {
                        "provider": "codex",
                        "activity_id": "😀" * 512,
                        "kind": "command",
                        "phase": "completed",
                        "title": "😀" * 512,
                        "status": "completed",
                        "detail": "d" * (40 * 1024),
                        "output": "o" * (200 * 1024),
                    }
                },
            }
            for seq in range(6)
        ]
        with patch(
            "host.runtime.admin_api.service.state.page_thread_events",
            return_value=events,
        ):
            response = admin_api.thread_route(
                "GET",
                "/v1/threads/thread-1/events",
                {"limit": ["6"], "message_bytes": [str(message_bytes)]},
                None,
            )

        self.assertEqual(len(response["events"]), 6)
        self.assertLess(
            len(json.dumps(response).encode()),
            admin_api.MAX_REQUEST_BODY_BYTES,
        )

    def test_large_event_page_stays_below_bridge_cap_after_json_escaping(self) -> None:
        message_bytes = 120 * 1024
        events = [
            {
                "seq": seq,
                "thread_id": "thread-1",
                "event_type": "thread.message",
                "payload": {"message": "\x01" * message_bytes, "source": "agent"},
            }
            for seq in range(8)
        ]
        with patch(
            "host.runtime.admin_api.service.state.page_thread_events",
            return_value=events,
        ):
            response = admin_api.thread_route(
                "GET",
                "/v1/threads/thread-1/events",
                {"limit": ["8"], "message_bytes": [str(message_bytes)]},
                None,
            )

        self.assertEqual(len(response["events"]), 8)
        self.assertTrue(all(
            event["payload"]["message"].endswith("… (truncated)")
            for event in response["events"]
        ))
        self.assertLess(len(json.dumps(response).encode()), admin_api.MAX_REQUEST_BODY_BYTES)

    def test_large_non_ascii_event_page_uses_wire_json_size(self) -> None:
        message_bytes = 120 * 1024
        events = [
            {
                "seq": seq,
                "thread_id": "thread-1",
                "event_type": "thread.message",
                "payload": {"message": "😀" * message_bytes, "source": "agent"},
            }
            for seq in range(8)
        ]
        with patch(
            "host.runtime.admin_api.service.state.page_thread_events",
            return_value=events,
        ):
            response = admin_api.thread_route(
                "GET",
                "/v1/threads/thread-1/events",
                {"limit": ["8"], "message_bytes": [str(message_bytes)]},
                None,
            )

        self.assertTrue(all(
            event["payload"]["message"].endswith("… (truncated)")
            for event in response["events"]
        ))
        self.assertLess(len(json.dumps(response).encode()), admin_api.MAX_REQUEST_BODY_BYTES)

    def test_conversation_search_projects_bounded_public_matches_and_cursor(self) -> None:
        rows = [
            {
                "seq": 8,
                "event_id": "event_8",
                "timestamp": "2026-07-01T00:00:00Z",
                "thread_id": "thread-2",
                "source": "agent",
                "search_rank": 0.5,
                "excerpt": "x" * 3000,
                "excerpt_truncated": True,
            },
            {
                "seq": 7,
                "event_id": "event_7",
                "timestamp": "2026-06-30T00:00:00Z",
                "thread_id": "thread-1",
                "source": "user",
                "search_rank": 0.25,
                "excerpt": "older",
                "excerpt_truncated": False,
            },
        ]
        with (
            patch.object(admin_api.state, "search_thread_messages", return_value=rows) as search,
            patch.object(
                admin_api.embedding_client,
                "embed_texts",
                side_effect=admin_api.embedding_client.EmbeddingError("offline"),
            ),
        ):
            response = admin_api.search_conversation_history(
                {
                    "query": " tunnel status ",
                    "thread_id": "app-2",
                    "limit": 1,
                }
            )

        search.assert_called_once_with(
            ("tunnel status",),
            from_timestamp=None,
            to_timestamp=None,
            thread_id="app-2",
            sources=("user", "agent"),
            # One past the window, to detect whether a lexical tail exists.
            limit=admin_api.CONVERSATION_SEMANTIC_CANDIDATES + 1,
            before=None,
            max_seq=10**12,
        )
        self.assertEqual(response["matches"][0]["role"], "assistant")
        self.assertTrue(response["matches"][0]["excerpt_truncated"])
        self.assertLessEqual(
            len(json.dumps(response["matches"][0]["excerpt"]).encode()),
            admin_api.CONVERSATION_SEARCH_EXCERPT_BYTES,
        )
        self.assertEqual(response["provenance"], "retained_conversation_history")
        self.assertEqual(response["trust"], "untrusted")
        self.assertEqual(response["instruction_authority"], "none")
        self.assertIsInstance(response["next_cursor"], str)

    def test_conversation_search_normalizes_filters_and_resumes_its_cursor(self) -> None:
        row = {
            "seq": 8,
            "event_id": "event_8",
            "timestamp": "2026-07-01T00:00:00Z",
            "thread_id": "schedule-daily",
            "source": "agent",
            "search_rank": 0.5,
            "excerpt": "A degraded tunnel can still serve traffic.",
            "excerpt_truncated": False,
        }
        with (
            patch.object(
                admin_api.state,
                "search_thread_messages",
                side_effect=[[row, {**row, "seq": 7}], []],
            ) as search,
            patch.object(
                admin_api.embedding_client,
                "embed_texts",
                side_effect=admin_api.embedding_client.EmbeddingError("offline"),
            ),
        ):
            request = {
                "query": " degraded tunnel ",
                "query_variants": ["serving traffic", "serving traffic"],
                "from": "2026-07-01T01:00:00+01:00",
                "roles": ["assistant"],
                "limit": 1,
            }
            first = admin_api.search_conversation_history(request)
            second = admin_api.search_conversation_history(
                {**request, "cursor": first["next_cursor"]}
            )

        self.assertIsNone(second["next_cursor"])
        first_call = search.call_args_list[0]
        self.assertEqual(first_call.args[0], ("degraded tunnel", "serving traffic"))
        self.assertEqual(first_call.kwargs["from_timestamp"], "2026-07-01T00:00:00Z")
        self.assertEqual(first_call.kwargs["sources"], ("agent",))
        self.assertEqual(search.call_args_list[1].kwargs["before"], None)

    def test_legacy_rank_cursor_stays_on_plain_lexical_pagination(self) -> None:
        row = {
            "seq": 7,
            "event_id": "event_7",
            "timestamp": "2026-07-01T00:00:00Z",
            "thread_id": "thread-1",
            "source": "user",
            "search_rank": 0.4,
            "excerpt": "deployment detail",
            "excerpt_truncated": False,
        }
        fingerprint = admin_api._conversation_search_fingerprint(
            ["deployment"], None, None, None, ["user", "assistant"]
        )
        legacy_cursor = admin_api._encode_conversation_search_cursor(
            fingerprint,
            True,
            {"rank": 0.5, "seq": 8},
        )
        with (
            patch.object(
                admin_api.state,
                "search_thread_messages",
                return_value=[row, {**row, "seq": 6}],
            ) as search,
            patch.object(admin_api.embedding_client, "embed_texts") as embed,
        ):
            response = admin_api.search_conversation_history(
                {"query": "deployment", "limit": 1, "cursor": legacy_cursor}
            )

        embed.assert_not_called()
        self.assertEqual(search.call_args.kwargs["before"], (0.5, 8))
        self.assertEqual(search.call_args.kwargs["exclude_seqs"], ())
        decoded = admin_api._decode_conversation_search_cursor(
            response["next_cursor"], fingerprint, True
        )
        self.assertIsNotNone(decoded)
        self.assertEqual(decoded[0], "rank")

    def test_conversation_search_rejects_missing_filter_and_cursor_reuse(self) -> None:
        with self.assertRaises(admin_api.ApiError) as missing:
            admin_api.search_conversation_history({})
        self.assertEqual(missing.exception.status, HTTPStatus.BAD_REQUEST)

        with self.assertRaises(admin_api.ApiError) as nul:
            admin_api.search_conversation_history({"query": "tunnel\x00status"})
        self.assertIn("must not contain NUL", nul.exception.message)

        cursor = admin_api._encode_conversation_search_cursor(
            "different",
            True,
            {"rank": 1.0, "seq": 2},
        )
        with self.assertRaises(admin_api.ApiError) as invalid:
            admin_api.search_conversation_history(
                {"query": "deployment", "cursor": cursor}
            )
        self.assertIn("different search filters", invalid.exception.message)

        fingerprint = admin_api._conversation_search_fingerprint(
            ["deployment"], None, None, None, ["user", "assistant"]
        )
        oversized_cursor = admin_api._encode_conversation_search_cursor(
            fingerprint,
            True,
            {"rank": 1.0, "seq": admin_api.POSTGRES_BIGINT_MAX + 1},
        )
        with self.assertRaises(admin_api.ApiError) as oversized:
            admin_api.search_conversation_history(
                {"query": "deployment", "cursor": oversized_cursor}
            )
        self.assertIn("cursor is invalid", oversized.exception.message)

    def test_conversation_search_rejects_hostile_cursor_values(self) -> None:
        rank_fingerprint = admin_api._conversation_search_fingerprint(
            ["deployment"], None, None, None, ["user", "assistant"]
        )
        time_fingerprint = admin_api._conversation_search_fingerprint(
            [], "2026-01-01T00:00:00Z", None, None, ["user", "assistant"]
        )
        cases = (
            (
                {"query": "deployment"},
                admin_api._encode_conversation_search_cursor(
                    rank_fingerprint,
                    True,
                    {"rank": 10**320, "seq": 1},
                ),
            ),
            (
                {"from": "2026-01-01T00:00:00Z"},
                admin_api._encode_conversation_search_cursor(
                    time_fingerprint,
                    False,
                    {"timestamp": "2026-99-31T00:00:00Z", "seq": 1},
                ),
            ),
            (
                {"query": "deployment"},
                admin_api._encode_conversation_search_cursor(
                    rank_fingerprint,
                    True,
                    {
                        "rank": admin_api.CONVERSATION_SEMANTIC_CANDIDATES * 2 + 1,
                        "seq": 1,
                    },
                    mode="hybrid",
                    min_seq=1,
                    max_seq=10,
                    embedding_min_seq=1,
                    embedding_generation=20,
                    semantic_seqs=(),
                ),
            ),
            ({"query": "deployment"}, "not-base64!"),
        )
        with patch.object(admin_api.state, "search_thread_messages") as search:
            for request, cursor in cases:
                with self.subTest(cursor=cursor[:40]), self.assertRaises(
                    admin_api.ApiError
                ) as error:
                    admin_api.search_conversation_history(
                        {**request, "cursor": cursor}
                    )
                self.assertEqual(error.exception.status, HTTPStatus.BAD_REQUEST)

        search.assert_not_called()

    def test_conversation_history_rejects_hostile_public_shapes_before_database(self) -> None:
        search_requests = (
            {"query": {"$ne": None}},
            {"query": "ok", "query_variants": ["ok"] * 9},
            {"query": "ok", "query_variants": [{"nested": "value"}]},
            {"query": "ok", "thread_id": "../thread-1"},
            {"query": "ok", "thread_id": "legacy-thread"},
            {"query": "ok", "roles": ["user", {"role": "assistant"}]},
            {"query": "ok", "limit": float("nan")},
            {"query": "ok", "limit": 10**5_000},
            {"query": "ok", "unexpected": {"deeply": ["nested"]}},
        )
        read_requests = (
            {"thread_id": {"$ne": None}},
            {"thread_id": "../thread-1"},
            {"thread_id": "legacy-thread"},
            {"thread_id": "thread-1", "before": "event_0"},
            {"thread_id": "thread-1", "include_activity": 1},
            {"thread_id": "thread-1", "limit": float("inf")},
            {"thread_id": "thread-1", "unexpected": []},
        )
        with (
            patch.object(admin_api.state, "search_thread_messages") as search,
            patch.object(admin_api.state, "page_thread_events") as page,
            patch.object(admin_api.state, "page_thread_events_around") as around,
        ):
            for request in search_requests:
                with self.subTest(search=request), self.assertRaises(
                    admin_api.ApiError
                ) as error:
                    admin_api.search_conversation_history(request)
                self.assertEqual(error.exception.status, HTTPStatus.BAD_REQUEST)
            for request in read_requests:
                with self.subTest(read=request), self.assertRaises(
                    admin_api.ApiError
                ) as error:
                    admin_api.read_conversation_history(request)
                self.assertEqual(error.exception.status, HTTPStatus.BAD_REQUEST)

        search.assert_not_called()
        page.assert_not_called()
        around.assert_not_called()

    def test_conversation_search_rejects_timestamps_outside_utc_range(self) -> None:
        for timestamp in (
            "9999-12-31T23:59:59.9Z",
            "0001-01-01T00:00:00+14:00",
            "2026-01-01 00:00:00Z",
            "2026-01-01T00:00:00+00:00:00",
        ):
            with (
                self.subTest(timestamp=timestamp),
                patch.object(admin_api.state, "search_thread_messages") as search,
                self.assertRaises(admin_api.ApiError) as error,
            ):
                admin_api.search_conversation_history({"from": timestamp})

            self.assertEqual(error.exception.status, HTTPStatus.BAD_REQUEST)
            self.assertIn("RFC 3339", error.exception.message)
            search.assert_not_called()

    def test_conversation_search_rounds_sub_microsecond_filters_up(self) -> None:
        with patch.object(
            admin_api.state, "search_thread_messages", return_value=[]
        ) as search:
            admin_api.search_conversation_history(
                {
                    "from": "2026-07-01T00:00:00.0000001Z",
                    "to": "2026-07-02T00:00:00.0000000Z",
                }
            )

        self.assertEqual(
            search.call_args.kwargs["from_timestamp"], "2026-07-01T00:00:01Z"
        )
        self.assertEqual(
            search.call_args.kwargs["to_timestamp"], "2026-07-02T00:00:00Z"
        )

    def test_conversation_search_accepts_lowercase_rfc3339_separators(self) -> None:
        with patch.object(
            admin_api.state, "search_thread_messages", return_value=[]
        ) as search:
            admin_api.search_conversation_history(
                {"from": "2026-07-01t01:00:00+01:00", "to": "2026-07-02t00:00:00z"}
            )

        self.assertEqual(
            search.call_args.kwargs["from_timestamp"], "2026-07-01T00:00:00Z"
        )
        self.assertEqual(
            search.call_args.kwargs["to_timestamp"], "2026-07-02T00:00:00Z"
        )

    def test_conversation_search_rejects_non_utf8_public_strings(self) -> None:
        for request in (
            {"query": "\ud800"},
            {"query": "valid", "query_variants": ["\ud800"]},
            {"from": "\ud800"},
            {"query": "valid", "cursor": "\ud800"},
        ):
            with (
                self.subTest(field=next(iter(request))),
                patch.object(admin_api.state, "search_thread_messages") as search,
                self.assertRaises(admin_api.ApiError) as error,
            ):
                admin_api.search_conversation_history(request)

            self.assertEqual(error.exception.status, HTTPStatus.BAD_REQUEST)
            self.assertIn("valid UTF-8", error.exception.message)
            search.assert_not_called()

    def test_conversation_search_fuses_local_semantic_and_lexical_candidates(self) -> None:
        lexical = [
            {
                "seq": 8,
                "event_id": "event_8",
                "timestamp": "2026-07-01T00:00:00Z",
                "thread_id": "thread-2",
                "source": "agent",
                "search_rank": 0.8,
                "excerpt": "[[OAuth]] callback failure",
                "excerpt_truncated": False,
            }
        ]
        semantic = [
            {
                "seq": 7,
                "event_id": "event_7",
                "timestamp": "2026-06-30T00:00:00Z",
                "thread_id": "thread-1",
                "source": "user",
                "search_rank": 0.9,
                "excerpt": "Could not sign in with the identity provider",
                "excerpt_truncated": False,
            },
            lexical[0],
        ]
        with (
            patch.object(admin_api.state, "search_thread_messages", return_value=lexical) as text_search,
            patch.object(admin_api.embedding_client, "embed_texts", return_value=[[0.0] * 384]) as embed,
            patch.object(admin_api.state, "search_thread_messages_semantic", return_value=semantic) as vector_search,
        ):
            response = admin_api.search_conversation_history(
                {
                    "query": "Why could I not sign in?",
                    "query_variants": ["login problem", "OAuth"],
                    "from": None,
                    "to": None,
                    "thread_id": None,
                    "roles": ["user", "assistant"],
                    "limit": 2,
                }
            )

        embed.assert_called_once_with(["Why could I not sign in?"], kind="query")
        self.assertEqual(text_search.call_args.kwargs["limit"], 201)
        self.assertEqual(
            vector_search.call_args.kwargs["minimum_similarity"],
            admin_api.embedding_client.MINIMUM_SIMILARITY,
        )
        # A hit present in both channels outranks a semantic-only result, and
        # retains the lexical excerpt highlighting.
        self.assertEqual([match["event_id"] for match in response["matches"]], ["event_8", "event_7"])
        self.assertEqual(response["matches"][0]["excerpt"], "[[OAuth]] callback failure")
        self.assertEqual(response["search_mode"], "hybrid")

    def test_conversation_search_falls_back_when_local_model_is_unavailable(self) -> None:
        row = {
            "seq": 3,
            "event_id": "event_3",
            "timestamp": "2026-07-01T00:00:00Z",
            "thread_id": "thread-1",
            "source": "user",
            "search_rank": 0.4,
            "excerpt": "exact deployment identifier",
            "excerpt_truncated": False,
        }
        older = {
            **row,
            "seq": 2,
            "event_id": "event_2",
            "search_rank": 0.3,
        }
        with (
            patch.object(admin_api.state, "search_thread_messages", return_value=[row, older]),
            patch.object(
                admin_api.embedding_client,
                "embed_texts",
                side_effect=admin_api.embedding_client.EmbeddingError("offline"),
            ),
            patch.object(admin_api.state, "search_thread_messages_semantic") as vector_search,
        ):
            response = admin_api.search_conversation_history(
                {"query": "Which deployment did we discuss?", "limit": 1}
            )

        vector_search.assert_not_called()
        self.assertEqual(response["search_mode"], "lexical_fallback")
        self.assertEqual(response["matches"][0]["event_id"], "event_3")
        self.assertIsInstance(response["next_cursor"], str)

    def test_hybrid_cursor_reuses_frozen_semantic_candidates(self) -> None:
        lexical = [
            {
                "seq": seq,
                "event_id": f"event_{seq}",
                "timestamp": "2026-07-01T00:00:00Z",
                "thread_id": "thread-1",
                "source": "user",
                "search_rank": float(seq),
                "excerpt": f"match {seq}",
                "excerpt_truncated": False,
            }
            for seq in (3, 2, 1)
        ]
        semantic = [
            {
                **lexical[0],
                "seq": 10,
                "event_id": "event_10",
                "excerpt": "semantic-only match",
            }
        ]
        request = {"query": "deployment details", "limit": 1}
        with (
            patch.object(
                admin_api.state, "search_thread_messages", return_value=lexical
            ) as text_search,
            patch.object(
                admin_api.embedding_client,
                "embed_texts",
                return_value=[[0.0] * 384],
            ) as embed,
            patch.object(
                admin_api.state,
                "search_thread_messages_semantic",
                return_value=semantic,
            ) as vector_search,
            patch.object(
                admin_api.state,
                "thread_messages_by_seqs",
                return_value=semantic,
            ) as frozen_search,
        ):
            first = admin_api.search_conversation_history(request)
            resumed = admin_api.search_conversation_history(
                {**request, "cursor": first["next_cursor"]}
            )

        self.assertEqual(first["matches"][0]["event_id"], "event_3")
        self.assertEqual(resumed["search_mode"], "hybrid")
        self.assertEqual(resumed["matches"][0]["event_id"], "event_2")
        self.assertEqual(text_search.call_count, 2)
        embed.assert_called_once()
        vector_search.assert_called_once()
        self.assertEqual(frozen_search.call_args.args, ((10,),))
        self.assertEqual(
            frozen_search.call_args.kwargs,
            {
                "from_timestamp": None,
                "to_timestamp": None,
                "thread_id": None,
                "sources": ("user", "agent"),
                "max_seq": 10**12,
            },
        )
        self.assertEqual(
            {call.kwargs["max_seq"] for call in text_search.call_args_list},
            {10**12},
        )
        self.assertEqual(
            {
                call.kwargs["max_embedding_generation"]
                for call in vector_search.call_args_list
            },
            {20},
        )

    def test_conversation_cursor_expires_when_retention_advances(self) -> None:
        row = {
            "seq": 3,
            "event_id": "event_3",
            "timestamp": "2026-07-01T00:00:00Z",
            "thread_id": "thread-1",
            "source": "user",
            "search_rank": 0.4,
            "excerpt": "deployment detail",
            "excerpt_truncated": False,
        }
        request = {"query": "deployment", "limit": 1}
        with (
            patch.object(
                admin_api.state,
                "search_thread_messages",
                return_value=[row, {**row, "seq": 2}],
            ),
            patch.object(
                admin_api.embedding_client,
                "embed_texts",
                return_value=[[0.0] * 384],
            ),
            patch.object(
                admin_api.state,
                "search_thread_messages_semantic",
                return_value=[],
            ),
            patch.object(
                admin_api.state,
                "conversation_search_retention",
                side_effect=[(1, 1), (1, 1), (2, 1)],
            ),
        ):
            first = admin_api.search_conversation_history(request)
            with self.assertRaises(admin_api.ApiError) as expired:
                admin_api.search_conversation_history(
                    {**request, "cursor": first["next_cursor"]}
                )

        self.assertEqual(expired.exception.status, HTTPStatus.CONFLICT)
        self.assertIn("restart", expired.exception.message)

    def test_hybrid_search_transitions_to_the_lexical_tail(self) -> None:
        lexical = [
            {
                "seq": 400 - index,
                "event_id": f"event_{400 - index}",
                "timestamp": "2026-07-01T00:00:00Z",
                "thread_id": "thread-1",
                "source": "user",
                "search_rank": 1.0 - index / 1000,
                "excerpt": f"match {index}",
                "excerpt_truncated": False,
            }
            # One past the candidate window, so the service sees a real tail.
            for index in range(admin_api.CONVERSATION_SEMANTIC_CANDIDATES + 1)
        ]
        older = {
            **lexical[-1],
            "seq": 199,
            "event_id": "event_199",
            "search_rank": 0.1,
            "excerpt": "older exact match",
        }

        def text_search(*_args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            return [older] if kwargs["before"] is not None else lexical

        request = {
            "query": "What exact match was discussed?",
            "query_variants": ["exact match"],
            "from": None,
            "to": None,
            "thread_id": None,
            "roles": ["user", "assistant"],
            "limit": 25,
        }
        with (
            patch.object(admin_api.state, "search_thread_messages", side_effect=text_search),
            patch.object(admin_api.embedding_client, "embed_texts", return_value=[[0.0] * 384]),
            patch.object(admin_api.state, "search_thread_messages_semantic", return_value=[]),
        ):
            response = admin_api.search_conversation_history(request)
            for _page in range(7):
                response = admin_api.search_conversation_history(
                    {**request, "cursor": response["next_cursor"]}
                )

            tail = response["next_cursor"]
            final = admin_api.search_conversation_history(
                {**request, "cursor": tail}
            )

        self.assertEqual(final["search_mode"], "lexical")
        self.assertEqual(final["matches"][0]["event_id"], "event_199")

    def test_fallback_transitions_to_an_unfiltered_lexical_tail(self) -> None:
        """Inference recovery must not hide an unseen deep lexical match."""
        lexical = [
            {
                "seq": 400 - index,
                "event_id": f"event_{400 - index}",
                "timestamp": "2026-07-01T00:00:00Z",
                "thread_id": "thread-1",
                "source": "user",
                "search_rank": 1.0 - index / 1000,
                "excerpt": f"match {index}",
                "excerpt_truncated": False,
            }
            for index in range(admin_api.CONVERSATION_SEMANTIC_CANDIDATES + 1)
        ]
        older = {
            **lexical[-1],
            "seq": 199,
            "event_id": "event_199",
            "search_rank": 0.1,
            "excerpt": "unseen deep lexical match",
        }

        def text_search(*_args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            return [older] if kwargs["before"] is not None else lexical

        request = {
            "query": "exact match",
            "roles": ["user", "assistant"],
            "limit": 25,
        }
        unavailable = admin_api.embedding_client.EmbeddingError("offline")
        with (
            patch.object(admin_api.state, "search_thread_messages", side_effect=text_search) as search,
            patch.object(
                admin_api.embedding_client,
                "embed_texts",
                side_effect=[unavailable, [[0.0] * 384]],
            ) as embed,
        ):
            first = admin_api.search_conversation_history(request)
            fingerprint = admin_api._conversation_search_fingerprint(
                ["exact match"], None, None, None, ["user", "assistant"]
            )
            decoded = None
            while first["next_cursor"] is not None:
                decoded = admin_api._decode_conversation_search_cursor(
                    first["next_cursor"], fingerprint, True
                )
                if decoded[0] == "rank":
                    break
                first = admin_api.search_conversation_history(
                    {**request, "cursor": first["next_cursor"]}
                )
            second = admin_api.search_conversation_history(
                {**request, "cursor": first["next_cursor"]}
            )

        self.assertIsNotNone(decoded)
        self.assertEqual(decoded[0], "rank")
        self.assertEqual(second["matches"][0]["event_id"], "event_199")
        self.assertEqual(search.call_args_list[-1].kwargs["exclude_seqs"], ())
        self.assertEqual(embed.call_count, 1)

    def test_in_progress_hybrid_cursor_does_not_require_inference(self) -> None:
        fingerprint = admin_api._conversation_search_fingerprint(
            ["deployment"], None, None, None, ["user", "assistant"]
        )
        cursor = admin_api._encode_conversation_search_cursor(
            fingerprint,
            True,
            {"rank": 25, "seq": 1},
            mode="hybrid",
            min_seq=1,
            max_seq=10,
            embedding_min_seq=1,
            embedding_generation=20,
            semantic_seqs=(7,),
        )
        semantic = {
            "seq": 7,
            "event_id": "event_7",
            "timestamp": "2026-07-01T00:00:00Z",
            "thread_id": "thread-1",
            "source": "user",
            "search_rank": 0.0,
            "excerpt": "frozen match",
            "excerpt_truncated": False,
        }
        with (
            patch.object(admin_api.state, "search_thread_messages", return_value=[]),
            patch.object(
                admin_api.embedding_client,
                "embed_texts",
                side_effect=admin_api.embedding_client.EmbeddingError("offline"),
            ) as embed,
            patch.object(
                admin_api.state, "thread_messages_by_seqs", return_value=[semantic]
            ) as frozen_search,
        ):
            response = admin_api.search_conversation_history(
                {"query": "deployment", "cursor": cursor}
            )

        embed.assert_not_called()
        self.assertEqual(frozen_search.call_args.args, ((7,),))
        self.assertEqual(response["search_mode"], "hybrid")

    def test_forged_frozen_ids_cannot_cross_search_filters(self) -> None:
        request = {
            "query": "deployment",
            "thread_id": "thread-1",
            "roles": ["user"],
        }
        fingerprint = admin_api._conversation_search_fingerprint(
            ["deployment"], None, None, "thread-1", ["user"]
        )
        cursor = admin_api._encode_conversation_search_cursor(
            fingerprint,
            True,
            {"rank": 1, "seq": 1},
            mode="hybrid",
            min_seq=1,
            max_seq=10,
            embedding_min_seq=1,
            embedding_generation=20,
            semantic_seqs=(7,),
        )
        with (
            patch.object(admin_api.state, "search_thread_messages", return_value=[]),
            # A forged id for another thread/role is removed by the database
            # lookup after the original request filters are reapplied.
            patch.object(
                admin_api.state, "thread_messages_by_seqs", return_value=[]
            ) as frozen_search,
            self.assertRaises(admin_api.ApiError) as raised,
        ):
            admin_api.search_conversation_history({**request, "cursor": cursor})

        self.assertEqual(raised.exception.status, HTTPStatus.BAD_REQUEST)
        self.assertEqual(
            frozen_search.call_args.kwargs["sources"],
            ("user",),
        )
        self.assertEqual(frozen_search.call_args.kwargs["thread_id"], "thread-1")

    def test_frozen_semantic_ids_are_authenticated_by_the_cursor(self) -> None:
        fingerprint = admin_api._conversation_search_fingerprint(
            ["deployment"], None, None, None, ["user", "assistant"]
        )
        cursor = admin_api._encode_conversation_search_cursor(
            fingerprint,
            True,
            {"rank": 1, "seq": 1},
            mode="hybrid",
            min_seq=1,
            max_seq=10,
            embedding_min_seq=1,
            embedding_generation=20,
            semantic_seqs=(7,),
        )
        padded = cursor + "=" * (-len(cursor) % 4)
        fields = json.loads(base64.urlsafe_b64decode(padded))
        fields[8] = [8]
        forged = base64.urlsafe_b64encode(
            json.dumps(fields, separators=(",", ":")).encode()
        ).decode().rstrip("=")

        with (
            patch.object(admin_api.state, "search_thread_messages") as lexical,
            patch.object(admin_api.state, "thread_messages_by_seqs") as frozen,
            self.assertRaises(admin_api.ApiError) as raised,
        ):
            admin_api.search_conversation_history(
                {"query": "deployment", "cursor": forged}
            )

        self.assertEqual(raised.exception.status, HTTPStatus.BAD_REQUEST)
        lexical.assert_not_called()
        frozen.assert_not_called()

    def test_hybrid_search_ends_when_matches_stop_at_the_candidate_window(self) -> None:
        # Exactly CONVERSATION_SEMANTIC_CANDIDATES matches means the result set
        # ends at the window rather than continuing below it, so the last fused
        # page must not advertise a lexical continuation that returns nothing.
        lexical = [
            {
                "seq": 400 - index,
                "event_id": f"event_{400 - index}",
                "timestamp": "2026-07-01T00:00:00Z",
                "thread_id": "thread-1",
                "source": "user",
                "search_rank": 1.0 - index / 1000,
                "excerpt": f"match {index}",
                "excerpt_truncated": False,
            }
            for index in range(admin_api.CONVERSATION_SEMANTIC_CANDIDATES)
        ]

        def text_search(*_args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            return [] if kwargs["before"] is not None else lexical

        request = {
            "query": "What exact match was discussed?",
            "query_variants": ["exact match"],
            "from": None,
            "to": None,
            "thread_id": None,
            "roles": ["user", "assistant"],
            "limit": 25,
        }
        with (
            patch.object(admin_api.state, "search_thread_messages", side_effect=text_search),
            patch.object(admin_api.embedding_client, "embed_texts", return_value=[[0.0] * 384]),
            patch.object(admin_api.state, "search_thread_messages_semantic", return_value=[]),
        ):
            response = admin_api.search_conversation_history(request)
            pages = 1
            while response.get("next_cursor"):
                response = admin_api.search_conversation_history(
                    {**request, "cursor": response["next_cursor"]}
                )
                pages += 1
                self.assertTrue(response["matches"], "paged into an empty result page")
                self.assertLessEqual(pages, 16, "pagination did not terminate")

        self.assertEqual(pages, admin_api.CONVERSATION_SEMANTIC_CANDIDATES // 25)

    def test_embedding_passages_use_the_full_utf8_budget(self) -> None:
        # embed_texts validates UTF-8 bytes, so passages must be clipped by the
        # same measure. Budgeting against JSON-escaped size would spend roughly
        # three bytes per non-ASCII byte and drop the tail of a valid message.
        limit = admin_api.embedding_client.MAX_TEXT_BYTES
        message = "🙂" * (limit // 2)
        sent: list[list[str]] = []

        def embed(texts: list[str], **_kwargs: Any) -> list[list[float]]:
            sent.append(texts)
            raise admin_api.embedding_client.EmbeddingError("stop the loop")

        with (
            patch.object(
                admin_api.state,
                "unembedded_thread_messages",
                return_value=[(1, message)],
            ),
            patch.object(admin_api.embedding_client, "embed_texts", side_effect=embed),
            patch.object(admin_api.time, "sleep", side_effect=RuntimeError("halt")),
            patch.object(admin_api.host_errors, "report_unexpected"),
        ):
            with self.assertRaises(RuntimeError):
                admin_api.embedding_index_loop()

        self.assertTrue(sent, "the indexer never called the embedding client")
        encoded = sent[0][0].encode()
        self.assertLessEqual(len(encoded), limit)
        # Comfortably past the ~1/3 an escaped budget would have allowed.
        self.assertGreater(len(encoded), limit * 3 // 4)

    def test_embedding_index_batches_bound_head_of_line_wait(self) -> None:
        pending = [
            (0, "x" * (15 * 1024)),
            (1, "x" * (15 * 1024)),
            (2, "x" * admin_api.embedding_client.MAX_TEXT_BYTES),
        ]

        bounded = admin_api._bounded_embedding_batch(pending)

        self.assertEqual([seq for seq, _message in bounded], [0, 1])
        self.assertLessEqual(
            sum(len(text.encode()) for _seq, text in bounded),
            admin_api.CONVERSATION_EMBEDDING_BATCH_BYTES,
        )

    def test_hybrid_lexical_tail_uses_frozen_semantic_exclusion(self) -> None:
        fingerprint = admin_api._conversation_search_fingerprint(
            ["deployment"], None, None, None, ["user", "assistant"]
        )
        cursor = admin_api._encode_conversation_search_cursor(
            fingerprint,
            True,
            {"rank": 0.5, "seq": 8},
            mode="lexical",
            min_seq=1,
            max_seq=10,
            embedding_min_seq=1,
            embedding_generation=20,
            semantic_seqs=(7,),
        )
        with (
            patch.object(
                admin_api.embedding_client,
                "embed_texts",
                side_effect=admin_api.embedding_client.EmbeddingError("offline"),
            ),
            patch.object(admin_api.state, "search_thread_messages", return_value=[]) as search,
            patch.object(
                admin_api.state,
                "thread_messages_by_seqs",
                return_value=[
                    {
                        "seq": 7,
                        "event_id": "event_7",
                        "timestamp": "2026-07-01T00:00:00Z",
                        "thread_id": "thread-1",
                        "source": "user",
                        "search_rank": 0.0,
                        "excerpt": "frozen match",
                        "excerpt_truncated": False,
                    }
                ],
            ),
        ):
            response = admin_api.search_conversation_history(
                {"query": "deployment", "cursor": cursor}
            )

        self.assertEqual(response["search_mode"], "lexical")
        self.assertEqual(search.call_args.kwargs["exclude_seqs"], (7,))

    def test_lexical_tail_does_not_repeat_semantic_hits(self) -> None:
        # A message can be a semantic candidate and also match lexically below
        # the fused window. It is returned on a hybrid page; the tail must not
        # hand it back again at its lexical rank.
        window = admin_api.CONVERSATION_SEMANTIC_CANDIDATES
        total = 260

        def row(index: int) -> dict[str, Any]:
            return {
                "seq": 1000 - index,
                "event_id": f"event_{1000 - index}",
                "timestamp": "2026-07-01T00:00:00Z",
                "thread_id": "thread-1",
                "source": "user",
                "search_rank": 1.0 - index / 10000,
                "excerpt": f"match {index}",
                "excerpt_truncated": False,
            }

        # Rank 241 overall: inside the tail, and also a semantic candidate.
        semantic = [row(240)]

        def text_search(*_args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            excluded = set(kwargs.get("exclude_seqs") or ())
            candidates = [row(i) for i in range(total)]
            if kwargs["before"] is not None:
                rank, seq = kwargs["before"]
                candidates = [
                    r
                    for r in candidates
                    if (r["search_rank"], r["seq"]) < (rank, seq)
                ]
            candidates = [r for r in candidates if r["seq"] not in excluded]
            return candidates[: kwargs["limit"]]

        request = {
            "query": "What exact match was discussed?",
            "query_variants": ["exact match"],
            "from": None,
            "to": None,
            "thread_id": None,
            "roles": ["user", "assistant"],
            "limit": 25,
        }
        seen: list[str] = []
        with (
            patch.object(admin_api.state, "search_thread_messages", side_effect=text_search),
            patch.object(admin_api.embedding_client, "embed_texts", return_value=[[0.0] * 384]),
            patch.object(
                admin_api.state, "search_thread_messages_semantic", return_value=semantic
            ),
            patch.object(
                admin_api.state, "thread_messages_by_seqs", return_value=semantic
            ),
        ):
            response = admin_api.search_conversation_history(request)
            seen.extend(m["event_id"] for m in response["matches"])
            while response.get("next_cursor"):
                response = admin_api.search_conversation_history(
                    {**request, "cursor": response["next_cursor"]}
                )
                seen.extend(m["event_id"] for m in response["matches"])
                self.assertLessEqual(len(seen), total + window, "paging did not terminate")

        self.assertIn(f"event_{1000 - 240}", seen)
        self.assertEqual(len(seen), len(set(seen)), "a conversation hit was returned twice")

    def test_conversation_search_reports_the_relevance_work_limit(self) -> None:
        cancelled = pgclient.Error(
            "canceling statement due to statement timeout",
            {"C": "57014"},
        )
        with (
            patch.object(
                admin_api.state,
                "search_thread_messages",
                side_effect=cancelled,
            ),
            self.assertRaises(admin_api.ApiError) as raised,
        ):
            admin_api.search_conversation_history(
                {
                    "query": "the",
                }
            )

        self.assertEqual(raised.exception.status, HTTPStatus.UNPROCESSABLE_ENTITY)
        self.assertIn("narrow the query", raised.exception.message)

    def test_conversation_read_bounds_messages_and_summarizes_activity(self) -> None:
        raw = [
            {
                "seq": 1,
                "event_id": "event_1",
                "timestamp": "2026-07-01T00:00:00Z",
                "event_type": "thread.message",
                "payload": {"source": "user", "message": "\x01" * 100_000},
            },
            {
                "seq": 2,
                "event_id": "event_2",
                "timestamp": "2026-07-01T00:00:01Z",
                "event_type": "thread.activity",
                "payload": {
                    "activity": {
                        "kind": "command",
                        "title": "test",
                        "output": "o" * 10_000,
                        "private_provider_shape": {"large": "secret"},
                    }
                },
            },
        ]
        with (
            patch.object(admin_api.state, "page_thread_events", return_value=raw) as page,
            patch.object(
                admin_api.state,
                "thread_event_page_bounds",
                return_value=(True, True),
            ),
        ):
            response = admin_api.read_conversation_history(
                {
                    "thread_id": "thread-1",
                    "include_activity": True,
                    "limit": 20,
                }
            )

        page.assert_called_once_with(
            "thread-1",
            None,
            20,
            before=None,
            event_types=("thread.message", "thread.activity"),
        )
        self.assertEqual([event["type"] for event in response["events"]], ["message", "activity"])
        self.assertTrue(response["events"][0]["truncated"])
        self.assertNotIn(
            "private_provider_shape", response["events"][1]["activity"]
        )
        self.assertTrue(response["events"][1]["truncated"])
        self.assertEqual(
            (response["older_cursor"], response["newer_cursor"]),
            ("event_1", "event_2"),
        )
        self.assertEqual(response["thread"], {"thread_id": "thread-1"})
        self.assertLess(
            len(json.dumps(response).encode()),
            admin_api.CONVERSATION_RESPONSE_BYTES,
        )

    def test_conversation_read_rejects_an_anchor_outside_the_thread(self) -> None:
        with (
            patch.object(admin_api.state, "page_thread_events_around", return_value=None),
            self.assertRaises(admin_api.ApiError) as error,
        ):
            admin_api.read_conversation_history(
                {
                    "thread_id": "app-1",
                    "around_event_id": "event_99",
                    "include_activity": False,
                }
            )
        self.assertEqual(error.exception.status, HTTPStatus.NOT_FOUND)

    def test_conversation_read_rejects_combined_public_cursors(self) -> None:
        with self.assertRaises(admin_api.ApiError) as error:
            admin_api.read_conversation_history(
                {
                    "thread_id": "app-1",
                    "before": "event_2",
                    "after": "event_3",
                }
            )
        self.assertEqual(error.exception.status, HTTPStatus.BAD_REQUEST)
        self.assertIn("around_event_id", error.exception.message)

    def test_conversation_read_rejects_event_ids_outside_bigint(self) -> None:
        for event_id in (
            "event_9223372036854775808",
            "event_" + "9" * 5_000,
        ):
            with (
                self.subTest(event_id=event_id[:32]),
                patch.object(admin_api.state, "page_thread_events") as page,
                self.assertRaises(admin_api.ApiError) as error,
            ):
                admin_api.read_conversation_history(
                    {"thread_id": "app-1", "before": event_id}
                )

            self.assertEqual(error.exception.status, HTTPStatus.BAD_REQUEST)
            self.assertIn("event id", error.exception.message)
            page.assert_not_called()

    def test_workspace_bulk_thread_list_forwards_optional_prefix_filter(self) -> None:
        with patch(
            "host.runtime.admin_api.workspace_api.admin_api.route",
            return_value={
                "threads": [
                    {"thread_id": "thread-1", "status": "running"},
                    {"thread_id": "thread-2", "status": "idle"},
                ]
            },
        ) as route:
            response = workspace_admin_api.route_workspace_request(
                "GET", "/v1/threads", {"prefix": ["thread-"]}, None
            )

        route.assert_called_once_with(
            "GET",
            "/v1/threads",
            {"prefix": ["thread-"]},
            None,
            principal=admin_api.WorkspacePrincipal(),
        )
        self.assertEqual(
            response["threads"],
            [{"thread_id": "thread-1", "status": "running"}, {"thread_id": "thread-2", "status": "idle"}],
        )

    def test_thread_prefix_is_a_query_filter_not_an_authority_claim(self) -> None:
        self.assertEqual(admin_api._thread_list_prefix({"prefix": ["app-"]}), "app-")
        for invalid in ("legacy", "bad prefix"):
            with self.subTest(invalid=invalid), self.assertRaises(admin_api.ApiError):
                admin_api._thread_list_prefix({"prefix": [invalid]})


    def test_shared_route_requires_a_valid_explicit_principal(self) -> None:
        with self.assertRaises(TypeError):
            admin_api.route("GET", "/v1/health", {}, None)  # type: ignore[call-arg]
        with self.assertRaisesRegex(TypeError, "route principal is invalid"):
            admin_api.route("GET", "/v1/health", {}, None, principal=None)  # type: ignore[arg-type]
        for path in ("/v1/health", "/v1/network/policy", "/v1/tools"):
            with self.subTest(path=path):
                with self.assertRaises(admin_api.ApiError) as denied:
                    admin_api.route(
                        "GET",
                        path,
                        {},
                        None,
                        principal=admin_api.WorkspacePrincipal(),
                    )
                self.assertEqual(denied.exception.status, HTTPStatus.FORBIDDEN)

    def test_product_thread_prefixes_are_available_to_both_principals(self) -> None:
        body = {
            "message": "scheduled work",
            "agent_runtime": "codex",
            "model": "gpt-5.2-codex",
            "effort": "medium",
        }
        paths = (
            "/v1/threads/thread-17/messages",
            "/v1/threads/thread-custom/messages",
            "/v1/threads/app-17/messages",
            "/v1/threads/app-custom/messages",
            "/v1/threads/schedule-17-run-23/messages",
            "/v1/threads/schedule-custom/messages",
        )
        principals = (
            admin_api.OperatorPrincipal("test-session"),
            admin_api.WorkspacePrincipal(),
        )
        for principal in principals:
            for path in paths:
                with self.subTest(principal=principal, path=path), patch(
                    "host.runtime.admin_api.service.thread_route",
                    return_value={"status": "accepted"},
                ) as thread_route:
                    response = admin_api.route(
                        "POST", path, {}, body, principal=principal
                    )
                    self.assertEqual(response, {"status": "accepted"})
                    thread_route.assert_called_once_with("POST", path, {}, body)

    def test_every_thread_route_rejects_an_unprefixed_id(self) -> None:
        for method, path in (
            ("GET", "/v1/threads/legacy"),
            ("POST", "/v1/threads/legacy/messages"),
            ("POST", "/v1/threads/legacy/stop"),
            ("POST", "/v1/threads/legacy/clear-memory"),
            ("GET", "/v1/threads/legacy/events"),
        ):
            with self.subTest(method=method, path=path), self.assertRaises(
                admin_api.ApiError
            ) as rejected:
                admin_api.thread_route(method, path, {}, None)
            self.assertEqual(rejected.exception.status, HTTPStatus.NOT_FOUND)

    def test_http_service_cannot_mint_auth_cookies_or_sessions(self) -> None:
        source = Path(admin_api.__file__).read_text()
        for private_auth_operation in (
            "_create_session",
            "_destroy_session",
            "_session_cookie",
            "_clear_session_cookie",
            "_passkey_login_cookie",
            "_clear_passkey_login_cookie",
            "_completed_session",
        ):
            with self.subTest(operation=private_auth_operation):
                self.assertNotIn(
                    f"admin_auth.{private_auth_operation}",
                    source,
                )
        self.assertIn("admin_auth.begin_password_login(", source)
        self.assertIn("admin_auth.complete_passkey_login(", source)
