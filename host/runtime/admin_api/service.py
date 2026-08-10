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
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import threading
import time
from typing import Any, Callable, cast, NamedTuple
from urllib.parse import parse_qs, urlparse

from host.config import AGENT_RUNTIMES, ConfigError, parse_network_controls
from host.constants import ADMIN_API_PORT, LOOPBACK, MAX_REQUEST_BODY_BYTES, PROXY_PORT
from host.network_integrations.bedrock.manifest import SUPPORTED_REGIONS as BEDROCK_REGIONS
from host.network_integrations.github.push_gate import pending as github_pending_push
from host.session_options import session_config_error
# workspace_admin_api imports this module back to dispatch through route().
# The cycle is safe with plain module imports: each side binds the module
# object and reads its attributes only at request time, never during import.
from host.runtime.admin_api import admin_auth, admin_passkeys, agent_activity, workspace_api as workspace_admin_api, workspace_proxy, bedrock_credentials, claude_code, codex_app_server, github_credential, github_repo_audit, orchestrator, tools_client as tools_admin_api, upgrade_check
from host.runtime.core import host_errors, network_policy, pgclient, state
from host.runtime.tools import tools_host
from host.runtime.admin_api.orchestrator import agent_runtime_status
from host.runtime.core.state import (
    load_config,
    page_agent_events_before,
    read_claude_account,
    read_openai_account,
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
    "/favicon.ico": (ADMIN_UI_DIR / "favicon.svg", "image/svg+xml"),
    "/favicon.svg": (ADMIN_UI_DIR / "favicon.svg", "image/svg+xml"),
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
    r"(?=^[a-z0-9-]{1,64}$)^(?:app|thread|schedule)-[a-z0-9-]+$"
)
PRODUCT_THREAD_PREFIX_RE = re.compile(
    r"(?=^[a-z0-9-]{1,64}$)^(?:app|thread|schedule)-[a-z0-9-]*$"
)
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
CONVERSATION_CURSOR_BYTES = 512
CONVERSATION_MESSAGE_BYTES = 16 * 1024
CONVERSATION_RESPONSE_BYTES = 256 * 1024
CONVERSATION_EVENT_TYPES = ("thread.message", "thread.activity")
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
AGENT_FILE_HELPER_TIMEOUT_SECONDS = 10
AGENT_FILE_HELPER_COMMAND = ["/usr/bin/sudo", "-n", "/usr/local/lib/kern-host/read-agent-file"]
AGENT_FILE_UPLOAD_HELPER_COMMAND = ["/usr/bin/sudo", "-n", "/usr/local/lib/kern-host/upload-agent-file"]
AGENT_FILE_UPLOAD_MAX_BYTES = 25 * 1024 * 1024
AGENT_FILE_UPLOAD_FILENAME_MAX_BYTES = 200
AGENT_FILE_STREAM_MAX_BYTES = 200_000_000
AGENT_FILE_IMAGE_STREAM_MAX_BYTES = 25 * 1024 * 1024
AGENT_FILE_STREAM_MEDIA_TYPES = {
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}
AGENT_AUTH_CLEAR_HELPER_TIMEOUT_SECONDS = 10
AGENT_AUTH_CLEAR_HELPER_COMMAND = ["/usr/bin/sudo", "-n", "/usr/local/lib/kern-host/clear-agent-auth"]
AGENT_CGROUP_ROOT = Path("/sys/fs/cgroup/kern_agent.slice")
PROC_ROOT = Path("/proc")
AGENT_PROCESS_LIMIT = 1000
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

    def _send_agent_file(self, path: str) -> None:
        expected_media_type = AGENT_FILE_STREAM_MEDIA_TYPES.get(Path(path).suffix.lower())
        if expected_media_type is None:
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                "agent file streaming supports only MP4, MOV, JPEG, PNG, or WebP files",
            )
        maximum_size = (
            AGENT_FILE_IMAGE_STREAM_MAX_BYTES
            if expected_media_type.startswith("image/")
            else AGENT_FILE_STREAM_MAX_BYTES
        )
        process = subprocess.Popen(
            [*AGENT_FILE_HELPER_COMMAND, "stream", path],
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
                self.send_header(name, value)
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
    if method == "GET" and path == "/v1/health":
        return health()
    if method == "GET" and path == "/v1/agent-runtime/status":
        return agent_runtime_status()
    if method == "GET" and path == "/v1/agent-runtime/account":
        if query:
            raise ApiError(HTTPStatus.BAD_REQUEST, "agent-runtime account endpoint does not accept query parameters")
        return current_agent_accounts()
    if method == "POST" and path == "/v1/agent-runtime/refresh":
        return refresh_agent_runtime_accounts(body)
    if path == "/v1/workspace/chat" or path.startswith("/v1/workspace/chat/"):
        if not is_operator:
            raise ApiError(HTTPStatus.FORBIDDEN, "Workspace service route is not allowed")
        return workspace_proxy.route_request(method, path, query, body)
    if path == "/v1/workspace/web-apps" or path.startswith("/v1/workspace/web-apps/"):
        if not is_operator:
            raise ApiError(HTTPStatus.FORBIDDEN, "Workspace service route is not allowed")
        return workspace_proxy.route_request(method, path, query, body)
    if path == "/v1/workspace/memory" or path.startswith("/v1/workspace/memory/"):
        if not is_operator:
            raise ApiError(HTTPStatus.FORBIDDEN, "Workspace service route is not allowed")
        return workspace_proxy.route_request(method, path, query, body)
    if path == "/v1/workspace/schedules" or path.startswith("/v1/workspace/schedules/"):
        if not is_operator:
            raise ApiError(HTTPStatus.FORBIDDEN, "Workspace service route is not allowed")
        return workspace_proxy.route_request(method, path, query, body)
    if path == "/v1/agent-runtime/codex-oauth-login":
        if method == "POST":
            return start_codex_oauth_login()
        if method == "GET":
            return current_codex_oauth_login()
    if path == "/v1/agent-runtime/claude-oauth-login":
        if method == "POST":
            return start_claude_oauth_login()
        if method == "GET":
            return current_claude_oauth_login()
    if path == "/v1/agent-runtime/claude-oauth-login/complete" and method == "POST":
        return complete_claude_oauth_login(body)
    if path == "/v1/agent-runtime/bedrock-credentials":
        if method == "GET":
            return current_bedrock_credentials()
        if method == "POST":
            return connect_bedrock_credentials(body)
        if method == "DELETE":
            return disconnect_bedrock_credentials()
    if path == "/v1/agent-runtime/reset-linked-account" and method == "POST":
        return reset_linked_account(body)
    if path == "/v1/threads" and method == "GET":
        _reject_query_keys(query, {"before", "limit", "prefix"}, "thread list")
        return list_threads(query)
    if path == "/v1/conversation-history/search" and method == "POST":
        return search_conversation_history(body)
    if path == "/v1/conversation-history/read" and method == "POST":
        return read_conversation_history(body)
    if path.startswith("/v1/threads/"):
        return thread_route(method, path, query, body)
    if path == "/v1/events" and method == "GET":
        _reject_query_keys(query, {"before", "limit"}, "event")
        return {
            "events": page_agent_events_before(
                _optional_non_negative_int(query, "before"),
                limit=_event_page_limit(query),
            )
        }
    if path == "/v1/network/policy":
        if method == "GET":
            return network_policy.network_policy_response()
        if method == "PUT":
            return replace_network_policy(body)
    if path == "/v1/tools/events" and method == "GET":
        _reject_query_keys(query, {"before", "limit"}, "tool event")
        return {
            "events": state.page_tool_events_before(
                _optional_non_negative_int(query, "before"),
                limit=_event_page_limit(query),
            )
        }
    tool_event_match = re.fullmatch(r"/v1/tools/events/([1-9][0-9]*)", path)
    if tool_event_match and method == "GET":
        if query:
            raise ApiError(HTTPStatus.BAD_REQUEST, "tool event detail does not accept query parameters")
        event = state.tool_event(int(tool_event_match.group(1)))
        if event is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "tool event not found")
        return {"event": event}
    if path == "/v1/host-diagnostics" and method == "GET":
        _reject_query_keys(query, {"before", "limit", "service", "severity"}, "host diagnostic")
        service_filter = _one(query, "service")
        if service_filter is not None and SERVICE_NAME_RE.fullmatch(service_filter) is None:
            raise ApiError(HTTPStatus.BAD_REQUEST, "host diagnostic service is invalid")
        severity_filter = _one(query, "severity")
        if severity_filter is not None and severity_filter not in {"error", "warning"}:
            raise ApiError(HTTPStatus.BAD_REQUEST, "host diagnostic severity is invalid")
        return {
            "events": state.page_host_diagnostics_before(
                _optional_non_negative_int(query, "before"),
                service=service_filter,
                severity=severity_filter,
                limit=_event_page_limit(query),
            )
        }
    host_diagnostic_match = re.fullmatch(r"/v1/host-diagnostics/([1-9][0-9]*)", path)
    if host_diagnostic_match and method == "GET":
        if query:
            raise ApiError(HTTPStatus.BAD_REQUEST, "host diagnostic detail does not accept query parameters")
        diagnostic = state.host_diagnostic(int(host_diagnostic_match.group(1)))
        if diagnostic is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "host diagnostic not found")
        return {"diagnostic": diagnostic}
    if path == "/v1/tools" or path.startswith("/v1/tools/"):
        return tools_admin_api.tools_route(method, path, body)
    if path == "/v1/network/events" and method == "GET":
        _reject_query_keys(query, {"before", "decision", "limit"}, "network event")
        return {
            "events": state.page_network_events_before(
                _optional_non_negative_int(query, "before"),
                decision=_network_event_decision(query),
                limit=_event_page_limit(query),
            )
        }
    if path == "/v1/network-tools/github-credential":
        # Deliberately not gated on the GitHub integration being enabled:
        # staging the credential first, then enabling, is the flow that never
        # leaves the proxy allowing repositories with no working token.
        # reconcile() ties the published token to enablement either way.
        if method == "GET":
            return _credential_response(github_credential.metadata())
        if method == "PUT":
            return replace_github_credential(body)
        if method == "DELETE":
            deleted = github_credential.delete()
            github_repo_audit.refresh(force=True)
            return _credential_response(deleted)
    if path == "/v1/network-tools/github-audit" and method == "POST":
        # The UI's re-check action: re-converge with a fresh mint first
        # (grants may have changed on GitHub), force-refresh the repository
        # audits with that published token, and
        # return the updated credential view (warnings included).
        github_credential.reconcile(mint_fresh=True)
        github_repo_audit.refresh(force=True)
        return _credential_response(github_credential.metadata())
    if path == "/v1/network-tools/github-pending-pushes" and method == "GET":
        return {"pending_pushes": state.read_pending_pushes()}
    if path.startswith("/v1/network-tools/github-pending-pushes/") and method == "POST":
        parts = [part for part in path.split("/") if part]
        # .../github-pending-pushes/<id>/<approve|reject>
        if len(parts) == 5 and parts[4] in ("approve", "reject"):
            return resolve_pending_push(parts[3], parts[4])
    if path == "/v1/agent-files" and method == "GET":
        return agent_file_list(_agent_file_path(query))
    if path == "/v1/agent-files/read" and method == "GET":
        return agent_file_read(_agent_file_path(query))
    if path == "/v1/agent-processes" and method == "GET":
        return agent_processes()
    if path == "/v1/host-runtime/reboot" and method == "POST":
        return reboot_host()
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


