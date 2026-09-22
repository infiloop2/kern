"""Peer-authenticated API for host-owned AI provider calls."""

from __future__ import annotations

from http import HTTPStatus
import os
import socket
import struct
import threading
from typing import Any

from host.constants import HOST_INFERENCE_SOCKET_PATH
from host.runtime.core.unix_socket_service import (
    UnixSocketRequestHandler,
    UnixSocketServer,
    peer_uids,
)
from host.runtime.host_inference import providers
from host.runtime.host_inference.typesafe import DEFAULT_TIMEOUT_SECONDS as JEV_MAX_TIMEOUT_SECONDS


SOCKET_PATH = os.environ.get("KERN_HOST_INFERENCE_SOCKET", HOST_INFERENCE_SOCKET_PATH)
MAX_REQUEST_BODY_BYTES = 128 * 1024
MAX_CONCURRENT_CALLS = 4
MAX_CONCURRENT_CONNECTIONS = 8
_CALL_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT_CALLS)


def caller_uids() -> frozenset[int]:
    return peer_uids("kern-admin") | peer_uids("kern-workspace") | peer_uids("kern-tools")


def dispatch(path: str, body: dict[str, Any]) -> dict[str, Any]:
    if path == "/openai/text-completion":
        if set(body) != {"prompt", "schema", "schema_name", "purpose"}:
            raise ValueError("invalid OpenAI text-completion request")
        if not all(isinstance(body.get(key), str) for key in ("prompt", "schema_name", "purpose")):
            raise ValueError("invalid OpenAI text-completion request")
        if not isinstance(body.get("schema"), dict):
            raise ValueError("invalid OpenAI text-completion request")
        return {
            "result": providers.openai_text_completion(
                body["prompt"], body["schema"], body["schema_name"], purpose=body["purpose"]
            )
        }
    if path == "/typesafe/jev-judgment":
        if set(body) != {"state", "questions", "timeout_seconds"}:
            raise ValueError("invalid TypeSafe Jev judgment request")
        questions = body.get("questions")
        timeout = body.get("timeout_seconds")
        if (
            not isinstance(questions, dict)
            or not questions
            or isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not 0.1 <= timeout <= JEV_MAX_TIMEOUT_SECONDS
        ):
            raise ValueError("invalid TypeSafe Jev judgment request")
        return {
            "result": providers.typesafe_jev_judgment(
                body.get("state"), questions, timeout_seconds=float(timeout)
            )
        }
    raise LookupError("unknown host inference path")


class HostInferenceHandler(UnixSocketRequestHandler):
    server: "HostInferenceServer"

    def do_POST(self) -> None:
        peer_uid = self._peer()[1]
        if peer_uid not in self.server.allowed_uids:
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "Host service peer required."})
            return
        if self.path not in {"/openai/text-completion", "/typesafe/jev-judgment"}:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Unknown path."})
            return
        length = self.bounded_content_length(MAX_REQUEST_BODY_BYTES)
        if length is None:
            return
        if not _CALL_SLOTS.acquire(blocking=False):
            self._send_json(
                HTTPStatus.TOO_MANY_REQUESTS,
                {"error": "Host inference is busy; try again shortly."},
            )
            return
        try:
            body = self.read_json_object_body(length)
            if body is None:
                return
            try:
                result = dispatch(self.path, body)
            except ValueError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            except LookupError:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "Unknown path."})
                return
            self._send_json(HTTPStatus.OK, result)
        finally:
            _CALL_SLOTS.release()


class HostInferenceServer(UnixSocketServer):
    def __init__(self, socket_path: str, allowed_uids: frozenset[int] | None = None) -> None:
        self.allowed_uids = allowed_uids if allowed_uids is not None else caller_uids()
        self._connection_slots = threading.BoundedSemaphore(MAX_CONCURRENT_CONNECTIONS)
        super().__init__(socket_path, HostInferenceHandler)

    def process_request(self, request: Any, client_address: Any) -> None:
        # Authenticate from SO_PEERCRED and reserve bounded capacity before
        # ThreadingHTTPServer allocates a handler thread. A local process that
        # cannot call this service therefore cannot exhaust it with idle
        # connections that never send an HTTP request line.
        try:
            peer_allowed = _peer_uid(request) in self.allowed_uids
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


def _peer_uid(connection: socket.socket) -> int:
    raw = connection.getsockopt(
        socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
    )
    _pid, uid, _gid = struct.unpack("3i", raw)
    return uid


def serve_forever(socket_path: str = SOCKET_PATH) -> None:
    HostInferenceServer(socket_path).serve_forever()
