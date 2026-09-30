"""In-process Browser relay for public HTTPS tunnels and private proxy settings."""
from __future__ import annotations

import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import select
import socket
import threading
import time
from typing import Any
import ssl

from host.constants import BROWSER_NETWORK_PORT
from host.runtime.browser.client import BrowserError
from host.runtime.browser_network.config import Settings
from host.runtime.browser.storage import Store
from host.runtime.browser_network.transport import connect_addresses, connect_proxy, target


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
        addresses = target(authority)
        # DNS/TCP/TLS must not serialize independent page resources behind a lock.
        if value["mode"] == "direct":
            stream = connect_addresses(addresses, 443)
        elif credentials is not None:
            last_error: OSError | BrowserError = BrowserError("Browser destination has no public addresses.")
            for address in addresses:
                try:
                    stream = connect_proxy(("gate.decodo.com", 7000), address, credentials)
                    break
                except ssl.SSLError:
                    raise  # Another destination cannot repair gateway TLS.
                except (OSError, BrowserError) as exc:
                    last_error = exc
            else:
                raise last_error
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
        stream = self.dial("api.ipify.org:443")
        try:
            # MemoryBIO keeps destination TLS inside the proxy's existing TLS.
            # wrap_socket on an SSLSocket would discard the outer TLS layer.
            incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
            secured = ssl.create_default_context().wrap_bio(incoming, outgoing, server_hostname="api.ipify.org")
            deadline = time.monotonic() + 30
            def tls(operation: Any) -> Any:
                while True:
                    stream.settimeout(max(0.001, deadline - time.monotonic()))
                    if time.monotonic() >= deadline:
                        raise BrowserError("Connection test timed out.")
                    try:
                        result = operation()
                    except ssl.SSLWantReadError:
                        if outgoing.pending:
                            stream.sendall(outgoing.read())
                        chunk = stream.recv(16384)
                        if not chunk:
                            raise BrowserError("Connection test ended before the response was complete.")
                        incoming.write(chunk)
                    else:
                        if outgoing.pending:
                            stream.sendall(outgoing.read())
                        return result
            tls(secured.do_handshake)
            tls(lambda: secured.write(b"GET / HTTP/1.0\r\nHost: api.ipify.org\r\nConnection: close\r\n\r\n"))
            # HTTPResponse reads exactly one bounded response from the inner TLS.
            import io
            class Reader(io.RawIOBase):
                def readable(self) -> bool:
                    return True
                def readinto(self, buffer: Any) -> int:
                    data = tls(lambda: secured.read(len(buffer)))
                    buffer[:len(data)] = data
                    return len(data)
            class Connection:
                def makefile(self, *_args: Any, **_kwargs: Any) -> Any:
                    return io.BufferedReader(Reader())
            response = http.client.HTTPResponse(Connection())  # type: ignore[arg-type]
            try:
                response.begin()
                raw = response.read(100)
                if response.status != 200:
                    raise BrowserError("Connection test failed. Check the selected service.")
                address = ipaddress.ip_address(raw.decode("ascii").strip())
                if not address.is_global:
                    raise BrowserError("Connection test did not return a public IP address.")
                return {"mode": self.settings.value["mode"], "ip": str(address)}
            finally:
                response.close()
        finally:
            self.release(stream)


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
        except (OSError, ValueError, BrowserError):
            if not connected:
                self.send_error(502, "Browser connection unavailable. Test the connection in Browser settings.")
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
