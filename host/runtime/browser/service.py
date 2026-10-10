"""Isolated service with separate operator controls and fixed tools actions."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import pwd
import signal
import socket
import struct
import threading
from typing import Any

from host.constants import BROWSER_SOCKET_PATH
from host.runtime.browser.client import BrowserError
from host.runtime.browser.accounts import Accounts
from host.runtime.browser_network.relay import TunnelServer
from host.runtime.core import host_errors, host_metrics
from host.runtime.core.unix_socket_service import UnixSocketRequestHandler, UnixSocketServer

OPERATOR_OPERATIONS = {"network_get", "network_save", "network_test", "ready", "list", "create", "check", "open", "frame", "input", "save", "cancel", "disconnect"}
TOOL_OPERATIONS = {"list", "post_tweet", "linkedin_resolve_recipient", "linkedin_read_conversation", "linkedin_send_dm"}


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
        # Cancel must queue behind a slow frame instead of being rejected as
        # busy. The single worker still serializes it and validates its lease.
        # Other operations retain the admission guard against request backlog.
        acquired = operation != "cancel"
        if acquired and not server.busy.acquire(timeout=1):
            self._send_json(409, {"error": "Browser is busy. Wait for the current action to finish.",
                                  "code": "browser_busy", "execution_state": "not_started"})
            return
        try:
            result = server.worker.submit(
                server.profiles.dispatch, operation, body,
                agent_action=self.path.startswith("/actions/"),
            ).result()
            self._send_json(200, result)
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
            if operation in {"network_get", "network_save", "network_test"}:
                message = "Browser connection failed. Check credentials or provider balance, then test again."
            if operation in {"post_tweet", "linkedin_send_dm"}:
                message += " Check the website before approving another attempt."
            self._send_json(503, {"error": message})
        finally:
            if acquired:
                server.busy.release()


class Server(UnixSocketServer):
    def __init__(self) -> None:
        self.allowed_uids = frozenset({pwd.getpwnam("kern-admin").pw_uid, pwd.getpwnam("kern-tools").pw_uid})
        self.busy = threading.Lock()
        self.worker = ThreadPoolExecutor(max_workers=1)
        self.profiles = self.worker.submit(Accounts).result()
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
        if self.busy.acquire(blocking=False):
            future = self.worker.submit(self.profiles.expire)
            future.add_done_callback(lambda _future: self.busy.release())


def main() -> int:
    def terminate(_signal: int, _frame: Any) -> None:
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, terminate)
    server = Server()
    try:
        with TunnelServer(server.profiles.network) as tunnel:
            relay = threading.Thread(target=tunnel.serve_forever, daemon=True)
            relay.start()
            try:
                server.serve_forever()
            finally:
                tunnel.shutdown()
                relay.join()
    finally:
        server.server_close()
        with server.profiles.network.lock:
            server.profiles.network.disconnect()
        for profile in [*server.profiles.profiles.values(), *server.profiles.pending.values()]:
            server.worker.submit(profile.close).result()
        server.worker.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
