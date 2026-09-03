"""Peer-authenticated agent API owned by the main Workspace service.

The MCP shim sends one bounded JSON call over this Unix socket. Caller identity
proves only that the request came from ``kern-agent``. The current thread is
derived from its root-created cgroup for ``/agent/identity`` responses and
first-class self-memory selection; it does not authorize or select a Web App.
"""

from __future__ import annotations

from http import HTTPStatus
import json
import re
import socket
import struct
import threading
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from host.constants import MAX_WORKSPACE_RESPONSE_BODY_BYTES
from host.runtime.core import host_errors
from host.runtime.core.unix_socket_service import (
    UnixSocketRequestHandler,
    UnixSocketServer,
    peer_uids,
)
from host.runtime.workspace import conversation_history, memory, schedules
from host.runtime.workspace.host_api import WorkspaceError
from host.runtime.workspace.web_apps import backend as web_apps


AGENT_PEER_USER = "kern-agent"
MAX_REQUEST_BODY_BYTES = 256 * 1024
MAX_RESPONSE_BODY_BYTES = MAX_WORKSPACE_RESPONSE_BODY_BYTES
MAX_CONCURRENT_CALLS = 8
MAX_CONCURRENT_CONNECTIONS = 16
ALLOWED_METHODS = frozenset({"GET", "POST", "PUT", "DELETE"})
AGENT_PATH_RE = re.compile(
    r"^/agent/[A-Za-z0-9._~/-]{0,512}(?:\?[A-Za-z0-9._~=&%+-]{0,512})?$"
)
_CALL_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT_CALLS)
PROC_ROOT = Path("/proc")
THREAD_SCOPE_RE = re.compile(r"(?:^|/)kern-agent-thread-([A-Za-z0-9_-]{1,64})\.scope$")


def agent_peer_uids() -> frozenset[int]:
    return peer_uids(AGENT_PEER_USER)


def dispatch_call(
    method: Any,
    path: Any,
    body: Any,
    *,
    peer_thread_id: str | None = None,
) -> dict[str, Any]:
    if not isinstance(method, str) or method.upper() not in ALLOWED_METHODS:
        raise ValueError(f"method must be one of {', '.join(sorted(ALLOWED_METHODS))}")
    method = method.upper()
    if method in {"GET", "DELETE"} and body is not None:
        raise ValueError(f"{method} requests do not accept a body")
    if (
        not isinstance(path, str)
        or not AGENT_PATH_RE.fullmatch(path)
        or "/../" in path
        or path.endswith("/..")
    ):
        raise ValueError(f"path must match {AGENT_PATH_RE.pattern}")
    encoded = None if body is None else json.dumps(body, sort_keys=True).encode()
    if encoded is not None and len(encoded) > MAX_REQUEST_BODY_BYTES:
        raise ValueError(f"body exceeds {MAX_REQUEST_BODY_BYTES} bytes")

    parsed = urlparse(path)
    query = parse_qs(parsed.query, keep_blank_values=True)
    response: dict[str, Any]
    if parsed.path == "/agent/identity":
        if method != "GET" or body is not None or query:
            raise WorkspaceError(
                HTTPStatus.BAD_REQUEST, "agent identity accepts only GET without query parameters"
            )
        if peer_thread_id is None:
            raise WorkspaceError(HTTPStatus.CONFLICT, "agent thread identity is unavailable")
        response = {"thread_id": peer_thread_id}
    elif parsed.path == "/agent/self/memory":
        if method not in {"GET", "PUT"}:
            raise WorkspaceError(HTTPStatus.NOT_FOUND, "agent self-memory route not found")
        if query:
            raise WorkspaceError(
                HTTPStatus.BAD_REQUEST,
                "agent self-memory does not accept query parameters",
            )
        if peer_thread_id is None:
            raise WorkspaceError(HTTPStatus.CONFLICT, "agent thread identity is unavailable")
        if peer_thread_id.startswith("schedule-") and re.fullmatch(
            r"schedule-[1-9][0-9]*", peer_thread_id
        ) is None:
            raise WorkspaceError(
                HTTPStatus.CONFLICT,
                "self-memory is unavailable for this schedule identity",
            )
        self_page_id = memory.individual_page_id(peer_thread_id)
        if method == "GET":
            response = {"page": memory.load_page(self_page_id)}
        else:
            response = {
                "page": memory.save_page(
                    self_page_id,
                    body,
                    actor="agent",
                )
            }
    elif parsed.path.startswith("/agent/conversation-history/"):
        response = conversation_history.route_agent(method, parsed.path, body, query)
    elif parsed.path == "/agent/memory" or parsed.path.startswith("/agent/memory/"):
        response = memory.route_agent(method, parsed.path, body, query)
    elif parsed.path == "/agent/schedules" or parsed.path.startswith("/agent/schedules/"):
        response = schedules.route_agent(method, parsed.path, body, query)
    else:
        response = web_apps.route_agent(method, parsed.path, body, query)
    result = {"status": HTTPStatus.OK.value, "body": response}
    # Match UnixSocketRequestHandler._send_json's wire encoding exactly; a
    # compact-size estimate could admit a response whose actual serialized
    # envelope exceeds the cap because of separator whitespace.
    if len(json.dumps(result).encode()) > MAX_RESPONSE_BODY_BYTES:
        raise RuntimeError("workspace response too large")
    return result


