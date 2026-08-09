from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import http.client
import io
from http import HTTPStatus
import json
import os
from pathlib import Path
import socket
import socketserver
import stat
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, call, patch
import subprocess
import sys
from typing import Any
import urllib.error
from urllib.parse import quote
import urllib.request

import pg_harness

from host.config import parse_network_controls
from host.network_integrations.github.push_gate import pending as github_pending_push
from host.runtime.admin_api import workspace_proxy as workspace_api_proxy, workspace_api as workspace_admin_api, service as admin_api, github_credential, orchestrator, tools_client as tools_admin_api
from host.runtime.tools import api as tools_api
from host.runtime.core.network_policy import load_policy
from host.runtime.core.state import save_network_policy as save_policy
from host.runtime.core.state import save_github_credential
from host.runtime.core import pgclient, state
from host.runtime.core.state import (
    append_network_event,
    read_claude_account,
    read_openai_account,
    read_proxy_claude_account_id,
    read_proxy_openai_account_id,
    save_config,
    save_claude_account,
    save_openai_account,
)


def save_approved_openai_account(account_id: str, **extra: Any) -> None:
    save_openai_account(
        {"account_id": account_id, "operator_approval": orchestrator.OPENAI_OPERATOR_APPROVAL, **extra}
    )


# One offered (model, effort) pair per runtime for tests that do not care
# which configuration a thread runs.
DEFAULT_SESSION_OPTIONS = {
    "codex": ("gpt-5.6-terra", "high"),
    "claude_code": ("claude-opus-5", "high"),
    "hermes": ("deepseek.v3.2", "high"),
}


def set_runtime_statuses(**statuses: str) -> None:
    """Replace the cached in-memory runtime status records. They are process
    globals, so tests reset them instead of leaking status across cases; a
    runtime left unset reads back as "loading"."""
    with orchestrator._RUNTIME_STATUS_LOCK:
        orchestrator._RUNTIME_STATUSES.clear()
        for runtime_type, status in statuses.items():
            orchestrator._RUNTIME_STATUSES[runtime_type] = {"status": status}


def save_oauth_login(key: str, record: dict[str, Any] | None) -> None:
    with state.mutation() as cur:
        state.set_oauth_login(cur, key, record)


def seed_thread_session(
    thread_id: str,
    agent_runtime: str = "codex",
    *,
    model: str | None = None,
    effort: str | None = None,
    provider_session_id: str | None = None,
    last_used_at: str | None = "2026-06-08T00:00:00Z",
) -> None:
    default_model, default_effort = DEFAULT_SESSION_OPTIONS[agent_runtime]
    with state.mutation() as cur:
        state.save_thread_session(
            cur,
            agent_runtime,
            thread_id,
            provider_session_id,
            last_used_at,
            model or default_model,
            effort or default_effort,
        )


def register_live_turn(
    thread_id: str,
    runtime_type: str = "codex",
    *,
    server: Any = None,
) -> "orchestrator._Turn":
    """Register a fake live turn, the in-memory shape a launched turn leaves
    behind, without spawning any runtime process. Callers rely on setUp
    clearing orchestrator._LIVE between cases."""
    model, effort = DEFAULT_SESSION_OPTIONS[runtime_type]
    config = state.thread_session_config(thread_id)
    if config is None:
        seed_thread_session(thread_id, runtime_type, model=model, effort=effort)
    with state.mutation() as cur:
        run_number = state.start_thread_run(cur, thread_id)
    turn = orchestrator._Turn(runtime_type, thread_id, model, effort, run_number)
    turn.server = server
    turn.phase = orchestrator.ExecutionPhase.RUNNING
    with orchestrator._LIVE_LOCK:
        orchestrator._LIVE[orchestrator._live_key(runtime_type, thread_id)] = turn
    return turn


class RecordingSteerServer:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def steer(self, message: str) -> None:
        self.messages.append(message)

    def interrupt(self) -> None:
        return


def attach_recording_steer_server(
    turn: "orchestrator._Turn",
    _message: str,
    _provider_session_id: str | None,
) -> None:
    turn.server = RecordingSteerServer()
    turn.phase = orchestrator.ExecutionPhase.RUNNING


# The zeroed live-usage payload every accounts read carries when Bedrock has
# no metered requests this month.
EMPTY_BEDROCK_USAGE = {
    "month_to_date": 0.0,
    "currency": "USD",
    "requests": 0,
    "metered_requests": 0,
    "input_tokens": 0,
    "output_tokens": 0,
    "cache_read_tokens": 0,
    "cache_write_tokens": 0,
}


def save_attested_claude_account(account_id: str, **extra: Any) -> None:
    save_claude_account(
        {"account_id": account_id, "identity_attestation": orchestrator.CLAUDE_IDENTITY_ATTESTATION, **extra}
    )


def _without_last_used_at(response: dict[str, Any]) -> dict[str, Any]:
    """A thread response with the wall-clock field dropped, so two responses
    can be compared for equality without racing a second boundary."""
    thread = {
        key: value for key, value in response["thread"].items() if key != "last_used_at"
    }
    return {**response, "thread": thread}


def _session_headers(token: str) -> dict[str, str]:
    """Cookie + CSRF headers for a request authenticated by an admin session."""
    return {"Cookie": f"tc_admin_session={token}", "X-Kern-Csrf": "1"}


def _add_session_auth(request: "urllib.request.Request", token: str) -> None:
    for name, value in _session_headers(token).items():
        request.add_header(name, value)


# The agent's loopback TCP egress is firewalled on Kern hosts, so the test
# admin server listens on a Unix socket and requests reach it through a
# urllib opener that dials that socket. The HTTP request/response layer under
# test (auth headers, cookies, raw request bytes) is unchanged.
ADMIN_TEST_ORIGIN = "http://kern-admin.test"


class UnixSocketHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def get_request(self):  # type: ignore[override]
        request, _ = super().get_request()
        # BaseHTTPRequestHandler (and the login throttle's client key) expect
        # a TCP-shaped (host, port) peer address.
        return request, ("127.0.0.1", 0)


class _UnixSocketHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, host: str, timeout: Any = None) -> None:
        super().__init__(host)
        self._socket_path = socket_path
        self._timeout_value = timeout

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        if isinstance(self._timeout_value, (int, float)):
            self.sock.settimeout(self._timeout_value)
        self.sock.connect(self._socket_path)


class _UnixSocketHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, socket_path: str) -> None:
        super().__init__()
        self._socket_path = socket_path

    def http_open(self, req: Any) -> Any:
        def connection(host: str, timeout: Any = None) -> _UnixSocketHTTPConnection:
            return _UnixSocketHTTPConnection(self._socket_path, host, timeout=timeout)

        return self.do_open(connection, req)


