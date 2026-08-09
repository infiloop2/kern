"""Shared scaffolding for the peer-authenticated Unix-socket HTTP services.

The tools, Workspace, and agent-network services each expose HTTP over an
AF_UNIX socket to peers identified by kernel-verified SO_PEERCRED
credentials. This module owns the transport plumbing those services share —
socket binding, the peer-credential read, and JSON request/response framing —
while each service keeps its own routes and peer-authorization policy.
"""

from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import pwd
import socket
import struct
from typing import Any

# A service may accept a world-connectable socket before its handler performs a
# second peer-credential check. A read timeout keeps an admitted connection
# from stalling forever while sending its request line and headers. Services
# with a tighter connection budget may additionally authenticate in
# ``process_request`` before allocating a handler thread.
REQUEST_READ_TIMEOUT_SECONDS = 30
MAX_JSON_NESTING_DEPTH = 64


def _json_nesting_exceeds(value: object, max_depth: int) -> bool:
    """Return whether a decoded JSON value exceeds the structural depth cap."""
    pending = [(value, 0)]
    while pending:
        current, depth = pending.pop()
        if not isinstance(current, (dict, list)):
            continue
        if depth >= max_depth:
            return True
        children = current.values() if isinstance(current, dict) else current
        pending.extend((child, depth + 1) for child in children)
    return False


def peer_uids(user: str) -> frozenset[int]:
    """The uids for one service account. Outside a bootstrapped host (tests,
    the UI mock) the service accounts do not exist; the socket then belongs to
    the developer running it."""
    try:
        return frozenset({pwd.getpwnam(user).pw_uid})
    except KeyError:
        return frozenset({os.getuid()})


class UnixSocketRequestHandler(BaseHTTPRequestHandler):
    # Bound how long a connection may stall while sending its request line and
    # headers, before do_GET/do_POST (and the peer-credential check) run.
    timeout = REQUEST_READ_TIMEOUT_SECONDS

    def address_string(self) -> str:  # AF_UNIX has no client address tuple
        return "local"

    def log_message(self, format: str, *args: object) -> None:
        pass

    def _peer(self) -> tuple[int, int]:
        creds = self.connection.getsockopt(
            socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
        )
        pid, uid, _gid = struct.unpack("3i", creds)
        return pid, uid

    def _send_json(self, status: HTTPStatus | int, body: dict[str, Any]) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def bounded_content_length(self, max_bytes: int) -> int | None:
        """Parse and bound the declared Content-Length, or send the error
        response and return None."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > max_bytes:
            self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "Request too large."})
            return None
        return length

    def read_json_object_body(self, length: int) -> dict[str, Any] | None:
        """Read and parse a length-validated request body as a JSON object, or
        send the error response and return None. An empty body is tolerated as
        an empty object."""
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Request body must be JSON."})
            return None
        if not isinstance(body, dict):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Request body must be a JSON object."})
            return None
        if _json_nesting_exceeds(body, MAX_JSON_NESTING_DEPTH):
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "Request body is too deeply nested."},
            )
            return None
        return body


class UnixSocketServer(ThreadingHTTPServer):
    address_family = socket.AF_UNIX
    daemon_threads = True

    def __init__(self, socket_path: str, handler_class: type[UnixSocketRequestHandler]) -> None:
        # typeshed models HTTPServer addresses as (host, port) tuples only;
        # with address_family = AF_UNIX the address is the socket path.
        super().__init__(socket_path, handler_class)  # type: ignore[arg-type]

    def server_bind(self) -> None:
        path = Path(str(self.server_address))
        path.unlink(missing_ok=True)
        self.socket.bind(str(path))
        # World-connectable like the Postgres socket; the handler's
        # peer-credential check is the authentication.
        path.chmod(0o666)
