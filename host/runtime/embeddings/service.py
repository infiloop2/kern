"""Socket-activated, CPU-only FastEmbed inference service.

The systemd socket is owned by ``kern-embedding:kern-workspace-api`` and this
handler also verifies the connecting uid. The service has no network and no
database access. It exits after an idle window so the small host does not
permanently pay ONNX's resident-memory cost; systemd activates it again on
demand.
"""

from __future__ import annotations

from http import HTTPStatus
from http.server import HTTPServer
import os
from pathlib import Path
import pwd
import socket
import threading
import time
from typing import Any, cast

from host.constants import EMBEDDING_SOCKET_PATH
from host.runtime.core.unix_socket_service import UnixSocketRequestHandler
from host.runtime.embeddings.client import (
    MAX_JSON_EXPANSION,
    MAX_TEXT_BYTES,
    MAX_TEXTS,
    MODEL_DIMENSIONS,
    MODEL_NAME,
)


MODEL_DIR = Path(
    os.environ.get(
        "KERN_EMBEDDING_MODEL_DIR",
        "/usr/local/share/kern-embedding-models/bge-small-en-v1.5-onnx-Q",
    )
)
MAX_REQUEST_BYTES = MAX_TEXTS * MAX_TEXT_BYTES * MAX_JSON_EXPANSION + 4096
IDLE_EXIT_SECONDS = 5 * 60

_model_instance: Any | None = None
_model_lock = threading.Lock()
_last_request = time.monotonic()
_request_active = False
_activity_lock = threading.Lock()


def _allowed_uids() -> frozenset[int]:
    """The service accounts permitted to request inference.

    On a deployed host both accounts always resolve. They are missing only in
    tests and local runs, where falling back to this process's own uid keeps the
    handler exercisable; a host that somehow lost the account gets a closed door
    rather than a widened one, since the socket group already gates callers.
    """
    uids = set()
    for user in ("kern-admin", "kern-workspace"):
        try:
            uids.add(pwd.getpwnam(user).pw_uid)
        except KeyError:
            if os.environ.get("KERN_EMBEDDING_ALLOW_SELF_UID") == "1":
                uids.add(os.getuid())
    return frozenset(uids)


def _model() -> Any:
    global _model_instance
    with _model_lock:
        if _model_instance is None:
            from fastembed import TextEmbedding  # type: ignore[import-not-found]

            # specific_model_path short-circuits fastembed's hub cache lookup
            # and loads this directory directly, so bootstrap installs a flat
            # directory of model files instead of a reconstructed hub cache.
            _model_instance = TextEmbedding(
                model_name=MODEL_NAME,
                specific_model_path=str(MODEL_DIR),
                threads=1,
            )
        return _model_instance


def embed(texts: list[str], kind: str) -> list[list[float]]:
    model = _model()
    generated = model.query_embed(texts) if kind == "query" else model.passage_embed(texts)
    vectors = [[float(value) for value in vector] for vector in generated]
    if len(vectors) != len(texts) or any(len(vector) != MODEL_DIMENSIONS for vector in vectors):
        raise RuntimeError("embedding model returned an unexpected shape")
    return vectors


def _valid_texts(value: Any) -> bool:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_TEXTS:
        return False
    try:
        return all(
            isinstance(text, str)
            and bool(text)
            and len(text.encode("utf-8")) <= MAX_TEXT_BYTES
            for text in value
        )
    except UnicodeEncodeError:
        return False


class Handler(UnixSocketRequestHandler):
    def do_POST(self) -> None:
        global _last_request, _request_active
        if self._peer()[1] not in _allowed_uids():
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return
        if self.path != "/v1/embed":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "route not found"})
            return
        length = self.bounded_content_length(MAX_REQUEST_BYTES)
        if length is None:
            return
        body = self.read_json_object_body(length)
        if body is None:
            return
        if set(body) != {"kind", "texts"} or body.get("kind") not in {"query", "passage"}:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid embedding request"})
            return
        texts = body.get("texts")
        if not _valid_texts(texts):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid embedding texts"})
            return
        assert isinstance(texts, list)
        with _activity_lock:
            _request_active = True
            _last_request = time.monotonic()
        try:
            vectors = embed(texts, str(body["kind"]))
            self._send_json(
                HTTPStatus.OK,
                {"model": MODEL_NAME, "dimensions": MODEL_DIMENSIONS, "embeddings": vectors},
            )
        except Exception:
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "embedding failed"})
        finally:
            with _activity_lock:
                _request_active = False
                _last_request = time.monotonic()


class ActivatedUnixHTTPServer(HTTPServer):
    address_family = socket.AF_UNIX

    def __init__(self, handler: type[Handler]) -> None:
        super().__init__(EMBEDDING_SOCKET_PATH, handler, bind_and_activate=False)  # type: ignore[arg-type]
        self.socket.close()
        self.socket = socket.socket(fileno=3)
        if self.socket.getsockname() != EMBEDDING_SOCKET_PATH:
            raise RuntimeError("systemd passed the wrong embedding listener")
        self.server_address = cast(Any, EMBEDDING_SOCKET_PATH)


def _stop_when_idle(server: HTTPServer) -> None:
    while True:
        time.sleep(30)
        with _activity_lock:
            idle = not _request_active and time.monotonic() - _last_request >= IDLE_EXIT_SECONDS
        if idle:
            server.shutdown()
            return


def main() -> int:
    if (
        int(os.environ.get("LISTEN_PID", "0")) != os.getpid()
        or int(os.environ.get("LISTEN_FDS", "0")) != 1
    ):
        raise RuntimeError("embedding service must be started by its systemd socket")
    server = ActivatedUnixHTTPServer(Handler)
    threading.Thread(target=_stop_when_idle, args=(server,), daemon=True).start()
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