class HelperTimedOut(Exception):
    """A root helper ran past its timeout. could_not_terminate means the
    unprivileged kill of the sudo-spawned child failed too."""

    def __init__(self, could_not_terminate: bool) -> None:
        super().__init__("root helper timed out")
        self.could_not_terminate = could_not_terminate


def _run_root_helper(argv: list[str], timeout_seconds: int) -> "subprocess.CompletedProcess[str]":
    """Run one sudo root helper; each caller maps returncodes and
    HelperTimedOut to its own status policy."""
    try:
        return subprocess.run(
            argv,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_seconds,
        )
    except (subprocess.TimeoutExpired, PermissionError) as exc:
        # On timeout, subprocess.run kills the child — but the helper runs as
        # root via sudo, so the unprivileged service user's kill raises
        # PermissionError in place of TimeoutExpired.
        raise HelperTimedOut(isinstance(exc, PermissionError)) from exc


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


def agent_file_list(path: str) -> dict[str, Any]:
    return _run_agent_file_helper("list", path)


def agent_file_read(path: str) -> dict[str, Any]:
    return _run_agent_file_helper("read", path)


def _run_agent_file_helper(action: str, path: str) -> dict[str, Any]:
    try:
        proc = _run_root_helper([*AGENT_FILE_HELPER_COMMAND, action, path], AGENT_FILE_HELPER_TIMEOUT_SECONDS)
    except HelperTimedOut as exc:
        message = (
            "agent file helper timed out (the root helper could not be terminated)"
            if exc.could_not_terminate
            else "agent file helper timed out"
        )
        raise ApiError(HTTPStatus.GATEWAY_TIMEOUT, message) from exc
    if proc.returncode != 0:
        message = _helper_error_message(proc.stdout, proc.stderr)
        status = {
            2: HTTPStatus.NOT_FOUND,
            3: HTTPStatus.BAD_REQUEST,
            4: HTTPStatus.BAD_REQUEST,
        }.get(proc.returncode, HTTPStatus.INTERNAL_SERVER_ERROR)
        raise ApiError(status, message or "agent file helper failed")
    try:
        value = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ApiError(HTTPStatus.INTERNAL_SERVER_ERROR, "agent file helper returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise ApiError(HTTPStatus.INTERNAL_SERVER_ERROR, "agent file helper returned invalid JSON")
    return value


def agent_processes() -> dict[str, Any]:
    """Return a bounded process snapshot for the agent runtime slice — exactly
    the fields the admin UI renders."""
    pids = sorted(_agent_slice_pids())
    uptime = _proc_uptime()
    clk_tck = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
    processes: list[dict[str, Any]] = []
    for pid in pids[:AGENT_PROCESS_LIMIT]:
        process = _agent_process_info(pid, uptime, clk_tck)
        if process is not None:
            processes.append(process)
    return {"processes": processes, "truncated": len(pids) > AGENT_PROCESS_LIMIT}


def _agent_slice_pids() -> set[int]:
    if not AGENT_CGROUP_ROOT.is_dir():
        return set()
    pids: set[int] = set()
    try:
        for proc_file in AGENT_CGROUP_ROOT.rglob("cgroup.procs"):
            try:
                lines = proc_file.read_text().splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            for line in lines:
                try:
                    pid = int(line)
                except ValueError:
                    continue
                if pid > 0:
                    pids.add(pid)
    except OSError:
        return set()
    return pids


def _agent_process_info(pid: int, uptime: float, clk_tck: int) -> dict[str, Any] | None:
    proc_dir = PROC_ROOT / str(pid)
    try:
        stat = _proc_stat(proc_dir / "stat")
        status = _proc_status(proc_dir / "status")
    except (OSError, ValueError, IndexError):
        return None
    name = status.get("Name") or stat["name"]
    cmdline = _proc_cmdline(proc_dir / "cmdline") or f"[{name}]"
    result: dict[str, Any] = {
        "pid": pid,
        "state": stat["state"],
        "name": name,
        "cmdline": cmdline,
    }
    rss_bytes = _rss_bytes(status.get("VmRSS"))
    if rss_bytes is not None:
        result["rss_bytes"] = rss_bytes
    if uptime > 0 and clk_tck > 0:
        result["elapsed_seconds"] = int(max(0.0, uptime - (stat["start_ticks"] / clk_tck)))
    return result


def _proc_stat(path: Path) -> dict[str, Any]:
    raw = path.read_text()
    left = raw.find("(")
    right = raw.rfind(")")
    if left < 0 or right <= left:
        raise ValueError("malformed proc stat")
    fields = raw[right + 2 :].split()
    return {
        "name": raw[left + 1 : right],
        "state": fields[0],
        "start_ticks": int(fields[19]),
    }


def _proc_status(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        values[key] = value.strip()
    return values


def _proc_cmdline(path: Path) -> str:
    try:
        raw = path.read_bytes()
    except OSError:
        return ""
    return raw.rstrip(b"\0").replace(b"\0", b" ").decode("utf-8", "replace")


def _proc_uptime() -> float:
    try:
        return float((PROC_ROOT / "uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0


def _rss_bytes(rss_line: str | None) -> int | None:
    if not rss_line:
        return None
    parts = rss_line.split()
    if not parts:
        return None
    try:
        value = int(parts[0])
    except ValueError:
        return None
    unit = parts[1].lower() if len(parts) > 1 else "kb"
    return value * 1024 if unit == "kb" else value


def _helper_error_message(stdout: str, stderr: str) -> str:
    try:
        value = json.loads(stdout)
    except json.JSONDecodeError:
        return stderr.strip()
    if isinstance(value, dict):
        error = value.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str):
                return message
    return stderr.strip()


def thread_route(
    method: str,
    path: str,
    query: dict[str, list[str]],
    body: Any,
) -> Any:
    parts = path.strip("/").split("/")
    if len(parts) < 3 or not PRODUCT_THREAD_ID_RE.fullmatch(parts[2]):
        raise ApiError(HTTPStatus.NOT_FOUND, "thread route not found")
    thread_id = parts[2]
    if len(parts) == 3 and method == "GET":
        if query:
            raise ApiError(HTTPStatus.BAD_REQUEST, "thread detail does not accept query parameters")
        return {"thread": get_thread(thread_id)}
    if len(parts) == 4 and parts[3] == "messages" and method == "POST":
        return send_thread_message(thread_id, body)
    if len(parts) == 4 and parts[3] == "stop" and method == "POST":
        return stop_thread(thread_id)
    if len(parts) == 4 and parts[3] == "clear-memory" and method == "POST":
        return clear_thread_memory(thread_id)
    if len(parts) == 4 and parts[3] == "events" and method == "GET":
        _reject_query_keys(
            query,
            {"since", "before", "limit", "message_bytes", "event_type"},
            "thread event",
        )
        message_bytes = _optional_bounded_positive_query_int(
            query, "message_bytes", THREAD_EVENT_MESSAGE_BYTES_LIMIT
        )
        requested_event_types = query.get("event_type")
        event_types: tuple[str, ...] | None = None
        if requested_event_types:
            unknown_event_types = sorted(
                set(requested_event_types) - THREAD_DISPLAY_EVENT_TYPES
            )
            if unknown_event_types:
                raise ApiError(
                    HTTPStatus.BAD_REQUEST,
                    f"unsupported thread event type: {unknown_event_types[0]}",
                )
            event_types = tuple(dict.fromkeys(requested_event_types))
        since = _optional_non_negative_int(query, "since")
        before = _optional_non_negative_int(query, "before")
        if since is not None and before is not None:
            raise ApiError(HTTPStatus.BAD_REQUEST, "since and before cannot be combined")
        page_kwargs: dict[str, Any] = {"before": before}
        if event_types is not None:
            page_kwargs["event_types"] = event_types
        events = state.page_thread_events(
            thread_id, since, _event_page_limit(query), **page_kwargs
        )
        if message_bytes is not None:
            for event in events:
                payload = event.get("payload")
                if not isinstance(payload, dict):
                    continue
                for field in ("message", "error_message"):
                    value = payload.get(field)
                    if isinstance(value, str):
                        payload[field] = _clip_json_encoded_text(value, message_bytes)
                activity = payload.get("activity")
                if isinstance(activity, dict):
                    # A single event must stay below the Workspace proxy's 1 MiB
                    # response cap. Keep the useful command/tool output large,
                    # but bound both rich text fields and mark every clip.
                    detail_budget = min(message_bytes, 24 * 1024)
                    output_budget = max(1, message_bytes - detail_budget)
                    for field, budget in (("detail", detail_budget), ("output", output_budget)):
                        value = activity.get(field)
                        if isinstance(value, str):
                            activity[field] = _clip_json_encoded_text(value, budget)
        return {"events": events}
    raise ApiError(HTTPStatus.NOT_FOUND, "thread route not found")


def health() -> dict[str, Any]:
    runtime = agent_runtime_status()
    network_status = network_policy.network_status()
    version = version_status()
    if network_status == "active" and not proxy_alive():
        network_status = "error"  # policy says active but nothing is enforcing it
    degraded = (
        any(item["status"] == "error" for item in runtime["runtimes"])
        or network_status == "error"
        or version["status"] != "ok"
    )
    return {
        "status": "degraded" if degraded else "ok",
        "agent_name": load_config().get("agent_name"),
        "version": version,
        "upgrade": upgrade_check.status(),
        "agent_runtime": runtime,
        "network_controls": {"status": network_status},
        "host_runtime": host_metrics(),
        "history": state.agent_history_counts(),
    }


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
        # mappings use the ordinary per-runtime LRU cap.
        for runtime_type in AGENT_RUNTIME_TYPES:
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


def _mint_codex_login() -> tuple[dict[str, str], dict[str, str]]:
    login = codex_app_server.start_device_login()
    response = {
        "status": "awaiting_login",
        "device_code": login.user_code,
        "login_url": login.verification_url,
        "expires_at": _minutes_from_now(10),
    }
    return response, response | {"login_id": login.login_id}


def _mint_claude_login() -> tuple[dict[str, str], dict[str, str]]:
    login = claude_code.start_oauth_login()
    response = {
        "status": "awaiting_code",
        "login_url": login.login_url,
        "expires_at": _minutes_from_now(10),
    }
    return response, response


class _OAuthLoginFlow(NamedTuple):
    """One runtime's login flow: the codex and claude endpoints are the same
    machine, differing only in these fields. mint returns (public response,
    persisted record); close tears down a login whose gate re-check lost."""

    runtime_type: str
    # oauth_logins keys on the provider spelling ('claude'), not the runtime
    # type ('claude_code'); orchestrator.mark_oauth_login_completed keys the
    # same way.
    oauth_key: str
    display: str
    provider: str
    response_keys: tuple[str, ...]
    mint: Callable[[], tuple[dict[str, str], dict[str, str]]]
    close: Callable[[], None]


_OAUTH_LOGIN_FLOWS = {
    "codex": _OAuthLoginFlow(
        runtime_type="codex",
        oauth_key="codex",
        display="Codex",
        provider="OpenAI",
        response_keys=("status", "device_code", "login_url", "expires_at"),
        mint=_mint_codex_login,
        close=lambda: codex_app_server.close_login_server(),
    ),
    "claude_code": _OAuthLoginFlow(
        runtime_type="claude_code",
        oauth_key="claude",
        display="Claude",
        provider="Claude",
        response_keys=("status", "login_url", "expires_at"),
        mint=_mint_claude_login,
        close=lambda: claude_code.close_login_process(),
    ),
}


def _require_oauth_login_available(flow: _OAuthLoginFlow) -> None:
    if not orchestrator.runtime_network_enabled(flow.runtime_type):
        raise ApiError(
            HTTPStatus.CONFLICT,
            f"{flow.display} OAuth login is unavailable while {flow.provider} provider access is disabled",
        )
    if orchestrator.runtime_status(flow.runtime_type) not in OAUTH_LOGIN_STATUSES:
        raise ApiError(
            HTTPStatus.CONFLICT,
            f"{flow.display} OAuth login is only available while awaiting_login or in error",
        )


def _start_oauth_login(flow: _OAuthLoginFlow) -> dict[str, str]:
    if not OAUTH_LOGIN_LOCK.acquire(timeout=OAUTH_LOGIN_LOCK_TIMEOUT_SECONDS):
        raise ApiError(HTTPStatus.CONFLICT, f"{flow.display} OAuth login is already starting")
    try:
        _require_oauth_login_available(flow)
        oauth = state.oauth_login(flow.oauth_key)
        if oauth:
            return {key: oauth[key] for key in flow.response_keys}
        response, persisted = flow.mint()
        with state.mutation() as cur:
            # Re-check the gate inside the mutation: a policy disable or a
            # completed refresh that raced the slow mint must not park a
            # fresh login process, so the loser closes it here.
            try:
                _require_oauth_login_available(flow)
            except ApiError:
                flow.close()
                raise
            state.set_oauth_login(cur, flow.oauth_key, persisted)
        return response
    finally:
        OAUTH_LOGIN_LOCK.release()


def _current_oauth_login_response(flow: _OAuthLoginFlow) -> dict[str, str]:
    _require_oauth_login_available(flow)
    oauth = state.oauth_login(flow.oauth_key)
    if not oauth:
        raise ApiError(HTTPStatus.NOT_FOUND, f"{flow.display} OAuth login has not been started")
    return {key: oauth[key] for key in flow.response_keys}


def start_codex_oauth_login() -> dict[str, str]:
    return _start_oauth_login(_OAUTH_LOGIN_FLOWS["codex"])


def current_codex_oauth_login() -> dict[str, str]:
    return _current_oauth_login_response(_OAUTH_LOGIN_FLOWS["codex"])


def start_claude_oauth_login() -> dict[str, str]:
    return _start_oauth_login(_OAUTH_LOGIN_FLOWS["claude_code"])


def current_claude_oauth_login() -> dict[str, str]:
    return _current_oauth_login_response(_OAUTH_LOGIN_FLOWS["claude_code"])


def complete_claude_oauth_login(body: Any) -> dict[str, str]:
    if not isinstance(body, dict) or not isinstance(body.get("code"), str) or not body["code"].strip():
        raise ApiError(HTTPStatus.BAD_REQUEST, "code must be a non-empty string")
    if not orchestrator.runtime_network_enabled("claude_code"):
        raise ApiError(HTTPStatus.CONFLICT, "Claude OAuth login is unavailable while Claude provider access is disabled")
    try:
        claude_code.complete_oauth_login(body["code"])
    except claude_code.ClaudeCodeError as exc:
        raise ApiError(HTTPStatus.CONFLICT, str(exc)) from exc
    orchestrator.mark_oauth_login_completed("claude", _claude_completed_token_hash())
    status = orchestrator.refresh_runtime_status("claude_code")
    if status != "active":
        # The pending login record must survive until the refresh above: it is
        # the operator-approval window that lets the refresh capture the first
        # trusted account. On an active result the refresh clears it itself.
        with state.mutation() as cur:
            state.set_oauth_login(cur, "claude", None)
    return {"status": "accepted"}


def _claude_completed_token_hash() -> str | None:
    """Bind the operator approval to the token the login just wrote: first
    capture requires attesting this exact token, so agent credentials swapped
    after completion do not inherit the approval. If the read fails, the
    completion refresh cannot capture a first trusted account and the
    non-active completion path clears the spent login so the operator can
    retry."""
    try:
        account = claude_code.read_claude_account()
    except claude_code.ClaudeCodeError:
        return None
    value = account.get("access_token_sha256") if account else None
    return value if isinstance(value, str) and value else None


# Long-term IAM user access key ids only (AKIA prefix, 20 characters).
# Temporary session credentials (ASIA...) need an X-Amz-Security-Token the
# proxy deliberately denies, so rejecting them here with a clear message
# beats the generic STS failure they would otherwise hit.
BEDROCK_ACCESS_KEY_ID_RE = re.compile(r"^AKIA[0-9A-Z]{16}$")


def connect_bedrock_credentials(body: Any) -> dict[str, str]:
    """Store the operator-pasted AWS key pair and region as one connection.

    Only this operator API
    writes that row, so the stored credential is the approval. The request
    synchronously attests the key even while Bedrock is disabled; a failed
    candidate is never stored and leaves any previous validated connection
    unchanged."""
    if not isinstance(body, dict):
        raise ApiError(HTTPStatus.BAD_REQUEST, "request body must be an object")
    unexpected = sorted(set(body) - {"access_key_id", "secret_access_key", "region"})
    if unexpected:
        raise ApiError(HTTPStatus.BAD_REQUEST, "unexpected request fields: " + ", ".join(unexpected))
    access_key_id = body.get("access_key_id")
    secret_access_key = body.get("secret_access_key")
    region = body.get("region")
    if not isinstance(access_key_id, str) or not access_key_id.strip():
        raise ApiError(HTTPStatus.BAD_REQUEST, "access_key_id must be a non-empty string")
    if not BEDROCK_ACCESS_KEY_ID_RE.fullmatch(access_key_id.strip()):
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            "access_key_id must be a long-term IAM access key id (20 characters, AKIA prefix); "
            "temporary session credentials (ASIA...) are not supported — create a long-term "
            "access key for a dedicated IAM user instead",
        )
    if not isinstance(secret_access_key, str) or not secret_access_key.strip():
        raise ApiError(HTTPStatus.BAD_REQUEST, "secret_access_key must be a non-empty string")
    if region not in BEDROCK_REGIONS:
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            "region must be one of " + ", ".join(BEDROCK_REGIONS),
        )
    try:
        status, error_message = orchestrator.replace_and_validate_bedrock_credentials(
            access_key_id.strip(),
            secret_access_key.strip(),
            region,
        )
    except bedrock_credentials.BedrockCredentialsError as exc:
        raise ApiError(HTTPStatus.BAD_REQUEST, str(exc)) from exc
    if status != "active":
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            error_message or "AWS credential validation failed",
        )
    if not orchestrator.runtime_network_enabled("hermes"):
        return {"status": "accepted"}
    # Runtime refresh reads the validated row without another AWS call. The
    # proxy reads that same row directly.
    orchestrator.refresh_runtime_status("hermes")
    return {"status": "accepted"}


