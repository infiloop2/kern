"""Single restricted backend for Kern's operator and agent Workspace APIs."""

from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

from host.constants import LOOPBACK, WORKSPACE_AGENT_SOCKET_PATH, WORKSPACE_PORT
from host.runtime.core import host_errors
from host.runtime.workspace import agent_api, getting_started, memory, schedules
from host.runtime.workspace.chat import backend as chat
from host.runtime.workspace.host_api import WorkspaceError
from host.runtime.workspace.web_apps import backend as web_apps


HOST = os.environ.get("KERN_WORKSPACE_HOST", LOOPBACK)
PORT = int(os.environ.get("KERN_WORKSPACE_PORT", str(WORKSPACE_PORT)))
MAINTENANCE_INTERVAL_SECONDS = 3600


class Handler(BaseHTTPRequestHandler):
    server_version = "KernWorkspace/1"

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
        try:
            if parsed.path == "/chat" or parsed.path.startswith("/chat/"):
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
    server = ThreadingHTTPServer((HOST, PORT), Handler)
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
