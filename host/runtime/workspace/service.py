"""Single restricted backend for Kern's operator and agent Workspace APIs."""

from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
import grp
import json
import os
from pathlib import Path
import socket
import stat
import struct
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

from host.constants import (
    SERVICE_ACCOUNTS,
    WORKSPACE_ADMIN_GROUP,
    WORKSPACE_AGENT_SOCKET_PATH,
    WORKSPACE_BROWSER_SOCKET_PATH,
)
from host.runtime.core import host_errors
from host.runtime.core.unix_socket_service import UnixSocketServer, peer_uids
from host.runtime.workspace import agent_api, agent_messages, getting_started, memory, schedules
from host.runtime.workspace.chat import backend as chat
from host.runtime.workspace.host_api import WorkspaceError
from host.runtime.workspace.web_apps import backend as web_apps


MAINTENANCE_INTERVAL_SECONDS = 3600


class WorkspaceBrowserServer(UnixSocketServer):
    """Admin-only socket for the browser proxy, checked before a worker starts."""

    request_queue_size = 64

    def server_bind(self) -> None:
        path = Path(str(self.server_address))
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISSOCK(mode):
                raise OSError(f"refusing to replace non-socket Workspace browser path: {path}")
            path.unlink()
        self.socket.bind(str(path))
        if os.geteuid() == SERVICE_ACCOUNTS["kern-workspace"]:
            group_id = grp.getgrnam(WORKSPACE_ADMIN_GROUP).gr_gid
            if group_id not in os.getgroups() and group_id != os.getegid():
                raise PermissionError(f"Workspace service is not in {WORKSPACE_ADMIN_GROUP}")
        else:
            group_id = os.getgid()
            # Local tests run without the Workspace service account.
        os.chown(path, -1, group_id)
        path.chmod(0o660)

    def process_request(self, request: Any, client_address: Any) -> None:
        try:
            raw = request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            _pid, uid, _gid = struct.unpack("3i", raw)
            allowed = uid in peer_uids("kern-admin")
        except OSError:
            allowed = False
        if not allowed:
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)


class Handler(BaseHTTPRequestHandler):
    server_version = "KernWorkspace/1"
    timeout = 60

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
        parsed = urlparse(self.path)
        response: Any
        try:
            if method == "GET" and parsed.path == "/health":
                response = {"status": "ok"}
            elif method == "POST" and parsed.path.startswith("/messages/"):
                body = self._read_body(agent_api.MAX_REQUEST_BODY_BYTES)
                response = agent_messages.deliver_message(parsed.path.removeprefix("/messages/"), body)
            elif parsed.path == "/chat" or parsed.path.startswith("/chat/"):
                body = self._read_body(chat.MAX_REQUEST_BODY_BYTES)
                response = chat.route_browser(
                    method,
                    parsed.path.removeprefix("/chat") or "/",
                    body,
                    parse_qs(parsed.query, keep_blank_values=True),
                )
            elif parsed.path == "/apps" or parsed.path.startswith("/apps/"):
                body = self._read_body(web_apps.MAX_REQUEST_BODY_BYTES)
                backend_path = parsed.path.removeprefix("/apps") or "/"
                response = web_apps.route_browser(
                    method, backend_path, body, parse_qs(parsed.query, keep_blank_values=True)
                )
            elif parsed.path == "/memory" or parsed.path.startswith("/memory/"):
                body = self._read_body(agent_api.MAX_REQUEST_BODY_BYTES)
                response = memory.route_browser(
                    method, parsed.path, body, parse_qs(parsed.query, keep_blank_values=True)
                )
            elif parsed.path == "/schedules" or parsed.path.startswith("/schedules/"):
                body = self._read_body(agent_api.MAX_REQUEST_BODY_BYTES)
                response = schedules.route_browser(
                    method, parsed.path, body, parse_qs(parsed.query, keep_blank_values=True)
                )
            elif (
                parsed.path == "/getting-started"
                or parsed.path.startswith("/getting-started/")
            ):
                body = self._read_body(agent_api.MAX_REQUEST_BODY_BYTES)
                response = getting_started.route_browser(
                    method, parsed.path, body, parse_qs(parsed.query, keep_blank_values=True)
                )
            else:
                raise WorkspaceError(HTTPStatus.NOT_FOUND, "route not found")
            self._send_json(HTTPStatus.OK, response)
        except WorkspaceError as exc:
            self._send_json(exc.status, {"error": {"message": exc.message}})
        except Exception as exc:
            host_errors.report_unexpected(
                "workspace.request",
                exc,
                context={"method": method, "route": parsed.path},
            )
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": {"message": "workspace request failed"}},
            )

    def _read_body(self, max_bytes: int) -> Any:
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError as exc:
            raise WorkspaceError(
                HTTPStatus.BAD_REQUEST, "malformed Content-Length"
            ) from exc
        if length < 0:
            raise WorkspaceError(HTTPStatus.BAD_REQUEST, "malformed Content-Length")
        if length > max_bytes:
            raise WorkspaceError(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request body too large"
            )
        if length == 0:
            return None
        try:
            return json.loads(self.rfile.read(length))
        except json.JSONDecodeError as exc:
            raise WorkspaceError(
                HTTPStatus.BAD_REQUEST, "request body must be valid JSON"
            ) from exc

    def _send_json(self, status: HTTPStatus, body: Any) -> None:
        data = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.end_headers()
        self.wfile.write(data)


def maintain_storage() -> None:
    """Apply every Workspace-owned PostgreSQL retention policy."""
    memory.prune_deleted()
    schedules.prune_deleted()
    web_apps.prune_revisions()


def maintenance_loop() -> None:
    """Maintain Workspace storage at startup and hourly thereafter."""
    while True:
        try:
            maintain_storage()
        except Exception as exc:
            host_errors.report_unexpected("workspace.maintenance", exc)
        time.sleep(MAINTENANCE_INTERVAL_SECONDS)


def main() -> int:
    # Bind first. A duplicate service instance must fail before it can start a
    # scheduler outside the serving process's workspace-lock domain.
    server = WorkspaceBrowserServer(
        os.environ.get("KERN_WORKSPACE_BROWSER_SOCKET", WORKSPACE_BROWSER_SOCKET_PATH),
        Handler,  # type: ignore[arg-type]
    )
    agent_server = agent_api.AgentWorkspaceServer(
        os.environ.get("KERN_WORKSPACE_AGENT_SOCKET", WORKSPACE_AGENT_SOCKET_PATH),
        agent_api.agent_peer_uids(),
    )
    agent_thread = threading.Thread(
        target=agent_server.serve_forever,
        name="workspace-agent-api",
        daemon=True,
    )
    agent_thread.start()
    memory_embedding_thread = threading.Thread(
        target=memory.embedding_index_loop,
        name="workspace-memory-embedding-index",
        daemon=True,
    )
    memory_embedding_thread.start()
    scheduler = threading.Thread(
        target=schedules.scheduler_loop, name="workspace-scheduler", daemon=True
    )
    scheduler.start()
    maintenance = threading.Thread(
        target=maintenance_loop, name="workspace-maintenance", daemon=True
    )
    maintenance.start()
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
