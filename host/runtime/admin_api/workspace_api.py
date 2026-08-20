"""Peer-authenticated admin API boundary for the Workspace service.

The Workspace service reaches host thread routes through a Unix-domain socket instead
of the operator-facing TCP admin API. This module rejects every peer except the
fixed Workspace-service uid before it occupies a handler slot and exposes only the
small route allowlist below. Thread ids are passed through unchanged; any
product-level naming and filtering belongs to the Workspace service.
"""

from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
import grp
import json
import os
from pathlib import Path
import pwd
import socket
import socketserver
import stat
import struct
import threading
from typing import Any
from urllib.parse import parse_qs, urlparse

from host.constants import (
    MAX_REQUEST_BODY_BYTES,
    WORKSPACE_ADMIN_GROUP,
    WORKSPACE_ADMIN_SOCKET_PATH,
)
from host.runtime.admin_api import service as admin_api
from host.runtime.core import host_errors


WORKSPACE_ADMIN_SOCKET = Path(
    os.environ.get("KERN_WORKSPACE_ADMIN_SOCKET", WORKSPACE_ADMIN_SOCKET_PATH)
)
WORKSPACE_ALLOWED_ADMIN_ROUTES = (
    # Read-only runtime activation, so a workspace can offer only the providers
    # the operator has actually turned on.
    ("GET", "/v1/agent-runtime/status"),
    ("GET", "/v1/threads"),
    ("GET", "/v1/threads/:thread_id"),
    ("POST", "/v1/threads/:thread_id/messages"),
    ("POST", "/v1/threads/:thread_id/stop"),
    ("POST", "/v1/threads/:thread_id/clear-memory"),
    ("GET", "/v1/threads/:thread_id/events"),
    ("POST", "/v1/conversation-history/search"),
    ("POST", "/v1/conversation-history/read"),
)
# Filesystem permissions admit only the fixed Workspace service account; peer
# credentials then bind an admitted connection to that service. The socket is served from
# a daemon thread of the admin API process, whose fd table also holds the
# operator-facing TCP listener. Cap concurrent connections at the same figure
# as that listener's worker cap so neither transport may starve the other of file
# descriptors. Read at
# construction rather than import: service.py imports this module while its own
# constants are still being defined, so a module-level read of one would make
# importing service.py fail.
REQUEST_READ_TIMEOUT_SECONDS = 30


class ThreadingUnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    """Bound concurrent connections, rejecting rather than queueing at capacity.

    A non-Workspace peer is rejected from SO_PEERCRED before it takes a slot.
    For the admitted Workspace peer, dropping connections at capacity rather than queueing
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
        # uid that is not the fixed Workspace service before it acquires a slot or
        # starts a handler: BaseHTTPRequestHandler cannot authenticate the
        # request until it has read the headers, and an idle socket timeout alone
        # can be kept alive forever by trickling one byte per window.
        try:
            peer_is_workspace = _peer_uid(request) == _workspace_uid()
        except OSError:
            peer_is_workspace = False
        if not peer_is_workspace:
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
            # would permanently refuse every Workspace service.
            self._connection_slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._connection_slots.release()


class Handler(BaseHTTPRequestHandler):
    server_version = "KernWorkspace/0.1"
    # Only the fixed Workspace uid reaches a handler. Bound an admitted service that
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
            self._authenticate_workspace()
            path = urlparse(self.path)
            body = self._read_body()
            response = route_workspace_request(
                method,
                path.path,
                parse_qs(path.query),
                body,
            )
            self._send_json(HTTPStatus.OK, response)
        except admin_api.ApiError as exc:
            self._send_json(exc.status, {"error": {"message": exc.message}})
        except Exception as exc:
            host_errors.report_unexpected(
                "admin_api.workspace_request",
                exc,
                context={"method": method, "route": urlparse(self.path).path},
            )
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": {"message": "internal server error"}})

    def _authenticate_workspace(self) -> None:
        if _peer_uid(self.request) != _workspace_uid():
            raise admin_api.ApiError(HTTPStatus.UNAUTHORIZED, "missing or invalid Workspace service identity")

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
        except (UnicodeDecodeError, ValueError, RecursionError) as exc:
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


def create_workspace_admin_server() -> ThreadingUnixHTTPServer:
    WORKSPACE_ADMIN_SOCKET.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = WORKSPACE_ADMIN_SOCKET.lstat().st_mode
    except FileNotFoundError:
        pass
    else:
        if stat.S_ISSOCK(mode):
            WORKSPACE_ADMIN_SOCKET.unlink()
        else:
            raise OSError(f"refusing to replace non-socket Workspace service admin path: {WORKSPACE_ADMIN_SOCKET}")
    server = ThreadingUnixHTTPServer(str(WORKSPACE_ADMIN_SOCKET), Handler)
    os.chown(
        WORKSPACE_ADMIN_SOCKET,
        -1,
        grp.getgrnam(WORKSPACE_ADMIN_GROUP).gr_gid,
    )
    WORKSPACE_ADMIN_SOCKET.chmod(0o660)
    return server


def unlink_workspace_admin_socket() -> None:
    try:
        WORKSPACE_ADMIN_SOCKET.unlink()
    except FileNotFoundError:
        pass


def _workspace_uid() -> int:
    try:
        return pwd.getpwnam("kern-workspace").pw_uid
    except KeyError:
        return os.getuid()


def route_workspace_request(
    method: str,
    path: str,
    query: dict[str, list[str]],
    body: Any,
) -> Any:
    _require_workspace_route(method, path)
    return admin_api.route(
        method,
        path,
        query,
        body,
        principal=admin_api.WorkspacePrincipal(),
    )


def _peer_uid(conn: socket.socket) -> int:
    raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    _pid, uid, _gid = struct.unpack("3i", raw)
    return uid


def _require_workspace_route(method: str, path: str) -> None:
    if not is_allowed_workspace_admin_route(method, path):
        raise admin_api.ApiError(HTTPStatus.FORBIDDEN, "Workspace service route is not allowed")


def is_allowed_workspace_admin_route(method: str, path: str) -> bool:
    return any(
        method == allowed_method and _route_pattern_matches(path, allowed_path)
        for allowed_method, allowed_path in WORKSPACE_ALLOWED_ADMIN_ROUTES
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