def start_admin_http_server(test: unittest.TestCase) -> str:
    """Serve admin_api.Handler for one test case and route urllib to it.

    Returns the base URL test requests should target."""
    socket_dir = tempfile.TemporaryDirectory()
    test.addCleanup(socket_dir.cleanup)
    socket_path = str(Path(socket_dir.name) / "admin-api.sock")
    server = UnixSocketHTTPServer(socket_path, admin_api.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    test.addCleanup(server.server_close)
    test.addCleanup(server.shutdown)
    urllib.request.install_opener(urllib.request.build_opener(_UnixSocketHTTPHandler(socket_path)))
    test.addCleanup(urllib.request.install_opener, urllib.request.build_opener())
    test.admin_socket_path = socket_path  # type: ignore[attr-defined]
    return ADMIN_TEST_ORIGIN


def raw_admin_request(socket_path: str, request: bytes) -> bytes:
    """One raw HTTP exchange against the test admin server."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(5)
        sock.connect(socket_path)
        try:
            sock.sendall(request)
            sock.shutdown(socket.SHUT_WR)
        except (BrokenPipeError, ConnectionResetError):
            # The server may reject and close before the whole request lands
            # (e.g. an oversized body); its buffered response is still readable.
            pass
        chunks: list[bytes] = []
        while chunk := sock.recv(65536):
            chunks.append(chunk)
        return b"".join(chunks)


class AdminUiStaticTests(unittest.TestCase):
    def test_database_free_admin_ui_contract(self) -> None:
        # The database-backed integration-test class is skipped when local PostgreSQL is
        # unavailable, but this method reads static assets only. Run the same
        # assertions here so exact UI-copy and domain-list contracts are always
        # exercised before CI.
        AdminApiIntegrationTests.test_admin_ui_has_activity_and_diagnostic_views(self)

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
        self.assertIn(
            'api("POST", "/messages", request, AGENT_DELIVERY_TIMEOUT_MS)',
            script,
        )
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
        self.assertIn('let showingActivity = true;', script)
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
        self.assertIn('id="activity-toggle"', (
            Path(__file__).parents[1] / "host/runtime/workspace/chat/ui/index.html"
        ).read_text())
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
        self.assertIn("upload failed (${response.status})", api)
        self.assertIn("/v1/agent-files/upload?filename=", api)
        self.assertIn("window.KernHost.chooseFiles", chat)
        self.assertIn("window.KernHost.refreshNavigation()", chat)
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
        self.assertNotIn("renderDataFlows", guide)
        self.assertIn("What happens to your data", guide)
        self.assertIn("Technical notes", guide)
        self.assertIn("renderGuide(selected)", guide)
        self.assertNotIn("guide.connection", guide)
        self.assertNotIn("guides.map(renderGuide)", guide)
        self.assertNotIn("scrollIntoView", guide)
        self.assertIn('id="home-integration-groups"', html)
        self.assertIn("Integration guide", html)
        self.assertNotIn('id="panel-connection-guide"', html)
        self.assertNotIn("What each integration enables", html)

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
            ("GET", "/v1/threads/thread_1"),
            ("POST", "/v1/threads/thread_1/messages"),
            ("POST", "/v1/threads/thread_1/stop"),
            ("POST", "/v1/threads/thread_1/clear-memory"),
            ("GET", "/v1/threads/thread_1/events"),
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
            ("GET", "/v1/threads/thread_1/tasks"),
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
                "thread_id": "thread_1",
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
                "/v1/threads/thread_1/events",
                {
                    "since": ["2"],
                    "limit": ["5"],
                    "message_bytes": [str(message_bytes)],
                },
                None,
            )

        page.assert_called_once_with("thread_1", 2, 5, before=None)
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
                    "/v1/threads/thread_1/events",
                    {"before": ["42"], "limit": ["5"]},
                    None,
                ),
                {"events": []},
            )

        page.assert_called_once_with("thread_1", None, 5, before=42)
        with self.assertRaises(admin_api.ApiError) as error:
            admin_api.thread_route(
                "GET",
                "/v1/threads/thread_1/events",
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
                    "/v1/threads/thread_1/events",
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
            "thread_1",
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
                "/v1/threads/thread_1/events",
                {"event_type": ["turn.started"]},
                None,
            )
        self.assertEqual(error.exception.status, HTTPStatus.BAD_REQUEST)

    def test_thread_events_bound_nested_activity_output_without_dropping_event(self) -> None:
        message_bytes = 120 * 1024
        events = [{
            "seq": 1,
            "thread_id": "thread_1",
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
                "/v1/threads/thread_1/events",
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
                "thread_id": "thread_1",
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
                "/v1/threads/thread_1/events",
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
                "thread_id": "thread_1",
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
                "/v1/threads/thread_1/events",
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
                "thread_id": "thread_1",
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
                "/v1/threads/thread_1/events",
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
        with patch.object(admin_api.state, "search_thread_messages", return_value=rows) as search:
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
            limit=2,
            before=None,
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
        with patch.object(
            admin_api.state,
            "search_thread_messages",
            side_effect=[[row, {**row, "seq": 7}], []],
        ) as search:
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
        self.assertEqual(search.call_args_list[1].kwargs["before"], (0.5, 8))

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
            {"query": "ok", "roles": ["user", {"role": "assistant"}]},
            {"query": "ok", "limit": float("nan")},
            {"query": "ok", "limit": 10**5_000},
            {"query": "ok", "unexpected": {"deeply": ["nested"]}},
        )
        read_requests = (
            {"thread_id": {"$ne": None}},
            {"thread_id": "../thread-1"},
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
        with self.assertRaises(admin_api.ApiError):
            admin_api._thread_list_prefix({"prefix": ["bad prefix"]})


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

    def test_workspace_thread_creation_is_reserved_for_workspace_service(self) -> None:
        body = {
            "message": "scheduled work",
            "agent_runtime": "codex",
            "model": "gpt-5.2-codex",
            "effort": "medium",
        }
        for path in (
            "/v1/threads/thread-17/messages",
            "/v1/threads/app-17/messages",
            "/v1/threads/schedule-17-run-23/messages",
            "/v1/threads/schedule-custom/messages",
        ):
            with self.subTest(path=path):
                with self.assertRaises(admin_api.ApiError) as denied:
                    admin_api.route(
                        "POST",
                        path,
                        {},
                        body,
                        principal=admin_api.OperatorPrincipal("test-session"),
                    )
                self.assertEqual(denied.exception.status, HTTPStatus.FORBIDDEN)

        for path in (
            "/v1/threads/thread-17/messages",
            "/v1/threads/app-17/messages",
            "/v1/threads/schedule-17-run-23/messages",
            "/v1/threads/schedule-custom/messages",
        ):
            with self.subTest(path=path), patch(
                "host.runtime.admin_api.service.thread_route",
                return_value={"status": "accepted"},
            ) as thread_route:
                response = admin_api.route(
                    "POST",
                    path,
                    {},
                    body,
                    principal=admin_api.WorkspacePrincipal(),
                )
                self.assertEqual(response, {"status": "accepted"})
                thread_route.assert_called_once_with("POST", path, {}, body)

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


class AgentFileUploadHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        password_hash = hashlib.sha256(b"admin-secret").hexdigest()
        self.config_patch = patch(
            "host.runtime.admin_api.service.load_config",
            return_value={"admin_password_sha256": password_hash},
        )
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)
        admin_api.admin_auth._sessions.clear()
        self.session_token = admin_api.admin_auth._create_session()
        self.addCleanup(admin_api.admin_auth._sessions.clear)
        self.base_url = start_admin_http_server(self)

    def raw_request(self, request: bytes) -> bytes:
        return raw_admin_request(self.admin_socket_path, request)

    def test_upload_streams_to_the_fixed_helper(self) -> None:
        class RecordingStdin(io.BytesIO):
            written = b""

            def close(self) -> None:
                self.written = self.getvalue()
                super().close()

        payload = b"mock-image-bytes"
        process = MagicMock()
        process.stdin = RecordingStdin()
        process.stdout = io.BytesIO(json.dumps({
            "path": "user-files/20260722T120000.000000Z_reference image.png",
            "name": "20260722T120000.000000Z_reference image.png",
            "original_name": "reference image.png",
            "size_bytes": len(payload),
            "uploaded_at": "2026-07-22T12:00:00Z",
        }).encode())
        process.stderr = io.BytesIO()
        process.returncode = 0
        process.wait.return_value = 0

        request = urllib.request.Request(
            f"{self.base_url}/v1/agent-files/upload?filename={quote('reference image.png')}",
            data=payload,
            method="POST",
            headers=_session_headers(self.session_token),
        )
        with (
            patch("host.runtime.admin_api.service.subprocess.Popen", return_value=process) as popen,
            urllib.request.urlopen(request, timeout=5) as response,
        ):
            self.assertEqual(response.status, 200)
            body = json.loads(response.read())

        self.assertEqual(body["file"]["path"], "user-files/20260722T120000.000000Z_reference image.png")
        self.assertEqual(process.stdin.written, payload)
        self.assertEqual(
            popen.call_args.args[0],
            [*admin_api.AGENT_FILE_UPLOAD_HELPER_COMMAND, "reference image.png", str(len(payload))],
        )

    def test_upload_requires_auth_and_rejects_invalid_requests_before_starting_helper(self) -> None:
        payload = b"bytes"
        unauthenticated = self.raw_request(
            b"POST /v1/agent-files/upload?filename=photo.png HTTP/1.1\r\n"
            b"Host: kern-admin.test\r\n"
            + f"Content-Length: {len(payload)}\r\n\r\n".encode()
            + payload
        )
        self.assertIn(b" 401 ", unauthenticated)

        with patch("host.runtime.admin_api.service.subprocess.Popen") as popen:
            for path in (
                "/v1/agent-files/upload?filename=..%2Fphoto.png",
                "/v1/agent-files/upload?filename=photo.png&extra=1",
            ):
                with self.subTest(path=path):
                    response = self.raw_request(
                        f"POST {path} HTTP/1.1\r\n".encode()
                        + b"Host: kern-admin.test\r\n"
                        + f"Cookie: tc_admin_session={self.session_token}\r\n".encode()
                        + b"X-Kern-Csrf: 1\r\n"
                        + f"Content-Length: {len(payload)}\r\n\r\n".encode()
                        + payload
                    )
                    self.assertIn(b" 400 ", response)
            popen.assert_not_called()

    def test_upload_cap_is_enforced_before_body_read(self) -> None:
        auth = f"Cookie: tc_admin_session={self.session_token}\r\nX-Kern-Csrf: 1\r\n"
        oversized = self.raw_request(
            b"POST /v1/agent-files/upload?filename=photo.png HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            + auth.encode()
            + f"Content-Length: {admin_api.AGENT_FILE_UPLOAD_MAX_BYTES + 1}\r\n\r\n".encode()
        )
        self.assertIn(b" 413 ", oversized)
        self.assertIn(b"upload exceeds", oversized)

    def test_ssh_forward_does_not_expose_any_passkey_management_route(self) -> None:
        request = urllib.request.Request(
            f"{self.base_url}/v1/admin-passkeys",
            method="GET",
            headers=_session_headers(self.session_token),
        )
        with patch(
            "host.runtime.admin_api.service.admin_passkeys.status"
        ) as status, self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request, timeout=5)

        self.assertEqual(error.exception.code, 404)
        self.assertEqual(
            json.loads(error.exception.read()),
            {"error": {"message": "route not found"}},
        )
        status.assert_not_called()

    def test_short_upload_gives_helper_time_to_remove_its_partial_file(self) -> None:
        process = MagicMock()
        process.stdin = io.BytesIO()
        process.stdout = io.BytesIO()
        process.stderr = io.BytesIO()
        process.poll.return_value = None
        process.wait.return_value = 2

        with patch("host.runtime.admin_api.service.subprocess.Popen", return_value=process):
            response = self.raw_request(
                b"POST /v1/agent-files/upload?filename=photo.png HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                + f"Cookie: tc_admin_session={self.session_token}\r\nX-Kern-Csrf: 1\r\n".encode()
                + b"Content-Length: 20\r\n\r\n"
                b"short"
            )

        self.assertIn(b" 400 ", response)
        self.assertIn(b"upload ended before Content-Length", response)
        self.assertTrue(process.stdin.closed)
        process.wait.assert_called_once_with(timeout=admin_api.AGENT_FILE_HELPER_TIMEOUT_SECONDS)
        process.kill.assert_not_called()

    def test_helper_epipe_returns_its_actionable_error(self) -> None:
        process = MagicMock()
        process.stdin.write.side_effect = BrokenPipeError
        process.stdout = io.BytesIO(b'{"error":{"message":"user-files must not be a symlink"}}')
        process.stderr = io.BytesIO()
        process.returncode = 2
        process.wait.return_value = 2

        request = urllib.request.Request(
            f"{self.base_url}/v1/agent-files/upload?filename=photo.png",
            data=b"bytes",
            method="POST",
            headers=_session_headers(self.session_token),
        )
        with (
            patch("host.runtime.admin_api.service.subprocess.Popen", return_value=process),
            self.assertRaises(urllib.error.HTTPError) as error,
        ):
            urllib.request.urlopen(request, timeout=5)

        self.assertEqual(error.exception.code, 400)
        self.assertIn("user-files must not be a symlink", error.exception.read().decode())
        process.wait.assert_called_once_with(timeout=admin_api.AGENT_FILE_HELPER_TIMEOUT_SECONDS)
        process.kill.assert_not_called()

    def test_upload_timeout_with_denied_kill_never_waits_unbounded(self) -> None:
        process = MagicMock()
        process.stdin = io.BytesIO()
        process.stdout = io.BytesIO()
        process.stderr = io.BytesIO()
        process.poll.return_value = None

        def wait(*, timeout: int | None = None) -> int:
            if timeout is None:
                raise AssertionError("upload cleanup used an unbounded wait")
            raise subprocess.TimeoutExpired(cmd="upload-agent-file", timeout=timeout)

        process.wait.side_effect = wait
        process.kill.side_effect = PermissionError("signal denied")
        request = urllib.request.Request(
            f"{self.base_url}/v1/agent-files/upload?filename=photo.png",
            data=b"bytes",
            method="POST",
            headers=_session_headers(self.session_token),
        )
        with (
            patch("host.runtime.admin_api.service.subprocess.Popen", return_value=process),
            self.assertRaises(urllib.error.HTTPError) as error,
        ):
            urllib.request.urlopen(request, timeout=5)

        self.assertEqual(error.exception.code, 504)
        self.assertIn("agent file upload helper timed out", error.exception.read().decode())
        process.wait.assert_called_once_with(timeout=admin_api.AGENT_FILE_HELPER_TIMEOUT_SECONDS)
        process.kill.assert_called_once_with()


class AgentProcessSnapshotTests(unittest.TestCase):
    def test_agent_processes_reads_descendant_cgroup_proc_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cgroup = root / "cgroup" / "run-codex.scope"
            proc = root / "proc"
            cgroup.mkdir(parents=True)
            proc.mkdir()
            (cgroup / "cgroup.procs").write_text("123\nnot-a-pid\n")
            (proc / "uptime").write_text("300.00 1000.00\n")

            proc_123 = proc / "123"
            proc_123.mkdir()
            stat_fields = ["S", "1", *["0"] * 17, "200"]
            (proc_123 / "stat").write_text(f"123 (codex app) {' '.join(stat_fields)}\n")
            (proc_123 / "status").write_text("Name:\tcodex\nUid:\t47743\t47743\t47743\t47743\nVmRSS:\t1234 kB\n")
            (proc_123 / "cmdline").write_bytes(b"codex\0app-server\0--listen\0stdio://\0")

            with (
                patch("host.runtime.admin_api.service.AGENT_CGROUP_ROOT", root / "cgroup"),
                patch("host.runtime.admin_api.service.PROC_ROOT", proc),
            ):
                snapshot = admin_api.agent_processes()

        self.assertFalse(snapshot["truncated"])
        self.assertEqual(len(snapshot["processes"]), 1)
        process = snapshot["processes"][0]
        # Exactly the fields the admin UI renders.
        self.assertEqual(process["pid"], 123)
        self.assertEqual(process["state"], "S")
        self.assertEqual(process["name"], "codex")
        self.assertEqual(process["cmdline"], "codex app-server --listen stdio://")
        self.assertEqual(process["rss_bytes"], 1234 * 1024)
        self.assertGreaterEqual(process["elapsed_seconds"], 0)
        self.assertEqual(
            set(process), {"pid", "state", "name", "cmdline", "rss_bytes", "elapsed_seconds"}
        )


class AdminApiClientDisconnectTests(unittest.TestCase):
    def test_client_disconnect_mid_response_is_not_a_host_error(self) -> None:
        # A client that closes its connection while the response is being
        # written is expected transport termination: no structured host-error
        # report (which spawns /usr/bin/logger) and no second JSON write to
        # the dead socket. Loopback TCP is firewalled on Kern hosts, so the
        # handler runs directly on one end of a socketpair whose peer closed.
        client, request = socket.socketpair()
        self.addCleanup(request.close)
        client.sendall(b"GET / HTTP/1.1\r\nHost: kern-admin.test\r\n\r\n")
        client.close()
        with patch.object(admin_api.host_errors, "report_unexpected") as report:
            # Without the disconnect handling, the BrokenPipeError from the
            # retried error write would propagate out of the handler here.
            admin_api.Handler(request, ("127.0.0.1", 0), MagicMock())
        report.assert_not_called()


class AdminApiIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        pg_harness.reset_database()
        # Login sessions and the failed-login throttle are process-global; reset
        # them so a throttle test never leaks a lockout into another test.
        admin_api.admin_auth._sessions.clear()
        admin_api.admin_auth._client_failures.clear()
        admin_api.admin_auth._ADMIN_PASSWORD_HASH = None  # reload from this test's config
        self.session_token = admin_api.admin_auth._create_session()
        self.addCleanup(admin_api.admin_auth._sessions.clear)
        self.addCleanup(admin_api.admin_auth._client_failures.clear)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.proxy_temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.addCleanup(self.proxy_temp_dir.cleanup)
        self.env_patch = patch.dict(
            "os.environ",
            {"KERN_STATE_DIR": self.temp_dir.name, "KERN_PROXY_STATE_DIR": self.proxy_temp_dir.name},
        )
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        save_config(
            {
                "agent_name": "kern-test",
                "admin_password_sha256": hashlib.sha256(b"admin-secret").hexdigest(),
            }
        )
        save_policy(
            {"network_integrations": {"openai": {"enabled": True}}},
            "2026-06-08T00:00:00Z",
        )
        set_runtime_statuses(codex="active", claude_code="deactivated")
        # Live turns are process-global orchestrator state; never leak a fake
        # or admitted turn from one case into the next.
        with orchestrator._LIVE_LOCK:
            orchestrator._LIVE.clear()
        self.addCleanup(orchestrator._LIVE.clear)
        self.reconcile_patch = patch(
            "host.runtime.admin_api.service.orchestrator.reconcile_runtime_status_after_policy_change"
        )
        self.mock_reconcile = self.reconcile_patch.start()
        self.addCleanup(self.reconcile_patch.stop)
        self.base_url = start_admin_http_server(self)

    def request(self, method: str, path: str, body: object | None = None, auth: bool = True):
        if method == "POST" and path.endswith("/messages") and isinstance(body, dict):
            body = dict(body)
            if (
                body.get("agent_runtime") in DEFAULT_SESSION_OPTIONS
                and "model" not in body
                and "effort" not in body
            ):
                model, effort = DEFAULT_SESSION_OPTIONS[body["agent_runtime"]]
                body.update({"model": model, "effort": effort})
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(f"{self.base_url}{path}", data=data, method=method)
        if auth:
            _add_session_auth(request, self.session_token)
        if body is not None:
            request.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())

    def raw_request(self, request: bytes) -> bytes:
        return raw_admin_request(self.admin_socket_path, request)

    def health(self, proxy_alive: bool = True, version: dict[str, str] | None = None):
        reported_version = version or {"status": "ok", "runtime": "0.2.0", "state": "0.2.0"}
        with (
            patch("host.runtime.admin_api.service.host_metrics", return_value={"cpu": {}, "memory": {}, "filesystem": {}, "swap": {}}),
            patch("host.runtime.admin_api.service.proxy_alive", return_value=proxy_alive),
            patch("host.runtime.admin_api.service.version_status", return_value=reported_version),
            patch(
                "host.runtime.admin_api.service.upgrade_check.status",
                return_value={"available": True, "latest": "0.3.0"},
            ),
        ):
            return self.request("GET", "/v1/health")

    def runtime(self, body: dict[str, object], runtime_type: str = "codex") -> dict[str, object]:
        runtimes = body["agent_runtime"]["runtimes"]  # type: ignore[index]
        return next(item for item in runtimes if item["type"] == runtime_type)  # type: ignore[union-attr]

    def workspace_request(
        self,
        method: str,
        path: str,
        body: object | None = None,
    ) -> dict[str, Any]:
        return workspace_admin_api.route_workspace_request(
            method,
            path,
            {},
            body,
        )

    def test_health_requires_auth_and_reports_state(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("GET", "/v1/health", auth=False)
        self.assertEqual(error.exception.code, 401)

        status, body = self.health()

        self.assertEqual(status, 200)
        self.assertEqual(body["agent_name"], "kern-test")
        self.assertEqual(self.runtime(body)["status"], "active")
        self.assertEqual(self.runtime(body, "claude_code")["status"], "deactivated")
        self.assertEqual(body["network_controls"]["status"], "active")
        self.assertEqual(body["version"], {"status": "ok", "runtime": "0.2.0", "state": "0.2.0"})
        self.assertEqual(body["upgrade"], {"available": True, "latest": "0.3.0"})
        self.assertEqual(
            body["history"],
            {"threads": 0, "messages": 0, "activities": 0},
        )


    def test_agent_file_content_route_requires_operator_auth(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("GET", "/v1/agent-files/content?path=%2Fworkspace%2Freel.mp4", auth=False)
        self.assertEqual(error.exception.code, 401)

    def test_workspace_header_does_not_authenticate_tcp_admin_api(self) -> None:
        request = urllib.request.Request(f"{self.base_url}/v1/threads", method="GET")

        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request, timeout=5)

        self.assertEqual(error.exception.code, 401)

    def login(self, password: str = "admin-secret"):
        data = json.dumps({"password": password}).encode()
        request = urllib.request.Request(f"{self.base_url}/v1/login", data=data, method="POST")
        request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, response.headers, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, error.headers, json.loads(error.read())

    def session_token_from_headers(self, headers) -> str | None:
        for value in headers.get_all("Set-Cookie") or []:
            token = admin_api.admin_auth.parse_session_token(
                value,
                context=(
                    admin_api.admin_auth.RequestAuthContext(
                        admin_api.admin_auth.AccessPath.PUBLIC_HTTPS,
                        "admin.example.com",
                    )
                    if value.startswith(
                        f"{admin_api.admin_auth.HOST_SESSION_COOKIE_NAME}="
                    )
                    else admin_api.admin_auth.LOCAL_SSH_FORWARD
                ),
            )
            if token:
                return token
        return None

    def seed_public_passkey(self) -> None:
        save_config({
            "agent_name": "kern-test",
            "admin_password_sha256": hashlib.sha256(b"admin-secret").hexdigest(),
            "operator_connections": [{
                "mode": "cloudflare_tunnel",
                "hostname": "admin.example.com",
                "tunnel_token": "mock-tunnel-token",
            }],
        })
        state.save_admin_passkey(
            user_handle="u" * 43,
            credential_id="credential-id",
            rp_id="admin.example.com",
            public_key_spki="A" * 120,
            sign_count=0,
            transports=["internal"],
            backed_up=True,
            created_at="2026-07-29T00:00:00Z",
        )

    def cookie_request(
        self,
        method: str,
        path: str,
        token: str,
        *,
        csrf: bool = True,
        cookie_name: str | None = None,
        session_activity: bool = False,
        forwarded_proto: str | None = None,
    ):
        request = urllib.request.Request(f"{self.base_url}{path}", method=method)
        request.add_header(
            "Cookie",
            f"{cookie_name or admin_api.admin_auth.SESSION_COOKIE_NAME}={token}",
        )
        if csrf:
            request.add_header(admin_api.admin_auth.CSRF_HEADER_NAME, "1")
        if session_activity:
            request.add_header(admin_api.admin_auth.SESSION_ACTIVITY_HEADER_NAME, "1")
        if forwarded_proto is not None:
            request.add_header("X-Forwarded-Proto", forwarded_proto)
            request.add_header("Host", "admin.example.com")
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_login_issues_session_cookie_that_authenticates(self) -> None:
        status, headers, body = self.login()
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        cookie = (headers.get_all("Set-Cookie") or [""])[0]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        token = self.session_token_from_headers(headers)
        self.assertIsNotNone(token)
        assert token is not None

        status, health = self.cookie_request("GET", "/v1/health", token)
        self.assertEqual(status, 200)
        self.assertIn("status", health)

    def test_public_password_requires_passkey_but_ssh_forward_does_not(self) -> None:
        self.seed_public_passkey()
        # Loopback remains the SSH-key + password recovery path.
        with patch.object(
            admin_api.admin_passkeys,
            "configured",
            side_effect=RuntimeError("Postgres unavailable"),
        ) as configured:
            status, headers, body = self.login()
        configured.assert_not_called()
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertIsNotNone(self.session_token_from_headers(headers))

        data = json.dumps({"password": "admin-secret"}).encode()
        request = urllib.request.Request(
            f"{self.base_url}/v1/login", data=data, method="POST"
        )
        request.add_header("Host", "admin.example.com")
        request.add_header("X-Forwarded-Proto", "https")
        request.add_header("Cf-Connecting-Ip", "203.0.113.8")
        request.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(request, timeout=5) as response:
            public = json.loads(response.read())
            cookies = response.headers.get_all("Set-Cookie") or []
        self.assertTrue(public["passkey_required"])
        self.assertEqual(public["publicKey"]["rpId"], "admin.example.com")
        self.assertTrue(any(
            admin_api.admin_auth.PASSKEY_LOGIN_COOKIE_NAME in cookie
            for cookie in cookies
        ))
        self.assertFalse(any(
            admin_api.admin_auth.HOST_SESSION_COOKIE_NAME in cookie
            for cookie in cookies
        ))

    def test_public_passkey_login_requires_password_pre_authentication(self) -> None:
        self.seed_public_passkey()
        request = urllib.request.Request(
            f"{self.base_url}/v1/login/passkey",
            data=b"{}",
            method="POST",
            headers={
                "Host": "admin.example.com",
                "X-Forwarded-Proto": "https",
                "Cf-Connecting-Ip": "203.0.113.8",
                admin_api.admin_auth.CSRF_HEADER_NAME: "1",
                "Content-Type": "application/json",
            },
        )
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request, timeout=5)

        self.assertEqual(error.exception.code, 401)
        self.assertIn(
            "enter the admin password again",
            error.exception.read().decode(),
        )
        cookies = error.exception.headers.get_all("Set-Cookie") or []
        self.assertFalse(any(
            admin_api.admin_auth.HOST_SESSION_COOKIE_NAME in cookie
            for cookie in cookies
        ))

    def test_public_login_status_exposes_only_passkey_enrollment(self) -> None:
        self.seed_public_passkey()
        request = urllib.request.Request(
            f"{self.base_url}/v1/login/status", method="GET"
        )
        request.add_header("Host", "admin.example.com")
        request.add_header("X-Forwarded-Proto", "https")
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertEqual(
                json.loads(response.read()),
                {"passkey_configured": True},
            )

        state.reset_admin_passkeys()
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(
                json.loads(response.read()),
                {"passkey_configured": False},
            )

    def test_login_status_is_not_exposed_on_ssh_forward_or_wrong_host(self) -> None:
        with patch.object(
            admin_api.admin_passkeys,
            "configured",
            side_effect=RuntimeError("must not query on loopback"),
        ) as configured:
            request = urllib.request.Request(
                f"{self.base_url}/v1/login/status", method="GET"
            )
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request, timeout=5)
            self.assertEqual(error.exception.code, 404)
        configured.assert_not_called()

        self.seed_public_passkey()
        request = urllib.request.Request(
            f"{self.base_url}/v1/login/status", method="GET"
        )
        request.add_header("Host", "other.example.com")
        request.add_header("X-Forwarded-Proto", "https")
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(error.exception.code, 403)

    def test_session_cookie_without_csrf_header_is_forbidden(self) -> None:
        _, headers, _ = self.login()
        token = self.session_token_from_headers(headers)
        assert token is not None
        status, body = self.cookie_request("GET", "/v1/health", token, csrf=False)
        self.assertEqual(status, 403)

    def test_session_cookie_name_is_bound_to_its_transport(self) -> None:
        self.seed_public_passkey()
        token = admin_api.admin_auth._create_session()
        # The public tunnel accepts only the un-tossable __Host- cookie.
        status, _ = self.cookie_request(
            "GET",
            "/v1/health",
            token,
            cookie_name=admin_api.admin_auth.HOST_SESSION_COOKIE_NAME,
            forwarded_proto="https",
        )
        self.assertEqual(status, 200)
        status, _ = self.cookie_request(
            "GET",
            "/v1/health",
            token,
            cookie_name=admin_api.admin_auth.SESSION_COOKIE_NAME,
            forwarded_proto="https",
        )
        self.assertEqual(status, 401)
        # The plain loopback transport never accepts the public cookie.
        status, _ = self.cookie_request(
            "GET",
            "/v1/health",
            token,
            cookie_name=admin_api.admin_auth.HOST_SESSION_COOKIE_NAME,
        )
        self.assertEqual(status, 401)

    def test_background_requests_do_not_keep_an_idle_session_alive(self) -> None:
        admin_api.admin_auth._sessions.clear()
        start = 1000.0
        with patch.object(admin_api.admin_auth, "_now", return_value=start):
            token = admin_api.admin_auth._create_session()
        with patch.object(
            admin_api.admin_auth,
            "_now",
            return_value=start + admin_api.admin_auth.SESSION_IDLE_TIMEOUT_SECONDS - 1,
        ):
            status, _ = self.cookie_request("GET", "/v1/health", token)
            self.assertEqual(status, 200)
        with patch.object(
            admin_api.admin_auth,
            "_now",
            return_value=start + admin_api.admin_auth.SESSION_IDLE_TIMEOUT_SECONDS + 1,
        ):
            status, _ = self.cookie_request("GET", "/v1/health", token)
            self.assertEqual(status, 401)

    def test_recent_operator_activity_refreshes_the_idle_session(self) -> None:
        admin_api.admin_auth._sessions.clear()
        start = 2000.0
        idle = admin_api.admin_auth.SESSION_IDLE_TIMEOUT_SECONDS
        with patch.object(admin_api.admin_auth, "_now", return_value=start):
            token = admin_api.admin_auth._create_session()
        with patch.object(admin_api.admin_auth, "_now", return_value=start + idle - 1):
            status, _ = self.cookie_request(
                "GET",
                "/v1/health",
                token,
                session_activity=True,
            )
            self.assertEqual(status, 200)
        with patch.object(admin_api.admin_auth, "_now", return_value=start + (2 * idle) - 2):
            status, _ = self.cookie_request("GET", "/v1/health", token)
            self.assertEqual(status, 200)

    def test_login_rejects_a_wrong_password_without_a_cookie(self) -> None:
        status, headers, body = self.login("not-the-password")
        self.assertEqual(status, 401)
        self.assertIsNone(self.session_token_from_headers(headers))

    def test_logout_revokes_the_session(self) -> None:
        _, headers, _ = self.login()
        token = self.session_token_from_headers(headers)
        assert token is not None
        status, _ = self.cookie_request("POST", "/v1/logout", token)
        self.assertEqual(status, 200)
        status, _ = self.cookie_request("GET", "/v1/health", token)
        self.assertEqual(status, 401)

    def test_a_source_is_blocked_past_the_limit_even_with_the_correct_password(self) -> None:
        # Once a source uses its per-window attempt budget it is fully blocked:
        # further attempts return 429 before the password is compared, so even
        # the correct password is refused until the window clears.
        for _ in range(admin_api.admin_auth.MAX_FAILURES_PER_CLIENT):
            status, _, _ = self.login("wrong")
            self.assertEqual(status, 401)
        data = json.dumps({"password": "admin-secret"}).encode()
        blocked = self.raw_request(
            b"POST /v1/login HTTP/1.1\r\n"
            b"Host: kern-admin.test\r\n"
            b"Content-Type: application/json\r\n"
            + f"Content-Length: {len(data)}\r\n\r\n".encode()
            + data
        )
        self.assertIn(b" 429 ", blocked)
        self.assertNotIn(b"Set-Cookie:", blocked)

    def test_a_correct_login_within_the_budget_succeeds_and_clears_the_streak(self) -> None:
        # Wrong attempts short of the limit, then the correct password (still
        # within budget) succeeds and resets the source's streak.
        for _ in range(admin_api.admin_auth.MAX_FAILURES_PER_CLIENT - 1):
            status, _, _ = self.login("wrong")
            self.assertEqual(status, 401)
        status, headers, body = self.login("admin-secret")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertIsNotNone(self.session_token_from_headers(headers))
        # Streak cleared: a fresh wrong attempt is a 401, not an immediate 429.
        status, _, _ = self.login("wrong")
        self.assertEqual(status, 401)

    def test_malformed_login_bodies_do_not_consume_throttle_attempts(self) -> None:
        # The throttle bucket is keyed on the browser's egress IP, so a
        # hostile page the operator visits can fire bodiless no-cors POSTs at
        # /v1/login charged to the operator's own bucket. Malformed bodies
        # must fail without consuming the attempt budget.
        garbage = b"not-json"
        empty = (
            b"POST /v1/login HTTP/1.1\r\n"
            b"Host: kern-admin.test\r\n"
            b"Content-Length: 0\r\n\r\n"
        )
        malformed = (
            b"POST /v1/login HTTP/1.1\r\n"
            b"Host: kern-admin.test\r\n"
            b"Content-Type: application/json\r\n"
            + f"Content-Length: {len(garbage)}\r\n\r\n".encode()
            + garbage
        )
        for request in (empty, malformed):
            for _ in range(admin_api.admin_auth.MAX_FAILURES_PER_CLIENT):
                self.assertIn(b" 401 ", self.raw_request(request))
        # No attempt was consumed: the correct password still logs in.
        status, headers, body = self.login()
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertIsNotNone(self.session_token_from_headers(headers))

    def test_valid_shaped_wrong_password_attempts_still_count_toward_the_block(self) -> None:
        # The brute-force protection is unchanged: a valid-shaped body with a
        # wrong password charges the throttle, and past the limit even the
        # correct password is refused until the window clears.
        for _ in range(admin_api.admin_auth.MAX_FAILURES_PER_CLIENT):
            status, _, _ = self.login("wrong")
            self.assertEqual(status, 401)
        status, _, _ = self.login("admin-secret")
        self.assertEqual(status, 429)

    def test_tunnel_login_requires_a_valid_cf_connecting_ip(self) -> None:
        # A tunnel request (X-Forwarded-Proto set) must carry exactly one
        # Cf-Connecting-Ip; a missing/stripped header fails closed so it cannot
        # collapse every internet visitor into one throttle bucket.
        self.seed_public_passkey()
        state.reset_admin_passkeys()
        data = json.dumps({"password": "admin-secret"}).encode()

        def login(headers: dict[str, str]):
            request = urllib.request.Request(f"{self.base_url}/v1/login", data=data, method="POST")
            request.add_header("Content-Type", "application/json")
            for name, value in headers.items():
                request.add_header(name, value)
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    return response.status
            except urllib.error.HTTPError as error:
                return error.code

        base = {
            "Host": "admin.example.com",
            "X-Forwarded-Proto": "https",
        }
        self.assertEqual(login(base), 403)  # missing
        self.assertEqual(login({**base, "Cf-Connecting-Ip": "not-an-ip"}), 403)  # invalid
        self.assertEqual(login({**base, "Cf-Connecting-Ip": "203.0.113.7"}), 200)  # IPv4
        self.assertEqual(login({**base, "Cf-Connecting-Ip": "2001:db8::1"}), 200)  # IPv6
        # Pseudo IPv4: the generated IPv4 is in Cf-Connecting-Ip, the real client
        # in Cf-Connecting-IPv6; the latter is used, so the login proceeds.
        self.assertEqual(
            login({**base, "Cf-Connecting-Ip": "192.0.2.1", "Cf-Connecting-Ipv6": "2001:db8::5"}), 200
        )
        # Duplicate Cf-Connecting-Ip headers fail closed.
        duplicate = self.raw_request(
            b"POST /v1/login HTTP/1.1\r\nHost: admin.example.com\r\nX-Forwarded-Proto: https\r\n"
            b"Cf-Connecting-Ip: 203.0.113.7\r\nCf-Connecting-Ip: 198.51.100.9\r\n"
            b"Content-Type: application/json\r\n"
            + f"Content-Length: {len(data)}\r\n\r\n".encode()
            + data
        )
        self.assertIn(b" 403 ", duplicate)

    def test_login_rejects_oversized_and_malformed_bodies(self) -> None:
        # Raw bytes: the server rejects on Content-Length and may close before
        # the whole body lands, which urllib surfaces as a send error instead
        # of the buffered 413 response.
        data = b'{"password":"' + b"x" * 5000 + b'"}'
        oversized = self.raw_request(
            b"POST /v1/login HTTP/1.1\r\nHost: kern-admin.test\r\n"
            b"Content-Type: application/json\r\n"
            + f"Content-Length: {len(data)}\r\n\r\n".encode()
            + data
        )
        self.assertIn(b" 413 ", oversized)
        # A well-formed but wrong-shape body is a failed attempt, not a 200.
        status, headers, _ = self.login("admin-secret")  # correct baseline works
        self.assertEqual(status, 200)
        extra = urllib.request.Request(
            f"{self.base_url}/v1/login",
            data=json.dumps({"password": "admin-secret", "extra": 1}).encode(),
            method="POST",
        )
        extra.add_header("Content-Type", "application/json")
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(extra, timeout=5)
        self.assertEqual(error.exception.code, 401)

    def test_cleartext_tunnel_request_never_accepts_credentials(self) -> None:
        # X-Forwarded-Proto: http means the edge forwarded the request in the
        # clear. A credential POST is refused, and a GET is upgraded to https.
        save_config({
            "agent_name": "kern-test",
            "admin_password_sha256": hashlib.sha256(b"admin-secret").hexdigest(),
            "operator_connections": [{
                "mode": "cloudflare_tunnel",
                "hostname": "kern.example.com",
                "tunnel_token": "mock-tunnel-token",
            }],
        })
        data = json.dumps({"password": "admin-secret"}).encode()
        request = urllib.request.Request(f"{self.base_url}/v1/login", data=data, method="POST")
        request.add_header("Content-Type", "application/json")
        request.add_header("Host", "kern.example.com")
        request.add_header("X-Forwarded-Proto", "http")
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(error.exception.code, 403)

        response = self.raw_request(
            b"GET / HTTP/1.1\r\n"
            b"Host: kern.example.com\r\n"
            b"X-Forwarded-Proto: http\r\n"
            b"Connection: close\r\n\r\n"
        )
        self.assertIn(b" 301 ", response)
        self.assertIn(b"Location: https://kern.example.com/", response)

    def test_https_redirect_refuses_a_non_origin_form_request_target(self) -> None:
        # A request target need not be origin-form; echoing "GET @evil.com/"
        # into the Location would turn the stored hostname into a userinfo
        # component (https://kern.example.com@evil.com/ resolves to evil.com).
        save_config({
            "agent_name": "kern-test",
            "admin_password_sha256": hashlib.sha256(b"admin-secret").hexdigest(),
            "operator_connections": [{
                "mode": "cloudflare_tunnel",
                "hostname": "kern.example.com",
                "tunnel_token": "mock-tunnel-token",
            }],
        })
        response = self.raw_request(
            b"GET @evil.com/ HTTP/1.1\r\n"
            b"Host: kern.example.com\r\n"
            b"X-Forwarded-Proto: http\r\n"
            b"Connection: close\r\n\r\n"
        )
        self.assertIn(b" 301 ", response)
        self.assertIn(b"Location: https://kern.example.com/\r\n", response)
        self.assertNotIn(b"evil.com", response)

    def test_public_boundary_is_enforced_before_static_assets(self) -> None:
        save_config({
            "agent_name": "kern-test",
            "admin_password_sha256": hashlib.sha256(b"admin-secret").hexdigest(),
            "operator_connections": [{
                "mode": "cloudflare_tunnel",
                "hostname": "kern.example.com",
                "tunnel_token": "mock-tunnel-token",
            }],
        })
        wrong_host = self.raw_request(
            b"GET / HTTP/1.1\r\n"
            b"Host: other.example.com\r\n"
            b"X-Forwarded-Proto: https\r\n"
            b"Connection: close\r\n\r\n"
        )
        self.assertIn(b" 403 ", wrong_host)
        duplicate_marker = self.raw_request(
            b"GET / HTTP/1.1\r\n"
            b"Host: kern.example.com\r\n"
            b"X-Forwarded-Proto: https\r\n"
            b"X-Forwarded-Proto: https\r\n"
            b"Connection: close\r\n\r\n"
        )
        self.assertIn(b" 403 ", duplicate_marker)
        self.assertNotIn(b"<title>Kern</title>", wrong_host)
        self.assertNotIn(b"<title>Kern</title>", duplicate_marker)

    def test_workspace_messages_use_direct_host_thread_ids(self) -> None:
        with patch.object(
            orchestrator, "launch_turn", side_effect=attach_recording_steer_server
        ):
            started = self.workspace_request(
                "POST",
                "/v1/threads/thread-1/messages",
                {
                    "message": "from app",
                    "agent_runtime": "codex",
                    "model": "gpt-5.6-terra",
                    "effort": "high",
                },
            )

            self.assertEqual(started["status"], "accepted")
            self.assertEqual(started["thread"]["thread_id"], "thread-1")
            self.assertEqual(started["thread"]["status"], "running")
            stored = state.thread_session_config("thread-1")
            self.assertIsNotNone(stored)
            assert stored is not None
            self.assertEqual(stored["agent_runtime"], "codex")

        listed = workspace_admin_api.route_workspace_request(
            "GET",
            "/v1/threads",
            {"limit": ["1"], "prefix": ["thread-"]},
            None,
        )
        self.assertEqual([item["thread_id"] for item in listed["threads"]], ["thread-1"])

        detail = self.workspace_request("GET", "/v1/threads/thread-1")
        self.assertEqual(detail["thread"]["thread_id"], "thread-1")
        self.assertEqual(detail["thread"]["agent_runtime"], "codex")

        events = self.workspace_request("GET", "/v1/threads/thread-1/events")
        self.assertEqual(
            [(event["event_type"], event["thread_id"]) for event in events["events"]],
            [("thread.message", "thread-1")],
        )
        self.assertEqual(events["events"][0]["payload"]["message"], "from app")

    def test_workspace_repeated_message_steers_the_running_turn(self) -> None:
        request = {
            "message": "from app",
            "agent_runtime": "codex",
            "model": "gpt-5.6-terra",
            "effort": "high",
        }
        with patch.object(
            orchestrator, "launch_turn", side_effect=attach_recording_steer_server
        ):
            first = self.workspace_request("POST", "/v1/threads/repeated-send/messages", request)
            repeated = self.workspace_request("POST", "/v1/threads/repeated-send/messages", request)

        self.assertEqual(first["status"], "accepted")
        self.assertEqual(repeated["status"], "accepted")
        self.assertEqual(repeated["thread"]["thread_id"], "repeated-send")
        turn = orchestrator._LIVE["codex:repeated-send"]
        self.assertEqual(turn.server.messages, ["from app"])

    def test_workspace_repeated_steer_appends_again(self) -> None:
        with patch.object(
            orchestrator, "launch_turn", side_effect=attach_recording_steer_server
        ):
            self.workspace_request(
                "POST",
                "/v1/threads/durable-steer/messages",
                {
                    "message": "from app",
                    "agent_runtime": "codex",
                    "model": "gpt-5.6-terra",
                    "effort": "high",
                },
            )
            first = self.workspace_request(
                "POST", "/v1/threads/durable-steer/messages", {"message": "nudge"}
            )
            repeated = self.workspace_request(
                "POST", "/v1/threads/durable-steer/messages", {"message": "nudge"}
            )

        self.assertEqual(first["status"], "accepted")
        # Both steers return the same response apart from last_used_at, which
        # each send rewrites from the wall clock: comparing the whole payload
        # fails whenever the two requests straddle a second boundary.
        self.assertEqual(_without_last_used_at(first), _without_last_used_at(repeated))
        for response in (first, repeated):
            self.assertRegex(
                response["thread"]["last_used_at"],
                r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$",
            )
        turn = orchestrator._LIVE["codex:durable-steer"]
        self.assertEqual(turn.server.messages, ["nudge", "nudge"])
        events = self.workspace_request("GET", "/v1/threads/durable-steer/events")
        self.assertEqual(
            [event["event_type"] for event in events["events"]],
            ["thread.message", "thread.message", "thread.message"],
        )

    def test_workspace_task_routes_are_forbidden(self) -> None:
        for method, path in (
            ("POST", "/v1/tasks"),
            ("GET", "/v1/tasks/task_1"),
            ("POST", "/v1/tasks/task_1/steer"),
            ("POST", "/v1/tasks/task_1/cancel"),
            ("POST", "/v1/tasks/task_1/kill"),
            ("GET", "/v1/threads/chat/tasks"),
        ):
            with self.subTest(method=method, path=path):
                with self.assertRaises(admin_api.ApiError) as error:
                    self.workspace_request(method, path)
                self.assertEqual(error.exception.status, HTTPStatus.FORBIDDEN)

    def test_workspace_thread_id_limit_matches_the_host_limit(self) -> None:
        with self.assertRaises(admin_api.ApiError) as error:
            self.workspace_request(
                "POST",
                f"/v1/threads/{'a' * 65}/messages",
                {
                    "message": "too long",
                    "agent_runtime": "codex",
                    "model": "gpt-5.6-terra",
                    "effort": "high",
                },
            )

        self.assertEqual(error.exception.status, HTTPStatus.NOT_FOUND)

    def test_workspace_assets_are_static_host_content(self) -> None:
        request = urllib.request.Request(f"{self.base_url}/workspace/chat.html", method="GET")
        with urllib.request.urlopen(request, timeout=5) as response:
            body = response.read().decode()

        self.assertEqual(response.status, 200)
        self.assertIn("Agent Chat", body)
        self.assertNotIn("<script", body)
        self.assertNotIn("<link", body)
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        csp = response.headers["Content-Security-Policy"]
        self.assertNotIn("worker-src", csp)

        for asset_path, content_type, expected in (
            ("/workspace/chat.css", "text/css", ".chat-app"),
            ("/workspace/chat.js", "application/javascript", "window.KernHost"),
            ("/workspace/rich_text.css", "text/css", ".md-content"),
            ("/workspace/rich_text.js", "application/javascript", "renderMarkdown"),
        ):
            request = urllib.request.Request(f"{self.base_url}{asset_path}", method="GET")
            with urllib.request.urlopen(request, timeout=5) as response:
                asset_body = response.read().decode()

            self.assertEqual(response.status, 200)
            self.assertTrue(response.headers["Content-Type"].startswith(content_type))
            self.assertIn(expected, asset_body)

    def test_generated_capability_worker_uses_a_networkless_broker(self) -> None:
        request = urllib.request.Request(
            f"{self.base_url}/workspace/capability-worker-sandbox.js",
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            body = response.read().decode()

        self.assertIn("data:application/javascript", body)
        csp = response.headers["Content-Security-Policy"]
        self.assertIn("connect-src 'none'", csp)
        self.assertIn("worker-src data:", csp)
        self.assertIn("script-src 'none'", csp)

    def test_workspace_proxy_uses_backend_path_without_identity_headers(self) -> None:
        captured: dict[str, Any] = {}

        class FakeResponse:
            status = 200

            def read(self, _limit: int) -> bytes:
                return b'{"status":"ok"}'

        class FakeConnection:
            def __init__(self, host: str, port: int, timeout: int) -> None:
                captured["connect"] = (host, port, timeout)

            def request(
                self,
                method: str,
                target: str,
                body: bytes | None = None,
                headers: dict[str, str] | None = None,
            ) -> None:
                captured["request"] = (method, target, body, headers or {})

            def getresponse(self) -> FakeResponse:
                return FakeResponse()

            def close(self) -> None:
                captured["closed"] = True

        with patch("host.runtime.admin_api.workspace_proxy.http.client.HTTPConnection", FakeConnection):
            body = workspace_api_proxy.route_request("GET", "/v1/workspace/chat/health", {}, None)

        self.assertEqual(body, {"status": "ok"})
        self.assertEqual(captured["connect"][1], 7450)
        self.assertEqual(captured["request"][1], "/chat/health")
        headers = captured["request"][3]
        self.assertEqual(headers, {})

        with patch("host.runtime.admin_api.workspace_proxy.http.client.HTTPConnection", FakeConnection):
            body = workspace_api_proxy.route_request(
                "GET", "/v1/workspace/memory", {"limit": ["50"]}, None
            )
        self.assertEqual(body, {"status": "ok"})
        self.assertEqual(captured["request"][1], "/memory?limit=50")

    def test_filesystem_metrics_reports_root_and_data_mounts(self) -> None:
        class Usage:
            def __init__(self, used: int, total: int) -> None:
                self.used = used
                self.total = total

        def fake_disk_usage(path: str) -> Usage:
            values = {
                "/": Usage(1, 10),
                "/mnt/kern-admin": Usage(2, 20),
                "/mnt/kern-agent": Usage(3, 30),
            }
            return values[path]

        with patch("host.runtime.admin_api.service.shutil.disk_usage", side_effect=fake_disk_usage):
            metrics = admin_api.filesystem_metrics()

        self.assertEqual(metrics["mounts"], {
            "root": {"used_bytes": 1, "total_bytes": 10},
            "admin": {"used_bytes": 2, "total_bytes": 20},
            "agent": {"used_bytes": 3, "total_bytes": 30},
        })

    def test_malformed_or_huge_content_length_returns_4xx(self) -> None:
        invalid = self.raw_request(
            b"POST /v1/threads/t1/messages HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            + f"Cookie: tc_admin_session={self.session_token}\r\nX-Kern-Csrf: 1\r\n".encode()
            + b"Content-Length: nope\r\n\r\n"
        )
        huge = self.raw_request(
            b"POST /v1/threads/t1/messages HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            + f"Cookie: tc_admin_session={self.session_token}\r\nX-Kern-Csrf: 1\r\n".encode()
            + b"Content-Length: 1048577\r\n\r\n"
        )

        self.assertIn(b"400", invalid)
        self.assertIn(b"malformed Content-Length", invalid)
        self.assertIn(b"413", huge)
        self.assertIn(b"request body too large", huge)

    def test_admin_json_decoders_reject_hostile_documents_as_bad_requests(self) -> None:
        payloads = (
            b"\xff",
            b'{"nested":' + b"[" * 1_100 + b"0" + b"]" * 1_100 + b"}",
        )

        class Request:
            def __init__(self, payload: bytes) -> None:
                self.headers = {"Content-Length": str(len(payload))}
                self.rfile = io.BytesIO(payload)

        for handler in (admin_api.Handler, workspace_admin_api.Handler):
            for payload in payloads:
                with (
                    self.subTest(handler=handler.__name__, payload=payload[:16]),
                    self.assertRaises(admin_api.ApiError) as error,
                ):
                    handler._read_body(Request(payload))  # type: ignore[arg-type]
                self.assertEqual(error.exception.status, HTTPStatus.BAD_REQUEST)
                self.assertIn("invalid JSON", error.exception.message)

    def test_health_reports_error_when_proxy_is_down(self) -> None:
        _, body = self.health(proxy_alive=False)
        self.assertEqual(body["network_controls"]["status"], "error")
        self.assertEqual(body["status"], "degraded")

    def test_health_is_degraded_when_runtime_and_state_versions_differ(self) -> None:
        _, body = self.health(
            version={"status": "mismatch", "runtime": "1.8.0", "state": "1.7.0"}
        )
        self.assertEqual(body["version"]["status"], "mismatch")
        self.assertEqual(body["status"], "degraded")

    def test_health_never_spawns_codex(self) -> None:
        # The health/status path must read cached state only — a hanging Codex
        # app-server must never be able to block it.
        set_runtime_statuses(codex="loading", claude_code="deactivated")
        with patch(
            "host.runtime.admin_api.orchestrator.codex_app_server.account_status",
            side_effect=AssertionError("health must not call Codex"),
        ):
            _, body = self.health()
        self.assertEqual(self.runtime(body)["status"], "loading")

    def test_runtime_status_loop_refreshes_cached_status(self) -> None:
        set_runtime_statuses(codex="loading", claude_code="deactivated")
        with patch(
            "host.runtime.admin_api.orchestrator.codex_app_server.account_status",
            return_value=("awaiting_login", None, None),
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("codex"), "awaiting_login")
        _, body = self.request("GET", "/v1/agent-runtime/status")
        self.assertEqual(self.runtime({"agent_runtime": body})["status"], "awaiting_login")
        self.assertNotIn("error_message", self.runtime({"agent_runtime": body}))

    def test_runtime_status_error_surfaces_error_message(self) -> None:
        with patch(
            "host.runtime.admin_api.orchestrator.codex_app_server.account_status",
            return_value=("error", "timed out waiting for Codex app-server; app-server stderr: boom", None),
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("codex"), "error")
        _, body = self.request("GET", "/v1/agent-runtime/status")
        self.assertEqual(self.runtime({"agent_runtime": body})["status"], "error")
        self.assertIn("boom", self.runtime({"agent_runtime": body})["error_message"])
        # The error message clears once the runtime recovers.
        with patch(
            "host.runtime.admin_api.orchestrator.codex_app_server.account_status",
            return_value=("awaiting_login", None, None),
        ):
            orchestrator.refresh_runtime_status("codex")
        _, body = self.request("GET", "/v1/agent-runtime/status")
        self.assertNotIn("error_message", self.runtime({"agent_runtime": body}))

    def test_disabled_provider_runtime_is_deactivated_without_cli_check(self) -> None:
        save_claude_account({"account_id": "acct_smoke", "access_token_sha256": "f" * 64})
        set_runtime_statuses(codex="active")
        with orchestrator._RUNTIME_STATUS_LOCK:
            orchestrator._RUNTIME_STATUSES["claude_code"] = {"status": "active", "error_message": "old failure"}
        save_oauth_login("claude", {
            "status": "awaiting_code",
            "login_url": "https://claude.com/cai/oauth/authorize",
            "expires_at": "2099-06-08T00:10:00Z",
        })

        with patch(
            "host.runtime.admin_api.orchestrator.claude_code.account_status",
            side_effect=AssertionError("disabled Claude runtime must not touch Claude Code"),
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("claude_code"), "deactivated")

        self.assertEqual(orchestrator.runtime_status_record("claude_code"), {"status": "deactivated"})
        self.assertIsNone(state.oauth_login("claude"))
        self.assertEqual(read_claude_account(), {"account_id": "acct_smoke", "access_token_sha256": "f" * 64})
        _, body = self.request("GET", "/v1/agent-runtime/status")
        self.assertNotIn("error_message", self.runtime({"agent_runtime": body}, "claude_code"))

    def test_ui_page_is_served_without_auth(self) -> None:
        request = urllib.request.Request(f"{self.base_url}/")
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertIn("text/html", response.headers["Content-Type"])
            self.assert_security_headers(response.headers)
            page = response.read().decode()
        self.assertIn("Kern", page)
        self.assertIn('/admin_ui.css', page)
        self.assertIn('/admin_ui/app.js', page)

        for path, content_type, expected in (
            ("/admin_ui.css", "text/css", ".shell"),
            ("/admin_ui/app.js", "application/javascript", "setInterval(tick, 5000)"),
            ("/admin_ui/health.js", "application/javascript", "/v1/health"),
            ("/favicon.ico", "image/svg+xml", "<svg"),
            ("/favicon.svg", "image/svg+xml", "<svg"),
        ):
            request = urllib.request.Request(f"{self.base_url}{path}")
            with urllib.request.urlopen(request, timeout=5) as response:
                self.assertEqual(response.status, 200)
                self.assertIn(content_type, response.headers["Content-Type"])
                self.assert_security_headers(response.headers)
                body = response.read().decode()
            self.assertIn(expected, body)

        request = urllib.request.Request(f"{self.base_url}/v1/health")
        _add_session_auth(request, self.session_token)
        with (
            patch("host.runtime.admin_api.service.host_metrics", return_value={"cpu": {}, "memory": {}, "filesystem": {}, "swap": {}}),
            patch("host.runtime.admin_api.service.proxy_alive", return_value=True),
            patch("host.runtime.admin_api.service.version_status", return_value={"status": "ok", "runtime": "0.2.0", "state": "0.2.0"}),
            urllib.request.urlopen(request, timeout=5) as response,
        ):
            self.assertEqual(response.status, 200)
            self.assert_security_headers(response.headers)

    def assert_security_headers(self, headers: Any) -> None:
        self.assertEqual(headers["Content-Security-Policy"], admin_api.SECURITY_HEADERS["Content-Security-Policy"])
        self.assertIn("connect-src 'self'", headers["Content-Security-Policy"])
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["X-Frame-Options"], "DENY")

    def test_agent_file_routes_use_sudo_helper(self) -> None:
        calls: list[list[str]] = []

        def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            if command[-2] == "list":
                return subprocess.CompletedProcess(
                    command,
                    0,
                    json.dumps({"path": "/", "entries": [{"name": ".codex", "path": "/.codex", "type": "directory"}]}),
                    "",
                )
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps({
                    "path": "/README.md",
                    "size_bytes": 12,
                    "truncated": False,
                    "encoding": "utf-8-replacement",
                    "content": "hello\n",
                }),
                "",
            )

        with patch("host.runtime.admin_api.service.subprocess.run", side_effect=fake_run):
            status, listed = self.request("GET", "/v1/agent-files?path=/")
            self.assertEqual(status, 200)
            self.assertEqual(listed["entries"][0]["name"], ".codex")

            status, read = self.request("GET", "/v1/agent-files/read?path=/README.md")
            self.assertEqual(status, 200)
            self.assertEqual(read["content"], "hello\n")

        self.assertEqual(calls[0], [
            "/usr/bin/sudo",
            "-n",
            "/usr/local/lib/kern-host/read-agent-file",
            "list",
            "/",
        ])
        self.assertEqual(calls[1][-2:], ["read", "/README.md"])

    def test_agent_file_content_streams_authenticated_video(self) -> None:
        payload = b"mock-video-bytes"
        process = MagicMock()
        process.stdout = io.BytesIO(
            json.dumps({
                "path": "/workspace/reel.mp4",
                "size_bytes": len(payload),
                "media_type": "video/mp4",
            }).encode() + b"\n" + payload
        )
        process.stderr = io.BytesIO()
        process.poll.return_value = 0
        process.wait.return_value = 0

        request = urllib.request.Request(
            f"{self.base_url}/v1/agent-files/content?path=%2Fworkspace%2Freel.mp4"
        )
        _add_session_auth(request, self.session_token)
        with (
            patch("host.runtime.admin_api.service.subprocess.Popen", return_value=process) as popen,
            urllib.request.urlopen(request, timeout=5) as response,
        ):
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Content-Type"], "video/mp4")
            self.assertEqual(response.read(), payload)
            for name, value in admin_api.UNTRUSTED_FILE_SECURITY_HEADERS.items():
                self.assertEqual(response.headers[name], value)

        self.assertEqual(
            popen.call_args.args[0][-2:],
            ["stream", "/workspace/reel.mp4"],
        )

    def test_agent_file_content_streams_authenticated_image(self) -> None:
        payload = b"mock-png-bytes"
        process = MagicMock()
        process.stdout = io.BytesIO(
            json.dumps({
                "path": "/workspace/screenshot.png",
                "size_bytes": len(payload),
                "media_type": "image/png",
            }).encode() + b"\n" + payload
        )
        process.stderr = io.BytesIO()
        process.poll.return_value = 0
        process.wait.return_value = 0

        request = urllib.request.Request(
            f"{self.base_url}/v1/agent-files/content?path=%2Fworkspace%2Fscreenshot.png"
        )
        _add_session_auth(request, self.session_token)
        with (
            patch("host.runtime.admin_api.service.subprocess.Popen", return_value=process) as popen,
            urllib.request.urlopen(request, timeout=5) as response,
        ):
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Content-Type"], "image/png")
            self.assertEqual(response.read(), payload)
            for name, value in admin_api.UNTRUSTED_FILE_SECURITY_HEADERS.items():
                self.assertEqual(response.headers[name], value)

        self.assertEqual(
            popen.call_args.args[0][-2:],
            ["stream", "/workspace/screenshot.png"],
        )

    def test_agent_file_content_rejects_mismatched_helper_media_type(self) -> None:
        process = MagicMock()
        process.stdout = io.BytesIO(
            json.dumps({
                "path": "/workspace/payload.mp4",
                "size_bytes": 20,
                "media_type": "text/html",
            }).encode() + b"\n<script>bad()</script>"
        )
        process.stderr = io.BytesIO()
        process.poll.return_value = 0
        process.wait.return_value = 0

        with patch("host.runtime.admin_api.service.subprocess.Popen", return_value=process):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request("GET", "/v1/agent-files/content?path=%2Fworkspace%2Fpayload.mp4")

        self.assertEqual(error.exception.code, 500)
        self.assertIn("invalid metadata", error.exception.read().decode())

    def test_agent_file_content_rejects_unsupported_path_before_helper(self) -> None:
        with patch("host.runtime.admin_api.service.subprocess.Popen") as popen:
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request("GET", "/v1/agent-files/content?path=%2Fworkspace%2Fpayload.svg")

        self.assertEqual(error.exception.code, 400)
        self.assertIn("only MP4, MOV, JPEG, PNG, or WebP", error.exception.read().decode())
        popen.assert_not_called()

    def test_agent_file_content_rejects_oversized_image_metadata(self) -> None:
        process = MagicMock()
        process.stdout = io.BytesIO(
            json.dumps({
                "path": "/workspace/huge.png",
                "size_bytes": admin_api.AGENT_FILE_IMAGE_STREAM_MAX_BYTES + 1,
                "media_type": "image/png",
            }).encode() + b"\n"
        )
        process.stderr = io.BytesIO()
        process.poll.return_value = 0
        process.wait.return_value = 0

        with patch("host.runtime.admin_api.service.subprocess.Popen", return_value=process):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request("GET", "/v1/agent-files/content?path=%2Fworkspace%2Fhuge.png")

        self.assertEqual(error.exception.code, 500)
        self.assertIn("invalid metadata", error.exception.read().decode())

    def test_agent_file_helper_errors_map_to_http_status(self) -> None:
        def missing(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                command,
                2,
                json.dumps({"error": {"message": "path not found"}}),
                "",
            )

        with patch("host.runtime.admin_api.service.subprocess.run", side_effect=missing):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request("GET", "/v1/agent-files?path=/missing")
        self.assertEqual(error.exception.code, 404)
        self.assertIn("path not found", error.exception.read().decode())

    def test_agent_file_helper_permission_error_during_timeout_returns_504(self) -> None:
        with patch("host.runtime.admin_api.service.subprocess.run", side_effect=PermissionError("kill denied")):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request("GET", "/v1/agent-files?path=/")
        self.assertEqual(error.exception.code, 504)
        self.assertIn("root helper could not be terminated", error.exception.read().decode())

    def test_message_starts_turn_and_records_thread_events(self) -> None:
        with patch.object(orchestrator, "launch_turn") as launch:
            status, body = self.request(
                "POST",
                "/v1/threads/t1/messages",
                {"message": "first turn", "agent_runtime": "codex"},
            )

        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "accepted")
        self.assertEqual(body["thread"]["thread_id"], "t1")
        self.assertEqual(body["thread"]["agent_runtime"], "codex")
        self.assertEqual(body["thread"]["status"], "running")
        launch.assert_called_once()
        turn = launch.call_args.args[0]
        self.assertEqual((turn.runtime_type, turn.thread_id), ("codex", "t1"))
        self.assertEqual(launch.call_args.args[1], "first turn")

        _, events = self.request("GET", "/v1/threads/t1/events")
        self.assertEqual(
            [(event["event_type"], event["thread_id"]) for event in events["events"]],
            [("thread.message", "t1")],
        )
        self.assertEqual(events["events"][0]["payload"], {"message": "first turn", "source": "user"})

        _, listed = self.request("GET", "/v1/threads")
        self.assertEqual(listed["threads"][0]["thread_id"], "t1")
        self.assertEqual(listed["threads"][0]["status"], "running")

    def test_message_steers_running_turn_without_a_host_mailbox(self) -> None:
        with patch.object(
            orchestrator, "launch_turn", side_effect=attach_recording_steer_server
        ):
            self.request(
                "POST", "/v1/threads/t1/messages", {"message": "start", "agent_runtime": "codex"}
            )
            _, first = self.request("POST", "/v1/threads/t1/messages", {"message": "s1"})
            _, second = self.request("POST", "/v1/threads/t1/messages", {"message": "s2"})
            _, third = self.request("POST", "/v1/threads/t1/messages", {"message": "s3"})
        self.assertEqual(first["status"], "accepted")
        self.assertEqual(second["status"], "accepted")
        self.assertEqual(third["status"], "accepted")
        turn = orchestrator._LIVE["codex:t1"]
        self.assertEqual(turn.server.messages, ["s1", "s2", "s3"])
        _, events = self.request("GET", "/v1/threads/t1/events")
        self.assertEqual(
            [event["event_type"] for event in events["events"]],
            ["thread.message", "thread.message", "thread.message", "thread.message"],
        )

    def test_message_rejected_while_thread_finishes_previous_turn(self) -> None:
        seed_thread_session("t1")
        turn = register_live_turn("t1")
        turn.phase = orchestrator.ExecutionPhase.FINISHING

        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("POST", "/v1/threads/t1/messages", {"message": "retry"})

        self.assertEqual(error.exception.code, 409)
        self.assertIn("agent is finishing; retry shortly", error.exception.read().decode())

    def test_message_rejected_while_runtime_is_not_active(self) -> None:
        set_runtime_statuses(codex="awaiting_login", claude_code="deactivated")

        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request(
                "POST", "/v1/threads/t1/messages", {"message": "hello", "agent_runtime": "codex"}
            )
        self.assertEqual(error.exception.code, 409)
        self.assertIn(
            "Codex runtime is awaiting_login; messages run only while it is active",
            error.exception.read().decode(),
        )

        # A runtime whose managed provider is disabled rejects with the policy
        # pointer instead of a bare status.
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request(
                "POST", "/v1/threads/t2/messages", {"message": "hello", "agent_runtime": "claude_code"}
            )
        self.assertEqual(error.exception.code, 409)
        self.assertIn(
            "Claude Code runtime is deactivated; enable its provider",
            error.exception.read().decode(),
        )

        # The rejected messages created no thread.
        self.assertIsNone(state.thread_session_config("t1"))
        self.assertIsNone(state.thread_session_config("t2"))

    def test_eleventh_concurrent_turn_per_runtime_is_rejected_with_429(self) -> None:
        save_policy(
            {"network_integrations": {"openai": {"enabled": True}, "claude": {"enabled": True}}},
            "2026-06-08T00:00:00Z",
        )
        set_runtime_statuses(codex="active", claude_code="active")
        with patch.object(
            orchestrator, "launch_turn", side_effect=attach_recording_steer_server
        ):
            for index in range(1, orchestrator.TURN_LIMIT_PER_RUNTIME + 1):
                thread_id = f"t{index}"
                _, body = self.request(
                    "POST",
                    f"/v1/threads/{thread_id}/messages",
                    {"message": "go", "agent_runtime": "codex"},
                )
                self.assertEqual(body["status"], "accepted")

            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request(
                    "POST", "/v1/threads/t11/messages", {"message": "go", "agent_runtime": "codex"}
                )
            self.assertEqual(error.exception.code, 429)
            self.assertIn(
                "already running 10 concurrent threads; retry when one finishes",
                error.exception.read().decode(),
            )
            # The rejection rolled everything back: no thread, no events.
            self.assertIsNone(state.thread_session_config("t11"))
            _, events = self.request("GET", "/v1/threads/t11/events")
            self.assertEqual(events["events"], [])

            # Capacity is per runtime: Claude Code still has its own pool.
            _, claude = self.request(
                "POST", "/v1/threads/c1/messages", {"message": "go", "agent_runtime": "claude_code"}
            )
            self.assertEqual(claude["status"], "accepted")

    def test_message_validates_and_returns_session_options(self) -> None:
        save_policy(
            {"network_integrations": {"openai": {"enabled": True}, "claude": {"enabled": True}}},
            "2026-06-08T00:00:00Z",
        )
        set_runtime_statuses(codex="active", claude_code="active")
        with patch.object(
            orchestrator, "launch_turn", side_effect=attach_recording_steer_server
        ):
            status, codex = self.request(
                "POST",
                "/v1/threads/codex-options/messages",
                {
                    "message": "codex turn",
                    "agent_runtime": "codex",
                    "model": "gpt-5.6-luna",
                    "effort": "max",
                },
            )
            self.assertEqual(status, 200)
            self.assertEqual(
                (codex["thread"]["model"], codex["thread"]["effort"]), ("gpt-5.6-luna", "max")
            )

            status, claude = self.request(
                "POST",
                "/v1/threads/claude-options/messages",
                {
                    "message": "claude turn",
                    "agent_runtime": "claude_code",
                    "model": "claude-fable-5",
                    "effort": "ultracode",
                },
            )
            self.assertEqual(status, 200)
            self.assertEqual(
                (claude["thread"]["model"], claude["thread"]["effort"]),
                ("claude-fable-5", "ultracode"),
            )

        invalid = [
            {"model": "gpt-5.6-luna", "effort": "ultra"},
            {"model": "claude-opus-5", "effort": "high"},
            {"model": "gpt-5.6-terra"},
            {"effort": "high"},
            {"model": None, "effort": None},
        ]
        for index, fields in enumerate(invalid):
            with self.subTest(fields=fields), self.assertRaises(urllib.error.HTTPError) as error:
                self.request(
                    "POST",
                    f"/v1/threads/invalid-options-{index}/messages",
                    {"message": "invalid", "agent_runtime": "codex", **fields},
                )
            self.assertEqual(error.exception.code, 400)

    def test_follow_up_accepts_omitted_or_matching_configuration_and_locks_live_changes(
        self,
    ) -> None:
        body = {
            "message": "first",
            "agent_runtime": "codex",
            "model": "gpt-5.6-terra",
            "effort": "high",
        }
        path = "/v1/threads/fixed-options/messages"
        with patch.object(
            orchestrator, "launch_turn", side_effect=attach_recording_steer_server
        ):
            self.request("POST", path, body)

            _, repeated = self.request("POST", path, {**body, "message": "matching repeat"})
            self.assertEqual(repeated["status"], "accepted")
            thread = repeated["thread"]
            self.assertEqual(
                (thread["agent_runtime"], thread["model"], thread["effort"]),
                (body["agent_runtime"], body["model"], body["effort"]),
            )

            for fields in (
                {"model": "gpt-5.6-sol", "effort": "high"},
                {"model": "gpt-5.6-terra", "effort": "max"},
            ):
                with self.subTest(fields=fields), self.assertRaises(urllib.error.HTTPError) as error:
                    self.request("POST", path, {**body, "message": "conflict", **fields})
                self.assertEqual(error.exception.code, 409)
                self.assertIn(
                    "can change only while the thread is idle", error.exception.read().decode()
                )

            with self.assertRaises(urllib.error.HTTPError) as partial_error:
                self.request(
                    "POST", path, {"message": "partial conflict", "model": "gpt-5.6-terra"}
                )
            self.assertEqual(partial_error.exception.code, 400)
            self.assertIn("must be provided together", partial_error.exception.read().decode())

            _, follow_up = self.request("POST", path, {"message": "follow up"})
        self.assertEqual(follow_up["status"], "accepted")
        thread = follow_up["thread"]
        self.assertEqual(
            (thread["agent_runtime"], thread["model"], thread["effort"]),
            (body["agent_runtime"], body["model"], body["effort"]),
        )

    def test_idle_configuration_change_rotates_provider_and_hands_off_history(self) -> None:
        save_policy(
            {
                "network_integrations": {
                    "openai": {"enabled": True},
                    "claude": {"enabled": True},
                }
            },
            "2026-06-08T00:00:00Z",
        )
        set_runtime_statuses(codex="active", claude_code="active")
        seed_thread_session(
            "switchable",
            "codex",
            model="gpt-5.6-terra",
            effort="high",
            provider_session_id="old-provider-session",
        )
        with state.mutation() as cur:
            state.append_agent_event(
                cur,
                "thread.message",
                "switchable",
                {"message": "original question", "source": "user"},
            )
            state.append_agent_event(
                cur,
                "thread.message",
                "switchable",
                {"message": "original answer", "source": "agent"},
            )
            state.append_agent_event(
                cur,
                "thread.activity",
                "switchable",
                {
                    "activity": {
                        "provider": "codex",
                        "activity_id": "command-1",
                        "kind": "command",
                        "phase": "completed",
                        "title": "Inspect repository",
                        "detail": "command context",
                        "output": "complete command output",
                    }
                },
            )

        with patch.object(orchestrator, "launch_turn") as launch:
            _, accepted = self.request(
                "POST",
                "/v1/threads/switchable/messages",
                {
                    "message": "continue here",
                    "agent_runtime": "claude_code",
                    "model": "claude-opus-5",
                    "effort": "max",
                },
            )

        config = state.thread_session_config("switchable")
        assert config is not None
        self.assertEqual(
            (config["agent_runtime"], config["model"], config["effort"]),
            ("claude_code", "claude-opus-5", "max"),
        )
        self.assertIsNone(config["provider_session_id"])
        launched_turn, launch_message, provider_session_id = launch.call_args.args
        self.assertEqual(launched_turn.runtime_type, "claude_code")
        self.assertIsNone(provider_session_id)
        self.assertIn("new agent session continuing", launch_message)
        self.assertIn("User:\noriginal question", launch_message)
        self.assertIn("Agent:\noriginal answer", launch_message)
        self.assertIn("Agent activity (summary)", launch_message)
        self.assertIn('"detail": "command context"', launch_message)
        self.assertIn('"output": "complete command output"', launch_message)
        self.assertIn("CURRENT USER MESSAGE ---\ncontinue here", launch_message)
        _, events = self.request("GET", "/v1/threads/switchable/events?since=0")
        self.assertEqual(
            [event["event_type"] for event in events["events"]],
            [
                "thread.message",
                "thread.message",
                "thread.activity",
                "thread.activity",
                "thread.message",
            ],
        )
        change = events["events"][3]["payload"]["activity"]
        self.assertEqual(change["title"], "Agent provider changed")
        self.assertEqual(change["kind"], "status")
        self.assertEqual(change["phase"], "completed")
        self.assertIn("Codex · gpt-5.6-terra · high", change["detail"])
        self.assertIn("Claude Code · claude-opus-5 · max", change["detail"])
        visible_messages = [
            event["payload"]["message"]
            for event in events["events"]
            if event["event_type"] == "thread.message"
        ]
        self.assertEqual(
            visible_messages,
            ["original question", "original answer", "continue here"],
        )
        self.assertEqual(accepted["thread"]["agent_runtime"], "claude_code")

    def test_session_handoff_reserves_100k_for_newest_conversation(self) -> None:
        history = [
            {
                "event_type": "thread.message",
                "payload": {"source": "user", "message": "a" * 150_000},
            },
            {
                "event_type": "thread.message",
                "payload": {"source": "agent", "message": "b" * 150_000},
            },
        ]

        handoff = admin_api._session_handoff_message(history, "next")
        transcript = handoff.split(
            "--- RETAINED CONVERSATION ---\n", 1
        )[1].split("\n--- END RETAINED CONVERSATION ---", 1)[0]

        self.assertLessEqual(
            len(transcript),
            admin_api.THREAD_HANDOFF_MESSAGE_CHARACTER_LIMIT,
        )
        self.assertLess(transcript.count("a"), 150_000)
        self.assertGreater(transcript.count("b"), 90_000)
        self.assertLess(transcript.count("b"), 150_000)
        self.assertIn("Older retained thread events were omitted", transcript)

    def test_session_handoff_summarizes_large_activity_fields(self) -> None:
        history = [
            {
                "event_type": "thread.activity",
                "payload": {
                    "activity": {
                        "provider": "codex",
                        "activity_id": "not-needed-by-the-replacement",
                        "kind": "command",
                        "phase": "completed",
                        "title": "Build app",
                        "detail": "d" * 20_000,
                        "output": "o" * 100_000,
                        "image": "base64-is-never-forwarded",
                    }
                },
            }
        ]

        block = admin_api._handoff_event_block(history[0])

        self.assertIn("Agent activity (summary)", block)
        self.assertIn('"title": "Build app"', block)
        self.assertIn("truncated", block)
        self.assertNotIn("activity_id", block)
        self.assertNotIn("base64-is-never-forwarded", block)
        self.assertLessEqual(
            len(block), admin_api.THREAD_HANDOFF_ACTIVITY_EVENT_CHARACTER_LIMIT
        )
        self.assertGreater(block.count("o"), 6_000)

    def test_session_handoff_reserves_a_separate_150k_activity_section(self) -> None:
        history: list[dict[str, Any]] = [
            {
                "event_type": "thread.message",
                "payload": {"source": "user", "message": "important decision"},
            }
        ]
        history.extend(
            {
                "event_type": "thread.activity",
                "payload": {
                    "activity": {
                        "kind": "command",
                        "phase": "completed",
                        "title": f"Command {index}",
                        "output": str(index) + "o" * 20_000,
                    }
                },
            }
            for index in range(30)
        )

        handoff = admin_api._session_handoff_message(history, "next")
        conversation = handoff.split(
            "--- RETAINED CONVERSATION ---\n", 1
        )[1].split("\n--- END RETAINED CONVERSATION ---", 1)[0]
        activity = handoff.split(
            "--- RECENT AGENT ACTIVITY ---\n", 1
        )[1].split("\n--- END RECENT AGENT ACTIVITY ---", 1)[0]

        self.assertIn("important decision", conversation)
        self.assertLessEqual(
            len(conversation), admin_api.THREAD_HANDOFF_MESSAGE_CHARACTER_LIMIT
        )
        self.assertLessEqual(
            len(activity), admin_api.THREAD_HANDOFF_ACTIVITY_CHARACTER_LIMIT
        )
        self.assertIn("Command 29", activity)
        self.assertNotIn("Command 0\"", activity)
        self.assertIn("Older retained thread events were omitted", activity)

    def test_missing_provider_session_replays_retained_history_without_a_config_change(
        self,
    ) -> None:
        seed_thread_session(
            "missing-provider-session",
            "codex",
            provider_session_id=None,
        )
        with state.mutation() as cur:
            state.append_agent_event(
                cur,
                "thread.message",
                "missing-provider-session",
                {"message": "work already attempted", "source": "user"},
            )

        with patch.object(orchestrator, "launch_turn") as launch:
            self.request(
                "POST",
                "/v1/threads/missing-provider-session/messages",
                {"message": "retry with context"},
            )

        _turn, launch_message, provider_session_id = launch.call_args.args
        self.assertIsNone(provider_session_id)
        self.assertIn("User:\nwork already attempted", launch_message)
        self.assertIn("CURRENT USER MESSAGE ---\nretry with context", launch_message)
        _, events = self.request(
            "GET",
            "/v1/threads/missing-provider-session/events?since=0",
        )
        self.assertEqual(
            [event["event_type"] for event in events["events"]],
            ["thread.message", "thread.message"],
        )

    def test_thread_on_a_superseded_model_can_switch_to_an_offered_model(self) -> None:
        seed_thread_session(
            "legacy-alias-thread",
            "claude_code",
            model="opus",
            effort="high",
            last_used_at="2026-06-08T00:00:01Z",
        )

        for fields in (
            {},
            {"agent_runtime": "claude_code", "model": "opus", "effort": "high"},
        ):
            with self.subTest(fields=fields), self.assertRaises(urllib.error.HTTPError) as error:
                self.request(
                    "POST",
                    "/v1/threads/legacy-alias-thread/messages",
                    {"message": "follow up", **fields},
                )
            self.assertEqual(error.exception.code, 409)
            self.assertIn("no longer offered", error.exception.read().decode())

        with patch.object(orchestrator, "launch_turn"):
            _, switched = self.request(
                "POST",
                "/v1/threads/legacy-alias-thread/messages",
                {
                    "message": "continue on a current model",
                    "agent_runtime": "codex",
                    "model": "gpt-5.6-terra",
                    "effort": "high",
                },
            )
        self.assertEqual(switched["thread"]["agent_runtime"], "codex")
        self.assertEqual(switched["thread"]["model"], "gpt-5.6-terra")

        # The same thread stays in the listing on its replacement
        # configuration, and a superseded model is still refused for new
        # threads.
        _, threads = self.request("GET", "/v1/threads")
        listed = {thread["thread_id"]: thread for thread in threads["threads"]}
        self.assertEqual(listed["legacy-alias-thread"]["model"], "gpt-5.6-terra")
        self.assertEqual(listed["legacy-alias-thread"]["status"], "running")

        with self.assertRaises(urllib.error.HTTPError) as new_thread_error:
            self.request(
                "POST",
                "/v1/threads/new-alias-thread/messages",
                {
                    "message": "new thread",
                    "agent_runtime": "claude_code",
                    "model": "opus",
                    "effort": "high",
                },
            )
        self.assertEqual(new_thread_error.exception.code, 400)
        self.assertIn("model must be one of", new_thread_error.exception.read().decode())

    def test_message_without_session_options_requires_an_existing_thread(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("POST", "/v1/threads/unknown-options/messages", {"message": "first"})

        self.assertEqual(error.exception.code, 400)
        self.assertIn("required when starting a new thread", error.exception.read().decode())

    def test_message_session_options_must_be_provided_together(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request(
                "POST",
                "/v1/threads/partial-options/messages",
                {"message": "first", "agent_runtime": "codex", "model": "gpt-5.6-terra"},
            )

        self.assertEqual(error.exception.code, 400)
        self.assertIn("must be provided together", error.exception.read().decode())

    def test_hermes_thread_rejects_steering_without_recording_it(self) -> None:
        seed_thread_session("hermes-thread", "hermes")
        register_live_turn("hermes-thread", "hermes")
        _, before = self.request("GET", "/v1/threads/hermes-thread/events")

        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request(
                "POST", "/v1/threads/hermes-thread/messages", {"message": "change direction"}
            )

        self.assertEqual(error.exception.code, HTTPStatus.CONFLICT)
        self.assertIn(
            "Hermes cannot accept another message while running; wait for it to finish",
            error.exception.read().decode(),
        )
        _, after = self.request("GET", "/v1/threads/hermes-thread/events")
        self.assertEqual(after["events"], before["events"])

    def test_thread_list_reports_configuration_recency_and_live_status(self) -> None:
        seed_thread_session(
            "t1", "codex", provider_session_id="codex-t1", last_used_at="2026-06-08T00:00:03Z"
        )
        seed_thread_session("t2", "codex", last_used_at="2026-06-08T00:00:04Z")
        seed_thread_session(
            "t3", "claude_code", provider_session_id="claude-t3", last_used_at="2026-06-08T00:00:05Z"
        )
        register_live_turn("t2")

        _, body = self.request("GET", "/v1/threads")

        self.assertEqual(
            [(thread["thread_id"], thread["agent_runtime"], thread["status"]) for thread in body["threads"]],
            [("t3", "claude_code", "idle"), ("t2", "codex", "running"), ("t1", "codex", "idle")],
        )
        self.assertEqual(body["threads"][2]["last_used_at"], "2026-06-08T00:00:03Z")
        self.assertEqual(
            set(body["threads"][0]),
            {"thread_id", "agent_runtime", "model", "effort", "last_used_at", "status"},
        )

    def test_thread_list_is_bounded_and_pages_with_an_opaque_cursor(self) -> None:
        seed_thread_session("t1", "codex", last_used_at="2026-06-08T00:00:01Z")
        seed_thread_session("t2", "codex", last_used_at="2026-06-08T00:00:02Z")
        seed_thread_session("t3", "codex", last_used_at="2026-06-08T00:00:03Z")

        _, first = self.request("GET", "/v1/threads?limit=2")
        self.assertEqual(
            [thread["thread_id"] for thread in first["threads"]],
            ["t3", "t2"],
        )
        self.assertIsInstance(first.get("next_before"), str)

        cursor = urllib.parse.quote(first["next_before"], safe="")
        _, second = self.request("GET", f"/v1/threads?limit=2&before={cursor}")
        self.assertEqual(
            [thread["thread_id"] for thread in second["threads"]],
            ["t1"],
        )
        self.assertNotIn("next_before", second)

        for path in (
            "/v1/threads?limit=101",
            "/v1/threads?before=not-a-cursor",
            "/v1/threads?offset=1",
        ):
            with self.subTest(path=path), self.assertRaises(urllib.error.HTTPError) as error:
                self.request("GET", path)
            self.assertEqual(error.exception.code, HTTPStatus.BAD_REQUEST)

    def test_thread_list_cursor_uses_thread_id_as_the_equal_timestamp_tiebreaker(self) -> None:
        timestamp = "2026-06-08T00:00:03Z"
        seed_thread_session("same-a", "codex", last_used_at=timestamp)
        seed_thread_session("same-b", "claude_code", last_used_at=timestamp)

        _, first = self.request("GET", "/v1/threads?limit=1")
        self.assertEqual(
            [thread["thread_id"] for thread in first["threads"]],
            ["same-b"],
        )
        cursor = first["next_before"]
        decoded = json.loads(
            base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        )
        self.assertEqual(decoded, [timestamp, "same-b"])

        _, second = self.request(
            "GET",
            f"/v1/threads?limit=1&before={urllib.parse.quote(cursor, safe='')}",
        )
        self.assertEqual(
            [thread["thread_id"] for thread in second["threads"]],
            ["same-a"],
        )

    def test_thread_detail_returns_thread_or_404_and_rejects_query_params(self) -> None:
        seed_thread_session("t1", "codex", last_used_at="2026-06-08T00:00:03Z")

        _, body = self.request("GET", "/v1/threads/t1")
        self.assertEqual(
            body["thread"],
            {
                "thread_id": "t1",
                "agent_runtime": "codex",
                "model": "gpt-5.6-terra",
                "effort": "high",
                "last_used_at": "2026-06-08T00:00:03Z",
                "status": "idle",
            },
        )

        with self.assertRaises(urllib.error.HTTPError) as missing:
            self.request("GET", "/v1/threads/missing")
        self.assertEqual(missing.exception.code, 404)
        self.assertIn("thread not found", missing.exception.read().decode())

        with self.assertRaises(urllib.error.HTTPError) as query_error:
            self.request("GET", "/v1/threads/t1?limit=5")
        self.assertEqual(query_error.exception.code, 400)
        self.assertIn("does not accept query parameters", query_error.exception.read().decode())

    def test_stop_ends_running_turn_and_late_finish_does_not_resurrect_it(self) -> None:
        with patch.object(orchestrator, "launch_turn"):
            self.request(
                "POST", "/v1/threads/t1/messages", {"message": "long turn", "agent_runtime": "codex"}
            )
        turn = orchestrator._LIVE["codex:t1"]
        turn.server = MagicMock()

        _, body = self.request("POST", "/v1/threads/t1/stop")

        self.assertEqual(body["status"], "accepted")
        turn.server.interrupt.assert_called_once_with()
        self.assertEqual(turn.phase, orchestrator.ExecutionPhase.FINISHING)
        _, events = self.request("GET", "/v1/threads/t1/events")
        self.assertEqual(
            [event["event_type"] for event in events["events"]],
            ["thread.message", "thread.stopped"],
        )

        # The thread stays fenced until the owning turn thread releases it, so
        # a new message is rejected with a retry hint rather than queued.
        self.assertIn("t1", orchestrator.live_thread_ids())
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("POST", "/v1/threads/t1/messages", {"message": "again"})
        self.assertEqual(error.exception.code, 409)
        self.assertIn("agent is finishing", error.exception.read().decode())

        # The turn thread observing the dead process later must not resurrect
        # the stopped turn, but the session id it learned mid-turn is persisted
        # so the thread's next turn can resume it.
        orchestrator._finish_turn(turn, provider_session_id="sess-9")
        _, events = self.request("GET", "/v1/threads/t1/events")
        self.assertEqual(
            [event["event_type"] for event in events["events"]],
            ["thread.message", "thread.stopped"],
        )
        config = state.thread_session_config("t1")
        self.assertIsNotNone(config)
        assert config is not None
        self.assertEqual(config["provider_session_id"], "sess-9")

    def test_prune_state_caps_thread_maps_but_keeps_event_referenced_threads(self) -> None:
        map_limit = 6
        with state.mutation() as cur:
            for n in range(map_limit + 5):
                state.save_thread_session(
                    cur, "codex", f"codex-chat-{n}", f"thread_{n}",
                    f"2026-06-08T{n // 60:02d}:{n % 60:02d}:00Z", "gpt-5.6-terra", "high",
                )
                state.save_thread_session(
                    cur, "claude_code", f"claude-chat-{n}", f"session_{n}",
                    f"2026-06-09T{n // 60:02d}:{n % 60:02d}:00Z", "claude-opus-5", "high",
                )
            state.append_agent_event(
                cur,
                "thread.message",
                "codex-chat-0",
                {"message": "retained", "source": "user"},
            )

        with patch.object(admin_api, "THREAD_MAP_LIMIT", map_limit):
            admin_api.prune_state()

        remaining = {thread["thread_id"] for thread in state.page_thread_summaries(None, 100)}
        codex_history = {thread for thread in remaining if thread.startswith("codex-chat-")}
        claude_history = {thread for thread in remaining if thread.startswith("claude-chat-")}
        self.assertEqual(
            codex_history,
            {"codex-chat-0", *(f"codex-chat-{n}" for n in range(5, map_limit + 5))},
        )
        self.assertEqual(
            claude_history,
            {f"claude-chat-{n}" for n in range(5, map_limit + 5)},
        )

    def test_clearing_working_memory_starts_the_next_run_without_a_handoff(self) -> None:
        seed_thread_session("cleared", "codex", provider_session_id="codex-session")
        with state.mutation() as cur:
            state.append_agent_event(
                cur, "thread.message", "cleared", {"message": "secret plan", "source": "user"}
            )
            state.append_agent_event(
                cur, "thread.message", "cleared", {"message": "acknowledged", "source": "agent"}
            )

        _, cleared = self.request("POST", "/v1/threads/cleared/clear-memory")
        self.assertEqual(cleared["status"], "cleared")

        config = state.thread_session_config("cleared")
        assert config is not None
        self.assertIsNone(config["provider_session_id"])
        # Runtime configuration survives; only the provider conversation goes.
        self.assertEqual(config["agent_runtime"], "codex")

        _, events = self.request("GET", "/v1/threads/cleared/events?since=0")
        marker = events["events"][-1]
        self.assertEqual(marker["event_type"], "thread.memory_cleared")
        self.assertEqual(
            marker["payload"]["message"], admin_api.WORKING_MEMORY_CLEARED_NOTICE
        )
        # No activity payload: the Chat renderer merges events by activity id,
        # so a fixed id would collapse a second clear onto the first.
        self.assertIsNone(marker["payload"].get("activity"))
        # The prior conversation is still readable; clearing is not deletion.
        self.assertEqual(events["events"][0]["payload"]["message"], "secret plan")

        with patch.object(orchestrator, "launch_turn") as launch:
            self.request(
                "POST", "/v1/threads/cleared/messages", {"message": "fresh start", "agent_runtime": "codex"}
            )
        _, launch_message, provider_session_id = launch.call_args.args
        self.assertIsNone(provider_session_id)
        # The missing provider session would normally trigger a replay. The
        # cleared floor is what keeps the launch message bare.
        self.assertEqual(launch_message, "fresh start")
        self.assertNotIn("new agent session continuing", launch_message)
        self.assertNotIn("secret plan", launch_message)

    def test_clearing_then_switching_session_still_sends_a_raw_message(self) -> None:
        """Switching runtime after a clear must not resurrect the handoff.

        The floor empties the retained history, but the switch path would
        still wrap the message in a prompt telling the new session it is
        continuing a thread — the opposite of what a clear just established.
        """
        seed_thread_session(
            "switched",
            "codex",
            model="gpt-5.6-terra",
            effort="high",
            provider_session_id="codex-session",
        )
        with state.mutation() as cur:
            state.append_agent_event(
                cur, "thread.message", "switched", {"message": "old plan", "source": "user"}
            )
        self.request("POST", "/v1/threads/switched/clear-memory")

        with patch.object(orchestrator, "launch_turn") as launch:
            self.request(
                "POST",
                "/v1/threads/switched/messages",
                {
                    "message": "fresh start",
                    "agent_runtime": "codex",
                    "model": "gpt-5.6-sol",
                    "effort": "max",
                },
            )
        _, launch_message, provider_session_id = launch.call_args.args
        self.assertIsNone(provider_session_id)
        self.assertEqual(launch_message, "fresh start")
        self.assertNotIn("continuing a thread", launch_message)
        self.assertNotIn("old plan", launch_message)

    def test_repeated_clears_each_keep_their_own_boundary(self) -> None:
        """Chat merges events that share an activity id, regardless of type.

        A marker carrying a fixed activity id would collapse the second clear
        onto the first one's position, so the transcript would stop showing
        where the latest clear actually happened.
        """
        seed_thread_session("twice", "codex", provider_session_id="codex-session")
        with state.mutation() as cur:
            state.append_agent_event(
                cur, "thread.message", "twice", {"message": "first", "source": "user"}
            )
        self.request("POST", "/v1/threads/twice/clear-memory")
        with state.mutation() as cur:
            state.append_agent_event(
                cur, "thread.message", "twice", {"message": "second", "source": "user"}
            )
        self.request("POST", "/v1/threads/twice/clear-memory")

        _, events = self.request("GET", "/v1/threads/twice/events?since=0")
        markers = [
            event for event in events["events"]
            if event["event_type"] == "thread.memory_cleared"
        ]
        self.assertEqual(len(markers), 2)
        # Distinct seqs and no activity payload, so nothing can merge them.
        self.assertNotEqual(markers[0]["seq"], markers[1]["seq"])
        for marker in markers:
            self.assertIsNone(marker["payload"].get("activity"))
        # The newest floor is the later marker, so the earlier one is not
        # resurrected as handoff context.
        self.assertEqual(
            state.thread_session_config("twice")["context_cleared_seq"],
            markers[1]["seq"],
        )

    def test_clearing_working_memory_rejects_unknown_and_running_threads(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as missing:
            self.request("POST", "/v1/threads/unknown-thread/clear-memory")
        self.assertEqual(missing.exception.code, 404)

        seed_thread_session("busy", "codex", provider_session_id="codex-session")
        with patch.object(orchestrator, "launch_turn"):
            self.request(
                "POST", "/v1/threads/busy/messages", {"message": "long turn", "agent_runtime": "codex"}
            )
        with self.assertRaises(urllib.error.HTTPError) as running:
            self.request("POST", "/v1/threads/busy/clear-memory")
        self.assertEqual(running.exception.code, 409)
        self.assertIn("only while the thread is idle", running.exception.read().decode())
        # A refused clear leaves the live session mapping intact.
        self.assertEqual(
            state.thread_session_config("busy")["provider_session_id"], "codex-session"
        )

    def test_clearing_working_memory_waits_for_a_finishing_turn_to_close(self) -> None:
        """A stopped turn is durably idle while its process is still closing.

        That worker can still report its provider session for the same run
        number, which would restore the mapping the clear just dropped, so the
        clear has to wait for the turn to leave the live set.
        """
        seed_thread_session("finishing", "codex", provider_session_id="codex-session")
        turn = register_live_turn("finishing")
        turn.phase = orchestrator.ExecutionPhase.FINISHING
        # Stopping returns the thread to durable idle; the turn stays live
        # until its process closes, which is exactly the window at issue.
        with state.mutation() as cur:
            state.finish_thread_run(cur, "finishing", turn.run_number)
        self.assertEqual(state.thread_session_config("finishing")["status"], "idle")

        with self.assertRaises(urllib.error.HTTPError) as finishing:
            self.request("POST", "/v1/threads/finishing/clear-memory")
        self.assertEqual(finishing.exception.code, 409)
        self.assertIn("still finishing", finishing.exception.read().decode())
        self.assertEqual(
            state.thread_session_config("finishing")["provider_session_id"],
            "codex-session",
        )
        # No marker is written for a refused clear.
        _, events = self.request("GET", "/v1/threads/finishing/events?since=0")
        self.assertEqual(events["events"], [])

        orchestrator._LIVE.clear()
        _, cleared = self.request("POST", "/v1/threads/finishing/clear-memory")
        self.assertEqual(cleared["status"], "cleared")
        self.assertIsNone(
            state.thread_session_config("finishing")["provider_session_id"]
        )

    def test_stop_rejects_threads_without_a_stoppable_turn(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as missing:
            self.request("POST", "/v1/threads/unknown-thread/stop")
        self.assertEqual(missing.exception.code, 404)
        self.assertIn("thread not found", missing.exception.read().decode())

        seed_thread_session("idle-thread")
        with self.assertRaises(urllib.error.HTTPError) as idle:
            self.request("POST", "/v1/threads/idle-thread/stop")
        self.assertEqual(idle.exception.code, 409)
        self.assertIn("the thread has no running work", idle.exception.read().decode())

        # A turn already finishing (its process still closing) is not
        # stoppable again.
        turn = register_live_turn("idle-thread")
        turn.phase = orchestrator.ExecutionPhase.FINISHING
        with self.assertRaises(urllib.error.HTTPError) as finishing:
            self.request("POST", "/v1/threads/idle-thread/stop")
        self.assertEqual(finishing.exception.code, 409)

    def test_thread_event_history_can_be_paged_for_selected_thread(self) -> None:
        seed_thread_session("t1", "codex", last_used_at="2026-06-08T00:00:01Z")
        with state.mutation() as cur:
            state.append_agent_event(cur, "thread.message", "t1", {"message": "done", "source": "user"})
            state.append_agent_event(cur, "thread.message", "t1", {"message": "working", "source": "agent"})
            state.append_agent_event(cur, "thread.message", "t1", {"message": "ok", "source": "agent"})
            state.append_agent_event(cur, "thread.error", "t1", {"error_message": "retryable"})
            state.append_agent_event(cur, "thread.stopped", "t1", {})

        _, first = self.request("GET", "/v1/threads/t1/events")
        self.assertEqual(len(first["events"]), 5)
        self.assertEqual([event["event_type"] for event in first["events"]], [
            "thread.message",
            "thread.message",
            "thread.message",
            "thread.error",
            "thread.stopped",
        ])
        self.assertTrue(all(event["thread_id"] == "t1" for event in first["events"]))
        _, second = self.request("GET", f"/v1/threads/t1/events?since={first['events'][-1]['seq']}")
        self.assertEqual(second["events"], [])

    def test_runtime_status_and_health_report_active_thread_ids(self) -> None:
        register_live_turn("t2")
        register_live_turn("t1")
        register_live_turn("c1", "claude_code")

        _, body = self.request("GET", "/v1/agent-runtime/status")
        by_type = {runtime["type"]: runtime for runtime in body["runtimes"]}
        self.assertEqual(by_type["codex"]["active_thread_ids"], ["t1", "t2"])
        self.assertEqual(by_type["claude_code"]["active_thread_ids"], ["c1"])
        self.assertEqual(by_type["hermes"]["active_thread_ids"], [])

        _, health = self.health()
        self.assertEqual(self.runtime(health)["active_thread_ids"], ["t1", "t2"])

    def test_task_routes_are_removed(self) -> None:
        for method, path in (
            ("POST", "/v1/tasks"),
            ("GET", "/v1/tasks"),
            ("GET", "/v1/tasks/finished"),
            ("GET", "/v1/tasks/task_1"),
            ("PUT", "/v1/tasks/task_1"),
            ("GET", "/v1/tasks/task_1/events"),
            ("POST", "/v1/tasks/task_1/steer"),
            ("POST", "/v1/tasks/task_1/cancel"),
            ("POST", "/v1/tasks/task_1/kill"),
            ("GET", "/v1/threads/t1/tasks"),
        ):
            with self.subTest(method=method, path=path):
                with self.assertRaises(urllib.error.HTTPError) as error:
                    self.request(
                        method, path, {"input_message": "x"} if method != "GET" else None
                    )
                self.assertEqual(error.exception.code, 404)

    def test_message_rejects_partial_configuration_for_existing_threads(self) -> None:
        seed_thread_session("used-by-codex", "codex", last_used_at="2026-06-08T00:00:01Z")
        seed_thread_session("used-by-claude", "claude_code", last_used_at="2026-06-08T00:00:02Z")

        with self.assertRaises(urllib.error.HTTPError) as codex_error:
            self.request(
                "POST",
                "/v1/threads/used-by-codex/messages",
                {"message": "bad", "model": "gpt-5.6-sol"},
            )
        self.assertEqual(codex_error.exception.code, 400)

        with self.assertRaises(urllib.error.HTTPError) as claude_error:
            self.request(
                "POST",
                "/v1/threads/used-by-claude/messages",
                {"message": "bad", "effort": "max"},
            )
        self.assertEqual(claude_error.exception.code, 400)

        with patch.object(orchestrator, "launch_turn"):
            _, accepted = self.request(
                "POST", "/v1/threads/used-by-codex/messages", {"message": "ok"}
            )
        self.assertEqual(accepted["thread"]["thread_id"], "used-by-codex")

    def test_admin_ui_has_activity_and_diagnostic_views(self) -> None:
        runtime = Path(__file__).parents[1] / "host/runtime/admin_api"
        html = (runtime / "admin_ui/index.html").read_text()
        ui = "\n".join(
            path.read_text()
            for path in [runtime / "admin_ui/index.html", runtime / "admin_ui/admin_ui.css",
                         *sorted((runtime / "admin_ui").glob("*.js"))]
        )
        api = (runtime / "service.py").read_text()
        self.assertNotIn("<h2>Sessions</h2>", html)
        self.assertNotIn("Agent sessions", html)
        self.assertNotIn('id="panel-agent"', html)
        self.assertFalse((runtime / "admin_ui" / "threads.js").exists())
        self.assertIn('<link rel="stylesheet" href="/admin_ui.css">', html)
        self.assertIn('<link rel="icon" type="image/svg+xml" href="/favicon.svg">', html)
        self.assertIn('<script type="module" src="/admin_ui/app.js"></script>', html)
        self.assertIn("Cache-Control", api)
        self.assertIn("no-store, max-age=0", api)
        self.assertIn('<img class="brand-mark" width="30" height="30" src="/favicon.svg" alt="">', html)
        self.assertIn('<img class="login-mark" width="44" height="44" src="/favicon.svg" alt="">', html)
        self.assertIn('ADMIN_UI_DIR / "favicon.svg"', api)
        self.assertEqual(html.count('<svg width="19" height="19" viewBox="0 0 20 20"'), 3)
        self.assertNotIn('id="tab-processes"', html)
        self.assertNotIn('id="tab-host-errors"', html)
        self.assertIn('data-action="open-home-view" data-view="processes"', html)
        self.assertIn('data-action="open-home-view" data-view="host-errors"', html)
        self.assertLess(html.index('data-view="tool-log"'), html.index('data-view="host-errors"'))
        self.assertIn("should be investigated by a Kern developer, newest first", html)
        self.assertIn('id="host-error-pager"', html)
        self.assertIn('endpoint: "/v1/host-errors"', ui)
        self.assertIn('"host-error-page": () => hostErrorLog.showPage(button.dataset.page)', ui)
        self.assertNotIn('data-action="resolve-host-error"', ui)
        self.assertNotIn('data-action="dismiss-host-error"', ui)
        self.assertNotIn('data-action="report-host-error"', ui)
        self.assertIn("/v1/agent-processes", ui)
        self.assertIn("refreshAgentProcesses", ui)
        self.assertIn(".tab-button svg { display: block; height: 19px; width: 19px; }", ui)
        self.assertIn('`Host: ${health.agent_name}`', ui)
        self.assertNotIn("animation: panel-in", ui)
        self.assertIn("refreshVisibleTab(name).catch(() => {})", ui)
        self.assertIn('"agent-log": {', ui)
        self.assertIn("enter: [() => agentLog.showFirstPage()]", ui)
        self.assertIn('"net-log": {', ui)
        self.assertIn("enter: [() => netLog.showFirstPage()]", ui)
        self.assertIn('data-action="toggle-net-denied"', html)
        self.assertIn('id="net-event-pager"', html)
        self.assertIn('id="agent-event-pager"', html)
        self.assertIn('"net-page": () => netLog.showPage(button.dataset.page)', ui)
        self.assertIn('"agent-page": () => agentLog.showPage(button.dataset.page)', ui)
        self.assertIn("createPagedLog", ui)
        self.assertIn("EVENT_PAGER_WINDOW", ui)
        self.assertIn("formatNetworkReason", ui)
        self.assertIn("async function refreshOrSkip(work)", ui)
        # The browser authenticates with an HttpOnly session cookie minted by
        # /v1/login and never stores the admin password itself.
        self.assertIn("/v1/login", ui)
        self.assertIn("/v1/logout", ui)
        self.assertIn('"X-Kern-Csrf"', ui)
        # Upgraded browsers must expire the pre-0.44 cleartext password cookie.
        self.assertIn("kern_admin=; path=/; max-age=0", ui)
        self.assertNotIn("getPassword", ui)
        self.assertIn("Memory", ui)
        self.assertIn("Admin volume", ui)
        self.assertIn("Agent volume", ui)
        self.assertIn("filesystemMountTile", ui)
        self.assertIn("memorySwapTile", ui)
        self.assertIn('data-action="refresh-provider-usage"', ui)
        self.assertIn('id="runtime-overview"', html)
        self.assertNotIn("Agent runtimes", html)
        self.assertNotIn("Provider usage", html)
        self.assertIn("usageRing", ui)
        self.assertIn("/v1/agent-runtime/refresh", ui)
        self.assertIn("active_thread_ids", ui)
        self.assertIn("runtime-running-badge", ui)
        self.assertNotIn('data-action="show-thread"', ui)
        self.assertNotIn('data-action="show-task-events"', ui)
        self.assertNotIn('data-action="refresh-task"', ui)
        self.assertNotIn('data-action="refresh-task-events"', ui)
        self.assertNotIn('data-action="new-thread"', ui)
        self.assertNotIn('data-action="create-task"', ui)
        self.assertNotIn('data-action="steer-task"', ui)
        self.assertNotIn('data-action="cancel-task"', ui)
        self.assertNotIn('data-action="kill-task"', ui)
        self.assertNotIn('id="new-task"', html)
        self.assertNotIn('id="composer-target"', html)
        self.assertNotIn('$("new-task-thread").value = selectedThreadId', ui)
        self.assertNotIn('$("new-task-runtime").value = selectedThreadRuntime', ui)
        self.assertIn('button[data-action]', ui)
        self.assertNotIn("onclick=", ui)
        self.assertNotIn("oninput=", ui)
        self.assertIn('id="ai-inference-integrations"', html)
        self.assertIn('id="tools"', html)
        self.assertLess(html.index('id="ai-inference-heading"'), html.index('id="tools-heading"'))
        self.assertLess(html.index('id="tools-heading"'), html.index('id="manual-heading"'))
        self.assertIn('id="github-expansion"', html)
        self.assertIn('id="github-repos"', html)
        self.assertIn('id="domain-rules"', html)
        self.assertIn('id="github-repo"', html)
        self.assertIn('data-action="enable-github-require-approval"', html)
        self.assertIn('data-action="disable-github-require-approval"', html)
        self.assertIn('id="github-pending-pushes"', html)
        self.assertIn('id="github-token"', html)
        self.assertIn('id="github-credential-status"', html)
        self.assertIn('id="github-credential-form-label"', html)
        self.assertIn('data-action="add-github-repo"', html)
        self.assertIn('data-action="set-github-credential"', html)
        self.assertIn('data-action="delete-github-credential"', html)
        self.assertIn('data-action="add-domain-rule"', html)
        self.assertIn('data-action="recheck-github-audit"', html)
        self.assertIn("renderGithubAudit", ui)
        self.assertIn("recheckGithubAudit", ui)
        self.assertIn("refreshPendingGithubPushes", ui)
        self.assertIn('"network": {', ui)
        self.assertIn("tick: [refreshPendingGithubPushes, refreshExpandedToolApprovals]", ui)
        self.assertIn("audit-banner", ui)
        self.assertIn("repoAuditSummary", ui)
        self.assertIn('data-action="toggle-github-repo-audit"', ui)
        self.assertIn("/v1/network-tools/github-audit", ui)
        self.assertNotIn("toggleIntegrationInfo", ui)
        self.assertNotIn("closeIntegrationInfo", ui)
        self.assertNotIn('id="preset-info-popover"', html)
        # Per-integration rows publish immediately; there is no proposal state.
        self.assertIn("MANAGED_INTEGRATIONS", ui)
        self.assertIn("integration_catalog.js", ui)
        self.assertIn("publishPolicy", ui)
        self.assertIn("setIntegrationEnabled", ui)
        self.assertIn('data-action="enable-integration"', ui)
        self.assertIn('data-action="disable-integration"', ui)
        self.assertIn('data-action="remove-github-repo"', ui)
        self.assertIn('data-action="remove-domain-rule"', ui)
        self.assertIn("renderManagedIntegrations", ui)
        self.assertIn("renderGithubRepos", ui)
        self.assertIn("renderDomainRules", ui)
        self.assertIn("objectValue", ui)
        self.assertIn("!Array.isArray(value)", ui)
        self.assertNotIn("proposedNetworkPolicy", ui)
        self.assertNotIn("POLICY_PRESETS", ui)
        self.assertNotIn("applyPolicyPreset", ui)
        self.assertNotIn("Proposed policy", ui)
        self.assertNotIn("Managed integrations", html)
        self.assertNotIn("Curated access bundles", html)
        self.assertIn('<section class="integration-row"', ui)
        # Home cards open the focused configuration and complete guide page.
        self.assertIn('data-action="open-home-integration"', ui)
        self.assertIn("function sortHomeIntegrationCards()", ui)
        self.assertIn('leftEnabled ? -1 : 1', ui)
        self.assertIn('leftLabel.localeCompare(rightLabel', ui)
        self.assertIn("Integration guide", ui)
        self.assertIn("Authenticated traffic for another account is denied", ui)
        self.assertIn("writes work only for the repositories you configure", ui)
        self.assertNotIn("iconTile", ui)
        self.assertNotIn('class="icon-tile"', html)
        self.assertIn('data-provider-status="${esc(name)}"', ui)
        self.assertIn('connected: <span class="chip-label">${esc(identity)}</span>', ui)
        self.assertIn('Start ${esc(runtimeLabel)} login', ui)
        self.assertIn('>Disconnect</button>', ui)
        self.assertIn('<h2>${esc(meta.label)}</h2>', ui)
        self.assertNotIn('data-action="toggle-integration-expansion"', ui)
        self.assertNotIn("toggleCustomDomainAccess", ui)
        self.assertIn("Custom Domain Access", html)
        self.assertIn('class="status disabled">0 domains enabled', html)
        self.assertIn('id="domain-rule-count"', html)
        self.assertIn('id="custom-domain-details"', html)
        self.assertNotIn('data-action="toggle-custom-domain-access"', html)
        self.assertIn("api.openai.com", ui)
        self.assertIn("pinned-account and external-URL request guards", ui)
        self.assertIn("auth.openai.com", ui)
        self.assertIn("GET and POST", ui)
        self.assertIn("api.anthropic.com", ui)
        self.assertIn("pinned-account, OAuth-token, and server-side web-tool guards", ui)
        self.assertIn("api.github.com", ui)
        self.assertIn("GraphQL denied", ui)
        self.assertIn("LFS uploads denied", ui)
        self.assertIn("pypi.org", ui)
        self.assertIn("GET and HEAD only under /simple and /pypi/<package>/json", ui)
        self.assertIn("registry.npmjs.org", ui)
        self.assertIn("Add domain rule", html)
        self.assertIn("Path guards (optional)", html)
        for domain in (
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
            self.assertIn(domain, ui)
        self.assertIn("addDomainRule", ui)
        self.assertNotIn('id="tab-connection-guide"', html)
        self.assertNotIn('id="panel-connection-guide"', html)
        self.assertNotIn('id="connection-guide-index"', html)
        self.assertNotIn('id="connection-guide-select"', html)
        self.assertIn('id="connection-guide-content"', html)
        self.assertIn('data-action="home-back"', html)
        self.assertIn('data-action="open-home-integration"', ui)
        self.assertIn("overflow-y: auto", ui)
        self.assertNotIn('id="policy-message"', html)
        self.assertNotIn('id="tools-message"', html)
        self.assertIn('data-integration-message="${esc(name)}"', ui)
        self.assertIn('data-tool-message="${esc(tool.tool_id)}"', ui)
        self.assertIn("refreshConnectionGuide", ui)
        self.assertNotIn('data-action="open-connection-guide"', ui)
        self.assertIn("guide-protections", ui)
        self.assertIn("Exact network boundary", ui)
        self.assertIn("guide-assets", api)
        self.assertIn("removeDomainRule", ui)
        self.assertNotIn("/v1/tasks", ui)
        self.assertNotIn("task_count", ui)
        self.assertNotIn("ssh_port_opened", ui)

    def test_message_requires_valid_message_agent_runtime_and_thread_id(self) -> None:
        for body in (
            None,
            {},
            {"message": ""},
            {"message": 42},
            {"message": "x" * (admin_api.MESSAGE_LIMIT + 1)},
            {"message": "hello", "agent_runtime": "bad", "model": "x", "effort": "high"},
        ):
            with self.subTest(body=body), self.assertRaises(urllib.error.HTTPError) as error:
                self.request("POST", "/v1/threads/t1/messages", body)
            self.assertEqual(error.exception.code, 400)

        # Thread ids are path components; a malformed one never reaches the
        # message handler.
        for bad in ("x" * 65, quote("has space")):
            with self.subTest(thread_id=bad), self.assertRaises(urllib.error.HTTPError) as error:
                self.request(
                    "POST",
                    f"/v1/threads/{bad}/messages",
                    {"message": "hello", "agent_runtime": "codex"},
                )
            self.assertEqual(error.exception.code, 404)

        with patch.object(orchestrator, "launch_turn"):
            _, body = self.request(
                "POST",
                "/v1/threads/Chat_01-a/messages",
                {"message": "hello", "agent_runtime": "codex"},
            )
        self.assertEqual(body["thread"]["thread_id"], "Chat_01-a")

    def test_network_policy_replace_and_events(self) -> None:
        body = {
            "network_integrations": {
                "openai": {"enabled": True},
                "custom": {"domains": {"api.example.com": {"allow_http_methods": ["GET"], "path_guards": ["^/v1$"]}}},
            },
        }
        _, response = self.request("PUT", "/v1/network/policy", body)

        custom = response["network_controls"]["network_integrations"]["custom"]["domains"]
        self.assertEqual(custom["api.example.com"]["allow_http_methods"], ["GET"])
        # The stored policy keeps the operator-facing shape. Provider hosts are
        # owned by their integration, never listed as custom domains.
        self.assertNotIn("api.openai.com", custom)
        stored = load_policy()
        self.assertEqual(stored, response["network_controls"])
        self.assertNotIn("openai", stored["network_integrations"]["custom"]["domains"])
        self.mock_reconcile.assert_called_once()
        _, current = self.request("GET", "/v1/network/policy")
        self.assertEqual(current["network_controls"], response["network_controls"])

    def test_network_policy_rejects_ssh_port_field(self) -> None:
        body = {"ssh_port_opened": False, "network_integrations": {"openai": {"enabled": True}}}
        with patch("host.runtime.admin_api.service.subprocess.run") as run:
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request("PUT", "/v1/network/policy", body)
        self.assertEqual(error.exception.code, 400)
        run.assert_not_called()

    def test_network_policy_replace_succeeds_when_existing_policy_is_error(self) -> None:
        save_policy({"bogus": True}, "2026-06-08T00:00:01Z")
        body = {"network_integrations": {"openai": {"enabled": True}}}
        status, _ = self.request("PUT", "/v1/network/policy", body)
        self.assertEqual(status, 200)
        self.assertEqual(load_policy()["network_integrations"], {"openai": {"enabled": True}})

    def _enable_github_policy(self) -> None:
        save_policy(
            {
                "network_integrations": {
                    "github": {"enabled": True, "write_repositories": [{"owner": "infiloop2", "repo": "kern"}]}
                },
            },
            "2026-06-08T00:00:01Z",
        )

    def test_github_pending_push_approve_and_reject(self) -> None:
        state.save_proxy_github_token("ghs_working")
        state.enqueue_pending_push(
            "aa11bb22", "infiloop2", "kern",
            [{"old": "0" * 40, "new": "1" * 40, "ref": "refs/heads/main"}], [".github/workflows/ci.yml"],
        )
        state.enqueue_pending_push(
            "cc33dd44", "infiloop2", "kern",
            [{"old": "0" * 40, "new": "2" * 40, "ref": "refs/heads/feat"}], [".github/dependabot.yml"],
        )
        status, listing = self.request("GET", "/v1/network-tools/github-pending-pushes")
        self.assertEqual(status, 200)
        self.assertEqual({p["id"] for p in listing["pending_pushes"]}, {"aa11bb22", "cc33dd44"})

        # Approve invokes the replay helper with the working token, then marks
        # the row approved.
        calls: list[dict] = []
        timeouts: list[int | None] = []

        def fake_helper(command, payload, *, timeout=None):  # type: ignore[no-untyped-def]
            calls.append(payload)
            timeouts.append(timeout)
            return {"ok": True}

        with patch("host.network_integrations.github.push_gate.pending._run_helper_json", side_effect=fake_helper):
            status, approved = self.request(
                "POST", "/v1/network-tools/github-pending-pushes/aa11bb22/approve", {}
            )
        self.assertEqual(status, 200)
        self.assertEqual(approved["pending_push"]["status"], "approved")
        self.assertEqual(calls[0]["action"], "approve")
        self.assertEqual(calls[0]["token"], "ghs_working")
        self.assertEqual(calls[0]["ref_updates"][0]["ref"], "refs/heads/main")
        self.assertEqual(timeouts[0], github_pending_push.APPROVE_HELPER_TIMEOUT_SECONDS)

        # Reject invokes the helper in cleanup mode, then marks the row.
        with patch("host.network_integrations.github.push_gate.pending._run_helper_json", side_effect=fake_helper):
            _, rejected = self.request(
                "POST", "/v1/network-tools/github-pending-pushes/cc33dd44/reject", {}
            )
        self.assertEqual(rejected["pending_push"]["status"], "rejected")
        self.assertEqual(calls[1]["action"], "cleanup")
        self.assertNotIn("token", calls[1])
        self.assertNotIn("ref_updates", calls[1])
        self.assertEqual(timeouts[1], github_pending_push.APPROVE_HELPER_TIMEOUT_SECONDS)

        state.enqueue_pending_push(
            "dd55ee66", "infiloop2", "kern",
            [{"old": "0" * 40, "new": "3" * 40, "ref": "refs/heads/rejected"}], [".github/workflows/fail.yml"],
        )
        failure_calls: list[dict] = []

        def fail_approve_then_cleanup(command, payload, *, timeout=None):  # type: ignore[no-untyped-def]
            failure_calls.append(payload)
            self.assertEqual(timeout, github_pending_push.APPROVE_HELPER_TIMEOUT_SECONDS)
            if payload["action"] == "approve":
                raise github_credential.HelperError("lease rejected")
            return {"ok": True}

        with patch("host.network_integrations.github.push_gate.pending._run_helper_json", side_effect=fail_approve_then_cleanup):
            with self.assertRaises(urllib.error.HTTPError) as failed:
                self.request("POST", "/v1/network-tools/github-pending-pushes/dd55ee66/approve", {})
        self.assertEqual(failed.exception.code, 409)
        failed_row = state.get_pending_push("dd55ee66")
        self.assertIsNotNone(failed_row)
        assert failed_row is not None
        self.assertEqual(failed_row["status"], "failed")
        self.assertEqual([call["action"] for call in failure_calls], ["approve", "cleanup"])
        self.assertNotIn("token", failure_calls[1])

        state.save_proxy_github_token(None)
        state.enqueue_pending_push(
            "0badcafe", "infiloop2", "kern",
            [{"old": "0" * 40, "new": "8" * 40, "ref": "refs/heads/no-token"}],
            [".github/workflows/no-token.yml"],
        )
        # Approving with no working token resolves the row exactly once: the
        # replay never runs (no token to push with), so the push fails
        # terminally and only the quarantine refs are torn down. Recovery is a
        # fresh push once the credential is fixed.
        with patch("host.network_integrations.github.push_gate.pending._run_helper_json") as no_token_run:
            with self.assertRaises(urllib.error.HTTPError) as no_token:
                self.request("POST", "/v1/network-tools/github-pending-pushes/0badcafe/approve", {})
        self.assertEqual(no_token.exception.code, 409)
        self.assertEqual([call.args[1]["action"] for call in no_token_run.call_args_list], ["cleanup"])
        self.assertNotIn("token", no_token_run.call_args_list[0].args[1])
        no_token_row = state.get_pending_push("0badcafe")
        self.assertIsNotNone(no_token_row)
        assert no_token_row is not None
        self.assertEqual(no_token_row["status"], "failed")
        state.save_proxy_github_token("ghs_working")

        state.enqueue_pending_push(
            "ff99aa00", "infiloop2", "kern",
            [{"old": "0" * 40, "new": "5" * 40, "ref": "refs/heads/cleanup-lock"}],
            [".github/workflows/cleanup.yml"],
        )
        cleanup_calls: list[dict] = []

        def fail_cleanup(command, payload, *, timeout=None):  # type: ignore[no-untyped-def]
            cleanup_calls.append(payload)
            raise github_credential.HelperError("stale lock")

        with patch("host.network_integrations.github.push_gate.pending._run_helper_json", side_effect=fail_cleanup):
            # Rejecting means the push never leaves the box: a failed ref
            # cleanup (best-effort housekeeping) does not change the outcome.
            status, rejected_with_bad_cleanup = self.request(
                "POST", "/v1/network-tools/github-pending-pushes/ff99aa00/reject", {}
            )
        self.assertEqual(status, 200)
        self.assertEqual(rejected_with_bad_cleanup["pending_push"]["status"], "rejected")
        self.assertEqual([call["action"] for call in cleanup_calls], ["cleanup"])

        # A missing id is 404; an already-resolved one is 409.
        with self.assertRaises(urllib.error.HTTPError) as missing:
            self.request("POST", "/v1/network-tools/github-pending-pushes/deadbeef/approve", {})
        self.assertEqual(missing.exception.code, 404)
        with self.assertRaises(urllib.error.HTTPError) as resolved:
            self.request("POST", "/v1/network-tools/github-pending-pushes/cc33dd44/reject", {})
        self.assertEqual(resolved.exception.code, 409)

        state.enqueue_pending_push(
            "ee77ff88", "infiloop2", "kern",
            [{"old": "0" * 40, "new": "4" * 40, "ref": "refs/heads/racing"}], [".github/workflows/race.yml"],
        )
        # A resolution racing another one gets a crisp conflict: resolutions
        # serialize on RESOLVE_LOCK with a bounded wait.
        self.assertTrue(github_pending_push.RESOLVE_LOCK.acquire(timeout=1))
        self.addCleanup(github_pending_push.RESOLVE_LOCK.release)
        with patch.object(github_pending_push, "RESOLVE_LOCK_TIMEOUT_SECONDS", 0.05):
            with patch("host.network_integrations.github.push_gate.pending._run_helper_json") as run:
                with self.assertRaises(urllib.error.HTTPError) as racing:
                    self.request("POST", "/v1/network-tools/github-pending-pushes/ee77ff88/reject", {})
        self.assertEqual(racing.exception.code, 409)
        run.assert_not_called()

    def test_github_credential_pat_round_trip_publishes_working_token(self) -> None:
        self._enable_github_policy()
        _, empty = self.request("GET", "/v1/network-tools/github-credential")
        self.assertFalse(empty["configured"])
        self.assertEqual(empty["repository_audits"][0]["owner"], "infiloop2")
        self.assertEqual(empty["repository_audits"][0]["repo"], "kern")
        self.assertEqual(empty["repository_audits"][0]["warnings"][0]["code"], "repository_audit_incomplete")
        self.assertIn("repository audit has not run yet", empty["repository_audits"][0]["warnings"][0]["message"])

        _, saved = self.request(
            "PUT",
            "/v1/network-tools/github-credential",
            {"mode": "pat", "token": "github_pat_test"},
        )
        self.assertTrue(saved["configured"])
        self.assertEqual(saved["mode"], "pat")
        self.assertNotIn("github_pat_test", json.dumps(saved))
        self.assertEqual(state.read_proxy_github_token(), "github_pat_test")

        _, loaded = self.request("GET", "/v1/network-tools/github-credential")
        self.assertTrue(loaded["configured"])
        self.assertNotIn("github_pat_test", json.dumps(loaded))

        _, deleted = self.request("DELETE", "/v1/network-tools/github-credential")
        self.assertFalse(deleted["configured"])
        self.assertEqual(deleted["repository_audits"][0]["warnings"][0]["code"], "repository_audit_incomplete")
        self.assertIsNone(state.read_proxy_github_token())

    def test_github_repo_audit_without_credential_is_returned_as_warning(self) -> None:
        status, _ = self.request(
            "PUT",
            "/v1/network/policy",
            {
                "network_integrations": {
                    "github": {"enabled": True, "write_repositories": [{"owner": "infiloop2", "repo": "kern"}]}
                },
            },
        )
        self.assertEqual(status, 200)

        _, metadata = self.request("GET", "/v1/network-tools/github-credential")
        self.assertFalse(metadata["configured"])
        warning = metadata["repository_audits"][0]["warnings"][0]
        self.assertEqual(warning["code"], "repository_audit_incomplete")
        self.assertEqual(warning["severity"], "warning")
        self.assertIn("no credential token to audit with", warning["message"])

    def test_github_credential_app_mode_mints_and_publishes(self) -> None:
        self._enable_github_policy()
        mints: list[int] = []

        def fake_helper(command, payload):  # type: ignore[no-untyped-def]
            if command is github_credential.MINT_COMMAND:
                mints.append(1)
                self.assertEqual(payload["app_id"], "12345")
                self.assertEqual(payload["installation_id"], "67890")
                # Installation-wide: the mint request carries no repositories.
                self.assertNotIn("repositories", payload)
                return {"token": f"ghs_minted_{len(mints)}", "expires_at": "2999-01-01T00:00:00Z"}
            raise AssertionError(f"unexpected helper call: {command}")

        with patch("host.runtime.admin_api.github_credential._run_helper_json", side_effect=fake_helper):
            _, saved = self.request(
                "PUT",
                "/v1/network-tools/github-credential",
                {
                    "mode": "app",
                    "app_id": "12345",
                    "installation_id": "67890",
                    "private_key_pem": "-----BEGIN RSA PRIVATE KEY-----\nMIIB\n-----END RSA PRIVATE KEY-----",
                },
            )
            # A fresh token is reused, not re-minted, by plain convergences
            # (the poller path).
            github_credential.reconcile()
            self.assertEqual(len(mints), 1)
            # A policy publish that changes the write list force-mints even
            # though the cached token is fresh: an installation token only
            # covers repositories granted at mint time, so it must postdate the
            # write-repository list.
            self.request(
                "PUT",
                "/v1/network/policy",
                {
                    "network_integrations": {
                        "github": {
                            "enabled": True,
                            "write_repositories": [
                                {"owner": "infiloop2", "repo": "kern"},
                                {"owner": "infiloop2", "repo": "infibot"},
                            ],
                        }
                    },
                },
            )
        self.assertEqual(len(mints), 2)
        self.assertEqual(saved["mode"], "app")
        self.assertEqual(saved["app_id"], "12345")
        self.assertEqual(saved["app_token_expires_at"], "2999-01-01T00:00:00Z")
        self.assertEqual(saved["validation"]["status"], "ok")
        self.assertNotIn("ghs_minted_1", json.dumps({k: v for k, v in saved.items()}))
        self.assertNotIn("BEGIN RSA", json.dumps(saved))
        # The repository-list publish left the re-minted token published.
        self.assertEqual(state.read_proxy_github_token(), "ghs_minted_2")

    def test_github_credential_app_mode_mint_failure_records_validation(self) -> None:
        self._enable_github_policy()
        # A failing mint keeps the credential configured and lands the
        # failure in the validation status instead of an HTTP error.
        with patch(
            "host.runtime.admin_api.github_credential._run_helper_json",
            side_effect=github_credential.HelperError("mint upstream down"),
        ):
            _, saved = self.request(
                "PUT",
                "/v1/network-tools/github-credential",
                {
                    "mode": "app",
                    "app_id": "12345",
                    "installation_id": "67890",
                    "private_key_pem": "-----BEGIN RSA PRIVATE KEY-----\nMIIB\n-----END RSA PRIVATE KEY-----",
                },
            )
        self.assertEqual(saved["mode"], "app")
        self.assertEqual(saved["validation"]["status"], "error")

    def test_mint_failure_fails_closed_and_recovers_on_retry(self) -> None:
        # A mint failure fails closed — the working token is withdrawn
        # and the error recorded — and the next poller reconcile converges
        # once the mint recovers. Deliberately simple: no fallback token.
        self._enable_github_policy()
        near = (datetime.now(timezone.utc) + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        state.save_proxy_github_token("ghs_previous", near)
        save_github_credential(
            {
                "mode": "app",
                "app_id": "12345",
                "installation_id": "67890",
                "private_key_pem": "-----BEGIN RSA PRIVATE KEY-----\nMIIB\n-----END RSA PRIVATE KEY-----",
                "updated_at": "2026-06-08T00:00:00Z",
                "validation": {"status": "ok"},
            }
        )
        mint_up = {"up": False}

        def fake_helper(command, payload):  # type: ignore[no-untyped-def]
            if command is github_credential.MINT_COMMAND:
                if not mint_up["up"]:
                    raise github_credential.HelperError("mint upstream 503")
                return {"token": "ghs_recovered", "expires_at": "2999-01-01T00:00:00Z"}
            raise AssertionError(f"unexpected helper call: {command}")

        with patch("host.runtime.admin_api.github_credential._run_helper_json", side_effect=fake_helper):
            github_credential.reconcile()
            self.assertIsNone(state.read_proxy_github_token())
            _, metadata = self.request("GET", "/v1/network-tools/github-credential")
            self.assertEqual(metadata["validation"]["status"], "error")
            mint_up["up"] = True
            github_credential.reconcile()
        self.assertEqual(state.read_proxy_github_token(), "ghs_recovered")
        _, healthy = self.request("GET", "/v1/network-tools/github-credential")
        self.assertEqual(healthy["validation"]["status"], "ok")

    def test_replacement_mint_failure_retires_the_previous_token(self) -> None:
        # Replacing an installed PAT with an App credential that cannot mint
        # (bad installation id, GitHub outage) must not leave the retired PAT
        # injectable: the credential it belonged to is gone.
        self._enable_github_policy()
        self.request(
            "PUT",
            "/v1/network-tools/github-credential",
            {"mode": "pat", "token": "github_pat_retired"},
        )
        self.assertEqual(state.read_proxy_github_token(), "github_pat_retired")

        def fake_helper(command, payload):  # type: ignore[no-untyped-def]
            if command is github_credential.MINT_COMMAND:
                raise github_credential.HelperError("mint upstream 503")
            raise AssertionError(f"unexpected helper call: {command}")

        with patch("host.runtime.admin_api.github_credential._run_helper_json", side_effect=fake_helper):
            _, saved = self.request(
                "PUT",
                "/v1/network-tools/github-credential",
                {
                    "mode": "app",
                    "app_id": "12345",
                    "installation_id": "67890",
                    "private_key_pem": "-----BEGIN RSA PRIVATE KEY-----\nMIIB\n-----END RSA PRIVATE KEY-----",
                },
            )
        # The new credential is stored with the mint failure recorded, and
        # the retired PAT is withdrawn.
        self.assertEqual(saved["mode"], "app")
        self.assertEqual(saved["validation"]["status"], "error")
        self.assertIsNone(state.read_proxy_github_token())

    def test_enabling_github_mints_a_fresh_token_even_with_a_fresh_published_token(self) -> None:
        # A publish that enables GitHub in App mode always mints fresh: the
        # published repository list may include repositories granted to the
        # installation after the current token was minted, so the installed
        # token must postdate the list — the comfortably fresh published
        # token is deliberately not kept.
        save_github_credential(
            {
                "mode": "app",
                "app_id": "12345",
                "installation_id": "67890",
                "private_key_pem": "-----BEGIN RSA PRIVATE KEY-----\nMIIB\n-----END RSA PRIVATE KEY-----",
                "updated_at": "2026-06-08T00:00:00Z",
                "validation": {"status": "not_checked"},
            }
        )
        state.save_proxy_github_token("ghs_pre_grant", "2999-01-01T00:00:00Z")
        mints: list[int] = []

        def fake_helper(command, payload):  # type: ignore[no-untyped-def]
            if command is github_credential.MINT_COMMAND:
                mints.append(1)
                return {"token": "ghs_post_grant", "expires_at": "2999-01-01T00:00:00Z"}
            raise AssertionError(f"unexpected helper call: {command}")

        with patch("host.runtime.admin_api.github_credential._run_helper_json", side_effect=fake_helper):
            status, _ = self.request(
                "PUT",
                "/v1/network/policy",
                {
                    "network_integrations": {
                        "github": {"enabled": True, "write_repositories": [{"owner": "infiloop2", "repo": "just-granted"}]}
                    },
                },
            )
        self.assertEqual(status, 200)
        self.assertEqual(mints, [1])
        self.assertEqual(state.read_proxy_github_token(), "ghs_post_grant")
        # A publish that adds a write repository widens the scope and mints,
        # since an installation token only covers repositories granted at mint
        # time.
        with patch(
            "host.runtime.admin_api.github_credential._run_helper_json",
            return_value={"token": "ghs_widened", "expires_at": "2999-01-01T00:00:00Z"},
        ):
            self.request(
                "PUT",
                "/v1/network/policy",
                {
                    "network_integrations": {
                        "openai": {"enabled": True},
                        "github": {
                            "enabled": True,
                            "write_repositories": [
                                {"owner": "infiloop2", "repo": "just-granted"},
                                {"owner": "infiloop2", "repo": "kern"},
                            ],
                        },
                    },
                },
            )
        self.assertEqual(state.read_proxy_github_token(), "ghs_widened")
        # Any GitHub-integration change mints fresh — removals included — one
        # simple rule instead of widening-only bookkeeping.
        with patch(
            "host.runtime.admin_api.github_credential._run_helper_json",
            return_value={"token": "ghs_narrowed", "expires_at": "2999-01-01T00:00:00Z"},
        ):
            status, _ = self.request(
                "PUT",
                "/v1/network/policy",
                {
                    "network_integrations": {
                        "github": {"enabled": True, "write_repositories": [{"owner": "infiloop2", "repo": "just-granted"}]}
                    },
                },
            )
        self.assertEqual(status, 200)
        self.assertEqual(state.read_proxy_github_token(), "ghs_narrowed")

        # A publish that does not touch the GitHub integration keeps the
        # healthy published token: no mint, so a transient mint outage cannot
        # break working access.
        with patch(
            "host.runtime.admin_api.github_credential._run_helper_json",
            side_effect=AssertionError("a github-untouched publish must not mint"),
        ):
            status, _ = self.request(
                "PUT",
                "/v1/network/policy",
                {
                    "network_integrations": {
                        "openai": {"enabled": True},
                        "github": {"enabled": True, "write_repositories": [{"owner": "infiloop2", "repo": "just-granted"}]},
                    },
                },
            )
        self.assertEqual(status, 200)
        self.assertEqual(state.read_proxy_github_token(), "ghs_narrowed")

    def test_enabling_github_with_a_failing_mint_publishes_and_fails_closed(self) -> None:
        # Enablement and credential health are separate concerns: a publish
        # that enables GitHub succeeds even when the App mint is down. The
        # credential fails closed — validation error recorded, no token file
        # installed (git/gh run unauthenticated) — and the next reconcile
        # (poller cycle) converges once the mint recovers.
        save_github_credential(
            {
                "mode": "app",
                "app_id": "12345",
                "installation_id": "67890",
                "private_key_pem": "-----BEGIN RSA PRIVATE KEY-----\nMIIB\n-----END RSA PRIVATE KEY-----",
                "updated_at": "2026-06-08T00:00:00Z",
                "validation": {"status": "not_checked"},
            }
        )
        state.save_proxy_github_token("ghs_pre_grant", "2999-01-01T00:00:00Z")
        enabling_policy = {
            "network_integrations": {
                "github": {"enabled": True, "write_repositories": [{"owner": "infiloop2", "repo": "just-granted"}]}
            },
        }
        mint_up = {"up": False}

        def fake_helper(command, payload):  # type: ignore[no-untyped-def]
            if command is github_credential.MINT_COMMAND:
                if not mint_up["up"]:
                    raise github_credential.HelperError("mint upstream 503")
                return {"token": "ghs_post_grant", "expires_at": "2999-01-01T00:00:00Z"}
            raise AssertionError(f"unexpected helper call: {command}")

        with patch("host.runtime.admin_api.github_credential._run_helper_json", side_effect=fake_helper):
            status, _ = self.request("PUT", "/v1/network/policy", enabling_policy)
            self.assertEqual(status, 200)
            # Enabled, but failed closed: the previously published token is
            # withdrawn (it may not cover the published list), and the mint
            # error is visible in the validation status.
            self.assertTrue(load_policy()["network_integrations"]["github"]["enabled"])
            self.assertIsNone(state.read_proxy_github_token())
            _, metadata = self.request("GET", "/v1/network-tools/github-credential")
            self.assertEqual(metadata["validation"]["status"], "error")
            # Mint recovers: the next poller reconcile converges.
            mint_up["up"] = True
            github_credential.reconcile()
        self.assertEqual(state.read_proxy_github_token(), "ghs_post_grant")
        _, healthy = self.request("GET", "/v1/network-tools/github-credential")
        self.assertEqual(healthy["validation"]["status"], "ok")


    def test_enabling_read_only_github_app_publishes_working_token(self) -> None:
        save_github_credential(
            {
                "mode": "app",
                "app_id": "12345",
                "installation_id": "67890",
                "private_key_pem": "-----BEGIN RSA PRIVATE KEY-----\nMIIB\n-----END RSA PRIVATE KEY-----",
                "updated_at": "2026-06-08T00:00:00Z",
                "validation": {"status": "not_checked"},
            }
        )

        def fake_helper(command, payload):  # type: ignore[no-untyped-def]
            if command is github_credential.MINT_COMMAND:
                return {"token": "ghs_read_only", "expires_at": "2999-01-01T00:00:00Z"}
            raise AssertionError(f"unexpected helper call: {command}")

        with patch("host.runtime.admin_api.github_credential._run_helper_json", side_effect=fake_helper):
            status, _ = self.request(
                "PUT",
                "/v1/network/policy",
                {"network_integrations": {"github": {"enabled": True}}},
            )
        self.assertEqual(status, 200)
        self.assertEqual(state.read_proxy_github_token(), "ghs_read_only")


    def test_github_credential_removed_when_policy_disables_github(self) -> None:
        self._enable_github_policy()
        self.request(
            "PUT",
            "/v1/network-tools/github-credential",
            {"mode": "pat", "token": "github_pat_test"},
        )
        self.assertIsNotNone(state.read_proxy_github_token())
        status, _ = self.request(
            "PUT",
            "/v1/network/policy",
            {"network_integrations": {}},
        )
        self.assertEqual(status, 200)
        self.assertIsNone(state.read_proxy_github_token())

    def test_refresh_mid_mint_cannot_overwrite_a_concurrent_delete(self) -> None:
        self._enable_github_policy()
        mint_started = threading.Event()
        release_mint = threading.Event()

        def fake_helper(command, payload):  # type: ignore[no-untyped-def]
            if command is github_credential.MINT_COMMAND:
                mint_started.set()
                release_mint.wait(timeout=10)
                return {
                    "token": "ghs_raced",
                    "expires_at": "2999-01-01T00:00:00Z",
                }
            raise AssertionError(f"unexpected helper call: {command}")

        with patch("host.runtime.admin_api.github_credential._run_helper_json", side_effect=fake_helper):
            # Seed an app credential whose token needs minting, then start a
            # refresh that blocks inside the mint helper.
            save_github_credential(
                {
                    "mode": "app",
                    "app_id": "12345",
                    "installation_id": "67890",
                    "private_key_pem": "-----BEGIN RSA PRIVATE KEY-----\nMIIB\n-----END RSA PRIVATE KEY-----",
                    "updated_at": "2026-06-08T00:00:00Z",
                    "validation": {"status": "not_checked"},
                }
            )
            refresher = threading.Thread(target=github_credential.reconcile, daemon=True)
            refresher.start()
            self.assertTrue(mint_started.wait(timeout=10))
            # DELETE arrives while the mint is in flight; serialization makes
            # it wait for the refresh instead of interleaving with it.
            deleter_result: list[object] = []

            def run_delete() -> None:
                try:
                    deleter_result.append(self.request("DELETE", "/v1/network-tools/github-credential"))
                except Exception as exc:  # noqa: BLE001 - surfaced in assertions
                    deleter_result.append(exc)

            deleter = threading.Thread(target=run_delete, daemon=True)
            deleter.start()
            release_mint.set()
            refresher.join(timeout=15)
            deleter.join(timeout=15)
        self.assertFalse(refresher.is_alive())
        self.assertFalse(deleter.is_alive())
        # Whatever the interleaving, the end state is consistent: credential
        # gone and no working token left behind.
        _, cleared = self.request("GET", "/v1/network-tools/github-credential")
        self.assertFalse(cleared["configured"])
        self.assertEqual(cleared["repository_audits"][0]["warnings"][0]["code"], "repository_audit_incomplete")
        self.assertIsNone(state.read_proxy_github_token())

    def test_disabled_policy_after_crash_is_converged_by_the_poller(self) -> None:
        # Simulate a crash between committing a GitHub-disabled policy and
        # reconcile() running: the working token is still published and the
        # credential row still reads healthy (status ok).
        state.save_proxy_github_token("github_pat_leftover")
        self.assertIsNotNone(state.read_proxy_github_token())
        save_policy(
            {"network_integrations": {}},
            "2026-06-08T00:00:02Z",
        )
        save_github_credential(
            {
                "mode": "pat",
                "token": "github_pat_leftover",
                "updated_at": "2026-06-08T00:00:00Z",
                "validation": {"status": "ok"},
            }
        )
        # The poller must converge removal even though the status reads ok.
        github_credential.reconcile()
        self.assertIsNone(state.read_proxy_github_token())

    def test_github_credential_stages_while_disabled_and_installs_on_enable(self) -> None:
        # Storing the credential before enabling GitHub is the flow that
        # never leaves the proxy allowing repositories with no token: nothing
        # is published while disabled, and the enabling policy publish
        # publishes the staged token.
        _, saved = self.request(
            "PUT",
            "/v1/network-tools/github-credential",
            {"mode": "pat", "token": "github_pat_staged"},
        )
        self.assertTrue(saved["configured"])
        # Staging while disabled leaves the credential's own health untouched
        # (enablement is not a credential property); nothing is installed.
        self.assertEqual(saved["validation"]["status"], "not_checked")
        self.assertIsNone(state.read_proxy_github_token())
        _, loaded = self.request("GET", "/v1/network-tools/github-credential")
        self.assertTrue(loaded["configured"])
        status, _ = self.request(
            "PUT",
            "/v1/network/policy",
            {
                "network_integrations": {
                    "github": {"enabled": True, "write_repositories": [{"owner": "infiloop2", "repo": "kern"}]}
                },
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(state.read_proxy_github_token(), "github_pat_staged")
        # Deleting while disabled works too: staging is fully symmetric.
        self.request(
            "PUT",
            "/v1/network/policy",
            {"network_integrations": {}},
        )
        self.assertIsNone(state.read_proxy_github_token())
        _, cleared = self.request("DELETE", "/v1/network-tools/github-credential")
        self.assertFalse(cleared["configured"])
        self.assertNotIn("repository_audits", cleared)

    def test_github_credential_rejects_malformed_bodies(self) -> None:
        self._enable_github_policy()
        for index, body in enumerate(
            (
                {},
                {"mode": "pat"},
                {"mode": "pat", "token": ""},
                {"mode": "pat", "token": "token with spaces"},
                {"mode": "pat", "token": "github_pat_test", "credential_id": "github-primary"},
                {"mode": "pat", "token": "github_pat_test", "extra": True},
                {"token": "github_pat_test"},
                {"mode": "app", "app_id": "12345", "installation_id": "67890"},
                {"mode": "app", "app_id": "abc", "installation_id": "67890", "private_key_pem": "-----BEGIN X-----"},
                {"mode": "app", "app_id": "12345", "installation_id": "67890", "private_key_pem": "not a key"},
            )
        ):
            with self.subTest(body=body), self.assertRaises(urllib.error.HTTPError) as error:
                self.request("PUT", "/v1/network-tools/github-credential", body)
            self.assertEqual(error.exception.code, 400)

    def test_concurrent_network_policy_replacements_are_last_writer_wins(self) -> None:
        # No dedicated policy lock: the DB write is atomic under the mutation
        # lock, both requests succeed, and the stored policy is one of the two
        # submitted bodies (never a blend).
        enabled = {"network_integrations": {"openai": {"enabled": True}}}
        disabled = {"network_integrations": {"openai": {"enabled": False}}}
        results: list[dict[str, object]] = []
        threads = [
            threading.Thread(target=lambda body=body: results.append(admin_api.replace_network_policy(body)))
            for body in (enabled, disabled)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(results), 2)
        self.assertIn(load_policy(), [parse_network_controls(body).to_json() for body in (enabled, disabled)])

    def test_reboot_helper_swallows_unkillable_timeout(self) -> None:
        # A timed-out helper may still reboot the host, so neither timeout shape
        # (nor the PermissionError an unkillable root child produces) is an error.
        for effect in (subprocess.TimeoutExpired(cmd="reboot-host", timeout=10), PermissionError("not permitted")):
            with patch("host.runtime.admin_api.service.subprocess.run", side_effect=effect):
                self.assertEqual(admin_api.reboot_host(), {"status": "accepted"})

    def test_reboot_helper_failure_returns_500(self) -> None:
        failed = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="sudo: not allowed")
        with patch("host.runtime.admin_api.service.subprocess.run", return_value=failed):
            with self.assertRaises(admin_api.ApiError) as error:
                admin_api.reboot_host()
        self.assertEqual(error.exception.status, HTTPStatus.INTERNAL_SERVER_ERROR)
        self.assertEqual(error.exception.message, "sudo: not allowed")

    def test_login_completion_clears_device_login_record(self) -> None:
        # Once the account goes active the device code is spent; keeping the
        # record would replay a dead code if the session later expires back to
        # awaiting_login.
        set_runtime_statuses(codex="awaiting_login", claude_code="deactivated")
        save_oauth_login("codex", {
            "status": "awaiting_login",
            "device_code": "X",
            "login_id": "l1",
            "login_url": "https://auth.openai.com/device",
            "expires_at": "2099-06-08T00:10:00Z",
        })
        with (
            patch(
                "host.runtime.admin_api.orchestrator.codex_app_server.read_completed_device_login_account_id",
                return_value="acct_smoke",
            ),
            patch(
                "host.runtime.admin_api.orchestrator.codex_app_server.account_status",
                return_value=("active", None, "acct_smoke"),
            ),
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("codex"), "active")
        self.assertIsNone(state.oauth_login("codex"))
        self.assertEqual(read_openai_account().get("account_id"), "acct_smoke")
        self.assertEqual(read_proxy_openai_account_id(), "acct_smoke")

    def test_runtime_expiry_clears_openai_proxy_pin_only(self) -> None:
        set_runtime_statuses(codex="active", claude_code="deactivated")
        save_approved_openai_account("acct_smoke")

        with patch(
            "host.runtime.admin_api.orchestrator.codex_app_server.account_status",
            return_value=("awaiting_login", None, None),
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("codex"), "awaiting_login")

        self.assertEqual(read_openai_account().get("account_id"), "acct_smoke")
        self.assertIsNone(read_proxy_openai_account_id())

    def test_runtime_expiry_clears_claude_proxy_pin_only(self) -> None:
        save_policy(
            {
                "network_integrations": {"claude": {"enabled": True}},
            },
            "2026-06-08T00:00:01Z",
        )
        set_runtime_statuses(codex="active", claude_code="active")
        save_claude_account({"account_id": "acct_smoke", "access_token_sha256": "f" * 64})

        with patch(
            "host.runtime.admin_api.orchestrator.claude_code.account_status",
            return_value=("awaiting_login", None, None),
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("claude_code"), "awaiting_login")

        self.assertEqual(read_claude_account(), {"account_id": "acct_smoke", "access_token_sha256": "f" * 64})
        self.assertIsNone(read_proxy_claude_account_id())

    def test_active_claude_runtime_refresh_records_rotated_token(self) -> None:
        # The Claude CLI rotates its OAuth access token on its own schedule;
        # admin metadata follows it, while the proxy pin remains the stable
        # account identity.
        save_policy(
            {
                "network_integrations": {"claude": {"enabled": True}},
            },
            "2026-06-08T00:00:01Z",
        )
        set_runtime_statuses(codex="active", claude_code="active")
        save_attested_claude_account(
            "acct_smoke", organization_id="org_smoke", access_token_sha256="0" * 64
        )

        with (
            patch(
                "host.runtime.admin_api.orchestrator.claude_code.account_status",
                return_value=(
                    "active",
                    None,
                    {"account_id": "acct_smoke", "organization_id": "org_smoke", "access_token_sha256": "1" * 64},
                ),
            ),
            patch(
                "host.runtime.admin_api.orchestrator.claude_code.read_attested_identity",
                return_value={"access_token_sha256": "1" * 64, "account_uuid": "acct_smoke"},
            ),
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("claude_code"), "active")

        self.assertEqual(read_claude_account()["access_token_sha256"], "1" * 64)
        self.assertEqual(read_proxy_claude_account_id(), "acct_smoke")

    def test_active_claude_runtime_refresh_rejects_rotation_to_another_account(self) -> None:
        save_policy(
            {
                "network_integrations": {"claude": {"enabled": True}},
            },
            "2026-06-08T00:00:01Z",
        )
        set_runtime_statuses(codex="active", claude_code="active")
        save_attested_claude_account("acct_operator", access_token_sha256="0" * 64)

        with (
            patch(
                "host.runtime.admin_api.orchestrator.claude_code.account_status",
                return_value=(
                    "active",
                    None,
                    {"account_id": "acct_operator", "email": "operator@example.com", "access_token_sha256": "f" * 64},
                ),
            ),
            patch(
                "host.runtime.admin_api.orchestrator.claude_code.read_attested_identity",
                return_value={"access_token_sha256": "f" * 64, "account_uuid": "acct_attacker"},
            ),
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("claude_code"), "error")

        self.assertEqual(read_claude_account()["account_id"], "acct_operator")
        self.assertIsNone(read_proxy_claude_account_id())
        record = orchestrator.runtime_status_record("claude_code")
        self.assertIn("account changed", record["error_message"])

    def test_agent_accounts_keep_linked_identity_while_not_active(self) -> None:
        set_runtime_statuses(codex="error", claude_code="awaiting_login")
        save_approved_openai_account("acct_smoke", email="codex@example.com", plan_type="pro")
        save_attested_claude_account("acct_claude", email="claude@example.com", access_token_sha256="0" * 64)

        _, body = self.request("GET", "/v1/agent-runtime/account")

        # The anchor identity stays visible while the runtime is not active;
        # plan and usage metadata are reported only for active runtimes.
        self.assertEqual(
            body,
            {
                "accounts": [
                    {
                        "agent_runtime": "codex",
                        "provider": "openai",
                        "status": "error",
                        "account_id": "acct_smoke",
                        "email": "codex@example.com",
                    },
                    {
                        "agent_runtime": "claude_code",
                        "provider": "claude",
                        "status": "awaiting_login",
                        "account_id": "acct_claude",
                        "email": "claude@example.com",
                    },
                    {
                        "provider": "bedrock",
                        "agent_runtimes": ["hermes"],
                        "status": "loading",
                        "bedrock_usage": EMPTY_BEDROCK_USAGE,
                    },
                ]
            },
        )

    def test_agent_accounts_hide_legacy_openai_identity_without_operator_approval(self) -> None:
        set_runtime_statuses(codex="awaiting_login", claude_code="deactivated")
        save_openai_account({"account_id": "acct_legacy", "email": "legacy@example.com"})

        _, body = self.request("GET", "/v1/agent-runtime/account")

        self.assertEqual(body["accounts"][0], {"agent_runtime": "codex", "provider": "openai", "status": "awaiting_login"})

    def test_agent_accounts_hide_legacy_claude_identity_without_attestation(self) -> None:
        set_runtime_statuses(codex="active", claude_code="awaiting_login")
        save_claude_account(
            {"account_id": "acct_legacy", "email": "legacy@example.com", "access_token_sha256": "0" * 64}
        )

        _, body = self.request("GET", "/v1/agent-runtime/account")

        self.assertEqual(
            body["accounts"][1], {"agent_runtime": "claude_code", "provider": "claude", "status": "awaiting_login"}
        )

    def test_agent_accounts_return_provider_records(self) -> None:
        set_runtime_statuses(codex="active", claude_code="awaiting_login")
        save_approved_openai_account(
            "acct_smoke",
            email="codex@example.com",
            plan_type="pro",
            type="chatgpt",
            codex_usage={
                "last_checked_at": "2026-06-29T23:10:00Z",
                "rate_limits": {
                    "primary": {"used_percent": 8, "window_duration_mins": 300, "resets_at": 1782788897},
                    "secondary": {"used_percent": 11, "window_duration_mins": 10080, "resets_at": 1783296254},
                    "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
                },
            },
        )

        _, body = self.request("GET", "/v1/agent-runtime/account")

        self.assertEqual(
            body,
            {
                "accounts": [
                    {
                        "agent_runtime": "codex",
                        "provider": "openai",
                        "status": "active",
                        "account_id": "acct_smoke",
                        "email": "codex@example.com",
                        "plan_type": "pro",
                        "codex_usage": {
                            "last_checked_at": "2026-06-29T23:10:00Z",
                            "rate_limits": {
                                "primary": {
                                    "used_percent": 8,
                                    "window_duration_mins": 300,
                                    "resets_at": 1782788897,
                                },
                                "secondary": {
                                    "used_percent": 11,
                                    "window_duration_mins": 10080,
                                    "resets_at": 1783296254,
                                },
                                "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
                            },
                        },
                    },
                    {"agent_runtime": "claude_code", "provider": "claude", "status": "awaiting_login"},
                    {
                        "provider": "bedrock",
                        "agent_runtimes": ["hermes"],
                        "status": "loading",
                        "bedrock_usage": EMPTY_BEDROCK_USAGE,
                    },
                ]
            },
        )

    def test_agent_accounts_expose_stored_codex_usage(self) -> None:
        # The runtime adapter sanitizes usage at capture and every active
        # refresh rewrites the row, so the API exposes the stored shape as is.
        set_runtime_statuses(codex="active", claude_code="deactivated")
        save_approved_openai_account(
            "acct_smoke",
            plan_type="pro",
            codex_usage={
                "last_checked_at": "2026-06-29T23:10:00Z",
                "rate_limits": {
                    "primary": {
                        "used_percent": 8,
                        "window_duration_mins": 300,
                        "resets_at": 1782788897,
                    },
                    "secondary": {
                        "used_percent": 11,
                        "window_duration_mins": 10080,
                        "resets_at": 1783296254,
                    },
                    "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
                },
            },
        )

        _, body = self.request("GET", "/v1/agent-runtime/account")

        self.assertEqual(
            body["accounts"][0],
            {
                "agent_runtime": "codex",
                "provider": "openai",
                "status": "active",
                "account_id": "acct_smoke",
                "plan_type": "pro",
                "codex_usage": {
                    "last_checked_at": "2026-06-29T23:10:00Z",
                    "rate_limits": {
                        "primary": {
                            "used_percent": 8,
                            "window_duration_mins": 300,
                            "resets_at": 1782788897,
                        },
                        "secondary": {
                            "used_percent": 11,
                            "window_duration_mins": 10080,
                            "resets_at": 1783296254,
                        },
                        "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
                    },
                },
            },
        )

    def test_agent_accounts_return_active_claude_metadata(self) -> None:
        set_runtime_statuses(codex="deactivated", claude_code="active")
        save_attested_claude_account(
            "acct_smoke",
            organization_id="org_smoke",
            email="smoke@example.com",
            plan_type="pro",
            claude_usage={
                "current_session_used_percent": 0,
                "current_session_resets_at": 1782781800,
                "weekly_used_percent": 0,
                "weekly_resets_at": 1783094340,
                "last_checked_at": "2026-06-29T23:10:00Z",
            },
            access_token_sha256="f" * 64,
        )

        _, body = self.request("GET", "/v1/agent-runtime/account")

        self.assertEqual(
            body,
            {
                "accounts": [
                    {"agent_runtime": "codex", "provider": "openai", "status": "deactivated"},
                    {
                        "agent_runtime": "claude_code",
                        "provider": "claude",
                        "status": "active",
                        "account_id": "acct_smoke",
                        "email": "smoke@example.com",
                        "plan_type": "pro",
                        "claude_usage": {
                            "current_session_used_percent": 0,
                            "current_session_resets_at": 1782781800,
                            "weekly_used_percent": 0,
                            "weekly_resets_at": 1783094340,
                            "last_checked_at": "2026-06-29T23:10:00Z",
                        },
                    },
                    {
                        "provider": "bedrock",
                        "agent_runtimes": ["hermes"],
                        "status": "loading",
                        "bedrock_usage": EMPTY_BEDROCK_USAGE,
                    },
                ]
            },
        )

    def test_agent_accounts_return_partial_claude_usage_metadata(self) -> None:
        set_runtime_statuses(codex="active", claude_code="active")
        save_attested_claude_account(
            "acct_smoke",
            claude_usage={
                "current_session_used_percent": 0,
                "weekly_used_percent": 0,
                "weekly_resets_at": 1783094340,
            },
            access_token_sha256="f" * 64,
        )

        _, body = self.request("GET", "/v1/agent-runtime/account")

        self.assertEqual(
            body["accounts"][1]["claude_usage"],
            {
                "current_session_used_percent": 0,
                "weekly_used_percent": 0,
                "weekly_resets_at": 1783094340,
            },
        )

    def test_agent_runtime_refresh_endpoint_refreshes_requested_runtime(self) -> None:
        with patch("host.runtime.admin_api.service.orchestrator.refresh_runtime_status") as refresh:
            _, body = self.request(
                "POST",
                "/v1/agent-runtime/refresh",
                {"agent_runtime": "claude_code"},
            )

        refresh.assert_called_once_with("claude_code", force_provider_probe=True)
        self.assertEqual([account["provider"] for account in body["accounts"]], ["openai", "claude", "bedrock"])

    def test_agent_runtime_refresh_endpoint_forces_requested_bedrock_runtime(self) -> None:
        save_policy(
            {"network_integrations": {"bedrock": {"enabled": True}}},
            "2026-06-08T00:00:00Z",
        )
        with patch("host.runtime.admin_api.service.orchestrator.refresh_runtime_status") as refresh:
            self.request(
                "POST",
                "/v1/agent-runtime/refresh",
                {"agent_runtime": "hermes"},
            )
        refresh.assert_called_once_with("hermes", force_provider_probe=True)

    def test_agent_runtime_refresh_endpoint_refreshes_all_runtimes_by_default(self) -> None:
        save_policy(
            {
                "network_integrations": {
                    "bedrock": {"enabled": True},
                }
            },
            "2026-06-08T00:00:00Z",
        )
        with patch("host.runtime.admin_api.service.orchestrator.refresh_runtime_status") as refresh:
            self.request("POST", "/v1/agent-runtime/refresh", {})

        self.assertEqual(
            [(call.args[0], call.kwargs) for call in refresh.call_args_list],
            [
                ("codex", {"force_provider_probe": True}),
                ("claude_code", {"force_provider_probe": True}),
                ("hermes", {"force_provider_probe": True}),
            ],
        )

    def test_agent_runtime_refresh_endpoint_does_not_force_disabled_bedrock(self) -> None:
        save_policy(
            {"network_integrations": {}},
            "2026-06-08T00:00:00Z",
        )
        with patch("host.runtime.admin_api.service.orchestrator.refresh_runtime_status") as refresh:
            self.request("POST", "/v1/agent-runtime/refresh", {})

        self.assertEqual(
            [(entry.args[0], entry.kwargs) for entry in refresh.call_args_list],
            [
                ("codex", {"force_provider_probe": True}),
                ("claude_code", {"force_provider_probe": True}),
                ("hermes", {"force_provider_probe": False}),
            ],
        )

    def test_agent_runtime_refresh_endpoint_rejects_unknown_runtime(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request(
                "POST",
                "/v1/agent-runtime/refresh",
                {"agent_runtime": "bad"},
            )

        self.assertEqual(error.exception.code, HTTPStatus.BAD_REQUEST)

    def test_agent_account_endpoint_rejects_runtime_filter(self) -> None:
        set_runtime_statuses(codex="active", claude_code="deactivated")
        save_approved_openai_account("acct_smoke")

        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("GET", "/v1/agent-runtime/account?agent_runtime=codex")

        self.assertEqual(error.exception.code, HTTPStatus.BAD_REQUEST)

    def test_current_codex_oauth_login_rejects_active_runtime(self) -> None:
        set_runtime_statuses(codex="active", claude_code="deactivated")

        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("GET", "/v1/agent-runtime/codex-oauth-login")

        self.assertEqual(error.exception.code, 409)

    def test_oauth_start_rejects_disabled_provider_before_spawning_helper(self) -> None:
        save_policy({"network_integrations": {}}, "t")
        set_runtime_statuses(codex="awaiting_login", claude_code="awaiting_login")

        with (
            patch(
                "host.runtime.admin_api.service.codex_app_server.start_device_login",
                side_effect=AssertionError("disabled Codex provider must not spawn login helper"),
            ),
            patch(
                "host.runtime.admin_api.service.claude_code.start_oauth_login",
                side_effect=AssertionError("disabled Claude provider must not spawn login helper"),
            ),
        ):
            for path in ("/v1/agent-runtime/codex-oauth-login", "/v1/agent-runtime/claude-oauth-login"):
                with self.subTest(path=path), self.assertRaises(urllib.error.HTTPError) as error:
                    self.request("POST", path)
                self.assertEqual(error.exception.code, 409)

    def test_current_oauth_rejects_disabled_provider_even_with_stale_oauth_state(self) -> None:
        save_policy({"network_integrations": {}}, "t")
        set_runtime_statuses(codex="awaiting_login", claude_code="awaiting_login")
        save_oauth_login("codex", {
            "status": "awaiting_login",
            "device_code": "CODE",
            "login_id": "login-1",
            "login_url": "https://auth.openai.com/device",
            "expires_at": "2099-06-08T00:10:00Z",
        })
        save_oauth_login("claude", {
            "status": "awaiting_code",
            "login_url": "https://claude.com/cai/oauth/authorize",
            "expires_at": "2099-06-08T00:10:00Z",
        })

        for path in ("/v1/agent-runtime/codex-oauth-login", "/v1/agent-runtime/claude-oauth-login"):
            with self.subTest(path=path), self.assertRaises(urllib.error.HTTPError) as error:
                self.request("GET", path)
            self.assertEqual(error.exception.code, 409)

    def test_codex_oauth_start_closes_helper_if_provider_is_disabled_before_state_save(self) -> None:
        set_runtime_statuses(codex="awaiting_login", claude_code="deactivated")
        login = admin_api.codex_app_server.CodexLogin(
            login_id="login-1",
            verification_url="https://example.com/device",
            user_code="CODE-1",
        )

        with (
            patch("host.runtime.admin_api.service.orchestrator.runtime_network_enabled", side_effect=[True, False]),
            patch("host.runtime.admin_api.service.codex_app_server.start_device_login", return_value=login),
            patch("host.runtime.admin_api.service.codex_app_server.close_login_server") as close_login,
            self.assertRaises(admin_api.ApiError) as error,
        ):
            admin_api.start_codex_oauth_login()

        self.assertEqual(error.exception.status, HTTPStatus.CONFLICT)
        close_login.assert_called_once()
        self.assertIsNone(state.oauth_login("codex"))

    def test_claude_oauth_complete_rejects_disabled_provider_before_touching_helper(self) -> None:
        save_policy({"network_integrations": {}}, "t")

        with (
            patch(
                "host.runtime.admin_api.service.claude_code.complete_oauth_login",
                side_effect=AssertionError("disabled Claude provider must not complete OAuth"),
            ),
            self.assertRaises(admin_api.ApiError) as error,
        ):
            admin_api.complete_claude_oauth_login({"code": "browser-code"})

        self.assertEqual(error.exception.status, HTTPStatus.CONFLICT)

    def test_claude_oauth_complete_keeps_pending_login_for_trusted_account_capture(self) -> None:
        save_policy(
            {
                "network_integrations": {"claude": {"enabled": True}},
            },
            "2026-06-08T00:00:00Z",
        )
        set_runtime_statuses(codex="active", claude_code="awaiting_login")
        save_oauth_login("claude", {
            "status": "awaiting_code",
            "login_url": "https://claude.com/cai/oauth/authorize",
            "expires_at": "2099-06-08T00:10:00Z",
        })

        def refresh(runtime_type: str) -> str:
            self.assertEqual(runtime_type, "claude_code")
            oauth = state.oauth_login("claude")
            self.assertIsNotNone(oauth)
            assert oauth is not None
            self.assertEqual(oauth["status"], "completed")
            # The approval is bound to the token the login wrote: first
            # capture requires attesting this exact hash.
            self.assertEqual(oauth["access_token_sha256"], "a" * 64)
            with state.mutation() as cur:
                state.set_oauth_login(cur, "claude", None)
            return "active"

        with (
            patch("host.runtime.admin_api.service.claude_code.complete_oauth_login") as complete,
            patch(
                "host.runtime.admin_api.service.claude_code.read_claude_account",
                return_value={"access_token_sha256": "a" * 64},
            ),
            patch("host.runtime.admin_api.service.orchestrator.refresh_runtime_status", side_effect=refresh) as refresh_status,
        ):
            self.assertEqual(admin_api.complete_claude_oauth_login({"code": "browser-code"}), {"status": "accepted"})

        complete.assert_called_once_with("browser-code")
        refresh_status.assert_called_once_with("claude_code")
        self.assertIsNone(state.oauth_login("claude"))

    def test_claude_oauth_complete_clears_pending_login_after_non_active_refresh(self) -> None:
        save_policy(
            {"network_integrations": {"claude": {"enabled": True}}},
            "2026-06-08T00:00:00Z",
        )
        set_runtime_statuses(codex="active", claude_code="awaiting_login")
        save_oauth_login("claude", {
            "status": "awaiting_code",
            "login_url": "https://claude.com/cai/oauth/authorize",
            "expires_at": "2099-06-08T00:10:00Z",
        })

        with (
            patch("host.runtime.admin_api.service.claude_code.complete_oauth_login") as complete,
            patch("host.runtime.admin_api.service.claude_code.read_claude_account", return_value=None),
            patch(
                "host.runtime.admin_api.service.orchestrator.refresh_runtime_status",
                return_value="awaiting_login",
            ) as refresh_status,
        ):
            self.assertEqual(admin_api.complete_claude_oauth_login({"code": "browser-code"}), {"status": "accepted"})

        complete.assert_called_once_with("browser-code")
        refresh_status.assert_called_once_with("claude_code")
        self.assertIsNone(state.oauth_login("claude"))

    def test_connect_bedrock_credentials_validates_and_refreshes(self) -> None:
        save_policy(
            {"network_integrations": {"bedrock": {"enabled": True}}},
            "2026-06-08T00:00:00Z",
        )
        with (
            patch(
                "host.runtime.admin_api.orchestrator.replace_and_validate_bedrock_credentials",
                return_value=("active", None),
            ) as replace,
            patch(
                "host.runtime.admin_api.orchestrator.refresh_runtime_status",
                return_value="active",
            ) as refresh_status,
        ):
            response = admin_api.connect_bedrock_credentials(
                {"access_key_id": "AKIAOPERATORKEY00001", "secret_access_key": "S" * 40, "region": "us-west-2"}
            )

        self.assertEqual(response, {"status": "accepted"})
        self.assertEqual(
            refresh_status.call_args_list,
            [call("hermes")],
        )
        replace.assert_called_once_with("AKIAOPERATORKEY00001", "S" * 40, "us-west-2")

    def test_bedrock_account_metadata_has_one_provider_row(self) -> None:
        account = {
            "account_id": "123456789012",
            "arn": "arn:aws:iam::123456789012:user/kern-bedrock",
            "access_key_id": "AKIAOPERATORKEY00001",
        }
        with state.mutation() as cur:
            state.save_bedrock_credential("AKIAOPERATORKEY00001", "S" * 40, "us-east-1", cur)
        with state.mutation() as cur:
            state.save_bedrock_account(account, cur)
        statuses = {"hermes": {"status": "active"}}
        bedrock = admin_api._current_bedrock_account(statuses)
        self.assertEqual(bedrock["provider"], "bedrock")
        self.assertEqual(bedrock["agent_runtimes"], ["hermes"])
        for key in ("account_id", "arn"):
            self.assertEqual(bedrock[key], account[key])
        self.assertEqual(
            admin_api.current_bedrock_credentials(),
            {
                "connected": True,
                "access_key_id": "AKIAOPERATORKEY00001",
                "region": "us-east-1",
            },
        )

    def test_bedrock_account_reports_live_usage(self) -> None:
        # The proxy prices each response and stores the cost; the admin API
        # sums the stored counters for the current month.
        # 1M input at $0.62/M plus 100k output at $1.85/M is $0.805.
        state.record_bedrock_usage(
            "deepseek.v3.2",
            {
                "input_tokens": 1_000_000,
                "output_tokens": 100_000,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
            },
            0.805,
        )
        state.record_bedrock_usage("deepseek.v3.2", None, 0.0)
        # An unknown model was normalized to the shared 'other' bucket, priced
        # at 0 because it is outside the catalog.
        state.record_bedrock_usage(
            "other",
            {"input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 0, "cache_write_tokens": 0},
            0.0,
        )
        statuses = {"hermes": {"status": "awaiting_login"}}
        usage = admin_api._current_bedrock_account(statuses)["bedrock_usage"]
        self.assertEqual(usage["requests"], 3)
        self.assertEqual(usage["metered_requests"], 2)
        self.assertNotIn("unpriced_requests", usage)
        self.assertEqual(usage["input_tokens"], 1_000_010)
        self.assertEqual(usage["output_tokens"], 100_005)
        self.assertAlmostEqual(usage["month_to_date"], 0.805)
        self.assertEqual(usage["currency"], "USD")
        # An unknown model keeps its tokens and requests visible but, being
        # unpriced, adds nothing to the cost.

    def test_connect_bedrock_credentials_validates_while_disabled(self) -> None:
        save_policy(
            {"network_integrations": {"openai": {"enabled": True}}},
            "2026-06-08T00:00:00Z",
        )
        with patch(
            "host.runtime.admin_api.orchestrator.replace_and_validate_bedrock_credentials",
            return_value=("active", None),
        ) as replace:
            response = admin_api.connect_bedrock_credentials(
                {"access_key_id": "AKIAOPERATORKEY00001", "secret_access_key": "S" * 40, "region": "us-east-2"}
            )
        self.assertEqual(response, {"status": "accepted"})
        replace.assert_called_once_with("AKIAOPERATORKEY00001", "S" * 40, "us-east-2")

    def test_connect_bedrock_credentials_surfaces_rejected_candidate(self) -> None:
        save_policy(
            {"network_integrations": {"openai": {"enabled": True}}},
            "2026-06-08T00:00:00Z",
        )
        with patch(
            "host.runtime.admin_api.orchestrator.replace_and_validate_bedrock_credentials",
            return_value=("error", "AWS rejected the connected credential"),
        ):
            with self.assertRaises(admin_api.ApiError) as caught:
                admin_api.connect_bedrock_credentials(
                    {"access_key_id": "AKIAOPERATORKEY00001", "secret_access_key": "T" * 40, "region": "us-east-1"}
                )

        self.assertEqual(caught.exception.status, HTTPStatus.BAD_REQUEST)
        self.assertIn("AWS rejected", caught.exception.message)
        self.assertEqual(admin_api.current_bedrock_credentials(), {"connected": False})

    def test_connect_bedrock_credentials_rejects_missing_fields(self) -> None:
        for body in (
            None,
            [],
            {},
            {"access_key_id": "AKIAOPERATORKEY00001"},
            {"secret_access_key": "x"},
            {"access_key_id": "AKIAOPERATORKEY00001", "secret_access_key": "S" * 40},
        ):
            with self.subTest(body=body), self.assertRaises(admin_api.ApiError) as caught:
                admin_api.connect_bedrock_credentials(body)
            self.assertEqual(caught.exception.status, HTTPStatus.BAD_REQUEST)

    def test_connect_bedrock_credentials_rejects_runtime_and_unknown_fields(self) -> None:
        for field in ("agent_runtime", "runtime", "extra"):
            body = {
                "access_key_id": "AKIAOPERATORKEY00001",
                "secret_access_key": "S" * 40,
                "region": "us-east-1",
                field: "unsupported",
            }
            with self.subTest(field=field), self.assertRaises(admin_api.ApiError) as caught:
                admin_api.connect_bedrock_credentials(body)
            self.assertEqual(caught.exception.status, HTTPStatus.BAD_REQUEST)
            self.assertIn("unexpected request fields", caught.exception.message)

    def test_connect_bedrock_credentials_rejects_non_long_term_key_ids(self) -> None:
        save_policy(
            {"network_integrations": {"bedrock": {"enabled": True}}},
            "2026-06-08T00:00:00Z",
        )
        for access_key_id in (
            "ASIASESSIONKEY000001",  # temporary session credential
            "bad",
            "AKIAOPERATORKEY0001",  # 19 characters
            "AKIAOPERATORKEY000001",  # 21 characters
            "AKIAoperatorkey00001",  # lowercase
        ):
            with self.subTest(access_key_id=access_key_id):
                with self.assertRaises(admin_api.ApiError) as caught:
                    admin_api.connect_bedrock_credentials(
                        {
                            "access_key_id": access_key_id,
                            "secret_access_key": "S" * 40,
                            "region": "us-east-1",
                        }
                    )
                self.assertEqual(caught.exception.status, HTTPStatus.BAD_REQUEST)
                self.assertIn("long-term IAM access key id", caught.exception.message)
        self.assertIsNone(state.read_bedrock_access_key_id())

    def test_disconnect_bedrock_credentials_clears_shared_credential(self) -> None:
        save_policy(
            {"network_integrations": {
                "bedrock": {"enabled": True},
            }},
            "2026-06-08T00:00:00Z",
        )
        with state.mutation() as cur:
            state.save_bedrock_credential("AKIAOPERATORKEY00001", "S" * 40, "us-east-1", cur)
        with state.mutation() as cur:
            state.save_bedrock_account({"account_id": "123456789012", "access_key_id": "AKIAOPERATORKEY00001"}, cur)

        with patch(
            "host.runtime.admin_api.orchestrator._stop_runtime_processes"
        ) as stop_runtime_processes:
            status, response = self.request("DELETE", "/v1/agent-runtime/bedrock-credentials")

        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(response, {"status": "accepted"})
        self.assertEqual(
            [entry.args[0] for entry in stop_runtime_processes.call_args_list],
            ["hermes"],
        )

        self.assertEqual(state.read_bedrock_account(), {})
        self.assertIsNone(state.read_bedrock_proxy_credential())
        self.assertIsNone(state.read_bedrock_credential_secret())
        self.assertIsNone(state.read_bedrock_region())

    def test_reset_linked_account_clears_anchor_pin_and_pending_oauth(self) -> None:
        save_policy(
            {"network_integrations": {"openai": {"enabled": True}}},
            "2026-06-08T00:00:00Z",
        )
        set_runtime_statuses(codex="error", claude_code="deactivated")
        save_oauth_login("codex", {
            "status": "awaiting_login",
            "device_code": "CODE",
            "login_id": "login-1",
            "login_url": "https://auth.openai.com/device",
            "expires_at": "2099-06-08T00:10:00Z",
        })
        save_approved_openai_account("acct_old")
        state.save_proxy_openai_account_id("acct_old")

        completed = subprocess.CompletedProcess(
            [*admin_api.AGENT_AUTH_CLEAR_HELPER_COMMAND, "codex"], 0, stdout='{"removed":[]}', stderr=""
        )
        with (
            patch("host.runtime.admin_api.service.subprocess.run", return_value=completed) as run,
            patch(
                "host.runtime.admin_api.service.orchestrator.refresh_runtime_status",
                return_value="awaiting_login",
            ) as refresh_status,
        ):
            self.assertEqual(
                admin_api.reset_linked_account({"agent_runtime": "codex"}),
                {"status": "accepted"},
            )

        run.assert_called_once_with(
            [*admin_api.AGENT_AUTH_CLEAR_HELPER_COMMAND, "codex"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=admin_api.AGENT_AUTH_CLEAR_HELPER_TIMEOUT_SECONDS,
        )
        refresh_status.assert_called_once_with("codex")
        self.assertEqual(orchestrator.runtime_status("codex"), "awaiting_login")
        self.assertIsNone(state.oauth_login("codex"))
        self.assertIsNone(read_openai_account().get("account_id"))
        self.assertIsNone(read_proxy_openai_account_id())

    def test_reset_linked_account_clears_claude_anchor_and_pin(self) -> None:
        save_policy(
            {"network_integrations": {"claude": {"enabled": True}}},
            "2026-06-08T00:00:00Z",
        )
        set_runtime_statuses(codex="active", claude_code="active")
        save_claude_account({"account_id": "acct_old", "access_token_sha256": "f" * 64})
        state.save_proxy_claude_account_id("acct_old")

        completed = subprocess.CompletedProcess(
            [*admin_api.AGENT_AUTH_CLEAR_HELPER_COMMAND, "claude"], 0, stdout='{"removed":[]}', stderr=""
        )
        with (
            patch("host.runtime.admin_api.service.subprocess.run", return_value=completed) as run,
            patch(
                "host.runtime.admin_api.service.orchestrator.refresh_runtime_status",
                return_value="awaiting_login",
            ) as refresh_status,
        ):
            self.assertEqual(
                admin_api.reset_linked_account({"agent_runtime": "claude_code"}),
                {"status": "accepted"},
            )

        run.assert_called_once_with(
            [*admin_api.AGENT_AUTH_CLEAR_HELPER_COMMAND, "claude"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=admin_api.AGENT_AUTH_CLEAR_HELPER_TIMEOUT_SECONDS,
        )
        refresh_status.assert_called_once_with("claude_code")
        self.assertEqual(read_claude_account(), {})
        self.assertIsNone(read_proxy_claude_account_id())

    def test_reset_linked_account_rejects_unknown_runtime(self) -> None:
        for body in (
            None,
            {},
            {"agent_runtime": "cursor"},
            {"agent_runtime": "hermes"},
        ):
            with self.assertRaises(admin_api.ApiError) as error:
                admin_api.reset_linked_account(body)
            self.assertEqual(error.exception.status, HTTPStatus.BAD_REQUEST)

    def test_reset_linked_account_stops_running_turns_and_clears_auth(self) -> None:
        save_policy(
            {"network_integrations": {"openai": {"enabled": True}}},
            "2026-06-08T00:00:00Z",
        )
        set_runtime_statuses(codex="active", claude_code="deactivated")
        save_oauth_login("codex", {
            "status": "awaiting_login",
            "device_code": "CODE",
            "login_id": "login-1",
            "login_url": "https://auth.openai.com/device",
            "expires_at": "2099-06-08T00:10:00Z",
        })
        seed_thread_session("chat")
        turn = register_live_turn("chat", server=MagicMock())
        save_approved_openai_account("acct_old")
        state.save_proxy_openai_account_id("acct_old")

        completed = subprocess.CompletedProcess(
            [*admin_api.AGENT_AUTH_CLEAR_HELPER_COMMAND, "codex"], 0, stdout='{"removed":[]}', stderr=""
        )
        with (
            patch("host.runtime.admin_api.service.subprocess.run", return_value=completed),
            patch(
                "host.runtime.admin_api.service.orchestrator.refresh_runtime_status",
                return_value="awaiting_login",
            ) as refresh_status,
        ):
            self.assertEqual(admin_api.reset_linked_account({"agent_runtime": "codex"}), {"status": "accepted"})

        refresh_status.assert_called_once_with("codex")
        # The live turn was stopped and failed with the reset reason; the
        # owning turn thread keeps the fence until it observes the close.
        self.assertEqual(turn.phase, orchestrator.ExecutionPhase.FINISHING)
        turn.server.interrupt.assert_called_once_with()
        _, events = self.request("GET", "/v1/threads/chat/events")
        self.assertEqual([event["event_type"] for event in events["events"]], ["thread.error"])
        self.assertIn(
            "linked provider account was reset by the operator",
            events["events"][0]["payload"]["error_message"],
        )
        self.assertIsNone(read_openai_account().get("account_id"))
        self.assertIsNone(read_proxy_openai_account_id())
        self.assertIsNone(state.oauth_login("codex"))

    def test_reset_linked_account_helper_failure_leaves_anchor_cleared_and_refreshes(self) -> None:
        save_policy(
            {"network_integrations": {"openai": {"enabled": True}}},
            "2026-06-08T00:00:00Z",
        )
        save_approved_openai_account("acct_old")
        state.save_proxy_openai_account_id("acct_old")
        failed = subprocess.CompletedProcess(
            [*admin_api.AGENT_AUTH_CLEAR_HELPER_COMMAND, "codex"],
            1,
            stdout="",
            stderr="permission denied",
        )

        with (
            patch("host.runtime.admin_api.service.subprocess.run", return_value=failed),
            patch(
                "host.runtime.admin_api.service.orchestrator.refresh_runtime_status",
                return_value="awaiting_login",
            ) as refresh_status,
            self.assertRaises(admin_api.ApiError) as error,
        ):
            admin_api.reset_linked_account({"agent_runtime": "codex"})

        self.assertEqual(error.exception.status, HTTPStatus.CONFLICT)
        self.assertIn("retry reset", error.exception.message)
        self.assertIn("permission denied", error.exception.message)
        refresh_status.assert_called_once_with("codex")
        self.assertIsNone(read_openai_account().get("account_id"))
        self.assertIsNone(read_proxy_openai_account_id())

    def test_codex_oauth_start_allowed_while_runtime_error(self) -> None:
        # Error states (changed account, malformed local credentials) are
        # recovered by logging in again, so the gate admits them.
        set_runtime_statuses(codex="error", claude_code="deactivated")
        login = admin_api.codex_app_server.CodexLogin(
            login_id="login-1",
            verification_url="https://example.com/device",
            user_code="CODE-1",
        )

        with patch("host.runtime.admin_api.service.codex_app_server.start_device_login", return_value=login):
            response = admin_api.start_codex_oauth_login()

        self.assertEqual(response["device_code"], "CODE-1")

    def test_codex_oauth_start_reuses_existing_login(self) -> None:
        set_runtime_statuses(codex="awaiting_login", claude_code="deactivated")
        login = admin_api.codex_app_server.CodexLogin(
            login_id="login-1",
            verification_url="https://example.com/device",
            user_code="CODE-1",
        )

        with patch("host.runtime.admin_api.service.codex_app_server.start_device_login", return_value=login) as start:
            first = admin_api.start_codex_oauth_login()
            second = admin_api.start_codex_oauth_login()

        self.assertEqual(first, second)
        self.assertEqual(start.call_count, 1)

    def test_claude_oauth_start_reuses_existing_login(self) -> None:
        save_policy(
            {"network_integrations": {"claude": {"enabled": True}}},
            "2026-06-08T00:00:01Z",
        )
        set_runtime_statuses(codex="active", claude_code="awaiting_login")
        login = admin_api.claude_code.ClaudeLogin(login_url="https://claude.com/cai/oauth/authorize?code=true")

        with patch("host.runtime.admin_api.service.claude_code.start_oauth_login", return_value=login) as start:
            first = admin_api.start_claude_oauth_login()
            second = admin_api.start_claude_oauth_login()

        self.assertEqual(first, second)
        self.assertEqual(first["status"], "awaiting_code")
        self.assertEqual(start.call_count, 1)

    def test_network_events_are_read_from_the_database_with_cursor_paging(self) -> None:
        # Network events live in the database now (the proxy writes them under
        # its own role); the admin API exposes a single newest-first cursor.
        for index in range(120):
            append_network_event("https", "GET", "example.com", 443, f"/p{index}", "", index % 2 == 0)

        _, body = self.request("GET", "/v1/network/events")
        seqs = [event["seq"] for event in body["events"]]
        self.assertEqual(len(seqs), 100)
        self.assertEqual(seqs, sorted(seqs, reverse=True))
        self.assertNotIn("page", body)
        self.assertNotIn("total_events", body)

        _, older = self.request("GET", f"/v1/network/events?before={seqs[-1]}")
        older_seqs = [event["seq"] for event in older["events"]]
        self.assertEqual(older_seqs, list(range(20, 0, -1)))

        _, limited = self.request("GET", "/v1/network/events?limit=7")
        self.assertEqual(len(limited["events"]), 7)

        _, denied = self.request("GET", "/v1/network/events?decision=denied")
        self.assertEqual(len(denied["events"]), 60)
        self.assertTrue(all(event["decision"] == "denied" for event in denied["events"]))

        with self.assertRaises(urllib.error.HTTPError) as since_error:
            self.request("GET", "/v1/network/events?since=0")
        self.assertEqual(since_error.exception.code, HTTPStatus.BAD_REQUEST)
        rejected = json.loads(since_error.exception.read())
        self.assertEqual(rejected["error"]["message"], "unsupported network event query parameter: since")

        with self.assertRaises(urllib.error.HTTPError) as limit_error:
            self.request("GET", "/v1/network/events?limit=101")
        self.assertEqual(limit_error.exception.code, HTTPStatus.BAD_REQUEST)
        rejected = json.loads(limit_error.exception.read())
        self.assertEqual(rejected["error"]["message"], "limit must be at most 100")

    def test_agent_events_use_the_same_newest_first_cursor_paging(self) -> None:
        # The agent audit log pages exactly like the network audit log: one
        # newest-first cursor with no filter (thread-scoped tailing has its own
        # since-based endpoint under /v1/threads/{id}/events).
        with state.mutation() as cur:
            for index in range(120):
                state.append_agent_event(cur, "thread.message", "t1", {"message": f"m{index}"})

        _, body = self.request("GET", "/v1/events")
        seqs = [event["seq"] for event in body["events"]]
        self.assertEqual(len(seqs), 100)
        self.assertEqual(seqs, sorted(seqs, reverse=True))
        # Global events carry the thread key, not a task id.
        self.assertEqual(body["events"][0]["thread_id"], "t1")
        self.assertNotIn("task_id", body["events"][0])

        _, older = self.request("GET", f"/v1/events?before={seqs[-1]}")
        older_seqs = [event["seq"] for event in older["events"]]
        self.assertEqual(len(older_seqs), 20)
        self.assertTrue(all(seq < seqs[-1] for seq in older_seqs))

        _, limited = self.request("GET", "/v1/events?limit=7")
        self.assertEqual(len(limited["events"]), 7)

        with self.assertRaises(urllib.error.HTTPError) as since_error:
            self.request("GET", "/v1/events?since=0")
        self.assertEqual(since_error.exception.code, HTTPStatus.BAD_REQUEST)
        rejected = json.loads(since_error.exception.read())
        self.assertEqual(rejected["error"]["message"], "unsupported event query parameter: since")

        with self.assertRaises(urllib.error.HTTPError) as limit_error:
            self.request("GET", "/v1/events?limit=101")
        self.assertEqual(limit_error.exception.code, HTTPStatus.BAD_REQUEST)
        rejected = json.loads(limit_error.exception.read())
        self.assertEqual(rejected["error"]["message"], "limit must be at most 100")

    def test_reboot_uses_privileged_helper(self) -> None:
        succeeded = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with patch("host.runtime.admin_api.service.subprocess.run", return_value=succeeded) as run:
            _, body = self.request("POST", "/v1/host-runtime/reboot")
        self.assertEqual(body["status"], "accepted")
        run.assert_called_with(
            ["/usr/bin/sudo", "-n", "/usr/local/lib/kern-host/reboot-host"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=admin_api.REBOOT_HELPER_TIMEOUT_SECONDS,
        )

    def test_get_network_policy_reads_policy_file(self) -> None:
        save_policy(
            parse_network_controls(
                {
                    "network_integrations": {
                        "openai": {"enabled": True},
                        "custom": {"domains": {"api.example.com": {"allow_http_methods": ["GET"]}}},
                    },
                }
            ).to_json(),
            "2026-06-08T00:00:03Z",
        )
        _, body = self.request("GET", "/v1/network/policy")
        self.assertEqual(
            body["network_controls"]["network_integrations"],
            {
                "openai": {"enabled": True},
                "custom": {"domains": {"api.example.com": {"allow_http_methods": ["GET"]}}},
            },
        )
        self.assertEqual(body["updated_at"], "2026-06-08T00:00:03Z")

    def test_initialize_state_fails_turns_orphaned_by_a_restart(self) -> None:
        seed_thread_session("t1")
        seed_thread_session("t2")
        with state.mutation() as cur:
            run_number = state.start_thread_run(cur, "t1")
            state.append_agent_event(
                cur,
                "thread.message",
                "t1",
                {"message": "interrupted turn", "source": "user"},
                run_number=run_number,
            )

        admin_api.initialize_state()

        _, open_events = self.request("GET", "/v1/threads/t1/events")
        self.assertEqual(
            [event["event_type"] for event in open_events["events"]],
            ["thread.message", "thread.error"],
        )
        self.assertIn(
            "restarted while the thread was running",
            open_events["events"][-1]["payload"]["error_message"],
        )
        # A thread whose newest turn already ended is left alone.
        _, closed_events = self.request("GET", "/v1/threads/t2/events")
        self.assertEqual(
            [event["event_type"] for event in closed_events["events"]],
            [],
        )
        self.assertEqual(state.thread_session_config("t1")["status"], "idle")

    def test_event_seq_commits_atomically_with_the_event(self) -> None:
        # Event seqs come from a database serial: unique and increasing, and
        # an aborted mutation rolls its event row back (burning the seq), so a
        # seq can never appear twice in the log — duplicate seqs would break
        # cursor-based event pagination.
        with state.mutation() as cur:
            first = state.append_agent_event(cur, "thread.message", "t1", {"message": "hello"})
        with self.assertRaises(RuntimeError):
            with state.mutation() as cur:
                state.append_agent_event(cur, "thread.message", "t1", {"message": "aborted"})
                raise RuntimeError("abort after allocating a seq")
        with state.mutation() as cur:
            second = state.append_agent_event(cur, "thread.message", "t1", {"message": "again"})

        self.assertGreater(second, first)
        _, body = self.request("GET", "/v1/events")
        self.assertEqual([event["seq"] for event in body["events"]], [second, first])

    def test_second_instance_fails_on_bind_before_touching_live_state(self) -> None:
        seed_thread_session("t1")
        with state.mutation() as cur:
            state.start_thread_run(cur, "t1")

        # The port bind is the single-instance gate: a second instance must die
        # there before restart recovery could fail the live instance's open
        # turn. The service never runs migrations (that is bootstrap's job), so
        # a stray start also cannot move the schema under the live instance.
        with patch(
            "host.runtime.admin_api.service.BoundedThreadingHTTPServer",
            side_effect=OSError("address already in use"),
        ):
            with self.assertRaises(OSError):
                admin_api.main()

        _, events = self.request("GET", "/v1/threads/t1/events")
        self.assertEqual(events["events"], [])



class ToolRoutesTests(unittest.TestCase):
    def setUp(self) -> None:
        pg_harness.reset_database()
        save_config(
            {
                "agent_name": "kern-test",
                "admin_password_sha256": hashlib.sha256(b"admin-secret").hexdigest(),
            }
        )
        admin_api.admin_auth._sessions.clear()
        self.session_token = admin_api.admin_auth._create_session()
        self.addCleanup(admin_api.admin_auth._sessions.clear)
        self.base_url = start_admin_http_server(self)
        # The operator delegation routes (connect complete/disconnect, approval
        # decide) forward to the kern-tools service socket, so stand one up
        # in-process (same DB and BUNDLED_TOOLS) and point the admin API at it.
        socket_dir = tempfile.TemporaryDirectory()
        self.addCleanup(socket_dir.cleanup)
        tools_socket = str(Path(socket_dir.name) / "tools.sock")
        previous_socket = tools_admin_api.TOOLS_SOCKET_PATH
        tools_admin_api.TOOLS_SOCKET_PATH = tools_socket
        self.addCleanup(setattr, tools_admin_api, "TOOLS_SOCKET_PATH", previous_socket)
        tools_server = tools_api.ToolsServer(
            tools_socket, frozenset({os.getuid()}), frozenset({os.getuid()})
        )
        threading.Thread(target=tools_server.serve_forever, daemon=True).start()
        self.addCleanup(tools_server.server_close)
        self.addCleanup(tools_server.shutdown)

    def request(self, method: str, path: str, body: object | None = None, auth: bool = True):
        data = json.dumps(body).encode() if body is not None else b"{}" if method != "GET" else None
        request = urllib.request.Request(f"{self.base_url}{path}", data=data, method=method)
        if auth:
            _add_session_auth(request, self.session_token)
        if method != "GET":
            request.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())

    def tool_entry(self, body: dict, tool_id: str) -> dict:
        return next(entry for entry in body["tools"] if entry["tool_id"] == tool_id)

    def test_listing_requires_auth_and_reports_manifest_state(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("GET", "/v1/tools", auth=False)
        self.assertEqual(error.exception.code, 401)

        status, body = self.request("GET", "/v1/tools")
        self.assertEqual(status, 200)
        # New bundled packages should not need an edit here; released ids may
        # not vanish. (test_tools_host uses the same issubset contract.)
        self.assertTrue(
            {
                "brave_search",
                "gmail",
                "google_calendar",
                "ibkr",
                "instagram",
                "instagram_discovery",
                "linkedin",
                "linkedin_discovery",
                "polymarket",
                "runway",
                "twitter",
            }.issubset({entry["tool_id"] for entry in body["tools"]})
        )
        gmail = self.tool_entry(body, "gmail")
        self.assertFalse(gmail["enabled"])
        self.assertEqual(gmail["connection"], "oauth")
        self.assertEqual(gmail["connection_status"], {"connected": False})
        self.assertTrue(any(action["id"] == "send_email" for action in gmail["actions"]))
        self.assertTrue(all("output_schema" in action for action in gmail["actions"]))
        # Data policy is per action.
        self.assertTrue(all(action["data_policy"] for action in gmail["actions"]))
        send = next(action for action in gmail["actions"] if action["id"] == "send_email")
        self.assertIn("approval", send["data_policy"].lower())
        self.assertEqual(send["approval"], "operator")
        read = next(action for action in gmail["actions"] if action["id"] == "read_message")
        self.assertEqual(read["approval"], "direct")
        self.assertTrue(all(gmail["protections"]))
        self.assertEqual(gmail["technical_details"], [])
        self.assertGreaterEqual(len(gmail["setup_steps"]), 5)
        self.assertIn("Google Cloud", gmail["setup_steps"][0]["description"])
        self.assertTrue(any(step["image_path"] for step in gmail["setup_steps"]))
        self.assertEqual(
            [card["title"] for card in gmail["data_summary"]["cards"]],
            ["What leaves this host", "Where it can go", "What Google can do with it", "How long Google retains it"],
        )
        reads_point = gmail["data_summary"]["cards"][0]["points"][0]
        self.assertEqual(reads_point["label"], "Reads")
        self.assertIn("Gmail search query", reads_point["text"])
        self.assertIn("your own Gmail account", gmail["data_summary"]["cards"][1]["description"])
        # The callback URI and config keys render inside the step that needs them.
        self.assertTrue(any(step["show_callback"] for step in gmail["setup_steps"]))
        self.assertTrue(gmail["setup_steps"][-1]["show_config"])
        self.assertTrue(
            all(
                link["url"].startswith("https://")
                for card in gmail["data_summary"]["cards"]
                for link in card["links"]
            )
        )
        config_keys = {entry["key"]: entry for entry in gmail["config"]}
        self.assertFalse(config_keys["GOOGLE_OAUTH_CLIENT_ID"]["set"])
        # All config values are secrets; there is no per-key secret flag.
        self.assertNotIn("secret", config_keys["GOOGLE_OAUTH_CLIENT_ID"])

        discovery = self.tool_entry(body, "instagram_discovery")
        self.assertIn("at most 25 unique items", " ".join(discovery["protections"]))
        self.assertIn("maps vendor responses to fixed fields", " ".join(discovery["technical_details"]))
        # Tools whose parameters are guarded carry the shared parameter-guard
        # description; tools without guarded request fields have none.
        for tool_id in ("runway", "twitter"):
            self.assertIn(
                "parameter guard", " ".join(self.tool_entry(body, tool_id)["technical_details"]).lower()
            )
        for tool_id in ("ibkr", "instagram", "linkedin"):
            self.assertEqual(self.tool_entry(body, tool_id)["technical_details"], [])

    def test_config_and_enable_flow(self) -> None:
        # Config is scoped per tool: a key must be declared by that tool.
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("PUT", "/v1/tools/brave_search/config", {"key": "NOT_DECLARED", "value": "x"})
        self.assertEqual(error.exception.code, 400)

        status, body = self.request("GET", "/v1/tools")
        brave = self.tool_entry(body, "brave_search")
        brave_config = {entry["key"]: entry for entry in brave["config"]}
        self.assertFalse(brave_config["BRAVE_SEARCH_API_KEY"]["set"])
        self.assertNotIn("secret", brave_config["BRAVE_SEARCH_API_KEY"])

        # Enablement is not gated on config: enabling without config succeeds, and
        # the tool is enabled even though its config is not set.
        status, body = self.request("POST", "/v1/tools/brave_search/enable")
        self.assertEqual(body, {"tool_id": "brave_search", "enabled": True})
        status, body = self.request("GET", "/v1/tools")
        entry = self.tool_entry(body, "brave_search")
        self.assertTrue(entry["enabled"])
        self.assertFalse(entry["config"][0]["set"])

        # Setting the config flips its set status; enablement is unchanged.
        status, body = self.request("PUT", "/v1/tools/brave_search/config", {"key": "BRAVE_SEARCH_API_KEY", "value": "key-1"})
        self.assertEqual(body, {"tool_id": "brave_search", "key": "BRAVE_SEARCH_API_KEY", "set": True})
        status, body = self.request("GET", "/v1/tools")
        entry = self.tool_entry(body, "brave_search")
        self.assertTrue(entry["enabled"])
        self.assertTrue(entry["config"][0]["set"])

        status, body = self.request("POST", "/v1/tools/brave_search/disable")
        self.assertEqual(body["enabled"], False)
        status, body = self.request("PUT", "/v1/tools/brave_search/config", {"key": "BRAVE_SEARCH_API_KEY", "value": ""})
        self.assertEqual(body["set"], False)

        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("POST", "/v1/tools/unknown_tool/enable")
        self.assertEqual(error.exception.code, 404)

    def test_connect_flow_routing_and_gating(self) -> None:
        # enable_only tools have no connect flow.
        self.request("PUT", "/v1/tools/brave_search/config", {"key": "BRAVE_SEARCH_API_KEY", "value": "key-1"})
        self.request("POST", "/v1/tools/brave_search/enable")
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("POST", "/v1/tools/brave_search/oauth_connect/start", {"redirect_uri": "http://x/cb"})
        self.assertEqual(error.exception.code, 409)

        # An enabled OAuth tool with no client config fails as an actionable
        # operator input error; it is not a tools-service gateway failure.
        self.request("POST", "/v1/tools/gmail/enable")
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request(
                "POST",
                "/v1/tools/gmail/oauth_connect/start",
                {"redirect_uri": "http://localhost:7443/oauth/callback"},
            )
        self.assertEqual(error.exception.code, 400)
        self.request("POST", "/v1/tools/gmail/disable")

        # OAuth tools require enablement before connecting.
        self.request("PUT", "/v1/tools/gmail/config", {"key": "GOOGLE_OAUTH_CLIENT_ID", "value": "client-1"})
        self.request("PUT", "/v1/tools/gmail/config", {"key": "GOOGLE_OAUTH_CLIENT_SECRET", "value": "secret-1"})
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("POST", "/v1/tools/gmail/oauth_connect/start", {"redirect_uri": "http://x/cb"})
        self.assertEqual(error.exception.code, 409)

        self.request("POST", "/v1/tools/gmail/enable")
        status, body = self.request(
            "POST", "/v1/tools/gmail/oauth_connect/start", {"redirect_uri": "http://localhost:7443/oauth/callback"}
        )
        self.assertEqual(status, 200)
        self.assertIn("accounts.google.com", body["authorization_url"])
        self.assertIn("client-1", body["authorization_url"])
        self.assertTrue(body["state"])

        # A forged callback state is rejected without any third-party call.
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request(
                "POST",
                "/v1/tools/gmail/oauth_connect/complete",
                {"code": "code-1", "state": "forged.state", "redirect_uri": "http://localhost:7443/oauth/callback"},
            )
        self.assertEqual(error.exception.code, 400)

        status, body = self.request("POST", "/v1/tools/gmail/oauth_connect/disconnect")
        self.assertEqual(body, {"tool_id": "gmail", "connected": False})

        # Disconnect must stay available after the tool is disabled, or
        # stored OAuth tokens would be stuck with no path to revoke them.
        self.request("POST", "/v1/tools/gmail/disable")
        status, body = self.request("POST", "/v1/tools/gmail/oauth_connect/disconnect")
        self.assertEqual(body, {"tool_id": "gmail", "connected": False})

    def test_approval_decisions(self) -> None:
        from test_tools_host import FakeTool

        with patch.dict(admin_api.tools_host.BUNDLED_TOOLS, {"fake_notes": FakeTool()}):
            self.request("PUT", "/v1/tools/fake_notes/config", {"key": "FAKE_NOTES_TOKEN", "value": "token-1"})
            self.request("POST", "/v1/tools/fake_notes/enable")
            pending = admin_api.tools_host.execute_action("fake_notes", "write_note", {"text": "hello"})
            approval_id = pending["approval_id"]

            status, body = self.request("GET", "/v1/tools/fake_notes/approvals")
            self.assertEqual(body["approvals"][0]["approval_id"], approval_id)
            self.assertEqual(body["approvals"][0]["status"], "pending")
            # The list is summary-only; the payload is fetched on demand.
            self.assertNotIn("payload", body["approvals"][0])
            status, single = self.request("GET", f"/v1/tools/fake_notes/approvals/{approval_id}")
            self.assertEqual(single["approval"]["payload"], {"text": "hello"})
            with self.assertRaises(urllib.error.HTTPError) as missing:
                self.request("GET", "/v1/tools/fake_notes/approvals/approval_9999")
            self.assertEqual(missing.exception.code, 404)
            # An approval is scoped to its tool: another tool's path cannot read
            # or decide it.
            with self.assertRaises(urllib.error.HTTPError) as wrong_tool:
                self.request("GET", f"/v1/tools/gmail/approvals/{approval_id}")
            self.assertEqual(wrong_tool.exception.code, 404)
            with self.assertRaises(urllib.error.HTTPError) as wrong_tool_decide:
                self.request("POST", f"/v1/tools/gmail/approvals/{approval_id}/approve")
            self.assertEqual(wrong_tool_decide.exception.code, 404)

            status, body = self.request("POST", f"/v1/tools/fake_notes/approvals/{approval_id}/approve")
            self.assertEqual(body["approval"]["status"], "executed")
            self.assertEqual(body["result"], {"status": "executed", "message": "Wrote the note (5 chars)."})

            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request("POST", f"/v1/tools/fake_notes/approvals/{approval_id}/deny")
            self.assertEqual(error.exception.code, 409)

            denied = admin_api.tools_host.execute_action("fake_notes", "write_note", {"text": "no"})
            status, body = self.request("POST", f"/v1/tools/fake_notes/approvals/{denied['approval_id']}/deny")
            self.assertEqual(body["approval"]["status"], "denied")

    def test_approval_list_limit_matches_pending_cap(self) -> None:
        with patch.object(admin_api.state, "list_tool_approvals", return_value=[]) as listing:
            status, body = self.request("GET", "/v1/tools/fake_notes/approvals")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"approvals": []})
        listing.assert_called_once_with(admin_api.tools_host.PENDING_APPROVAL_LIMIT, tool_id="fake_notes")

    def test_tool_events_endpoint_pages_newest_first(self) -> None:
        from test_tools_host import FakeTool

        with patch.dict(admin_api.tools_host.BUNDLED_TOOLS, {"fake_notes": FakeTool()}):
            self.request("PUT", "/v1/tools/fake_notes/config", {"key": "FAKE_NOTES_TOKEN", "value": "token-1"})
            self.request("POST", "/v1/tools/fake_notes/enable")
            for _ in range(3):
                admin_api.tools_host.execute_action("fake_notes", "read_note", {})

            status, body = self.request("GET", "/v1/tools/events?limit=2")
            self.assertEqual(status, 200)
            self.assertEqual(len(body["events"]), 2)
            seqs = [event["seq"] for event in body["events"]]
            self.assertEqual(seqs, sorted(seqs, reverse=True))
            self.assertEqual(body["events"][0]["tool_id"], "fake_notes")
            self.assertEqual(body["events"][0]["action_id"], "read_note")
            self.assertTrue(body["events"][0]["has_arguments"])
            self.assertNotIn("arguments", body["events"][0])

            status, detail = self.request("GET", f"/v1/tools/events/{seqs[0]}")
            self.assertEqual(status, 200)
            self.assertEqual(detail["event"]["arguments"], {})

            status, older = self.request("GET", f"/v1/tools/events?before={seqs[-1]}")
            self.assertTrue(all(event["seq"] < seqs[-1] for event in older["events"]))

            # Config and enablement changes are audited alongside calls.
            all_events = self.request("GET", "/v1/tools/events")[1]["events"]
            kinds = {(event["action_id"], event["outcome"], event["detail"]) for event in all_events}
            self.assertIn(("config", "set", "FAKE_NOTES_TOKEN"), kinds)
            self.assertIn(("enablement", "enabled", ""), kinds)
            config_event = next(event for event in all_events if event["action_id"] == "config")
            self.assertFalse(config_event["has_arguments"])
            self.request("POST", "/v1/tools/fake_notes/disable")
            after_disable = self.request("GET", "/v1/tools/events")[1]["events"]
            self.assertEqual(after_disable[0]["action_id"], "enablement")
            self.assertEqual(after_disable[0]["outcome"], "disabled")

            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request("GET", "/v1/tools/events?bogus=1")
            self.assertEqual(error.exception.code, 400)

            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request("GET", "/v1/tools/events/999999")
            self.assertEqual(error.exception.code, 404)

    def test_host_errors_endpoint_is_read_only_and_lazy_loads_details(self) -> None:
        event = {
            "service": "kern-admin-api",
            "component": "orchestrator.execution",
            "kind": "unexpected_exception",
            "exception_type": "RuntimeError",
            "summary": "thread session missing",
            "traceback": 'File "host/runtime/admin_api/orchestrator.py", line 1, in execute',
            "context": {"thread_id": "thread_1"},
            "fingerprint": "a" * 64,
            "host_version": "1.3.3",
            "boot_id": "boot-1",
            "pid": 1234,
        }
        first = state.ingest_host_error(1_800_000_000_000_000, event)
        tools_event = dict(event, service="kern-tools", fingerprint="b" * 64)
        second = state.ingest_host_error(
            1_800_000_001_000_000, tools_event
        )

        status, body = self.request("GET", "/v1/host-errors?limit=1")
        self.assertEqual(status, 200)
        self.assertEqual([row["seq"] for row in body["events"]], [second])
        self.assertNotIn("traceback", body["events"][0])
        self.assertNotIn("context", body["events"][0])

        _, filtered = self.request("GET", "/v1/host-errors?service=kern-admin-api")
        self.assertEqual([row["seq"] for row in filtered["events"]], [first])
        _, detail = self.request("GET", f"/v1/host-errors/{first}")
        self.assertEqual(detail["error"]["traceback"], event["traceback"])
        self.assertEqual(detail["error"]["context"], {"thread_id": "thread_1"})
        self.assertEqual(detail["error"]["id"], first)

        # Coalescing moves the row back to the top of seq-based paging without
        # invalidating a detail link rendered before the repeat arrived.
        repeated = state.ingest_host_error(
            1_800_000_010_000_000, event
        )
        self.assertEqual(repeated, first)
        _, repeated_detail = self.request("GET", f"/v1/host-errors/{first}")
        self.assertEqual(repeated_detail["error"]["occurrence_count"], 2)

        with self.assertRaises(urllib.error.HTTPError) as invalid:
            self.request("GET", "/v1/host-errors?bogus=1")
        self.assertEqual(invalid.exception.code, HTTPStatus.BAD_REQUEST)
        with self.assertRaises(urllib.error.HTTPError) as invalid_service:
            self.request("GET", "/v1/host-errors?service=not%20a%20unit")
        self.assertEqual(invalid_service.exception.code, HTTPStatus.BAD_REQUEST)
        with self.assertRaises(urllib.error.HTTPError) as missing:
            self.request("GET", "/v1/host-errors/999999")
        self.assertEqual(missing.exception.code, HTTPStatus.NOT_FOUND)
        with self.assertRaises(urllib.error.HTTPError) as write:
            self.request("POST", f"/v1/host-errors/{first}")
        self.assertEqual(write.exception.code, HTTPStatus.NOT_FOUND)

    def test_oauth_callback_serves_the_ui_shell(self) -> None:
        request = urllib.request.Request(f"{self.base_url}/oauth/callback?code=x&state=y")
        with urllib.request.urlopen(request, timeout=10) as response:
            self.assertEqual(response.status, 200)
            self.assertIn("text/html", response.headers["Content-Type"])
            self.assertIn(b"Kern", response.read())


class WorkspaceAdminSocketImportTests(unittest.TestCase):
    def test_service_imports_when_it_is_the_entry_module(self) -> None:
        # service.py imports workspace_api partway through its own import,
        # so anything workspace_api reads off service at module scope is not
        # defined yet. Importing service first is the shape that catches it.
        for entry in (
            "host.runtime.admin_api.service",
            "host.runtime.admin_api.workspace_api",
        ):
            with self.subTest(entry=entry):
                root = Path(__file__).resolve().parents[1]
                subprocess.run(
                    [sys.executable, "-c", f"import {entry}"],
                    check=True,
                    capture_output=True,
                    env={**os.environ, "PYTHONPATH": str(root)},
                )


class WorkspaceAdminSocketTests(unittest.TestCase):
    """The socket path is group-connectable, but only the Workspace service uid may
    occupy a handler slot in the admin API process."""

    def setUp(self) -> None:
        self.app_peer = patch.object(
            workspace_admin_api, "_workspace_uid", return_value=os.getuid()
        )
        self.app_peer.start()
        self.addCleanup(self.app_peer.stop)

    def socket_path(self) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return Path(directory.name) / "workspace.sock"

    def created_server(self, path: Path) -> workspace_admin_api.ThreadingUnixHTTPServer:
        # KERN_WORKSPACE_ADMIN_SOCKET is read into this constant at import,
        # so patching it is the same override the service unit uses.
        with (
            patch.object(workspace_admin_api, "WORKSPACE_ADMIN_SOCKET", path),
            patch.object(
                workspace_admin_api.grp,
                "getgrnam",
                return_value=MagicMock(gr_gid=os.getgid()),
            ),
        ):
            return workspace_admin_api.create_workspace_admin_server()

    def serve(self, server: workspace_admin_api.ThreadingUnixHTTPServer) -> None:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

    def test_created_socket_is_app_group_connectable_and_unlinkable(self) -> None:
        path = self.socket_path()
        with patch.object(workspace_admin_api, "WORKSPACE_ADMIN_SOCKET", path):
            server = self.created_server(path)
            self.addCleanup(server.server_close)
            mode = path.lstat().st_mode
            # Only provisioned app accounts share this group; the peer-uid
            # check then binds an admitted connection to one exact app.
            self.assertTrue(stat.S_ISSOCK(mode))
            self.assertEqual(stat.S_IMODE(mode), 0o660)
            self.assertEqual(path.stat().st_gid, os.getgid())
            workspace_admin_api.unlink_workspace_admin_socket()
            self.assertFalse(path.exists())
            # Shutdown may unlink a socket a restart already removed.
            workspace_admin_api.unlink_workspace_admin_socket()

    def test_creation_refuses_to_replace_a_non_socket_path(self) -> None:
        path = self.socket_path()
        path.write_text("not a socket")
        with (
            patch.object(workspace_admin_api, "WORKSPACE_ADMIN_SOCKET", path),
            self.assertRaises(OSError),
        ):
            workspace_admin_api.create_workspace_admin_server()

    def test_stalled_request_is_closed_by_the_read_timeout(self) -> None:
        # A peer that connects and never finishes its request must not pin a
        # handler thread and its fd before the peer-uid check runs.
        path = self.socket_path()
        self.serve(self.created_server(path))
        with (
            patch.object(workspace_admin_api.Handler, "timeout", 0.3),
            socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection,
        ):
            connection.settimeout(5)
            connection.connect(str(path))
            # A request line, but never the blank line that ends the headers.
            connection.sendall(b"GET /v1/threads HTTP/1.1\r\n")
            self.assertEqual(connection.recv(65536), b"")


    def test_non_workspace_peer_is_rejected_before_it_takes_a_slot(self) -> None:
        path = self.socket_path()
        server = self.created_server(path)
        self.serve(server)

        with (
            patch.object(workspace_admin_api, "_workspace_uid", return_value=os.getuid() + 1),
            socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection,
        ):
            connection.settimeout(5)
            connection.connect(str(path))
            connection.sendall(b"G")  # a trickle cannot reach a handler
            try:
                closed = connection.recv(65536)
            except ConnectionResetError:
                closed = b""
            self.assertEqual(closed, b"")

        # The rejected peer never acquired the semaphore.
        self.assertTrue(server._connection_slots.acquire(blocking=False))

    def test_connections_past_the_cap_are_rejected_not_queued(self) -> None:
        handling = threading.Event()
        finish = threading.Event()

        class BlockingHandler(workspace_admin_api.Handler):
            def handle(self) -> None:
                handling.set()
                finish.wait(5)

        path = self.socket_path()
        server = workspace_admin_api.ThreadingUnixHTTPServer(
            str(path), BlockingHandler, max_connections=1
        )
        self.serve(server)
        self.addCleanup(finish.set)

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as holder:
            holder.connect(str(path))
            self.assertTrue(handling.wait(5))
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as rejected:
                rejected.settimeout(5)
                rejected.connect(str(path))
                # The only slot is taken, so the server closes this connection
                # rather than parking its fd until a slot frees.
                self.assertEqual(rejected.recv(65536), b"")
        finish.set()

    def test_connection_slot_is_released_when_the_handler_thread_cannot_start(self) -> None:
        path = self.socket_path()
        server = workspace_admin_api.ThreadingUnixHTTPServer(
            str(path), workspace_admin_api.Handler, max_connections=1
        )
        self.addCleanup(server.server_close)
        client, request = socket.socketpair()
        self.addCleanup(client.close)
        self.addCleanup(request.close)

        with (
            patch.object(
                socketserver.ThreadingMixIn,
                "process_request",
                side_effect=RuntimeError("cannot start thread"),
            ),
            self.assertRaises(RuntimeError),
        ):
            server.process_request(request, ("local", 0))

        # process_request_thread, the normal release point, never ran; a leak
        # here would make the socket refuse the Workspace backend from now on.
        self.assertTrue(server._connection_slots.acquire(blocking=False))


if __name__ == "__main__":
    unittest.main()
