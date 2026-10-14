"""Isolated service with separate operator controls and fixed tools actions."""
from __future__ import annotations

import pwd
import signal
import socket
import struct
import threading
from typing import Any
from urllib.parse import quote

from host.constants import BROWSER_SOCKET_PATH
from host.runtime.browser.client import BrowserActionNotStarted, BrowserError
from host.runtime.browser.sessions import Sessions
from host.runtime.browser_network.relay import TunnelServer
from host.runtime.core import host_errors, host_metrics
from host.runtime.core.unix_socket_service import UnixSocketRequestHandler, UnixSocketServer

OPERATOR_OPERATIONS = {"network_get", "network_save", "network_test", "ready", "list", "create", "check", "open", "frame", "input", "save", "cancel", "disconnect"}
TOOL_OPERATIONS = {"list", "post_tweet", "linkedin_resolve_recipient", "linkedin_read_conversation", "linkedin_send_dm", "screenshot_url"}


def authorized(uid: int, path: str) -> str | None:
    namespace, _, operation = path.removeprefix("/").partition("/")
    try:
        if namespace == "operator" and uid == pwd.getpwnam("kern-admin").pw_uid and operation in OPERATOR_OPERATIONS:
            return operation
        if namespace == "actions" and uid == pwd.getpwnam("kern-tools").pw_uid and operation in TOOL_OPERATIONS:
            return operation
    except KeyError:
        pass
    return None


class Handler(UnixSocketRequestHandler):
    def do_POST(self) -> None:
        operation = authorized(self._peer()[1], self.path)
        if operation is None:
            self._send_json(403, {"error": "Browser access denied."})
            return
        length = self.bounded_content_length(65536)
        if length is None:
            return
        body = self.read_json_object_body(length)
        if body is None:
            return
        server: Any = self.server
        try:
            result = server.sessions.submit(
                operation, body, agent_action=self.path.startswith("/actions/"),
            ).result()
        except BrowserActionNotStarted as exc:
            self._send_json(409, {"error": str(exc), "code": "browser_busy", "execution_state": "not_started"})
        except BrowserError as exc:
            self._send_json(409, {"error": str(exc)})
        except Exception as exc:
            if operation == "frame":
                self._send_json(503, {"error": "Browser image unavailable. Wait for navigation or reopen the browser."})
                return
            # Browser exceptions can contain URLs, DOM fragments and credentials.
            # Preserve the call stack, but replace the exception message.
            safe = RuntimeError(type(exc).__name__).with_traceback(exc.__traceback__)
            host_errors.report_unexpected(
                "browser.dispatch", safe,
                context={"operation": operation, **host_metrics.service_resource_snapshot("browser")},
            )
            message = "Browser operation failed. Reopen the browser or check Host diagnostics."
            if operation == "screenshot_url":
                message = "Browser screenshot failed or timed out. Check Host diagnostics."
            if operation in {"network_get", "network_save", "network_test"}:
                message = "Browser connection failed. Check credentials or provider balance, then test again."
            if operation in {"post_tweet", "linkedin_send_dm"}:
                message += " Check the website before approving another attempt."
            self._send_json(503, {"error": message})
        else:
            # Response writes stay outside the operation-error handlers. If a
            # write fails, the HTTP server closes the connection; no second
            # response is attempted, for either JSON or PNG.
            if operation == "screenshot_url":
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(result.png)))
                self.send_header("X-Kern-Screenshot-Clipped", "true" if result.clipped else "false")
                self.send_header("X-Kern-Screenshot-URL", quote(result.url, safe=""))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(result.png)
            else:
                self._send_json(200, result)


class Server(UnixSocketServer):
    def __init__(self) -> None:
        self.allowed_uids = frozenset({pwd.getpwnam("kern-admin").pw_uid, pwd.getpwnam("kern-tools").pw_uid})
        self.sessions = Sessions()
        super().__init__(BROWSER_SOCKET_PATH, Handler)

    def process_request(self, request: Any, client_address: Any) -> None:
        # Reject untrusted peers before ThreadingHTTPServer allocates a thread
        # to read their headers. The handler still checks each operation.
        try:
            raw = request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            _pid, uid, _gid = struct.unpack("3i", raw)
            if uid in self.allowed_uids:
                super().process_request(request, client_address)
                return
        except OSError:
            pass
        request.close()

    def service_actions(self) -> None:
        self.sessions.expire()


def main() -> int:
    def terminate(_signal: int, _frame: Any) -> None:
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, terminate)
    server = Server()
    try:
        with TunnelServer(server.sessions.network) as tunnel:
            relay = threading.Thread(target=tunnel.serve_forever, daemon=True)
            relay.start()
            try:
                server.serve_forever()
            finally:
                tunnel.shutdown()
                relay.join()
    finally:
        server.server_close()
        server.sessions.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