def current_bedrock_credentials() -> dict[str, Any]:
    """Return non-secret metadata for the validated Bedrock credential."""
    access_key_id = state.read_bedrock_access_key_id()
    response: dict[str, Any] = {"connected": access_key_id is not None}
    if access_key_id is not None:
        response["access_key_id"] = access_key_id
        region = state.read_bedrock_region()
        if region is not None:
            response["region"] = region
    return response


def disconnect_bedrock_credentials() -> dict[str, str]:
    """Delete the AWS connection and stop Hermes."""
    orchestrator.disconnect_bedrock_connection()
    return {"status": "accepted"}


def reset_linked_account(body: Any) -> dict[str, str]:
    """Delete the linked-account guard: the operator-approved anchor, its
    proxy pin, pending OAuth approval, local agent auth files, and old runtime
    processes. Callable in any runtime status."""
    if not isinstance(body, dict) or body.get("agent_runtime") not in OAUTH_RUNTIME_TYPES:
        raise ApiError(HTTPStatus.BAD_REQUEST, "agent_runtime must be one of " + ", ".join(OAUTH_RUNTIME_TYPES))
    runtime_type = body["agent_runtime"]
    orchestrator.reset_linked_account(runtime_type)
    try:
        _clear_local_agent_auth(runtime_type)
    except ApiError:
        orchestrator.refresh_runtime_status(runtime_type)
        raise
    orchestrator.refresh_runtime_status(runtime_type)
    return {"status": "accepted"}


