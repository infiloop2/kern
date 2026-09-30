"""In-process Browser relay for public HTTPS tunnels and private proxy settings."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import select
import socket
import subprocess
import threading
import time
from typing import Any
import ssl

from host.constants import BROWSER_NETWORK_PORT
from host.runtime.browser.client import BrowserError
from host.runtime.browser_network.config import Settings
from host.runtime.browser.storage import Store
from host.runtime.browser_network.transport import ConnectionFailure, connect_proxy, failure, target
from host.runtime.core import host_errors


class Network:
    def __init__(self, store: Store) -> None:
        self.settings = Settings(store)
        self.lock = threading.RLock()
        self.connections: set[socket.socket] = set()
        self.generation = 0

    def dial(self, authority: str) -> socket.socket:
        with self.lock:
            value = self.settings.value.copy()
            generation = self.generation
            credentials = (self.settings.proxy_username(), value["password"]) if value["mode"] == "decodo" else None
        host = target(authority)
        # Use normal hostname connections. The Browser UID firewall protects the
        # host; Decodo resolves destinations at the proxy, like ordinary curl.
        if value["mode"] == "direct":
            try:
                stream = socket.create_connection((host, 443), timeout=10)
            except OSError as exc:
                raise failure("direct_connection", exc, host=host) from exc
        elif credentials is not None:
            stream = connect_proxy(("gate.decodo.com", 7000), host, credentials)
        else:
            raise BrowserError("Browser connection settings are invalid.")
        with self.lock:
            if generation != self.generation:
                stream.close()
                raise BrowserError("Browser connection settings changed during connection. Retry.")
            self.connections.add(stream)
            return stream

    def release(self, stream: socket.socket) -> None:
        with self.lock:
            self.connections.discard(stream)
            stream.close()

    def disconnect(self) -> None:
        with self.lock:
            self.generation += 1
            for stream in self.connections:
                try:
                    stream.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                stream.close()
            self.connections.clear()

    def dispatch(self, operation: str, body: dict[str, Any]) -> dict[str, Any]:
        if operation == "test" and not body:
            return self.test()
        with self.lock:
            if operation == "get" and not body:
                previous = self.settings.value
                self.settings.reload()
                if previous != self.settings.value:
                    self.disconnect()
                return self.settings.public()
            if operation == "save":
                value = self.settings.prepare(body)
                self.settings.save(value)
                self.disconnect()
                return self.settings.public()
            raise BrowserError("Unsupported Browser connection operation.")

    def test(self) -> dict[str, Any]:
        # Exercise the same local relay Chromium uses. curl owns TLS and the
        # whole-request deadline, including a relay stalled in the system resolver.
        # No proxy secret goes in argv, the environment, or diagnostic output.
        try:
            result = subprocess.run(
                ["/usr/bin/curl", "--disable", "--silent", "--fail", "--max-time", "30",
                 "--max-filesize", "100", "--proxy", f"http://127.0.0.1:{BROWSER_NETWORK_PORT}",
                 "--noproxy", "", "--dump-header", "/dev/stderr", "https://api.ipify.org"],
                capture_output=True, timeout=35, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise failure("connection_test", exc) from exc
        if result.returncode:
            message = ("Browser connection test timed out." if result.returncode == 28
                       else "Browser connection test failed.")
            message += " Check Host diagnostics."
            # Only the first header block belongs to our relay. Never expose
            # website headers, response bodies or curl's raw error output.
            headers = result.stderr.partition(b"\r\n\r\n")[0].split(b"\r\n")
            for line in headers[1:]:
                if line.startswith(b"X-Kern-Browser-Error: "):
                    message = line.removeprefix(b"X-Kern-Browser-Error: ").decode("ascii")
                    break
            host_errors.report_warning("browser.network", message, context={"stage": "connection_test", "curl_exit": result.returncode})
            raise BrowserError(message)
        try:
            address = ipaddress.ip_address(result.stdout.decode("ascii").strip())
            if not address.is_global:
                raise ValueError
        except ValueError as exc:
            raise failure("connection_test_response", exc) from exc
        return {"mode": self.settings.value["mode"], "ip": str(address)}


class TunnelHandler(BaseHTTPRequestHandler):
    timeout = 15
    rbufsize = 0

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_CONNECT(self) -> None:
        network: Network = self.server.network  # type: ignore[attr-defined]
        upstream: socket.socket | None = None
        connected = False
        try:
            upstream = network.dial(self.path)
            self.send_response(200, "Connection established")
            self.end_headers()
            connected = True
            # TLS passes through unchanged. No cookies, HTTP bodies or provider
            # errors are inspected or logged. Bound idle and total tunnel life.
            deadline = time.monotonic() + 600
            while time.monotonic() < deadline:
                # SSL may have decrypted bytes buffered after the socket stops
                # being readable. Drain them before waiting on the descriptors.
                readable = [upstream] if isinstance(upstream, ssl.SSLSocket) and upstream.pending() else []
                if not readable:
                    readable, _, _ = select.select([self.connection, upstream], [], [], 60)
                if not readable:
                    break
                for stream in readable:
                    data = stream.recv(65536)
                    if not data:
                        return
                    (upstream if stream is self.connection else self.connection).sendall(data)
        except (OSError, ValueError, BrowserError) as exc:
            if not connected:
                self.send_response(502)
                if isinstance(exc, ConnectionFailure):
                    self.send_header("X-Kern-Browser-Error", str(exc))
                self.send_header("Content-Length", "0")
                self.end_headers()
        finally:
            self.close_connection = True
            if upstream:
                network.release(upstream)


class TunnelServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, network: Network, port: int = BROWSER_NETWORK_PORT) -> None:
        self.network = network
        self.slots = threading.BoundedSemaphore(64)
        super().__init__(("127.0.0.1", port), TunnelHandler)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request: Any, client_address: Any) -> None:
        # BaseServer prints the exception, which could contain credentials.
        pass
