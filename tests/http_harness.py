"""Unix-socket HTTP transport for admin API tests.

Runs the real handler without requiring loopback TCP access.
The urllib opener is scoped to each test's cleanup stack.
Database setup remains in pg_harness.
"""

from __future__ import annotations

import http.client
from pathlib import Path
import socket
import socketserver
import tempfile
import threading
from typing import Any
import unittest
import urllib.request

from host.runtime.admin_api import service as admin_api


# The agent's loopback TCP egress is firewalled on Kern hosts, so the test
# admin server listens on a Unix socket and requests reach it through a
# urllib opener that dials that socket. The HTTP request/response layer under
# test (auth headers, cookies, raw request bytes) is unchanged.
ADMIN_TEST_ORIGIN = "http://kern-admin.test"


class UnixSocketHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def get_request(self):  # type: ignore[override]
        request, _ = super().get_request()
        # BaseHTTPRequestHandler (and the login throttle's client key) expect
        # a TCP-shaped (host, port) peer address.
        return request, ("127.0.0.1", 0)


class _UnixSocketHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, host: str, timeout: Any = None) -> None:
        super().__init__(host)
        self._socket_path = socket_path
        self._timeout_value = timeout

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        if isinstance(self._timeout_value, (int, float)):
            self.sock.settimeout(self._timeout_value)
        self.sock.connect(self._socket_path)


class _UnixSocketHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, socket_path: str) -> None:
        super().__init__()
        self._socket_path = socket_path

    def http_open(self, req: Any) -> Any:
        def connection(host: str, timeout: Any = None) -> _UnixSocketHTTPConnection:
            return _UnixSocketHTTPConnection(self._socket_path, host, timeout=timeout)

        return self.do_open(connection, req)


def start_admin_http_server(test: unittest.TestCase) -> str:
    """Serve admin_api.Handler for one test case and route urllib to it.

    Returns the base URL test requests should target."""
    socket_dir = tempfile.TemporaryDirectory()
    test.addCleanup(socket_dir.cleanup)
    socket_path = str(Path(socket_dir.name) / "admin-api.sock")
    server = UnixSocketHTTPServer(socket_path, admin_api.Handler)
    # Frequent polling speeds shutdown without shortening request deadlines.
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    test.addCleanup(server.server_close)
    test.addCleanup(server.shutdown)
    urllib.request.install_opener(urllib.request.build_opener(_UnixSocketHTTPHandler(socket_path)))
    test.addCleanup(urllib.request.install_opener, urllib.request.build_opener())
    test.admin_socket_path = socket_path  # type: ignore[attr-defined]
    return ADMIN_TEST_ORIGIN


def raw_admin_request(socket_path: str, request: bytes) -> bytes:
    """One raw HTTP exchange against the test admin server."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(5)
        sock.connect(socket_path)
        try:
            sock.sendall(request)
            sock.shutdown(socket.SHUT_WR)
        except (BrokenPipeError, ConnectionResetError):
            # The server may reject and close before the whole request lands
            # (e.g. an oversized body); its buffered response is still readable.
            pass
        chunks: list[bytes] = []
        while chunk := sock.recv(65536):
            chunks.append(chunk)
        return b"".join(chunks)