def _clear_local_agent_auth(runtime_type: str) -> None:
    helper_runtime = "claude" if runtime_type == "claude_code" else "codex"
    try:
        proc = _run_root_helper(
            [*AGENT_AUTH_CLEAR_HELPER_COMMAND, helper_runtime], AGENT_AUTH_CLEAR_HELPER_TIMEOUT_SECONDS
        )
    except HelperTimedOut as exc:
        message = (
            f"{runtime_type} reset helper could not be terminated; retry reset"
            if exc.could_not_terminate
            else f"{runtime_type} reset timed out clearing local auth files; retry reset"
        )
        raise ApiError(HTTPStatus.CONFLICT, message) from exc
    if proc.returncode != 0:
        detail = _helper_error_message(proc.stdout, proc.stderr)
        message = f"{runtime_type} reset failed clearing local auth files; retry reset"
        if detail:
            message = f"{message}: {detail}"
        raise ApiError(HTTPStatus.CONFLICT, message)


AGENT_RUNTIME_TYPES = ("codex", "claude_code", "hermes")
OAUTH_RUNTIME_TYPES = ("codex", "claude_code")


def current_agent_accounts() -> dict[str, Any]:
    statuses = orchestrator.all_runtime_status_records()
    return {
        "accounts": [
            _current_agent_account(statuses, "codex"),
            _current_agent_account(statuses, "claude_code"),
            _current_bedrock_account(statuses),
        ]
    }


def refresh_agent_runtime_accounts(body: Any) -> dict[str, Any]:
    runtime_types: tuple[str, ...]
    if body is None:
        runtime_types = AGENT_RUNTIME_TYPES
    elif isinstance(body, dict):
        runtime = body.get("agent_runtime")
        if runtime is None:
            runtime_types = AGENT_RUNTIME_TYPES
        elif isinstance(runtime, str) and runtime in AGENT_RUNTIME_TYPES:
            runtime_types = (runtime,)
        else:
            raise ApiError(HTTPStatus.BAD_REQUEST, "agent_runtime must be one of " + ", ".join(AGENT_RUNTIME_TYPES))
    else:
        raise ApiError(HTTPStatus.BAD_REQUEST, "request body must be an object")
    for runtime_type in runtime_types:
        force_probe = (
            runtime_type != "hermes"
            or orchestrator.runtime_network_enabled(runtime_type)
        )
        orchestrator.refresh_runtime_status(runtime_type, force_provider_probe=force_probe)
    return current_agent_accounts()


def _current_agent_account(statuses: dict[str, dict[str, Any]], runtime_type: str) -> dict[str, Any]:
    status = str(statuses.get(runtime_type, {}).get("status", "loading"))
    if runtime_type == "claude_code":
        response = {"agent_runtime": "claude_code", "provider": "claude", "status": status}
        account = read_claude_account()
        if account.get("identity_attestation") != orchestrator.CLAUDE_IDENTITY_ATTESTATION:
            account = {}
    else:
        response = {"agent_runtime": "codex", "provider": "openai", "status": status}
        account = read_openai_account()
        if account.get("operator_approval") != orchestrator.OPENAI_OPERATOR_APPROVAL:
            account = {}
    return _account_response_tail(response, account, status, runtime_type)


def _current_bedrock_account(statuses: dict[str, dict[str, Any]]) -> dict[str, Any]:
    status = str(statuses.get("hermes", {}).get("status", "loading"))
    response: dict[str, Any] = {
        "provider": "bedrock",
        "agent_runtimes": ["hermes"],
        "status": status,
        # Live usage survives credential state on purpose: the counters record
        # month-to-date work already done, and reporting them costs one local
        # aggregate read.
        "bedrock_usage": _bedrock_live_usage(),
    }
    # Credential and display metadata are stored or cleared atomically, so the
    # account is meaningful only while the validated credential remains.
    account = state.read_bedrock_account() if state.read_bedrock_access_key_id() else {}
    return _account_response_tail(response, account, status, "bedrock")


def _account_response_tail(
    response: dict[str, Any],
    account: dict[str, Any],
    status: str,
    runtime_type: str,
) -> dict[str, Any]:
    if status == "active":
        response.update(_account_response_metadata(account, runtime_type))
        return response
    # The account anchor outlives sessions and deactivation; expose its
    # identity (never plan/usage) so the UI can show which account is linked
    # while the runtime is logged out or in error.
    for key in ("account_id", "email", "arn"):
        value = account.get(key)
        if isinstance(value, str) and value:
            response[key] = value
    return response


_BEDROCK_USAGE_TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
)


def _bedrock_live_usage() -> dict[str, Any]:
    """Month-to-date Bedrock usage.

    The proxy counts the token usage AWS reports in each allowed response and
    the USD it priced that response at, per model and UTC day. This
    sums the current month straight from those stored counters — the cost is
    the recorded figure, not re-priced at read time. It remains an estimate of
    what AWS will bill, not the bill itself: unmetered requests (``requests``
    minus ``metered_requests``) are surfaced instead of silently rounding the
    estimate down."""
    month_start = time.strftime("%Y-%m-01", time.gmtime())
    usage: dict[str, Any] = {
        "month_to_date": 0.0,
        "currency": "USD",
        "requests": 0,
        "metered_requests": 0,
        **{field: 0 for field in _BEDROCK_USAGE_TOKEN_FIELDS},
    }
    for row in state.read_bedrock_usage(month_start):
        usage["requests"] += row["requests"]
        usage["metered_requests"] += row["metered_requests"]
        usage["month_to_date"] += row["cost_usd"]
        for field in _BEDROCK_USAGE_TOKEN_FIELDS:
            usage[field] += row[field]
    usage["month_to_date"] = round(usage["month_to_date"], 4)
    return usage


# Bedrock is absent on purpose: its usage is computed live from the proxy's
# token counters (_bedrock_live_usage), never stored on the account row.
_RUNTIME_USAGE_KEYS = {
    "codex": "codex_usage",
    "claude_code": "claude_usage",
}