class AgentWorkspaceRequestHandler(UnixSocketRequestHandler):
    server: "AgentWorkspaceServer"

    def do_GET(self) -> None:
        _pid, uid = self._peer()
        status = (
            HTTPStatus.FORBIDDEN
            if uid not in self.server.agent_uids
            else HTTPStatus.NOT_FOUND
        )
        error = "Peer not allowed." if status == HTTPStatus.FORBIDDEN else "Unknown path."
        self._send_json(status, {"error": error})

    def do_POST(self) -> None:
        pid, uid = self._peer()
        if uid not in self.server.agent_uids:
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "Peer not allowed."})
            return
        if self.path != "/call":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Unknown path."})
            return
        length = self.bounded_content_length(MAX_REQUEST_BODY_BYTES)
        if length is None:
            return
        if not _CALL_SLOTS.acquire(blocking=False):
            self._send_json(
                HTTPStatus.TOO_MANY_REQUESTS,
                {"error": "Too many concurrent Workspace calls."},
            )
            return
        try:
            request = self.read_json_object_body(length)
            if request is None:
                return
            if set(request) - {"method", "path", "body"}:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "Request has unsupported fields."},
                )
                return
            try:
                result = dispatch_call(
                    request.get("method"),
                    request.get("path"),
                    request.get("body"),
                    peer_thread_id=_peer_thread_id(pid),
                )
            except ValueError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            except WorkspaceError as exc:
                result = {
                    "status": exc.status.value,
                    "body": {"error": {"message": exc.message}},
                }
            except Exception as exc:
                host_errors.report_unexpected("workspace.agent_call", exc)
                self._send_json(
                    HTTPStatus.BAD_GATEWAY, {"error": "workspace backend unavailable."}
                )
                return
            self._send_json(HTTPStatus.OK, result)
        finally:
            _CALL_SLOTS.release()


class AgentWorkspaceServer(UnixSocketServer):
    def __init__(self, socket_path: str, agent_uids: frozenset[int]) -> None:
        self.agent_uids = agent_uids
        self._connection_slots = threading.BoundedSemaphore(
            MAX_CONCURRENT_CONNECTIONS
        )
        super().__init__(socket_path, AgentWorkspaceRequestHandler)

    def process_request(self, request: Any, client_address: Any) -> None:
        # Authenticate from SO_PEERCRED before allocating a handler thread.
        # The socket is connectable by local services so kern-agent can use it,
        # but no other uid may consume its bounded connection budget.
        try:
            peer_allowed = _peer_uid(request) in self.agent_uids
        except OSError:
            peer_allowed = False
        if not peer_allowed or not self._connection_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._connection_slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._connection_slots.release()


def _peer_uid(conn: socket.socket) -> int:
    raw = conn.getsockopt(
        socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
    )
    _pid, uid, _gid = struct.unpack("3i", raw)
    return uid


def _peer_thread_id(pid: int) -> str | None:
    """Derive the MCP shim's host thread from its kernel-assigned cgroup.

    This is an informational identity, not app authorization. The peer PID is
    obtained with SO_PEERCRED and the thread id is accepted only from the
    root-created per-turn scope name.
    """
    try:
        lines = (PROC_ROOT / str(pid) / "cgroup").read_text().splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    for line in lines:
        path = line.split(":", 2)[-1]
        # systemd may hex-escape unit-name bytes in a cgroup component.
        try:
            path = re.sub(
                r"\\x([0-9a-fA-F]{2})",
                lambda match: chr(int(match.group(1), 16)),
                path,
            )
        except ValueError:
            continue
        match = THREAD_SCOPE_RE.search(path)
        if match is not None:
            return match.group(1)
    return None
