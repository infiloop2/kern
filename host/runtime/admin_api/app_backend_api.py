"""Peer-authenticated admin API surface for app backends.

App backends reach host thread routes through a Unix-domain socket instead of
the operator-facing TCP admin API. This module owns the app-backend-specific
boundary: reject a non-app peer uid before it occupies a handler slot, verify
an admitted peer's claimed app id, narrow the route surface, and translate
app-visible thread ids to host-internal app-prefixed ids. Every route is
thread-scoped, so authorization is the mechanical prefix mapping — no
per-request ownership lookup exists.
"""

from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
import grp
import json
import os
from pathlib import Path
import pwd
import re
import socket
import socketserver
import stat
import struct
import threading
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from host.constants import APP_BACKEND_ADMIN_SOCKET_PATH, APP_BACKEND_GROUP, MAX_REQUEST_BODY_BYTES
from host.runtime.admin_api import service as admin_api
from host.runtime.core import app_platform, host_errors


APP_BACKEND_ADMIN_SOCKET = Path(
    os.environ.get("KERN_APP_BACKEND_ADMIN_SOCKET", APP_BACKEND_ADMIN_SOCKET_PATH)
)
APP_BACKEND_AUTH_HEADER = "X-Kern-App-Backend"
APP_BACKEND_ALLOWED_ADMIN_ROUTES = (
    ("GET", "/v1/tools"),
    ("GET", "/v1/network/policy"),
    ("GET", "/v1/threads"),
    ("GET", "/v1/threads/:thread_id"),
    ("POST", "/v1/threads/:thread_id/messages"),
    ("POST", "/v1/threads/:thread_id/stop"),
    ("GET", "/v1/threads/:thread_id/events"),
)
# Filesystem permissions admit only provisioned app accounts; peer credentials
# then bind an admitted connection to one exact app. The socket is served from
# a daemon thread of the admin API process, whose fd table also holds the
# operator-facing TCP listener. Cap concurrent connections at the same figure
# as that listener's worker cap so neither surface may starve the other of file
# descriptors. Read at
# construction rather than import: service.py imports this module while its own
# constants are still being defined, so a module-level read of one would make
# importing service.py fail.
REQUEST_READ_TIMEOUT_SECONDS = 30


class ThreadingUnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    """Bound concurrent connections, rejecting rather than queueing at capacity.

    A non-app peer is rejected from SO_PEERCRED before it takes a slot. Among
    admitted app peers, dropping connections at capacity rather than queueing
    keeps the handler-thread and fd cost bounded by the semaphore.
    """

    daemon_threads = True

    def __init__(self, *args: Any, max_connections: int | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if max_connections is None:
            max_connections = admin_api.MAX_CONCURRENT_REQUESTS
        self._connection_slots = threading.BoundedSemaphore(max_connections)

    def process_request(self, request: Any, client_address: Any) -> None:
        # SO_PEERCRED is available as soon as accept(2) returns. Refuse every
        # uid that is not an installed app before it acquires a slot or starts
        # a handler: BaseHTTPRequestHandler cannot authenticate the claimed
        # app id until it has read the headers, and an idle socket timeout alone
        # can be kept alive forever by trickling one byte per window.
        try:
            peer_is_app = app_id_for_peer_uid(_peer_uid(request)) is not None
        except OSError:
            peer_is_app = False
        if not peer_is_app:
            self.shutdown_request(request)
            return
        if not self._connection_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            # process_request_thread is the normal release point; if the handler
            # thread could not start at all the slot would leak and the socket
            # would permanently refuse every app backend.
            self._connection_slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._connection_slots.release()


class Handler(BaseHTTPRequestHandler):
    server_version = "KernAppBackend/0.1"
    # Only an installed app uid reaches a handler. Bound an admitted app that
    # stalls while sending its request line, headers, or body as well.
    timeout = REQUEST_READ_TIMEOUT_SECONDS

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def log_message(self, format: str, *args: object) -> None:
        return

    def _handle(self, method: str) -> None:
        try:
            app_id = self._authenticate_app_backend_id()
            path = urlparse(self.path)
            body = self._read_body()
            response = route_app_backend_request(
                app_id,
                method,
                path.path,
                parse_qs(path.query),
                body,
            )
            self._send_json(HTTPStatus.OK, response)
        except admin_api.ApiError as exc:
            self._send_json(exc.status, {"error": {"message": exc.message}})
        except app_platform.AppError as exc:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": {"message": str(exc)}})
        except Exception as exc:
            host_errors.report_unexpected(
                "admin_api.app_backend_request",
                exc,
                context={"method": method, "route": urlparse(self.path).path},
            )
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": {"message": "internal server error"}})

    def _authenticate_app_backend_id(self) -> str:
        claimed_app_id = self.headers.get(APP_BACKEND_AUTH_HEADER, "")
        if not claimed_app_id:
            raise admin_api.ApiError(HTTPStatus.UNAUTHORIZED, "missing or invalid app backend identity")
        peer_app_id = app_id_for_peer_uid(_peer_uid(self.request))
        if peer_app_id is None or peer_app_id != claimed_app_id:
            raise admin_api.ApiError(HTTPStatus.UNAUTHORIZED, "missing or invalid app backend identity")
        return peer_app_id

    def _read_body(self) -> Any:
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError as exc:
            raise admin_api.ApiError(HTTPStatus.BAD_REQUEST, "malformed Content-Length") from exc
        if length < 0:
            raise admin_api.ApiError(HTTPStatus.BAD_REQUEST, "malformed Content-Length")
        if length > MAX_REQUEST_BODY_BYTES:
            raise admin_api.ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request body too large")
        if length == 0:
            return None
        try:
            return json.loads(self.rfile.read(length))
        except json.JSONDecodeError as exc:
            raise admin_api.ApiError(HTTPStatus.BAD_REQUEST, f"invalid JSON: {exc}") from exc

    def _send_json(self, status: HTTPStatus, body: Any) -> None:
        data = json.dumps(body, sort_keys=True).encode()
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for name, value in admin_api.SECURITY_HEADERS.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)


