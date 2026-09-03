"""Localhost admin API (127.0.0.1:7443), reached through an operator endpoint.

The supported endpoint paths are SSH port forwarding and the optional
Cloudflare Tunnel. The admin login is the authentication boundary: the tunnel
carries transport and Cloudflare's edge (DDoS) protection only, with no
Cloudflare Access gate in front, so the login is hardened to stand alone. API
routes require an authenticated caller; only static release assets, the
side-effect-free OAuth callback shell, and the non-secret public-login status
are unauthenticated.

Route handlers validate the documented protocol and update admin state in the
local Postgres database (through the storage accessors in ``state``);
running turns through the selected agent runtime is delegated to
``orchestrator``, which owns turn admission and the live turn processes.
Operations that require root or agent-user authority cross through fixed
root-owned sudo helpers. Database-backed host state is updated directly under
the admin database role.

A caller authenticates only by posting the password to ``/v1/login``, which is
compared against a SHA-256 hash of ``admin_password_sha256`` from the config
table (so the cleartext password is never persisted) and returns an ``HttpOnly``
session cookie (see ``admin_auth``). Every other request authenticates with that
cookie plus the CSRF header; the password is never replayed on later requests.
Failed logins are throttled to keep the single shared password from being
brute-forced over the public tunnel.
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import threading
import time
from typing import Any, Callable, cast, NamedTuple
from urllib.parse import parse_qs, quote, urlparse

from host.config import AGENT_RUNTIMES, ConfigError, parse_network_controls
from host.constants import ADMIN_API_PORT, LOOPBACK, MAX_REQUEST_BODY_BYTES, PROXY_PORT
from host.network_integrations.bedrock.manifest import SUPPORTED_REGIONS as BEDROCK_REGIONS
from host.network_integrations.github.push_gate import pending as github_pending_push
from host.session_options import session_config_error
# workspace_admin_api imports this module back to dispatch through route().
# The cycle is safe with plain module imports: each side binds the module
# object and reads its attributes only at request time, never during import.
from host.runtime.admin_api import admin_auth, admin_passkeys, workspace_api as workspace_admin_api, workspace_proxy, github_credential, github_repo_audit, tools_client as tools_admin_api, upgrade_check
from host.runtime.agent_runtime import (
    agent_activity,
    bedrock_credentials,
    claude_code,
    codex_app_server,
    grok_agent,
    orchestrator,
)
from host.runtime.admin_api.agent_files import (
    DOWNLOAD_MAX_BYTES as AGENT_FILE_DOWNLOAD_MAX_BYTES,
    HELPER_COMMAND as AGENT_FILE_HELPER_COMMAND,
    HELPER_TIMEOUT_SECONDS as AGENT_FILE_HELPER_TIMEOUT_SECONDS,
    IMAGE_STREAM_MAX_BYTES as AGENT_FILE_IMAGE_STREAM_MAX_BYTES,
    STREAM_MAX_BYTES as AGENT_FILE_STREAM_MAX_BYTES,
    STREAM_MEDIA_TYPES as AGENT_FILE_STREAM_MEDIA_TYPES,
    UPLOAD_FILENAME_MAX_BYTES as AGENT_FILE_UPLOAD_FILENAME_MAX_BYTES,
    UPLOAD_HELPER_COMMAND as AGENT_FILE_UPLOAD_HELPER_COMMAND,
    UPLOAD_MAX_BYTES as AGENT_FILE_UPLOAD_MAX_BYTES,
    content_disposition as _agent_file_content_disposition,
    helper_error_message as _helper_error_message,
    list_files as agent_file_list,
    path_from_query as _agent_file_path,
    read_file as agent_file_read,
    upload_filename as _agent_file_upload_filename,
)
from host.runtime.admin_api.conversation_history import (
    _conversation_search_fingerprint,
    _decode_conversation_search_cursor,
    _encode_conversation_search_cursor,
    read_conversation_history,
    search_conversation_history,
)
from host.runtime.admin_api.request_params import one as _one
from host.runtime.core import host_errors, network_policy, pgclient, state
from host.runtime.core.host_metrics import (
    AGENT_CGROUP_ROOT,
    AGENT_PROCESS_LIMIT,
    PROC_ROOT,
    agent_processes,
    cpu_usage_percent,
    filesystem_metrics,
    host_metrics,
    memory_metrics,
    swap_metrics,
)
from host.runtime.core.root_helpers import HelperTimedOut, run_root_helper as _run_root_helper
from host.runtime.embeddings import client as embedding_client
from host.runtime.tools import tools_host
from host.runtime.agent_runtime.orchestrator import agent_runtime_status
from host.runtime.admin_api.request_params import clip_json_encoded_text as _clip_json_encoded_text
from host.runtime.admin_api.runtime_accounts import (
    OAUTH_RUNTIME_TYPES,
    _OAUTH_LOGIN_FLOWS,
    _current_bedrock_account,
    complete_claude_oauth_login,
    connect_bedrock_credentials,
    current_agent_accounts,
    current_bedrock_credentials,
    current_claude_oauth_login,
    current_codex_oauth_login,
    current_grok_oauth_login,
    disconnect_bedrock_credentials,
    refresh_agent_runtime_accounts,
    reset_linked_account,
    start_claude_oauth_login,
    start_codex_oauth_login,
    start_grok_oauth_login,
)
from host.runtime.admin_api.threads import (
    _handoff_event_block,
    _session_handoff_message,
    _thread_list_prefix,
    clear_thread_memory,
    get_thread,
    list_threads,
    send_thread_message,
    stop_thread,
    thread_route,
)
from host.runtime.core.state import (
    load_config,
    page_agent_events_before,
    read_claude_account,
    read_openai_account,
    read_xai_account,
    utc_now,
)
from host.version import version_status


class OperatorPrincipal(NamedTuple):
    """An operator session authenticated by the TCP request boundary."""

    session_token_hash: str


class WorkspacePrincipal(NamedTuple):
    """The fixed Workspace service authenticated by the Unix peer boundary."""


RoutePrincipal = OperatorPrincipal | WorkspacePrincipal


HOST = LOOPBACK
PORT = ADMIN_API_PORT
RUNTIME_DIR = Path(__file__).parent
ADMIN_UI_DIR = RUNTIME_DIR / "admin_ui"
TOOLS_DIR = RUNTIME_DIR.parents[1] / "tools"


def _tool_guide_assets() -> dict[str, tuple[Path, str]]:
    routes: dict[str, tuple[Path, str]] = {}
    for asset in sorted(TOOLS_DIR.glob("**/guide_assets/**/*.png")):
        route = f"/guide-assets/{asset.name}"
        if route in routes:
            raise RuntimeError(f"duplicate tool guide asset filename: {asset.name}")
        routes[route] = (asset, "image/png")
    return routes


UI_ASSETS = {
    "/": (ADMIN_UI_DIR / "index.html", "text/html; charset=utf-8"),
    # The page the operator registers as the OAuth redirect URI for tool
    # connect flows; the SPA reads the code/state query parameters on load.
    "/oauth/callback": (ADMIN_UI_DIR / "index.html", "text/html; charset=utf-8"),
    "/admin_ui.css": (ADMIN_UI_DIR / "admin_ui.css", "text/css; charset=utf-8"),
    "/manifest.webmanifest": (ADMIN_UI_DIR / "manifest.webmanifest", "application/manifest+json"),
    "/service-worker.js": (ADMIN_UI_DIR / "service-worker.js", "application/javascript; charset=utf-8"),
    "/favicon.ico": (ADMIN_UI_DIR / "favicon.svg", "image/svg+xml"),
    "/favicon.svg": (ADMIN_UI_DIR / "favicon.svg", "image/svg+xml"),
    "/icons/kern-180.png": (ADMIN_UI_DIR / "icons/kern-180.png", "image/png"),
    "/icons/kern-192.png": (ADMIN_UI_DIR / "icons/kern-192.png", "image/png"),
    "/icons/kern-512.png": (ADMIN_UI_DIR / "icons/kern-512.png", "image/png"),
    "/icons/kern-maskable-512.png": (ADMIN_UI_DIR / "icons/kern-maskable-512.png", "image/png"),
}
# The admin UI ships as native ES modules in host/runtime/admin_api/admin_ui/. The
# served set is fixed at startup from the files present, so any other
# /admin_ui/ path stays a 404.
UI_ASSETS.update({
    f"/admin_ui/{module.name}": (module, "application/javascript; charset=utf-8")
    for module in sorted(ADMIN_UI_DIR.glob("*.js"))
})
WORKSPACE_UI_ASSETS = {
    "/workspace/chat.html": (RUNTIME_DIR.parent / "workspace/chat/ui/index.html", "text/html; charset=utf-8"),
    "/workspace/chat.js": (RUNTIME_DIR.parent / "workspace/chat/ui/agent_chat.js", "application/javascript; charset=utf-8"),
    "/workspace/chat.css": (RUNTIME_DIR.parent / "workspace/chat/ui/agent_chat.css", "text/css; charset=utf-8"),
    "/workspace/rich_text.js": (RUNTIME_DIR.parent / "workspace/chat/ui/rich_text.js", "application/javascript; charset=utf-8"),
    "/workspace/rich_text.css": (RUNTIME_DIR.parent / "workspace/chat/ui/rich_text.css", "text/css; charset=utf-8"),
    "/workspace/composer.css": (RUNTIME_DIR.parent / "workspace/ui/composer.css", "text/css; charset=utf-8"),
    "/workspace/web-apps.html": (RUNTIME_DIR.parent / "workspace/web_apps/ui/index.html", "text/html; charset=utf-8"),
    "/workspace/web-apps.js": (RUNTIME_DIR.parent / "workspace/web_apps/ui/personal_web_app_builder.js", "application/javascript; charset=utf-8"),
    "/workspace/web-apps.css": (RUNTIME_DIR.parent / "workspace/web_apps/ui/personal_web_app_builder.css", "text/css; charset=utf-8"),
    "/workspace/global.html": (RUNTIME_DIR.parent / "workspace/ui/index.html", "text/html; charset=utf-8"),
    "/workspace/global.js": (RUNTIME_DIR.parent / "workspace/ui/workspace.js", "application/javascript; charset=utf-8"),
    "/workspace/global.css": (RUNTIME_DIR.parent / "workspace/ui/workspace.css", "text/css; charset=utf-8"),
    "/workspace/capability-worker-sandbox.js": (RUNTIME_DIR.parent / "workspace/web_apps/ui/capability_worker_sandbox.js", "application/javascript; charset=utf-8"),
}
UI_ASSETS.update(WORKSPACE_UI_ASSETS)
# Provider setup screenshots live with their owning tool integration. They are
# audited release assets and never load from provider domains in the operator's
# browser. Public filenames are unique across tools so manifests stay portable.
UI_ASSETS.update(_tool_guide_assets())
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; "
        "base-uri 'none'; "
        "connect-src 'self'; "
        "font-src 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'; "
        "img-src 'self' data: blob:; "
        "media-src blob:; "
        "object-src 'none'; "
        "script-src 'self'; "
        "style-src 'self' blob:"
    ),
    "Referrer-Policy": "no-referrer",
    "Strict-Transport-Security": "max-age=63072000",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}
UNTRUSTED_FILE_SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; sandbox",
    "Content-Disposition": "inline",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}
PRODUCT_THREAD_ID_RE = re.compile(
    r"(?=^[a-z0-9-]{1,64}$)^(?:app-[a-z0-9-]+|thread-[a-z0-9-]+|"
    r"schedule-[1-9][0-9]*)$"
)
PRODUCT_THREAD_PREFIX_RE = re.compile(
    r"(?=^[a-z0-9-]{1,64}$)^(?:app|thread|schedule)-[a-z0-9-]*$"
)
# Schedule threads are the one kind that may run a script instead of a model;
# the send path enforces that on the stable thread id itself.
UTC_TIMESTAMP_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
RFC3339_TIMESTAMP_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.(?P<fraction>[0-9]+))?(?:Z|[+-][0-9]{2}:[0-9]{2})$",
    re.IGNORECASE,
)
SERVICE_NAME_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,128}$")
MESSAGE_LIMIT = 50_000
# A fresh provider session gets independent recent-conversation and activity
# budgets. Tool output can therefore never crowd user or assistant messages
# out of the handoff prompt.
THREAD_HANDOFF_MESSAGE_CHARACTER_LIMIT = 100_000
THREAD_HANDOFF_ACTIVITY_CHARACTER_LIMIT = 150_000
THREAD_HANDOFF_CHARACTER_LIMIT = (
    THREAD_HANDOFF_MESSAGE_CHARACTER_LIMIT + THREAD_HANDOFF_ACTIVITY_CHARACTER_LIMIT
)
THREAD_HANDOFF_ACTIVITY_DETAIL_LIMIT = 1_000
THREAD_HANDOFF_ACTIVITY_OUTPUT_LIMIT = 8_000
THREAD_HANDOFF_ACTIVITY_EVENT_CHARACTER_LIMIT = 8_000
MAINTENANCE_INTERVAL_SECONDS = 3600  # scheduled state cleanup cadence (not per-request)
THREAD_EVENT_MESSAGE_BYTES_LIMIT = 200_000
# The in-thread boundary text. Retained events remain available to audit and
# history APIs, while Chat treats this marker as the new visible beginning.
# Carried as a plain message rather than an activity payload: the Chat renderer
# merges events that share an activity id, so a fixed id would collapse a
# second clear onto the first one's position.
WORKING_MEMORY_CLEARED_NOTICE = (
    "Working memory cleared. The agent starts fresh from here. Earlier "
    "messages are hidden and are no longer sent to it."
)
THREAD_DISPLAY_EVENT_TYPES = frozenset({
    "thread.message",
    "thread.activity",
    "thread.error",
    "thread.stopped",
    "thread.memory_cleared",
})
CONVERSATION_SEARCH_LIMIT = 25
CONVERSATION_SEARCH_EXCERPT_BYTES = 2 * 1024
CONVERSATION_READ_LIMIT = 50
CONVERSATION_QUERY_BYTES = 512
CONVERSATION_VARIANT_BYTES = 256
CONVERSATION_VARIANT_LIMIT = 8
# Relevance cursors freeze up to 200 semantic event ids so later pages never
# rerun an approximate HNSW scan against a physically changing graph.
CONVERSATION_CURSOR_BYTES = 8192
CONVERSATION_MESSAGE_BYTES = 16 * 1024
CONVERSATION_RESPONSE_BYTES = 256 * 1024
CONVERSATION_EVENT_TYPES = ("thread.message", "thread.activity")
CONVERSATION_SEMANTIC_CANDIDATES = 200
# Relevance cursors are short-lived capabilities owned by this admin API
# process.  Signing snapshot cursors prevents a caller from replacing the
# frozen semantic candidate ids with arbitrary messages that happen to satisfy
# the same public filters.  A service restart deliberately invalidates an
# in-progress relevance cursor; the caller can simply restart the search.
_CONVERSATION_CURSOR_SIGNING_KEY = secrets.token_bytes(32)
# Writers wake the indexer directly, so this is only a backstop for work that
# somehow reached the queue without signalling (a restart mid-backlog, or a
# migration seeding the table under a running service).
CONVERSATION_EMBEDDING_IDLE_SECONDS = 30
# Bounds how long an interactive search can queue behind one indexing batch.
CONVERSATION_EMBEDDING_BATCH_BYTES = 32 * 1024
HISTORY_PROVENANCE = "retained_conversation_history"
HISTORY_TRUST = "untrusted"
HISTORY_INSTRUCTION_AUTHORITY = "none"
POSTGRES_BIGINT_MAX = 9_223_372_036_854_775_807
EVENT_ID_RE = re.compile(r"^event_([1-9][0-9]{0,18})$")
CONVERSATION_ACTIVITY_FIELDS = (
    ("activity_id", 128),
    ("provider", 128),
    ("kind", 128),
    ("phase", 128),
    ("title", 256),
    ("status", 128),
    ("detail", 768),
    ("output", 1024),
    ("error", 1024),
)
THREAD_MAP_LIMIT = 100_000  # user thread -> runtime session mappings kept before LRU pruning
OAUTH_LOGIN_LOCK_TIMEOUT_SECONDS = 5
# OAuth login can start while awaiting login or in error: error states (a
# changed account, malformed local credentials) are recovered by simply
# logging in again — resetting the linked account never has to fix them first.
OAUTH_LOGIN_STATUSES = ("awaiting_login", "error")
REBOOT_HELPER_TIMEOUT_SECONDS = 10
AGENT_AUTH_CLEAR_HELPER_TIMEOUT_SECONDS = 10
AGENT_AUTH_CLEAR_HELPER_COMMAND = ["/usr/bin/sudo", "-n", "/usr/local/lib/kern-host/clear-agent-auth"]
# Lock inventory for this module (each request runs on its own handler
# thread, so every handler is concurrent with every other and with the
# orchestrator's workers):
# - The mutation lock (private to state.py, entered through state.mutation()):
#   every admin-state write cycle. Held briefly; slow work (runtime spawns,
#   helper subprocesses, process closes) always runs outside the mutation so
#   reads and /v1/health never stall behind it. Reads are lock-free queries.
# - OAUTH_LOGIN_LOCK: serializes device-login starts so two clicks cannot mint
#   two device codes (the mint runs outside the mutation lock, so that lock
#   alone cannot prevent a double mint, which would leak a login process).
#   Timeout-guarded so a stuck mint returns 409 instead of piling up threads.
OAUTH_LOGIN_LOCK = threading.Lock()
# Per-connection read timeout (request line, headers, and body) so a slow client
# cannot hold a worker thread open indefinitely, and a cap on concurrent worker
# threads so a flood of connections cannot exhaust host memory or threads;
# excess connections wait in the listen backlog.
REQUEST_TIMEOUT_SECONDS = 30
MAX_CONCURRENT_REQUESTS = 32
# A login body is a tiny JSON object; cap it far below the general request limit.
LOGIN_MAX_BODY_BYTES = 4096


# ApiError is defined in a shared module so it is one class whether admin_api is
# loaded as __main__ (the service) or as host.runtime.admin_api.service (by the modules
# it dispatches to, e.g. tools_admin_api). See host/runtime/admin_api/errors.py.
from host.runtime.admin_api.errors import ApiError


class Handler(BaseHTTPRequestHandler):
    server_version = "Kern/0.1"
    # Bound how long a single connection may take to send its request line,
    # headers, and body so a slow client cannot pin a worker thread indefinitely.
    timeout = REQUEST_TIMEOUT_SECONDS

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_PUT(self) -> None:
        self._handle("PUT")

    def do_DELETE(self) -> None:
        self._handle("DELETE")

    def log_message(self, format: str, *args: object) -> None:
        return

    def _handle(self, method: str) -> None:
        try:
            # Classify the request once, before static assets or auth routes.
            # The resulting immutable context owns every SSH-forward/public
            # distinction used below. Its hostname loader is not called for the
            # local path, preserving database-independent SSH recovery.
            self._auth_context = admin_auth.classify_request(
                forwarded_proto_values=(
                    self.headers.get_all("X-Forwarded-Proto") or []
                ),
                host_values=self.headers.get_all("Host") or [],
                public_hostname_loader=state.load_cloudflare_hostname,
            )
            path = urlparse(self.path)
            if not admin_auth.route_is_available(
                self._auth_context, method, path.path
            ):
                raise ApiError(HTTPStatus.NOT_FOUND, "route not found")
            if method == "GET" and path.path in UI_ASSETS:
                if path.path == "/workspace/capability-worker-sandbox.js":
                    self._send_capability_worker()
                else:
                    self._send_ui_asset(path.path)
                return
            # The login flow establishes the session, so it is reachable
            # before session authentication.
            if method == "POST" and path.path == "/v1/login":
                self._handle_login()
                return
            if method == "POST" and path.path == "/v1/login/passkey":
                self._handle_passkey_login()
                return
            if method == "GET" and path.path == "/v1/login/status":
                self._handle_login_status()
                return
            principal = self._authenticate()
            if method == "POST" and path.path == "/v1/logout":
                self._handle_logout(principal)
                return
            if path.path.startswith("/v1/admin-passkeys"):
                self._handle_admin_passkeys(method, path.path, principal)
                return
            if method == "GET" and path.path == "/v1/agent-files/content":
                self._send_agent_file(_agent_file_path(parse_qs(path.query)))
                return
            if method == "GET" and path.path == "/v1/agent-files/download":
                self._send_agent_file(
                    _agent_file_path(parse_qs(path.query)),
                    download=True,
                )
                return
            if method == "POST" and path.path == "/v1/agent-files/upload":
                self._send_agent_file_upload(parse_qs(path.query))
                return
            response = route(
                method,
                path.path,
                parse_qs(path.query),
                self._read_body(),
                principal=principal,
            )
            self._send_json(HTTPStatus.OK, response)
        except admin_auth.PublicHttpsRequired as exc:
            if method == "GET":
                self._send_https_redirect(exc.hostname)
            else:
                self._send_json(
                    HTTPStatus.FORBIDDEN,
                    {"error": {"message": "HTTPS is required"}},
                )
        except admin_auth.RequestBoundaryError as exc:
            self._send_json(
                HTTPStatus.FORBIDDEN,
                {"error": {"message": str(exc)}},
            )
        except ApiError as exc:
            self._send_json(exc.status, {"error": {"message": exc.message}})
        except (BrokenPipeError, ConnectionResetError):
            # The client closed the connection while the response was being
            # written: expected transport termination, not a service fault.
            # Close without reporting a host error or writing to the dead
            # socket again.
            self.close_connection = True
        except Exception as exc:
            # Never leak internal exception detail (database, filesystem,
            # subprocess, config) to the client; log the real error to the
            # protected service log and return a fixed message.
            host_errors.report_unexpected(
                "admin_api.request",
                exc,
                context={"method": method, "route": urlparse(self.path).path},
            )
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": {"message": "internal server error"}})

    def _send_ui_asset(self, path: str) -> None:
        asset, content_type = UI_ASSETS[path]
        data = asset.read_bytes()
        self.send_response(HTTPStatus.OK.value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self._send_ui_cache_headers()
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(data)

    def _send_ui_cache_headers(self) -> None:
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")

    def _send_security_headers(self) -> None:
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)

    def _send_capability_worker(self) -> None:
        asset, content_type = UI_ASSETS["/workspace/capability-worker-sandbox.js"]
        data = asset.read_bytes()
        self.send_response(HTTPStatus.OK.value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self._send_ui_cache_headers()
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; base-uri 'none'; connect-src 'none'; "
            "object-src 'none'; script-src 'none'; worker-src data:",
        )
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(data)

    def _authenticate(self) -> OperatorPrincipal:
        # The session cookie minted by the completed login flow is the only
        # accepted credential: the password itself is presented only at login,
        # never replayed on later requests. A cookie-authenticated request must
        # also carry the CSRF header, which same-origin UI code always sends and
        # a cross-site page cannot.
        try:
            session_token_hash = admin_auth.authenticate_session_request(
                self._auth_context,
                cookie_header=self.headers.get("Cookie", ""),
                csrf_header=self.headers.get(admin_auth.CSRF_HEADER_NAME, ""),
                activity_header=self.headers.get(
                    admin_auth.SESSION_ACTIVITY_HEADER_NAME, ""
                ),
            )
        except admin_auth.MissingSessionRequestHeader as exc:
            raise ApiError(HTTPStatus.FORBIDDEN, str(exc)) from exc
        except admin_auth.SessionAuthError as exc:
            raise ApiError(HTTPStatus.UNAUTHORIZED, str(exc)) from exc
        return OperatorPrincipal(session_token_hash)

    def _handle_login(self) -> None:
        # The HTTP adapter parses the bounded request; admin_auth owns password
        # verification, throttling, factor-two policy, and every auth cookie.
        client_key = self._client_key()

        def password_loader() -> str | None:
            # Keep HTTP parsing here; admin_auth charges the login throttle
            # only when this returns a valid-shaped body, so a cross-site
            # page's malformed POSTs cannot consume the source's attempts.
            length = self._content_length(LOGIN_MAX_BODY_BYTES)
            try:
                body = json.loads(self.rfile.read(length)) if length else None
            except (json.JSONDecodeError, UnicodeDecodeError):
                body = None
            if (
                isinstance(body, dict)
                and set(body) == {"password"}
                and isinstance(body.get("password"), str)
            ):
                return body["password"]
            return None

        try:
            result = admin_auth.begin_password_login(
                self._auth_context,
                client_key=client_key,
                password_loader=password_loader,
            )
        except admin_auth.LoginRateLimited as exc:
            self._send_json(
                HTTPStatus.TOO_MANY_REQUESTS,
                {"error": {"message": str(exc)}},
            )
            return
        except admin_auth.InvalidPassword as exc:
            self._send_json(
                HTTPStatus.UNAUTHORIZED,
                {"error": {"message": str(exc)}},
            )
            return
        except admin_auth.PasskeyStartError as exc:
            raise ApiError(HTTPStatus.FORBIDDEN, str(exc)) from exc
        if result.passkey_options is not None:
            body = {
                "passkey_required": True,
                "publicKey": result.passkey_options,
            }
        else:
            body = {"ok": True}
        self._send_json(
            HTTPStatus.OK,
            body,
            set_cookies=list(result.set_cookies),
        )

    def _handle_passkey_login(self) -> None:
        try:
            result = admin_auth.complete_passkey_login(
                self._auth_context,
                cookie_header=self.headers.get("Cookie", ""),
                csrf_header=self.headers.get(admin_auth.CSRF_HEADER_NAME, ""),
                client_key_loader=self._client_key,
                response_loader=self._read_body,
            )
        except admin_auth.MissingPasskeyRequestHeader as exc:
            raise ApiError(HTTPStatus.FORBIDDEN, str(exc)) from exc
        except admin_auth.PasskeyVerificationError as exc:
            self._send_json(
                HTTPStatus.UNAUTHORIZED,
                {"error": {"message": str(exc)}},
                set_cookies=list(exc.set_cookies),
            )
            return
        self._send_json(
            HTTPStatus.OK,
            {"ok": True},
            set_cookies=list(result.set_cookies),
        )

    def _handle_login_status(self) -> None:
        """Expose only whether public login has its second factor enrolled.

        This deliberately exists only on the configured HTTPS login origin.
        It lets the login page and Kern Cloud show an accurate security state
        without authenticating, but discloses no credential metadata.
        """
        self._send_json(
            HTTPStatus.OK,
            {"passkey_configured": admin_auth.passkey_login_configured()},
        )

    def _handle_admin_passkeys(
        self,
        method: str,
        path: str,
        principal: OperatorPrincipal,
    ) -> None:
        # HTTPS_ONLY_AUTH_ROUTES and _authenticate establish both invariants
        # before this handler is selected.
        context = cast(tuple[str, str], self._auth_context.passkey_context)
        if method == "GET" and path == "/v1/admin-passkeys":
            self._send_json(
                HTTPStatus.OK,
                admin_passkeys.status(
                    public_https=True,
                    rp_id=context[0],
                ),
            )
            return
        if method == "POST" and path == "/v1/admin-passkeys/register/options":
            self._send_json(
                HTTPStatus.OK,
                {
                    "publicKey": admin_passkeys.begin_registration(
                        principal.session_token_hash,
                        rp_id=context[0],
                        origin=context[1],
                        agent_name="Kern",
                    )
                },
            )
            return
        if method == "POST" and path == "/v1/admin-passkeys/register":
            try:
                result = admin_passkeys.finish_registration(
                    principal.session_token_hash, self._read_body()
                )
            except admin_passkeys.PasskeyError as exc:
                raise ApiError(HTTPStatus.BAD_REQUEST, str(exc)) from exc
            self._send_json(HTTPStatus.OK, result)
            return
        raise ApiError(HTTPStatus.NOT_FOUND, "route not found")

    def _handle_logout(self, principal: OperatorPrincipal) -> None:
        cookie = admin_auth.logout(
            self._auth_context,
            session_token_hash=principal.session_token_hash,
        )
        self._send_json(HTTPStatus.OK, {"ok": True}, set_cookies=[cookie])

    def _client_key(self) -> str:
        # The throttle bucket. A tunnel request (cloudflared sets X-Forwarded-Proto,
        # which the HTTPS enforcement above already required to be https) carries
        # exactly one Cf-Connecting-Ip that the edge sets and a browser cannot
        # spoof. Require it and fail closed if missing or malformed: a stripped
        # header must not collapse every internet visitor into one shared bucket
        # (which would let one source lock out others). IPv4 buckets by address,
        # IPv6 by /64 so address rotation within a prefix cannot spread out. The
        # plain loopback SSH forward has no such header and uses the socket peer.
        try:
            return admin_auth.login_client_key(
                self._auth_context,
                local_address=self.client_address[0],
                cf_connecting_ip_values=(
                    self.headers.get_all("Cf-Connecting-Ip") or []
                ),
                cf_connecting_ipv6_values=(
                    self.headers.get_all("Cf-Connecting-Ipv6") or []
                ),
            )
        except admin_auth.RequestBoundaryError as exc:
            raise ApiError(HTTPStatus.FORBIDDEN, str(exc)) from exc

    def _read_body(self) -> Any:
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, "malformed Content-Length") from exc
        if length < 0:
            raise ApiError(HTTPStatus.BAD_REQUEST, "malformed Content-Length")
        if length > MAX_REQUEST_BODY_BYTES:
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request body too large")
        if length == 0:
            return None
        try:
            return json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, ValueError, RecursionError) as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, f"invalid JSON: {exc}") from exc

    def _send_https_redirect(self, hostname: str) -> None:
        # Upgrade a cleartext GET to https on the same host so the browser
        # re-requests securely before any form or secret is served. Only an
        # origin-form target may be echoed: a crafted request line such as
        # "GET @evil.com/" would otherwise turn the stored hostname into a
        # userinfo component of the Location.
        path = self.path if self.path.startswith("/") else "/"
        self.send_response(HTTPStatus.MOVED_PERMANENTLY.value)
        self.send_header("Location", f"https://{hostname}{path}")
        self.send_header("Content-Length", "0")
        self._send_security_headers()
        self.end_headers()

    def _send_json(self, status: HTTPStatus, body: Any, *, set_cookies: list[str] | None = None) -> None:
        data = json.dumps(body, sort_keys=True).encode()
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        # Authenticated API responses carry operator data and must never be
        # cached by any intermediary or the browser.
        self.send_header("Cache-Control", "no-store")
        for cookie in set_cookies or ():
            self.send_header("Set-Cookie", cookie)
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(data)

    def _send_agent_file(self, path: str, *, download: bool = False) -> None:
        expected_media_type = (
            "application/octet-stream"
            if download
            else AGENT_FILE_STREAM_MEDIA_TYPES.get(Path(path).suffix.lower())
        )
        if expected_media_type is None:
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                "agent file streaming supports only MP4, MOV, JPEG, PNG, or WebP files",
            )
        if download:
            maximum_size = AGENT_FILE_DOWNLOAD_MAX_BYTES
        else:
            maximum_size = (
                AGENT_FILE_IMAGE_STREAM_MAX_BYTES
                if expected_media_type.startswith("image/")
                else AGENT_FILE_STREAM_MAX_BYTES
            )
        process = subprocess.Popen(
            [*AGENT_FILE_HELPER_COMMAND, "download" if download else "stream", path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert process.stdout is not None
        try:
            raw_header = process.stdout.readline(4097)
            if len(raw_header) > 4096 or not raw_header.endswith(b"\n"):
                process.kill()
                raise ApiError(HTTPStatus.INTERNAL_SERVER_ERROR, "agent file helper returned an invalid stream header")
            try:
                header = json.loads(raw_header)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                process.wait(timeout=AGENT_FILE_HELPER_TIMEOUT_SECONDS)
                raise ApiError(HTTPStatus.INTERNAL_SERVER_ERROR, "agent file helper returned invalid JSON") from exc
            if not isinstance(header, dict) or "size_bytes" not in header:
                process.wait(timeout=AGENT_FILE_HELPER_TIMEOUT_SECONDS)
                status = {
                    2: HTTPStatus.NOT_FOUND,
                    3: HTTPStatus.BAD_REQUEST,
                    4: HTTPStatus.BAD_REQUEST,
                }.get(process.returncode, HTTPStatus.INTERNAL_SERVER_ERROR)
                message = header.get("error", {}).get("message") if isinstance(header, dict) else None
                raise ApiError(status, str(message or "agent file helper failed"))
            size_bytes = header.get("size_bytes")
            media_type = header.get("media_type")
            if (
                not isinstance(size_bytes, int)
                or not 0 <= size_bytes <= maximum_size
                or media_type != expected_media_type
            ):
                process.kill()
                raise ApiError(HTTPStatus.INTERNAL_SERVER_ERROR, "agent file helper returned invalid metadata")
            self.send_response(HTTPStatus.OK.value)
            self.send_header("Content-Type", expected_media_type)
            self.send_header("Content-Length", str(size_bytes))
            self._send_ui_cache_headers()
            for name, value in UNTRUSTED_FILE_SECURITY_HEADERS.items():
                if name != "Content-Disposition":
                    self.send_header(name, value)
            self.send_header(
                "Content-Disposition",
                _agent_file_content_disposition(path) if download else "inline",
            )
            self.end_headers()
            remaining = size_bytes
            try:
                while remaining:
                    chunk = process.stdout.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True
            if remaining:
                process.kill()
                self.close_connection = True
        finally:
            if process.poll() is None:
                try:
                    process.wait(timeout=AGENT_FILE_HELPER_TIMEOUT_SECONDS)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()

    def _send_agent_file_upload(self, query: dict[str, list[str]]) -> None:
        filename = _agent_file_upload_filename(query)
        length = self._content_length(AGENT_FILE_UPLOAD_MAX_BYTES)
        process = subprocess.Popen(
            [*AGENT_FILE_UPLOAD_HELPER_COMMAND, filename, str(length)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        try:
            remaining = length
            while remaining:
                chunk = self.rfile.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise ApiError(HTTPStatus.BAD_REQUEST, "upload ended before Content-Length bytes were received")
                process.stdin.write(chunk)
                remaining -= len(chunk)
            process.stdin.close()
            process.wait(timeout=AGENT_FILE_HELPER_TIMEOUT_SECONDS)
        except BrokenPipeError as exc:
            try:
                process.stdin.close()
            except BrokenPipeError:
                pass
            try:
                process.wait(timeout=AGENT_FILE_HELPER_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                self._terminate_agent_file_upload_helper(process)
                raise ApiError(HTTPStatus.GATEWAY_TIMEOUT, "agent file upload helper timed out") from exc
        except subprocess.TimeoutExpired as exc:
            if process.poll() is None:
                self._terminate_agent_file_upload_helper(process)
            raise ApiError(HTTPStatus.GATEWAY_TIMEOUT, "agent file upload helper timed out") from exc
        except BaseException:
            try:
                process.stdin.close()
            except BrokenPipeError:
                pass
            if process.poll() is None:
                try:
                    # EOF lets the helper run its finally block and remove a
                    # partial .uploading-* file after a short client body.
                    process.wait(timeout=AGENT_FILE_HELPER_TIMEOUT_SECONDS)
                except subprocess.TimeoutExpired:
                    self._terminate_agent_file_upload_helper(process)
            raise
        stdout = process.stdout.read(64 * 1024).decode("utf-8", "replace")
        stderr = process.stderr.read(64 * 1024).decode("utf-8", "replace")
        if process.returncode != 0:
            raise ApiError(
                HTTPStatus.BAD_REQUEST if process.returncode == 2 else HTTPStatus.INTERNAL_SERVER_ERROR,
                _helper_error_message(stdout, stderr) or "agent file upload helper failed",
            )
        try:
            uploaded = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise ApiError(HTTPStatus.INTERNAL_SERVER_ERROR, "agent file upload helper returned invalid JSON") from exc
        if (
            not isinstance(uploaded, dict)
            or not isinstance(uploaded.get("name"), str)
            or uploaded.get("original_name") != filename
            or uploaded.get("path") != f"user-files/{uploaded.get('name')}"
            or uploaded.get("size_bytes") != length
            or not isinstance(uploaded.get("uploaded_at"), str)
        ):
            raise ApiError(HTTPStatus.INTERNAL_SERVER_ERROR, "agent file upload helper returned invalid JSON")
        self._send_json(HTTPStatus.OK, {"file": uploaded})


    @staticmethod
    def _terminate_agent_file_upload_helper(process: subprocess.Popen[bytes]) -> None:
        try:
            process.kill()
            process.wait(timeout=AGENT_FILE_HELPER_TIMEOUT_SECONDS)
        except (PermissionError, subprocess.TimeoutExpired):
            # The admin user may not be allowed to signal a sudo helper after
            # it demotes. Never turn the request timeout into an unbounded wait.
            pass

    def _content_length(self, maximum: int) -> int:
        raw = self.headers.get("Content-Length")
        if raw is None:
            raise ApiError(HTTPStatus.LENGTH_REQUIRED, "Content-Length is required")
        try:
            length = int(raw)
        except ValueError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, "malformed Content-Length") from exc
        if length < 0:
            raise ApiError(HTTPStatus.BAD_REQUEST, "malformed Content-Length")
        if length > maximum:
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, f"upload exceeds {maximum} bytes")
        return length


class _RouteRequest(NamedTuple):
    """One dispatched request, as its route handler sees it."""

    method: str
    path: str
    query: dict[str, list[str]]
    body: Any
    path_match: re.Match[str] | None

    def capture(self, group: int) -> str:
        """A capture from this route's path pattern. Only pattern routes have
        one, so a handler asking an exact-path route for a capture is a routing
        table mistake rather than a client error."""
        if self.path_match is None:
            raise TypeError("route has no path pattern")
        return self.path_match.group(group)


class _Route(NamedTuple):
    """One admin API route.

    ``method`` of ``None`` matches any method, and ``path`` is either an exact
    path or a pattern matched against the whole path. ``operator_only`` marks a
    route the Workspace principal may never reach even if the shared allowlist
    ever named it, and ``query_keys`` is the set of query parameters the route
    accepts; anything else is rejected before the handler runs.
    """

    method: str | None
    path: str | re.Pattern[str]
    handler: Callable[[_RouteRequest], Any]
    operator_only: bool = False
    query_keys: frozenset[str] | None = None
    query_label: str = ""


def _route_matches(entry: _Route, method: str, path: str) -> tuple[bool, re.Match[str] | None]:
    if entry.method is not None and entry.method != method:
        return False, None
    if isinstance(entry.path, str):
        return path == entry.path, None
    path_match = entry.path.fullmatch(path)
    return path_match is not None, path_match


def _workspace_proxy_route(request: _RouteRequest) -> Any:
    return workspace_proxy.route_request(request.method, request.path, request.query, request.body)


def _agent_accounts_route(request: _RouteRequest) -> dict[str, Any]:
    if request.query:
        raise ApiError(HTTPStatus.BAD_REQUEST, "agent-runtime account endpoint does not accept query parameters")
    return current_agent_accounts()


def _agent_events_route(request: _RouteRequest) -> dict[str, Any]:
    return {
        "events": page_agent_events_before(
            _optional_non_negative_int(request.query, "before"),
            limit=_event_page_limit(request.query),
        )
    }


def _tool_events_route(request: _RouteRequest) -> dict[str, Any]:
    return {
        "events": state.page_tool_events_before(
            _optional_non_negative_int(request.query, "before"),
            limit=_event_page_limit(request.query),
        )
    }


def _tool_event_route(request: _RouteRequest) -> dict[str, Any]:
    if request.query:
        raise ApiError(HTTPStatus.BAD_REQUEST, "tool event detail does not accept query parameters")
    event = state.tool_event(int(request.capture(1)))
    if event is None:
        raise ApiError(HTTPStatus.NOT_FOUND, "tool event not found")
    return {"event": event}


def _host_diagnostics_route(request: _RouteRequest) -> dict[str, Any]:
    service_filter = _one(request.query, "service")
    if service_filter is not None and SERVICE_NAME_RE.fullmatch(service_filter) is None:
        raise ApiError(HTTPStatus.BAD_REQUEST, "host diagnostic service is invalid")
    severity_filter = _one(request.query, "severity")
    if severity_filter is not None and severity_filter not in {"error", "warning"}:
        raise ApiError(HTTPStatus.BAD_REQUEST, "host diagnostic severity is invalid")
    return {
        "events": state.page_host_diagnostics_before(
            _optional_non_negative_int(request.query, "before"),
            service=service_filter,
            severity=severity_filter,
            limit=_event_page_limit(request.query),
        )
    }


def _host_diagnostic_route(request: _RouteRequest) -> dict[str, Any]:
    if request.query:
        raise ApiError(HTTPStatus.BAD_REQUEST, "host diagnostic detail does not accept query parameters")
    diagnostic = state.host_diagnostic(int(request.capture(1)))
    if diagnostic is None:
        raise ApiError(HTTPStatus.NOT_FOUND, "host diagnostic not found")
    return {"diagnostic": diagnostic}


def _network_events_route(request: _RouteRequest) -> dict[str, Any]:
    return {
        "events": state.page_network_events_before(
            _optional_non_negative_int(request.query, "before"),
            decision=_network_event_decision(request.query),
            limit=_event_page_limit(request.query),
        )
    }


def _delete_github_credential_route(request: _RouteRequest) -> dict[str, Any]:
    deleted = github_credential.delete()
    github_repo_audit.refresh(force=True)
    return _credential_response(deleted)


def _github_audit_route(request: _RouteRequest) -> dict[str, Any]:
    # The UI's re-check action: re-converge with a fresh mint first (grants may
    # have changed on GitHub), force-refresh the repository audits with that
    # published token, and return the updated credential view (warnings
    # included).
    github_credential.reconcile(mint_fresh=True)
    github_repo_audit.refresh(force=True)
    return _credential_response(github_credential.metadata())


def _resolve_pending_push_route(request: _RouteRequest) -> dict[str, Any]:
    parts = [part for part in request.path.split("/") if part]
    # .../github-pending-pushes/<id>/<approve|reject>
    if len(parts) != 5 or parts[4] not in ("approve", "reject"):
        raise ApiError(HTTPStatus.NOT_FOUND, "route not found")
    return resolve_pending_push(parts[3], parts[4])


# The Workspace service reaches the admin API through these proxied subtrees;
# each one is operator-only here so a Workspace principal can never take the
# proxy path back into the admin API.
_WORKSPACE_PROXY_SUBTREES = ("getting-started", "chat", "web-apps", "memory", "schedules")

# Order decides ties: the first entry whose method and path match handles the
# request. Specific paths therefore precede the subtree entries that would
# otherwise swallow them, and a pattern that does not match (a non-numeric
# event id, say) falls through to the next entry. A request matching no entry
# is a 404, and a method with no entry for an otherwise known path is a 404
# too, never a 405.
_ROUTES: tuple[_Route, ...] = (
    _Route("GET", "/v1/health", lambda request: health()),
    _Route("GET", "/v1/agent-runtime/status", lambda request: agent_runtime_status()),
    _Route("GET", "/v1/agent-runtime/account", _agent_accounts_route),
    _Route("POST", "/v1/agent-runtime/refresh", lambda request: refresh_agent_runtime_accounts(request.body)),
    *(
        _Route(
            None,
            re.compile(rf"/v1/workspace/{re.escape(subtree)}(?:/.*)?"),
            _workspace_proxy_route,
            operator_only=True,
        )
        for subtree in _WORKSPACE_PROXY_SUBTREES
    ),
    _Route("POST", "/v1/agent-runtime/codex-oauth-login", lambda request: start_codex_oauth_login()),
    _Route("GET", "/v1/agent-runtime/codex-oauth-login", lambda request: current_codex_oauth_login()),
    _Route("POST", "/v1/agent-runtime/claude-oauth-login", lambda request: start_claude_oauth_login()),
    _Route("GET", "/v1/agent-runtime/claude-oauth-login", lambda request: current_claude_oauth_login()),
    _Route(
        "POST",
        "/v1/agent-runtime/claude-oauth-login/complete",
        lambda request: complete_claude_oauth_login(request.body),
    ),
    _Route("POST", "/v1/agent-runtime/grok-oauth-login", lambda request: start_grok_oauth_login()),
    _Route("GET", "/v1/agent-runtime/grok-oauth-login", lambda request: current_grok_oauth_login()),
    _Route("GET", "/v1/agent-runtime/bedrock-credentials", lambda request: current_bedrock_credentials()),
    _Route(
        "POST",
        "/v1/agent-runtime/bedrock-credentials",
        lambda request: connect_bedrock_credentials(request.body),
    ),
    _Route("DELETE", "/v1/agent-runtime/bedrock-credentials", lambda request: disconnect_bedrock_credentials()),
    _Route("POST", "/v1/agent-runtime/reset-linked-account", lambda request: reset_linked_account(request.body)),
    _Route(
        "GET",
        "/v1/threads",
        lambda request: list_threads(request.query),
        query_keys=frozenset({"before", "limit", "prefix"}),
        query_label="thread list",
    ),
    _Route("POST", "/v1/conversation-history/search", lambda request: search_conversation_history(request.body)),
    _Route("POST", "/v1/conversation-history/read", lambda request: read_conversation_history(request.body)),
    _Route(
        None,
        re.compile(r"/v1/threads/.*"),
        lambda request: thread_route(request.method, request.path, request.query, request.body),
    ),
    _Route(
        "GET",
        "/v1/events",
        _agent_events_route,
        query_keys=frozenset({"before", "limit"}),
        query_label="event",
    ),
    _Route("GET", "/v1/network/policy", lambda request: network_policy.network_policy_response()),
    _Route("PUT", "/v1/network/policy", lambda request: replace_network_policy(request.body)),
    _Route(
        "GET",
        "/v1/tools/events",
        _tool_events_route,
        query_keys=frozenset({"before", "limit"}),
        query_label="tool event",
    ),
    _Route("GET", re.compile(r"/v1/tools/events/([1-9][0-9]*)"), _tool_event_route),
    _Route(
        "GET",
        "/v1/host-diagnostics",
        _host_diagnostics_route,
        query_keys=frozenset({"before", "limit", "service", "severity"}),
        query_label="host diagnostic",
    ),
    _Route("GET", re.compile(r"/v1/host-diagnostics/([1-9][0-9]*)"), _host_diagnostic_route),
    _Route(
        None,
        re.compile(r"/v1/tools(?:/.*)?"),
        lambda request: tools_admin_api.tools_route(request.method, request.path, request.body),
    ),
    _Route(
        "GET",
        "/v1/network/events",
        _network_events_route,
        query_keys=frozenset({"before", "decision", "limit"}),
        query_label="network event",
    ),
    # Deliberately not gated on the GitHub integration being enabled: staging
    # the credential first, then enabling, is the flow that never leaves the
    # proxy allowing repositories with no working token. reconcile() ties the
    # published token to enablement either way.
    _Route(
        "GET",
        "/v1/network-tools/github-credential",
        lambda request: _credential_response(github_credential.metadata()),
    ),
    _Route("PUT", "/v1/network-tools/github-credential", lambda request: replace_github_credential(request.body)),
    _Route("DELETE", "/v1/network-tools/github-credential", _delete_github_credential_route),
    _Route("POST", "/v1/network-tools/github-audit", _github_audit_route),
    _Route(
        "GET",
        "/v1/network-tools/github-pending-pushes",
        lambda request: {"pending_pushes": state.read_pending_pushes()},
    ),
    _Route(
        "POST",
        re.compile(r"/v1/network-tools/github-pending-pushes/.*"),
        _resolve_pending_push_route,
    ),
    _Route("GET", "/v1/agent-files", lambda request: agent_file_list(_agent_file_path(request.query))),
    _Route("GET", "/v1/agent-files/read", lambda request: agent_file_read(_agent_file_path(request.query))),
    _Route("GET", "/v1/agent-processes", lambda request: agent_processes()),
    _Route("POST", "/v1/host-runtime/reboot", lambda request: reboot_host()),
)


def route(
    method: str,
    path: str,
    query: dict[str, list[str]],
    body: Any,
    *,
    principal: RoutePrincipal,
) -> Any:
    # Authentication belongs to the two transport boundaries. Requiring their
    # resulting principal here (with no operator-like default) makes any new
    # in-process caller an explicit security-boundary decision.
    is_operator = isinstance(principal, OperatorPrincipal)
    if not is_operator and not isinstance(principal, WorkspacePrincipal):
        raise TypeError("route principal is invalid")
    if (
        not is_operator
        and not workspace_admin_api.is_allowed_workspace_admin_route(method, path)
    ):
        # The Unix-socket handler enforces the same allowlist before dispatch.
        # Keep the principal check here as defense in depth so a future
        # in-process caller cannot accidentally turn WorkspacePrincipal into
        # general admin authority.
        raise ApiError(HTTPStatus.FORBIDDEN, "Workspace service route is not allowed")
    for entry in _ROUTES:
        matched, path_match = _route_matches(entry, method, path)
        if not matched:
            continue
        if entry.operator_only and not is_operator:
            raise ApiError(HTTPStatus.FORBIDDEN, "Workspace service route is not allowed")
        if entry.query_keys is not None:
            _reject_query_keys(query, entry.query_keys, entry.query_label)
        return entry.handler(_RouteRequest(method, path, query, body, path_match))
    raise ApiError(HTTPStatus.NOT_FOUND, "route not found")


def resolve_pending_push(push_id: str, action: str) -> dict[str, Any]:
    try:
        push = github_pending_push.approve(push_id) if action == "approve" else github_pending_push.reject(push_id)
    except github_pending_push.PendingPushError as exc:
        status = HTTPStatus.NOT_FOUND if "not found" in str(exc) else HTTPStatus.CONFLICT
        raise ApiError(status, str(exc)) from exc
    return {"pending_push": push}


def replace_github_credential(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise ApiError(HTTPStatus.BAD_REQUEST, "GitHub credential request must be an object")
    mode = body.get("mode")
    if mode == "pat":
        extra = sorted(set(body) - {"mode", "token"})
        if extra:
            raise ApiError(HTTPStatus.BAD_REQUEST, f"GitHub credential request has unsupported fields: {', '.join(extra)}")
        token = body.get("token")
        if not isinstance(token, str) or not token.strip() or any(character.isspace() for character in token):
            raise ApiError(HTTPStatus.BAD_REQUEST, "token must be a non-empty token string without whitespace")
        saved = github_credential.set_pat(token.strip())
        github_repo_audit.refresh(force=True)
        return _credential_response(saved)
    if mode == "app":
        extra = sorted(set(body) - {"mode", "app_id", "installation_id", "private_key_pem"})
        if extra:
            raise ApiError(HTTPStatus.BAD_REQUEST, f"GitHub credential request has unsupported fields: {', '.join(extra)}")
        app_id = body.get("app_id")
        installation_id = body.get("installation_id")
        private_key_pem = body.get("private_key_pem")
        if not isinstance(app_id, str) or not re.fullmatch(r"[0-9]{1,20}", app_id.strip()):
            raise ApiError(HTTPStatus.BAD_REQUEST, "app_id must be the numeric GitHub App id")
        if not isinstance(installation_id, str) or not re.fullmatch(r"[0-9]{1,20}", installation_id.strip()):
            raise ApiError(HTTPStatus.BAD_REQUEST, "installation_id must be the numeric installation id")
        if not isinstance(private_key_pem, str) or not private_key_pem.strip().startswith("-----BEGIN"):
            raise ApiError(HTTPStatus.BAD_REQUEST, "private_key_pem must be the GitHub App PEM private key")
        saved = github_credential.set_app(app_id.strip(), installation_id.strip(), private_key_pem.strip() + "\n")
        github_repo_audit.refresh(force=True)
        return _credential_response(saved)
    raise ApiError(HTTPStatus.BAD_REQUEST, "mode must be 'pat' or 'app'")


def _credential_response(metadata: dict[str, Any]) -> dict[str, Any]:
    """The credential metadata plus per-repository audit warnings.

    Audit summaries are still useful without a configured credential: configured
    write repositories then cannot be verified, and the UI should show that as a
    warning instead of silently reporting no audit state.
    """
    audits = github_repo_audit.summaries()
    if audits:
        metadata = {**metadata, "repository_audits": audits}
    return metadata






def reboot_host() -> dict[str, str]:
    """Run the reboot helper synchronously. ``systemctl reboot`` only schedules
    the reboot and returns, so this stays fast — and a helper that fails to even
    schedule it (e.g. a broken sudoers entry) surfaces as a 500 instead of a
    silent "accepted" for a reboot that will never happen. The host goes down
    moments after the response is sent."""
    try:
        proc = _run_root_helper(
            ["/usr/bin/sudo", "-n", "/usr/local/lib/kern-host/reboot-host"],
            REBOOT_HELPER_TIMEOUT_SECONDS,
        )
    except HelperTimedOut:
        # The reboot may already be in flight; report accepted rather than a
        # false failure.
        return {"status": "accepted"}
    if proc.returncode != 0:
        raise ApiError(HTTPStatus.INTERNAL_SERVER_ERROR, proc.stderr.strip() or "reboot helper failed")
    return {"status": "accepted"}














def health() -> dict[str, Any]:
    runtime = agent_runtime_status()
    network_status = network_policy.network_status()
    version = version_status()
    host = host_metrics()
    network_error_detail = "The saved network-control policy could not be validated."
    if network_status == "active" and not proxy_alive():
        network_status = "error"  # policy says active but nothing is enforcing it
        network_error_detail = (
            "The network policy is active, but the enforcing proxy is not accepting connections."
        )
    issues = _health_issues(
        runtime,
        network_status,
        network_error_detail,
        version,
        host,
    )
    return {
        "status": "degraded" if issues else "ok",
        "issues": issues,
        "agent_name": load_config().get("agent_name"),
        "version": version,
        "upgrade": upgrade_check.status(),
        "agent_runtime": runtime,
        "network_controls": {"status": network_status},
        "host_runtime": host,
        "history": state.agent_history_counts(),
    }


def _health_issues(
    runtime: dict[str, Any],
    network_status: str,
    network_error_detail: str,
    version: dict[str, Any],
    host: dict[str, Any],
) -> list[dict[str, str]]:
    """Explain every predicate that makes the host health status degraded."""
    issues: list[dict[str, str]] = []
    integration_labels = {
        "codex": "Codex",
        "claude_code": "Claude Code",
        "hermes": "AWS Bedrock",
    }
    for record in runtime["runtimes"]:
        if record.get("status") != "error":
            continue
        runtime_type = str(record.get("type", "agent runtime"))
        label = orchestrator.RUNTIME_LABELS.get(runtime_type, runtime_type)
        integration_label = integration_labels.get(runtime_type, label)
        detail = record.get("error_message")
        if not isinstance(detail, str) or not detail:
            detail = "The runtime reported an error without additional details."
        issues.append(
            {
                "kind": "agent_runtime",
                "summary": f"{label} is unavailable",
                "detail": detail,
                "next_step": (
                    f"Open Home > Integrations > {integration_label}, refresh its status, "
                    "and reconnect or revalidate the account if the error continues."
                ),
            }
        )
    if network_status == "error":
        issues.append(
            {
                "kind": "network_controls",
                "summary": "Network controls are unavailable",
                "detail": network_error_detail,
                "next_step": (
                    "Stop starting agents and use the operator plane to recover or upgrade the host."
                ),
            }
        )
    if version.get("status") == "mismatch":
        runtime_version = str(version.get("runtime", "unknown"))
        state_version = str(version.get("state", "unknown"))
        issues.append(
            {
                "kind": "version",
                "summary": "Host versions do not match",
                "detail": f"Runtime {runtime_version}; durable state {state_version}.",
                "next_step": "Run a Kern upgrade or recovery from the operator plane.",
            }
        )
    elif version.get("status") != "ok":
        unavailable: list[str] = []
        if version.get("runtime") is None:
            unavailable.append("running root version (/opt/kern-host/VERSION)")
        if version.get("state") is None:
            unavailable.append(
                "durable-state version (/mnt/kern-admin/admin-state/version.json)"
            )
        detail = (
            f"Unavailable or invalid: {', '.join(unavailable)}."
            if unavailable
            else "The host version status could not be read."
        )
        issues.append(
            {
                "kind": "version",
                "summary": "Host version information is unavailable",
                "detail": detail,
                "next_step": (
                    "Run a Kern recovery from the operator plane to restore the version metadata."
                ),
            }
        )
    root = host.get("filesystem", {}).get("mounts", {}).get("root", {})
    used_bytes = root.get("used_bytes", 0)
    total_bytes = root.get("total_bytes", 0)
    if (
        isinstance(used_bytes, int)
        and isinstance(total_bytes, int)
        and total_bytes > 0
        and used_bytes / total_bytes >= 0.9
    ):
        used_percent = used_bytes / total_bytes * 100
        issues.append(
            {
                "kind": "root_filesystem",
                "summary": "Root volume is nearly full",
                "detail": f"The root filesystem is {used_percent:.1f}% full.",
                "next_step": (
                    "Stop agent work, inspect /tmp and /var/tmp, and remove unneeded temporary "
                    "files; redeploy if free space does not recover."
                ),
            }
        )
    return issues


def proxy_alive() -> bool:
    """The proxy binds loopback, which nftables always allows, so a TCP
    connect is a meaningful liveness probe even while the network is locked
    down. If the proxy is down the agent has no network path at all."""
    try:
        with socket.create_connection((HOST, PROXY_PORT), timeout=1):
            return True
    except OSError:
        return False


def prune_state() -> None:
    """Apply every time-based or append-only PostgreSQL retention policy."""
    now = datetime.now(timezone.utc)
    with state.mutation() as cur:
        state.prune_event_logs(cur)
        state.prune_host_diagnostics(cur)
        state.prune_pending_pushes(cur)
        state.prune_bedrock_usage(
            cur,
            (now - timedelta(days=state.BEDROCK_USAGE_RETAIN_DAYS))
            .date()
            .isoformat(),
        )
        # Threads with retained events keep their canonical row; unreferenced
        # mappings use the ordinary per-runtime LRU cap. Every runtime that can
        # own a thread is pruned, including the time-bounded script runtime.
        for runtime_type in sorted(AGENT_RUNTIMES):
            state.prune_thread_sessions(cur, runtime_type, THREAD_MAP_LIMIT)
    tools_host.maintain_approvals()


def maintenance_loop() -> None:
    """Prune bounded state on a schedule, never on the request path."""
    while True:
        try:
            prune_state()
        except Exception as exc:
            host_errors.report_unexpected("admin_api.maintenance", exc)
        time.sleep(MAINTENANCE_INTERVAL_SECONDS)


def embedding_index_loop() -> None:
    """Incrementally encode queued messages outside request transactions."""
    while True:
        try:
            # Clear before claiming, so work queued between the claim and the
            # wait still wakes this thread rather than waiting out the backstop.
            state.conversation_embedding_work.clear()
            pending = state.unembedded_thread_messages(embedding_client.MAX_TEXTS)
            if not pending:
                state.conversation_embedding_work.wait(
                    CONVERSATION_EMBEDDING_IDLE_SECONDS
                )
                continue
            pending = _bounded_embedding_batch(pending)
            # Clip by UTF-8 bytes, the bound embed_texts actually validates.
            # The JSON-escaped helper is for the event bridge; using it here
            # would spend roughly three bytes of budget per non-ASCII byte and
            # drop the tail of a valid message out of the index.
            texts = [
                agent_activity.clip_text(message, embedding_client.MAX_TEXT_BYTES)
                for _seq, message in pending
            ]
            try:
                vectors = embedding_client.embed_texts(texts, kind="passage")
            except embedding_client.EmbeddingError as exc:
                if exc.batch_rejected:
                    # Only a request-validation failure says anything about the
                    # texts. Service availability and response-shape failures
                    # must not consume their retry budget.
                    state.record_embedding_attempts([seq for seq, _ in pending])
                raise
            state.store_thread_message_embeddings(
                embedding_client.MODEL_NAME,
                [(pending[index][0], vector) for index, vector in enumerate(vectors)],
            )
            time.sleep(0.25)
        except Exception as exc:
            host_errors.report_unexpected("admin_api.embedding_index", exc)
            time.sleep(30)


def _bounded_embedding_batch(
    pending: list[tuple[int, str]],
) -> list[tuple[int, str]]:
    """Trim a claim to a bounded number of bytes.

    The embedding service handles one request at a time, so an in-flight batch
    is exactly how long an interactive search can be stuck behind indexing.
    Capping total bytes keeps that wait bounded and predictable instead of
    letting it scale with whatever eight messages happened to be queued.
    """
    bounded: list[tuple[int, str]] = []
    budget = CONVERSATION_EMBEDDING_BATCH_BYTES
    for seq, message in pending:
        size = min(len(message.encode()), embedding_client.MAX_TEXT_BYTES)
        if bounded and size > budget:
            break
        bounded.append((seq, message))
        budget -= size
        if budget <= 0:
            break
    return bounded




# Bedrock is absent on purpose: its usage is computed live from the proxy's
# token counters (_bedrock_live_usage), never stored on the account row.
_RUNTIME_USAGE_KEYS = {
    "codex": "codex_usage",
    "claude_code": "claude_usage",
    "grok": "grok_usage",
}

# Serialize sends for one thread from the first live-turn check through
# admission or synchronous steering. A fixed stripe set avoids an unbounded
# per-thread lock registry while unrelated threads normally proceed in
# parallel.
_THREAD_SEND_LOCKS = tuple(threading.Lock() for _ in range(64))




















def replace_network_policy(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise ApiError(HTTPStatus.BAD_REQUEST, "request body must be the replacement network_controls object")
    try:
        parsed = parse_network_controls(body)
    except ConfigError as exc:
        raise ApiError(HTTPStatus.BAD_REQUEST, str(exc)) from exc
    policy = parsed.to_json()
    # The validated policy goes straight to the database row the proxy reads
    # (the proxy role cannot write it back); the write is atomic under the
    # mutation lock. Two concurrent replacements are last-writer-wins — a
    # single operator double-submitting — and the runtime reconcile below is
    # idempotent and re-run by the poller either way.
    record = state.network_policy_record()
    previous_policy = record["controls"] if record else {}
    updated_at = utc_now()
    state.save_network_policy(policy, updated_at)
    orchestrator.reconcile_runtime_status_after_policy_change()
    # Converge the installed GitHub credential to the published policy —
    # install on enable, remove on disable, with a fresh App mint on any
    # GitHub-integration change (an installation token only covers
    # repositories granted at mint time, so it must postdate the
    # enablement/repository list it serves). Enablement and credential health
    # stay separate concerns: a publish never fails on credential problems; a
    # failed mint or install records itself in the credential's validation
    # status, the working token is withdrawn (fail closed), and the poller
    # retries. The policy is already committed, so a transient convergence
    # failure (e.g. a policy read racing a concurrent replace) must not turn
    # this publish into an error — the poller retries convergence either way.
    # Repository audits
    # follow with the published token (forced on a GitHub change, TTL-gated
    # otherwise); they warn, never gate, so the publish result does not
    # depend on them either.
    try:
        github_changed = network_policy.managed_integration("github", previous_policy) != network_policy.managed_integration("github", policy)
        github_credential.reconcile(mint_fresh=github_changed)
        github_repo_audit.refresh(force=github_changed)
    except Exception:
        pass
    return {"network_controls": policy, "updated_at": updated_at}
























def _optional_non_negative_int(query: dict[str, list[str]], key: str) -> int | None:
    value = _one(query, key)
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"{key} must be an integer") from exc
    if parsed < 0:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"{key} must be non-negative")
    return parsed


def _optional_bounded_positive_query_int(
    query: dict[str, list[str]],
    key: str,
    maximum: int,
) -> int | None:
    value = _one(query, key)
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"{key} must be an integer") from exc
    if parsed < 1:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"{key} must be positive")
    if parsed > maximum:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"{key} must be at most {maximum}")
    return parsed




def _network_event_decision(query: dict[str, list[str]]) -> str | None:
    value = _one(query, "decision")
    if value is None or value == "all":
        return None
    if value not in {"allowed", "denied"}:
        raise ApiError(HTTPStatus.BAD_REQUEST, "decision must be allowed, denied, or all")
    return value


def _event_page_limit(query: dict[str, list[str]]) -> int:
    value = _one(query, "limit")
    if value is None:
        return state.EVENT_PAGE_LIMIT
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ApiError(HTTPStatus.BAD_REQUEST, "limit must be an integer") from exc
    if parsed < 1:
        raise ApiError(HTTPStatus.BAD_REQUEST, "limit must be positive")
    if parsed > state.EVENT_PAGE_LIMIT:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"limit must be at most {state.EVENT_PAGE_LIMIT}")
    return parsed








def _reject_query_keys(query: dict[str, list[str]], allowed: frozenset[str], label: str) -> None:
    unexpected = sorted(set(query) - allowed)
    if unexpected:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"unsupported {label} query parameter: {unexpected[0]}")


def _minutes_from_now(minutes: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + minutes * 60))


def initialize_state() -> None:
    """Recover after a restart or reboot: a run persisted as running died with
    the admin process, so return its thread to idle and record the interruption.
    (A pending push interrupted mid-resolve is still pending and the operator
    approves or rejects it again.) The tools service applies the same policy
    to its own interrupted state at its startup: an approval caught
    mid-execution is marked failed, never re-executed
    (tools_host.recover_interrupted_approvals)."""
    error_message = "host runtime restarted while the thread was running"
    with state.mutation() as cur:
        for thread_id, run_number in state.recover_interrupted_thread_runs(cur):
            state.append_agent_event(
                cur,
                "thread.error",
                thread_id,
                {"error_message": error_message},
                run_number=run_number,
            )


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """A threading server that caps concurrent worker threads with a semaphore
    so a flood of connections cannot exhaust host memory or threads; excess
    connections wait in the listen backlog until a slot frees."""

    daemon_threads = True

    def __init__(self, *args: Any, max_workers: int = MAX_CONCURRENT_REQUESTS, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._request_slots = threading.BoundedSemaphore(max_workers)

    def process_request(self, request: Any, client_address: Any) -> None:
        # Runs on the accept loop: block here when at capacity so new
        # connections queue in the backlog instead of spawning unbounded threads.
        self._request_slots.acquire()
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._request_slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()


def main() -> int:
    # Schema migrations are deploy-plane work: bootstrap runs `migrate up`
    # before services start, and the service itself never migrates — a stray
    # service start can therefore never move the schema under other code, and
    # a schema/code mismatch (unsupported) fails loudly here instead of being
    # papered over.
    # Bind the port before touching state: the state lock is in-process only,
    # so the bind is the single-instance gate. A second instance must fail here
    # rather than fail the live instance's running turn first.
    httpd = BoundedThreadingHTTPServer((HOST, PORT), Handler)
    workspace_httpd = workspace_admin_api.create_workspace_admin_server()
    initialize_state()
    # Cache the admin password hash once so the login path never touches the
    # database (reconfigure restarts this service, which reloads it).
    admin_auth.preload_password_verifier()
    # The agent-facing tools socket and tool execution run in the dedicated
    # kern-tools service (its own user, egress, and scoped DB role); the
    # admin service only forwards operator operations to it.
    orchestrator.start_background_loops()
    threading.Thread(target=maintenance_loop, daemon=True).start()
    threading.Thread(target=embedding_index_loop, daemon=True).start()
    threading.Thread(target=upgrade_check.poll, daemon=True).start()
    threading.Thread(target=workspace_httpd.serve_forever, daemon=True).start()
    try:
        httpd.serve_forever()
    finally:
        workspace_httpd.server_close()
        workspace_admin_api.unlink_workspace_admin_socket()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