# Serialize sends for one thread from the first live-turn check through
# admission or synchronous steering. A fixed stripe set avoids an unbounded
# per-thread lock registry while unrelated threads normally proceed in
# parallel.
_THREAD_SEND_LOCKS = tuple(threading.Lock() for _ in range(64))


def _thread_send_lock(thread_id: str) -> threading.Lock:
    return _THREAD_SEND_LOCKS[hash(thread_id) % len(_THREAD_SEND_LOCKS)]


def _account_response_metadata(account: dict[str, Any], runtime_type: str) -> dict[str, Any]:
    # Provider capture sanitizes metadata before storage; this selects only the
    # public fields without re-normalizing provider-owned usage shapes.
    response: dict[str, Any] = {}
    for key in ("account_id", "email", "plan_type", "arn"):
        value = account.get(key)
        if isinstance(value, str) and value:
            response[key] = value
    usage_key = _RUNTIME_USAGE_KEYS.get(runtime_type)
    if usage_key is None:
        return response
    usage = account.get(usage_key)
    if isinstance(usage, dict) and usage:
        response[usage_key] = usage
    return response


def send_thread_message(
    thread_id: str,
    body: Any,
) -> dict[str, Any]:
    """The one write path for agent work: start a turn on an idle thread
    (creating the thread on its first message) or steer the thread's running
    turn. There is no queue — a message that cannot run now is rejected with
    a retry hint and the caller decides."""
    if PRODUCT_THREAD_ID_RE.fullmatch(thread_id) is None:
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            "thread_id must start with app-, thread-, or schedule-",
        )
    message = _message(body)
    with _thread_send_lock(thread_id):
        session_config = state.thread_session_config(thread_id)
        agent_runtime, model, effort = _resolve_session_config(body, session_config)
        switching_session = _session_configuration_changed(
            session_config, agent_runtime, model, effort
        )
        if not switching_session and orchestrator.steer_live_turn(
            thread_id, agent_runtime, message
        ):
            turn = None
            provider_session_id = None
        else:
            after_commit: list[Callable[[], None]] = []
            with state.mutation(after_commit=after_commit) as cur:
                # Re-read inside the admission transaction. The send lock keeps
                # same-thread messages ordered, while this snapshot keeps the
                # initial session row and turn events in one commit.
                session_config = state.thread_session_config(thread_id, cur)
                agent_runtime, model, effort = _resolve_session_config(body, session_config)
                switching_session = _session_configuration_changed(
                    session_config, agent_runtime, model, effort
                )
                launch_message = message
                session_change_activity = None
                handoff_events: list[dict[str, Any]] = []
                missing_provider_context = (
                    session_config is not None
                    and not session_config.get("provider_session_id")
                )
                if switching_session or missing_provider_context:
                    handoff_events = state.recent_thread_handoff_events(
                        cur,
                        thread_id,
                        message_character_limit=THREAD_HANDOFF_MESSAGE_CHARACTER_LIMIT,
                        activity_character_limit=THREAD_HANDOFF_ACTIVITY_CHARACTER_LIMIT,
                        activity_event_character_limit=THREAD_HANDOFF_ACTIVITY_EVENT_CHARACTER_LIMIT,
                        # A cleared thread has no provider session, so it takes
                        # the handoff path; the floor is what keeps that path
                        # from handing back the context that was cleared.
                        after_seq=int((session_config or {}).get("context_cleared_seq") or 0),
                    )
                if switching_session:
                    assert session_config is not None
                    if session_config["status"] != "idle":
                        raise ApiError(
                            HTTPStatus.CONFLICT,
                            "thread runtime, model, and effort can change only while the thread is idle",
                        )
                    try:
                        state.rotate_thread_session(
                            cur,
                            thread_id,
                            agent_runtime,
                            model,
                            effort,
                            utc_now(),
                        )
                    except ValueError as exc:
                        raise ApiError(
                            HTTPStatus.CONFLICT,
                            "thread runtime, model, and effort can change only while the thread is idle",
                        ) from exc
                    provider_session_id = None
                    session_change_activity = _session_change_activity(
                        session_config,
                        agent_runtime,
                        model,
                        effort,
                    )
                else:
                    provider_session_id = (
                        session_config.get("provider_session_id") if session_config else None
                    )
                    state.save_thread_session(
                        cur,
                        agent_runtime,
                        thread_id,
                        provider_session_id,
                        utc_now(),
                        model,
                        effort,
                    )
                # Only when there is history to hand over. Both paths above can
                # produce none — a cleared thread by its floor, a first-message
                # switch by having no events — and the prompt tells the new
                # session it is continuing a thread, which is exactly wrong for
                # a run that starts fresh.
                if handoff_events:
                    launch_message = _session_handoff_message(handoff_events, message)
                turn = orchestrator.admit_turn(
                    cur,
                    after_commit,
                    thread_id,
                    agent_runtime,
                    model,
                    effort,
                    message,
                    pre_message_activity=session_change_activity,
                )
            orchestrator.launch_turn(turn, launch_message, provider_session_id)
    return {
        "status": "accepted",
        "thread": _public_thread(thread_id, agent_runtime, model, effort),
    }


def get_thread(thread_id: str) -> dict[str, Any]:
    config = state.thread_session_config(thread_id)
    if config is None:
        raise ApiError(HTTPStatus.NOT_FOUND, "thread not found")
    return _public_thread(
        thread_id,
        config["agent_runtime"],
        config["model"],
        config["effort"],
        last_used_at=config.get("last_used_at"),
    )


def stop_thread(thread_id: str) -> dict[str, str]:
    if state.thread_session_config(thread_id) is None:
        raise ApiError(HTTPStatus.NOT_FOUND, "thread not found")
    if not orchestrator.stop_thread_turn(thread_id):
        raise ApiError(HTTPStatus.CONFLICT, "the thread has no running work")
    return {"status": "accepted"}


def clear_thread_memory(thread_id: str) -> dict[str, str]:
    """Drop the thread's provider session so its next run starts fresh.

    This deletes nothing. Retained events stay readable in the thread and in
    conversation history; they simply stop being replayed into the provider.
    The visible marker is committed with the state change, so a thread can
    never show a clear that did not take effect.
    """
    # Take the same lock a send does: the clear must land either wholly before
    # or wholly after a send, never between that send's session snapshot and
    # its launch, which would strip context the run was admitted with. Holding
    # it also means no new turn can be admitted between the live check below
    # and the write.
    with _thread_send_lock(thread_id):
        session_config = state.thread_session_config(thread_id)
        if session_config is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "thread not found")
        if session_config["status"] != "idle":
            raise ApiError(
                HTTPStatus.CONFLICT,
                "working memory can be cleared only while the thread is idle",
            )
        # A stopped turn returns the thread to durable idle while its process
        # is still closing, and that finishing worker may still report a
        # provider session for its run number. Clearing on the persisted status
        # alone would let that late write restore the session just cleared, so
        # the fence is the live set: once a thread leaves it, no worker can
        # still write for it.
        if thread_id in orchestrator.live_thread_ids():
            raise ApiError(
                HTTPStatus.CONFLICT,
                "the thread is still finishing; retry shortly",
            )
        with state.mutation() as cur:
            session_config = state.thread_session_config(thread_id, cur)
            if session_config is None:
                raise ApiError(HTTPStatus.NOT_FOUND, "thread not found")
            if session_config["status"] != "idle":
                raise ApiError(
                    HTTPStatus.CONFLICT,
                    "working memory can be cleared only while the thread is idle",
                )
            cleared_seq = state.append_agent_event(
                cur,
                # Its own display type, not thread.activity: the Chat UI can
                # hide activity, and the boundary must stay visible when it is.
                "thread.memory_cleared",
                thread_id,
                {"message": WORKING_MEMORY_CLEARED_NOTICE},
                run_number=session_config["run_number"],
            )
            try:
                state.clear_thread_context(cur, thread_id, cleared_seq, utc_now())
            except ValueError as exc:
                raise ApiError(
                    HTTPStatus.CONFLICT,
                    "working memory can be cleared only while the thread is idle",
                ) from exc
    return {"status": "cleared"}


def _conversation_utf8_bytes(value: str, field: str) -> bytes:
    try:
        return value.encode()
    except UnicodeEncodeError as exc:
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            f"{field} must be valid UTF-8",
        ) from exc


def _optional_conversation_text(value: Any, field: str, maximum: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ApiError(HTTPStatus.BAD_REQUEST, f"{field} must be a non-empty string")
    if "\x00" in value:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"{field} must not contain NUL")
    normalized = value.strip()
    if len(_conversation_utf8_bytes(normalized, field)) > maximum:
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            f"{field} must be at most {maximum} UTF-8 bytes",
        )
    return normalized