def create_app_backend_admin_server() -> ThreadingUnixHTTPServer:
    APP_BACKEND_ADMIN_SOCKET.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = APP_BACKEND_ADMIN_SOCKET.lstat().st_mode
    except FileNotFoundError:
        pass
    else:
        if stat.S_ISSOCK(mode):
            APP_BACKEND_ADMIN_SOCKET.unlink()
        else:
            raise OSError(f"refusing to replace non-socket app backend admin path: {APP_BACKEND_ADMIN_SOCKET}")
    server = ThreadingUnixHTTPServer(str(APP_BACKEND_ADMIN_SOCKET), Handler)
    os.chown(APP_BACKEND_ADMIN_SOCKET, -1, grp.getgrnam(APP_BACKEND_GROUP).gr_gid)
    APP_BACKEND_ADMIN_SOCKET.chmod(0o660)
    return server


def unlink_app_backend_admin_socket() -> None:
    try:
        APP_BACKEND_ADMIN_SOCKET.unlink()
    except FileNotFoundError:
        pass


def app_id_for_peer_uid(uid: int) -> str | None:
    """Installed app id for the Unix peer uid, derived from app manifests."""
    for app in app_platform.installed_apps():
        try:
            if pwd.getpwnam(app.linux_user).pw_uid == uid:
                return app.id
        except KeyError:
            continue
    return None


def route_app_backend_request(
    app_id: str,
    method: str,
    path: str,
    query: dict[str, list[str]],
    body: Any,
) -> Any:
    _require_app_backend_route(method, path)
    internal_path = _internal_path(app_id, path)
    response = admin_api.route(
        method,
        internal_path,
        query,
        body,
        principal=admin_api.AppBackendPrincipal(app_id),
    )
    if method == "GET" and path == "/v1/threads" and isinstance(response, dict):
        # The bulk thread list is host-global; an app backend sees only the
        # summaries under its own prefix. Filtering happens here, before the
        # generic response mapper, which treats any unprefixed thread_id as a
        # scoping bug.
        prefix = _thread_prefix(app_id)
        threads = response.get("threads")
        if isinstance(threads, list):
            response = {
                **response,
                "threads": [
                    thread
                    for thread in threads
                    if isinstance(thread, dict) and str(thread.get("thread_id", "")).startswith(prefix)
                ],
            }
    return _visible_response(app_id, response)


def _peer_uid(conn: socket.socket) -> int:
    raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    _pid, uid, _gid = struct.unpack("3i", raw)
    return uid


def _require_app_backend_route(method: str, path: str) -> None:
    if not _is_allowed_app_backend_admin_route(method, path):
        raise admin_api.ApiError(HTTPStatus.FORBIDDEN, "app backend route is not allowed")


def _is_allowed_app_backend_admin_route(method: str, path: str) -> bool:
    return any(
        method == allowed_method and _route_pattern_matches(path, allowed_path)
        for allowed_method, allowed_path in APP_BACKEND_ALLOWED_ADMIN_ROUTES
    )


def _route_pattern_matches(path: str, pattern: str) -> bool:
    path_parts = tuple(path.strip("/").split("/"))
    pattern_parts = tuple(pattern.strip("/").split("/"))
    if len(path_parts) != len(pattern_parts):
        return False
    return all(
        pattern_part.startswith(":") or path_part == pattern_part
        for path_part, pattern_part in zip(path_parts, pattern_parts)
    )


def _internal_path(app_id: str, path: str) -> str:
    parts = path.strip("/").split("/")
    if len(parts) >= 3 and parts[:2] == ["v1", "threads"]:
        rest = "/".join(parts[3:])
        internal = f"/v1/threads/{_internal_thread_id(app_id, parts[2])}"
        return f"{internal}/{rest}" if rest else internal
    return path


def _thread_prefix(app_id: str) -> str:
    return f"{app_id}{admin_api.APP_SCOPED_ID_SEPARATOR}"


def _internal_thread_id(app_id: str, thread_id: str) -> str:
    internal = f"{_thread_prefix(app_id)}{thread_id}"
    if not admin_api.THREAD_ID_RE.fullmatch(internal):
        raise admin_api.ApiError(
            HTTPStatus.BAD_REQUEST,
            "thread_id is too long for this app namespace; choose a shorter thread_id",
        )
    return internal


def _visible_thread_id(app_id: str, thread_id: str) -> str:
    prefix = _thread_prefix(app_id)
    if not thread_id.startswith(prefix):
        raise admin_api.ApiError(HTTPStatus.BAD_GATEWAY, "host admin returned unscoped app thread")
    return thread_id.removeprefix(prefix)


def _visible_response(app_id: str, value: Any) -> Any:
    return _map_response(value, lambda thread_id: _visible_thread_id(app_id, thread_id))


def _map_response(value: Any, thread_mapper: Callable[[str], str]) -> Any:
    if isinstance(value, list):
        return [_map_response(item, thread_mapper) for item in value]
    if not isinstance(value, dict):
        return value
    mapped: dict[str, Any] = {}
    for key, item in value.items():
        if key == "thread_id" and isinstance(item, str):
            mapped[key] = thread_mapper(item)
        else:
            mapped[key] = _map_response(item, thread_mapper)
    return mapped