def _optional_conversation_timestamp(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ApiError(HTTPStatus.BAD_REQUEST, f"{field} must be an RFC 3339 timestamp")
    if len(_conversation_utf8_bytes(value, field)) > 64:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"{field} must be an RFC 3339 timestamp")
    timestamp_match = RFC3339_TIMESTAMP_RE.fullmatch(value)
    if timestamp_match is None:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"{field} must be an RFC 3339 timestamp")
    try:
        fraction = timestamp_match.group("fraction")
        parse_value = value
        if fraction is not None and len(fraction) > 6:
            parse_value = (
                value[: timestamp_match.start("fraction") + 6]
                + value[timestamp_match.end("fraction") :]
            )
        parsed = datetime.fromisoformat(
            parse_value[:-1] + "+00:00"
            if parse_value.endswith(("Z", "z"))
            else parse_value
        )
        parsed = parsed.astimezone(timezone.utc)
        if fraction is not None and any(digit != "0" for digit in fraction):
            parsed = parsed.replace(microsecond=0) + timedelta(seconds=1)
    except (ValueError, OverflowError) as exc:
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            f"{field} must be an RFC 3339 timestamp",
        ) from exc
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")


def _conversation_limit(value: Any, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= maximum:
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            f"limit must be between 1 and {maximum}",
        )
    return value


def _conversation_event_seq(value: Any, field: str) -> int:
    if not isinstance(value, str) or (match := EVENT_ID_RE.fullmatch(value)) is None:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"{field} must be an event id")
    seq = int(match.group(1))
    if seq > POSTGRES_BIGINT_MAX:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"{field} must be an event id")
    return seq


def _conversation_search_fingerprint(
    queries: list[str],
    from_timestamp: str | None,
    to_timestamp: str | None,
    thread_id: str | None,
    roles: list[str],
) -> str:
    encoded = json.dumps(
        [queries, from_timestamp, to_timestamp, thread_id, roles],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()[:24]


def _encode_conversation_search_cursor(
    fingerprint: str,
    relevance: bool,
    value: dict[str, Any],
) -> str:
    fields = (
        [fingerprint, "rank", value.get("rank"), value.get("seq")]
        if relevance
        else [fingerprint, "time", value.get("timestamp"), value.get("seq")]
    )
    raw = json.dumps(fields, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_conversation_search_cursor(
    value: Any,
    fingerprint: str,
    relevance: bool,
) -> tuple[float, int] | tuple[str, int] | None:
    if value is None:
        return None
    try:
        if not isinstance(value, str) or not value:
            raise ValueError
        encoded = _conversation_utf8_bytes(value, "cursor")
        if len(encoded) > CONVERSATION_CURSOR_BYTES:
            raise ValueError
        padded = encoded + b"=" * (-len(encoded) % 4)
        decoded = json.loads(
            base64.b64decode(padded, altchars=b"-_", validate=True)
        )
        if not isinstance(decoded, list) or len(decoded) != 4:
            raise ValueError
        cursor_fingerprint, mode, position, seq = decoded
        if (
            cursor_fingerprint != fingerprint
            or mode != ("rank" if relevance else "time")
            or not isinstance(seq, int)
            or isinstance(seq, bool)
            or seq < 1
            or seq > POSTGRES_BIGINT_MAX
        ):
            raise ValueError
        if relevance:
            if (
                not isinstance(position, (int, float))
                or isinstance(position, bool)
            ):
                raise ValueError
            try:
                rank = float(position)
            except OverflowError as exc:
                raise ValueError from exc
            if not math.isfinite(rank) or rank < 0:
                raise ValueError
            return rank, seq
        if not isinstance(position, str) or UTC_TIMESTAMP_RE.fullmatch(position) is None:
            raise ValueError
        try:
            datetime.strptime(position, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError as exc:
            raise ValueError from exc
        return position, seq
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            "cursor is invalid or belongs to different search filters",
        ) from exc


def search_conversation_history(body: Any) -> dict[str, Any]:
    """Validate and execute one public, bounded history-search request."""
    if not isinstance(body, dict):
        raise ApiError(HTTPStatus.BAD_REQUEST, "conversation search must be an object")
    allowed = {
        "query",
        "query_variants",
        "from",
        "to",
        "thread_id",
        "roles",
        "limit",
        "cursor",
    }
    unexpected = sorted(set(body) - allowed)
    if unexpected:
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            f"unsupported conversation search field: {unexpected[0]}",
        )
    query = _optional_conversation_text(body.get("query"), "query", CONVERSATION_QUERY_BYTES)
    variants = body.get("query_variants", [])
    if not isinstance(variants, list) or len(variants) > CONVERSATION_VARIANT_LIMIT:
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            f"query_variants must contain at most {CONVERSATION_VARIANT_LIMIT} strings",
        )
    normalized_variants: list[str] = []
    for value in variants:
        variant = _optional_conversation_text(
            value, "query variant", CONVERSATION_VARIANT_BYTES
        )
        if variant is None:
            raise ApiError(HTTPStatus.BAD_REQUEST, "query variants must be non-empty")
        if variant not in normalized_variants:
            normalized_variants.append(variant)
    if normalized_variants and query is None:
        raise ApiError(HTTPStatus.BAD_REQUEST, "query_variants require query")
    queries = ([] if query is None else [query]) + [
        variant for variant in normalized_variants if variant != query
    ]
    from_timestamp = _optional_conversation_timestamp(body.get("from"), "from")
    to_timestamp = _optional_conversation_timestamp(body.get("to"), "to")
    if (
        from_timestamp is not None
        and to_timestamp is not None
        and from_timestamp >= to_timestamp
    ):
        raise ApiError(HTTPStatus.BAD_REQUEST, "from must be earlier than to")
    thread_id = body.get("thread_id")
    if thread_id is not None and (
        not isinstance(thread_id, str)
        or PRODUCT_THREAD_ID_RE.fullmatch(thread_id) is None
    ):
        raise ApiError(HTTPStatus.BAD_REQUEST, "thread_id is invalid")
    if query is None and from_timestamp is None and to_timestamp is None and thread_id is None:
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            "provide query, from, to, or thread_id",
        )
    roles = body.get("roles", ["user", "assistant"])
    if (
        not isinstance(roles, list)
        or not roles
        or not all(isinstance(role, str) for role in roles)
        or set(roles) - {"user", "assistant"}
    ):
        raise ApiError(HTTPStatus.BAD_REQUEST, "roles must contain user and/or assistant")
    roles = list(dict.fromkeys(roles))
    limit = _conversation_limit(body.get("limit", 10), CONVERSATION_SEARCH_LIMIT)
    fingerprint = _conversation_search_fingerprint(
        queries, from_timestamp, to_timestamp, thread_id, roles
    )
    before = _decode_conversation_search_cursor(
        body.get("cursor"), fingerprint, bool(queries)
    )

    try:
        rows = state.search_thread_messages(
            tuple(queries),
            from_timestamp=from_timestamp,
            to_timestamp=to_timestamp,
            thread_id=thread_id,
            sources=tuple("user" if role == "user" else "agent" for role in roles),
            limit=limit + 1,
            before=before,
        )
    except pgclient.Error as exc:
        if exc.sqlstate == "57014":
            raise ApiError(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "conversation search exceeded its work limit; narrow the query or time range",
            ) from exc
        raise
    page = rows[:limit]
    matches = []
    for row in page:
        excerpt = _clip_json_encoded_text(
            row["excerpt"], CONVERSATION_SEARCH_EXCERPT_BYTES
        )
        matches.append(
            {
                "thread_id": row["thread_id"],
                "event_id": row["event_id"],
                "timestamp": row["timestamp"],
                "role": "user" if row["source"] == "user" else "assistant",
                "excerpt": excerpt,
                "excerpt_truncated": row["excerpt_truncated"] or excerpt != row["excerpt"],
            }
        )
    response: dict[str, Any] = {
        "provenance": HISTORY_PROVENANCE,
        "trust": HISTORY_TRUST,
        "instruction_authority": HISTORY_INSTRUCTION_AUTHORITY,
        "matches": matches,
        "next_cursor": None,
    }
    if len(rows) > limit and page:
        last = page[-1]
        next_value = (
            {"rank": last["search_rank"], "seq": last["seq"]}
            if queries
            else {"timestamp": last["timestamp"], "seq": last["seq"]}
        )
        response["next_cursor"] = _encode_conversation_search_cursor(
            fingerprint, bool(queries), next_value
        )
    return response


def read_conversation_history(body: Any) -> dict[str, Any]:
    """Validate and execute one public, bounded thread-history request."""
    if not isinstance(body, dict):
        raise ApiError(HTTPStatus.BAD_REQUEST, "conversation read must be an object")
    allowed = {
        "thread_id",
        "before",
        "after",
        "around_event_id",
        "include_activity",
        "limit",
    }
    unexpected = sorted(set(body) - allowed)
    if unexpected:
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            f"unsupported conversation read field: {unexpected[0]}",
        )
    thread_id = body.get("thread_id")
    if (
        not isinstance(thread_id, str)
        or PRODUCT_THREAD_ID_RE.fullmatch(thread_id) is None
    ):
        raise ApiError(HTTPStatus.BAD_REQUEST, "thread_id is invalid")
    cursors: dict[str, int] = {}
    for public_name, internal_name in (
        ("before", "before"),
        ("after", "after"),
        ("around_event_id", "around"),
    ):
        value = body.get(public_name)
        if value is None:
            continue
        cursors[internal_name] = _conversation_event_seq(value, public_name)
    if len(cursors) > 1:
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            "before, after, and around_event_id cannot be combined",
        )
    include_activity = body.get("include_activity", False)
    if not isinstance(include_activity, bool):
        raise ApiError(HTTPStatus.BAD_REQUEST, "include_activity must be a boolean")
    limit = _conversation_limit(body.get("limit", 20), CONVERSATION_READ_LIMIT)
    event_types = CONVERSATION_EVENT_TYPES if include_activity else ("thread.message",)
    if "around" in cursors:
        raw_events = state.page_thread_events_around(
            thread_id,
            cursors["around"],
            limit,
            event_types=event_types,
        )
        if raw_events is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "anchor event not found in thread")
        mode = "around"
    elif "after" in cursors:
        raw_events = state.page_thread_events(
            thread_id,
            cursors["after"],
            limit,
            event_types=event_types,
        )
        mode = "after"
    else:
        raw_events = state.page_thread_events(
            thread_id,
            None,
            limit,
            before=cursors.get("before"),
            event_types=event_types,
        )
        mode = "before"

    projected = [_conversation_event(event) for event in raw_events]
    events = _bounded_conversation_events(projected, mode, cursors.get("around"))
    response: dict[str, Any] = {
        "provenance": HISTORY_PROVENANCE,
        "trust": HISTORY_TRUST,
        "instruction_authority": HISTORY_INSTRUCTION_AUTHORITY,
        "thread": {"thread_id": thread_id},
        "events": events,
        "older_cursor": None,
        "newer_cursor": None,
    }
    if events:
        oldest = _event_seq(events[0]["event_id"])
        newest = _event_seq(events[-1]["event_id"])
        has_older, has_newer = state.thread_event_page_bounds(
            thread_id,
            oldest,
            newest,
            event_types=event_types,
        )
        if has_older:
            response["older_cursor"] = f"event_{oldest}"
        if has_newer:
            response["newer_cursor"] = f"event_{newest}"
    return response


def _conversation_event(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    if event.get("event_type") == "thread.message":
        content = payload.get("message")
        content = content if isinstance(content, str) else ""
        clipped = _clip_json_encoded_text(content, CONVERSATION_MESSAGE_BYTES)
        return {
            "event_id": event["event_id"],
            "timestamp": event["timestamp"],
            "type": "message",
            "role": "user" if payload.get("source") == "user" else "assistant",
            "content": clipped,
            "truncated": clipped != content,
        }
    activity = payload.get("activity")
    activity = activity if isinstance(activity, dict) else {}
    summary: dict[str, Any] = {}
    truncated = False
    for field, budget in CONVERSATION_ACTIVITY_FIELDS:
        value = activity.get(field)
        if not isinstance(value, str) or not value:
            continue
        clipped = _clip_json_encoded_text(value, budget)
        summary[field] = clipped
        truncated = truncated or clipped != value
    return {
        "event_id": event["event_id"],
        "timestamp": event["timestamp"],
        "type": "activity",
        "activity": summary,
        "truncated": truncated or bool(set(activity) - set(summary)),
    }


def _bounded_conversation_events(
    events: list[dict[str, Any]], mode: str, anchor: int | None
) -> list[dict[str, Any]]:
    """Keep a contiguous page within the exact encoded response budget."""
    if not events:
        return []
    if mode == "after":
        order = list(range(len(events)))
    elif mode == "around" and anchor is not None:
        anchor_index = next(
            (index for index, event in enumerate(events) if _event_seq(event["event_id"]) == anchor),
            len(events) - 1,
        )
        order = [anchor_index]
        distance = 1
        while len(order) < len(events):
            if anchor_index - distance >= 0:
                order.append(anchor_index - distance)
            if anchor_index + distance < len(events):
                order.append(anchor_index + distance)
            distance += 1
    else:
        order = list(reversed(range(len(events))))
    selected: set[int] = set()
    for index in order:
        candidate = [events[item] for item in sorted((*selected, index))]
        if len(json.dumps({"events": candidate}).encode()) > CONVERSATION_RESPONSE_BYTES - 4096:
            break
        selected.add(index)
    return [events[index] for index in sorted(selected)]


def _event_seq(event_id: str) -> int:
    return int(event_id.removeprefix("event_"))


def list_threads(
    query: dict[str, list[str]],
) -> dict[str, Any]:
    limit = _event_page_limit(query)
    before = _thread_list_cursor(query)
    prefix = _thread_list_prefix(query)
    summaries = state.page_thread_summaries(
        before,
        limit + 1,
        thread_prefix=prefix,
    )
    page = summaries[:limit]
    live = orchestrator.live_thread_ids()
    for thread in page:
        if thread["thread_id"] in live:
            thread["status"] = "running"
    response: dict[str, Any] = {"threads": page}
    if len(summaries) > limit and page:
        response["next_before"] = _encode_thread_list_cursor(page[-1])
    return response


def _public_thread(
    thread_id: str,
    agent_runtime: str,
    model: str,
    effort: str,
    *,
    last_used_at: str | None = None,
) -> dict[str, Any]:
    config = state.thread_session_config(thread_id)
    if last_used_at is None:
        last_used_at = config.get("last_used_at") if config else None
    status = str(config.get("status") if config else "idle")
    if thread_id in orchestrator.live_thread_ids():
        status = "running"
    return {
        "thread_id": thread_id,
        "agent_runtime": agent_runtime,
        "model": model,
        "effort": effort,
        "last_used_at": str(last_used_at or ""),
        "status": status or "idle",
    }


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


def host_metrics() -> dict[str, Any]:
    return {
        "cpu": {"usage_percent": cpu_usage_percent()},
        "memory": memory_metrics(),
        "filesystem": filesystem_metrics(),
        "swap": swap_metrics(),
    }


def cpu_usage_percent() -> float:
    # Deliberately samples /proc/stat 50ms apart on the calling thread: health
    # requests each run on their own handler thread, so the brief block delays
    # only that response, and it keeps the metric stateless.
    first = _cpu_times()
    time.sleep(0.05)
    second = _cpu_times()
    idle_delta = second["idle"] - first["idle"]
    total_delta = second["total"] - first["total"]
    if total_delta <= 0:
        return 0.0
    return round(100.0 * (1.0 - idle_delta / total_delta), 1)


def _cpu_times() -> dict[str, int]:
    values = [int(part) for part in Path("/proc/stat").read_text().splitlines()[0].split()[1:]]
    idle = values[3] + values[4]
    return {"idle": idle, "total": sum(values)}


def memory_metrics() -> dict[str, int]:
    mem = _proc_meminfo()
    total = mem["MemTotal"] * 1024
    available = mem.get("MemAvailable", 0) * 1024
    return {"used_bytes": total - available, "total_bytes": total}


def _filesystem_usage(path: str) -> dict[str, int] | None:
    try:
        usage = shutil.disk_usage(path)
    except FileNotFoundError:
        return None
    return {"used_bytes": usage.used, "total_bytes": usage.total}


def filesystem_metrics() -> dict[str, Any]:
    root = _filesystem_usage("/") or {"used_bytes": 0, "total_bytes": 0}
    mounts = {"root": root}
    for name, path in (
        ("admin", "/mnt/kern-admin"),
        ("agent", "/mnt/kern-agent"),
    ):
        usage = _filesystem_usage(path)
        if usage is not None:
            mounts[name] = usage
    return {"mounts": mounts}


def swap_metrics() -> dict[str, int]:
    mem = _proc_meminfo()
    total = mem.get("SwapTotal", 0) * 1024
    free = mem.get("SwapFree", 0) * 1024
    return {"allocated_bytes": total, "used_bytes": total - free}


def _proc_meminfo() -> dict[str, int]:
    values: dict[str, int] = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, value = line.split(":", 1)
        values[key] = int(value.strip().split()[0])
    return values


def _message(body: Any) -> str:
    """The one request-body validation for a thread message send; the session
    configuration readers below trust the dict this establishes."""
    if not isinstance(body, dict):
        raise ApiError(HTTPStatus.BAD_REQUEST, "request body must be a JSON object")
    value = body.get("message")
    if not isinstance(value, str) or not value:
        raise ApiError(HTTPStatus.BAD_REQUEST, "message must be a non-empty string")
    if len(value) > MESSAGE_LIMIT:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"message must be at most {MESSAGE_LIMIT} characters")
    return value


def _agent_runtime(body: dict[str, Any]) -> str:
    value = body.get("agent_runtime")
    if not isinstance(value, str) or value not in AGENT_RUNTIMES:
        raise ApiError(HTTPStatus.BAD_REQUEST, "agent_runtime must be one of " + ", ".join(sorted(AGENT_RUNTIMES)))
    return value


def _session_config(body: dict[str, Any], runtime: str) -> tuple[str, str]:
    model = body.get("model")
    effort = body.get("effort")
    error = session_config_error(runtime, model, effort)
    if error is not None:
        raise ApiError(HTTPStatus.BAD_REQUEST, error)
    assert isinstance(model, str) and isinstance(effort, str)
    return model, effort


def _resolve_session_config(
    body: dict[str, Any],
    session_config: dict[str, Any] | None,
) -> tuple[str, str, str]:
    stored = None
    if session_config is not None:
        stored = (
            session_config["agent_runtime"],
            session_config["model"],
            session_config["effort"],
        )

    fields = ("agent_runtime", "model", "effort")
    supplied = [field for field in fields if field in body]
    if stored is not None:
        # A superseded configuration stays readable and can be replaced, but
        # cannot start another provider session as-is.
        if session_config_error(*stored) is not None and (
            not supplied or tuple(body.get(field) for field in fields) == stored
        ):
            raise ApiError(
                HTTPStatus.CONFLICT,
                "this thread runs a session configuration that is no longer offered;"
                " select a currently offered model to continue",
            )
        if not supplied:
            return stored
        if len(supplied) != len(fields):
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                "agent_runtime, model, and effort must be provided together",
            )
        requested_runtime = _agent_runtime(body)
        requested_model, requested_effort = _session_config(body, requested_runtime)
        return requested_runtime, requested_model, requested_effort
    if not supplied:
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            "agent_runtime, model, and effort are required when starting a new thread",
        )
    if len(supplied) != len(fields):
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            "agent_runtime, model, and effort must be provided together",
        )

    agent_runtime = _agent_runtime(body)
    model, effort = _session_config(body, agent_runtime)
    return agent_runtime, model, effort


def _session_configuration_changed(
    session_config: dict[str, Any] | None,
    runtime: str,
    model: str,
    effort: str,
) -> bool:
    if session_config is None:
        return False
    return (
        session_config["agent_runtime"],
        session_config["model"],
        session_config["effort"],
    ) != (runtime, model, effort)


def _session_change_activity(
    previous: dict[str, Any],
    runtime: str,
    model: str,
    effort: str,
) -> dict[str, Any]:
    previous_runtime = str(previous["agent_runtime"])
    title = (
        "Agent provider changed"
        if previous_runtime != runtime
        else "Agent session changed"
    )

    def label(runtime_type: str, model_name: str, effort_name: str) -> str:
        runtime_name = orchestrator.RUNTIME_LABELS.get(runtime_type, runtime_type)
        return f"{runtime_name} · {model_name} · {effort_name}"

    detail = (
        f"{label(previous_runtime, str(previous['model']), str(previous['effort']))}"
        f" → {label(runtime, model, effort)}"
    )
    return agent_activity.activity(
        "kern",
        "session-change",
        "status",
        "completed",
        title,
        detail=detail,
        status="completed",
    )


def _handoff_event_block(event: dict[str, Any]) -> str:
    payload = event.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    event_type = event.get("event_type")
    if event_type == "thread.message":
        label = "User" if payload.get("source") == "user" else "Agent"
        return f"{label}:\n{payload.get('message', '')}"
    if event_type == "thread.activity":
        activity = payload.get("activity")
        activity = activity if isinstance(activity, dict) else {}
        summary = {
            key: activity[key]
            for key in ("provider", "kind", "phase", "title", "status")
            if key in activity
        }
        for key, limit in (
            ("detail", THREAD_HANDOFF_ACTIVITY_DETAIL_LIMIT),
            ("output", THREAD_HANDOFF_ACTIVITY_OUTPUT_LIMIT),
            ("error", THREAD_HANDOFF_ACTIVITY_OUTPUT_LIMIT),
        ):
            value = activity.get(key)
            if isinstance(value, str) and value:
                summary[key] = agent_activity.clip_text(value, limit)
        block = "Agent activity (summary):\n" + json.dumps(
            summary, ensure_ascii=False, indent=2, default=str
        )
        return agent_activity.clip_text(
            block, THREAD_HANDOFF_ACTIVITY_EVENT_CHARACTER_LIMIT
        )
    return ""


def _bounded_handoff_section(
    history: list[dict[str, Any]], character_limit: int
) -> str:
    """Newest event blocks within one exact model-facing character budget."""
    if character_limit <= 0:
        return ""
    blocks_reversed: list[str] = []
    remaining = character_limit
    omitted = False
    for event in reversed(history):
        separator_size = 2 if blocks_reversed else 0
        block = _handoff_event_block(event)
        if not block:
            continue
        available = remaining - separator_size
        if available <= 0:
            omitted = True
            break
        if len(block) <= available:
            blocks_reversed.append(block)
            remaining -= separator_size + len(block)
            continue
        marker = "\n[Earlier event content truncated]\n"
        content_space = available - len(marker)
        if content_space > 1:
            prefix_size = content_space // 2
            suffix_size = content_space - prefix_size
            blocks_reversed.append(
                block[:prefix_size] + marker + block[-suffix_size:]
            )
        omitted = True
        break
    if len(blocks_reversed) < len(history):
        omitted = True
    transcript = "\n\n".join(reversed(blocks_reversed))
    if omitted:
        marker = "[Older retained thread events were omitted.]"
        if len(marker) >= character_limit:
            return marker[:character_limit]
        content_limit = character_limit - len(marker) - 2
        if len(transcript) > content_limit:
            transcript = transcript[-content_limit:]
        transcript = marker + ("\n\n" + transcript if transcript else "")
    return transcript


def _session_handoff_message(history: list[dict[str, Any]], message: str) -> str:
    """Build independently bounded conversation and activity handoff sections."""
    conversation = _bounded_handoff_section(
        [event for event in history if event.get("event_type") == "thread.message"],
        THREAD_HANDOFF_MESSAGE_CHARACTER_LIMIT,
    )
    activity = _bounded_handoff_section(
        [event for event in history if event.get("event_type") == "thread.activity"],
        THREAD_HANDOFF_ACTIVITY_CHARACTER_LIMIT,
    )
    return (
        "You are a new agent session continuing a thread previously handled by another "
        "agent session. Your provider-side context and cache are not available. Use the "
        "retained conversation and activity below, then respond to the current "
        "user message. Do not mention this handoff unless it is relevant.\n\n"
        "--- RETAINED CONVERSATION ---\n"
        f"{conversation or '[No retained messages.]'}\n"
        "--- END RETAINED CONVERSATION ---\n\n"
        "--- RECENT AGENT ACTIVITY ---\n"
        f"{activity or '[No retained activity.]'}\n"
        "--- END RECENT AGENT ACTIVITY ---\n\n"
        "--- CURRENT USER MESSAGE ---\n"
        f"{message}\n"
        "--- END CURRENT USER MESSAGE ---"
    )


def _one(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    if not values:
        return None
    if len(values) != 1:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"{key} must appear once")
    return values[0]


def _agent_file_path(query: dict[str, list[str]]) -> str:
    value = _one(query, "path")
    if value is None or value == "":
        return "/"
    if "\0" in value:
        raise ApiError(HTTPStatus.BAD_REQUEST, "path contains a NUL byte")
    if len(value) > 4096:
        raise ApiError(HTTPStatus.BAD_REQUEST, "path is too long")
    return value


def _agent_file_upload_filename(query: dict[str, list[str]]) -> str:
    _reject_query_keys(query, {"filename"}, "agent file upload")
    value = _one(query, "filename")
    if value is None or value in {"", ".", ".."}:
        raise ApiError(HTTPStatus.BAD_REQUEST, "filename must be non-empty")
    if any(character in value for character in ("/", "\\", "\0")):
        raise ApiError(HTTPStatus.BAD_REQUEST, "filename must not contain path separators or a NUL byte")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ApiError(HTTPStatus.BAD_REQUEST, "filename must not contain control characters")
    if len(value.encode("utf-8")) > AGENT_FILE_UPLOAD_FILENAME_MAX_BYTES:
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            f"filename must be at most {AGENT_FILE_UPLOAD_FILENAME_MAX_BYTES} UTF-8 bytes",
        )
    return value


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


def _clip_json_encoded_text(value: str, maximum: int) -> str:
    """Bound the encoded JSON string, including escaping of control bytes.

    Event pages cross a hard 1 MiB bridge. A UTF-8-only bound is insufficient:
    one control byte or non-ASCII code point can expand under the default JSON
    serializer used by both HTTP hops.
    """
    def encoded_size(text: str) -> int:
        return len(json.dumps(text).encode())

    if encoded_size(value) <= maximum:
        return value
    suffix = "\n… (truncated)"
    if encoded_size(suffix) > maximum:
        return agent_activity.clip_text(value, maximum)
    low = 0
    high = len(value)
    while low < high:
        middle = (low + high + 1) // 2
        if encoded_size(value[:middle] + suffix) <= maximum:
            low = middle
        else:
            high = middle - 1
    return value[:low] + suffix


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


def _thread_list_prefix(query: dict[str, list[str]]) -> str | None:
    prefix = _one(query, "prefix")
    if prefix is None:
        return None
    if PRODUCT_THREAD_PREFIX_RE.fullmatch(prefix) is None:
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            "prefix must start with app-, thread-, or schedule-",
        )
    return prefix


def _encode_thread_list_cursor(thread: dict[str, Any]) -> str:
    raw = json.dumps(
        [
            str(thread.get("last_used_at") or ""),
            str(thread["thread_id"]),
        ],
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _thread_list_cursor(
    query: dict[str, list[str]],
) -> tuple[str, str] | None:
    value = _one(query, "before")
    if value is None:
        return None
    try:
        if not value or len(value) > 512:
            raise ValueError
        padded = value + "=" * (-len(value) % 4)
        decoded = base64.b64decode(
            padded.encode(),
            altchars=b"-_",
            validate=True,
        )
        fields = json.loads(decoded)
        if (
            not isinstance(fields, list)
            or len(fields) != 2
            or not all(isinstance(field, str) for field in fields)
            or PRODUCT_THREAD_ID_RE.fullmatch(fields[1]) is None
        ):
            raise ValueError
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            "before must be a valid thread list cursor",
        ) from exc
    return fields[0], fields[1]


def _reject_query_keys(query: dict[str, list[str]], allowed: set[str], label: str) -> None:
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
