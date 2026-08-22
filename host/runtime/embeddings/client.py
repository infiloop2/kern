"""Bounded client for Kern's local socket-activated embedding service."""

from __future__ import annotations

import http.client
import json
import math
import socket
from typing import Any

from host.constants import EMBEDDING_SOCKET_PATH


MODEL_NAME = "BAAI/bge-small-en-v1.5"
MODEL_DIMENSIONS = 384
# BGE v1.5 cosine scores are compressed toward the high end, but the pinned
# quantized model scores useful paraphrases lower than the upstream model
# card's general 0.8 starting point.  The real-host replacement probe measures
# its intended pairs at 0.618-0.649 and the stale old-query pair at 0.438, so
# 0.55 keeps useful retrieval while rejecting that unrelated replacement.  The
# old 0.35 cutoff admitted effectively every indexed page on a small host.
MINIMUM_SIMILARITY = 0.55
MAX_TEXTS = 8
MAX_TEXT_BYTES = 16 * 1024
# ensure_ascii=False keeps non-ASCII verbatim, but JSON still escapes " and \
# to two bytes and other control bytes to six. A batch of otherwise valid texts
# can therefore serialize up to six times its UTF-8 size, so the request cap is
# sized for that; a valid batch the service refused would stall the indexer on
# it forever, since the same rows are simply reselected.
MAX_JSON_EXPANSION = 6
MAX_RESPONSE_BYTES = 256 * 1024
REQUEST_TIMEOUT_SECONDS = 20


class EmbeddingError(RuntimeError):
    """The local model could not return a valid embedding response.

    ``batch_rejected`` is true only when the service returned HTTP 400 for the
    submitted texts. Availability failures and malformed service responses say
    nothing about the texts and must not burn through their retry budget.
    """

    def __init__(self, message: str, *, batch_rejected: bool = False) -> None:
        super().__init__(message)
        self.batch_rejected = batch_rejected


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._socket_path)
        self.sock = sock


def embed_texts(texts: list[str], *, kind: str) -> list[list[float]]:
    """Embed one bounded batch as retrieval queries or passages."""
    if kind not in {"query", "passage"}:
        raise ValueError("embedding kind must be query or passage")
    if not texts or len(texts) > MAX_TEXTS:
        raise ValueError(f"embedding batch must contain 1-{MAX_TEXTS} texts")
    try:
        invalid_text = any(
            not isinstance(value, str)
            or not value
            or len(value.encode("utf-8")) > MAX_TEXT_BYTES
            for value in texts
        )
    except UnicodeEncodeError as exc:
        raise ValueError("embedding texts must be valid UTF-8") from exc
    if invalid_text:
        raise ValueError(f"embedding texts must be non-empty and at most {MAX_TEXT_BYTES} bytes")
    # ensure_ascii=False keeps the payload the same size as the UTF-8 bytes the
    # limits above measure; escaping non-ASCII would inflate a valid batch past
    # the service's request cap and stall the indexer on it forever.
    payload = json.dumps(
        {"kind": kind, "texts": texts}, separators=(",", ":"), ensure_ascii=False
    ).encode()
    connection = _UnixHTTPConnection(EMBEDDING_SOCKET_PATH, REQUEST_TIMEOUT_SECONDS)
    try:
        connection.request(
            "POST",
            "/v1/embed",
            body=payload,
            headers={"Content-Type": "application/json", "Content-Length": str(len(payload))},
        )
        response = connection.getresponse()
        length_text = response.getheader("Content-Length")
        if length_text is None or not length_text.isdigit():
            raise EmbeddingError("embedding service returned an unbounded response")
        length = int(length_text)
        if length > MAX_RESPONSE_BYTES:
            raise EmbeddingError("embedding service response is too large")
        raw = response.read(length)
        if response.status != 200:
            raise EmbeddingError(
                f"embedding service returned HTTP {response.status}",
                batch_rejected=response.status == 400,
            )
        decoded: Any = json.loads(raw)
    except (OSError, http.client.HTTPException, json.JSONDecodeError) as exc:
        raise EmbeddingError("local embedding service is unavailable") from exc
    finally:
        connection.close()
    if not isinstance(decoded, dict) or decoded.get("model") != MODEL_NAME:
        raise EmbeddingError("embedding service returned the wrong model")
    embeddings = decoded.get("embeddings")
    if not isinstance(embeddings, list) or len(embeddings) != len(texts):
        raise EmbeddingError("embedding service returned the wrong batch size")
    normalized: list[list[float]] = []
    for embedding in embeddings:
        if not isinstance(embedding, list) or len(embedding) != MODEL_DIMENSIONS:
            raise EmbeddingError("embedding service returned the wrong dimensions")
        if any(
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(float(value))
            for value in embedding
        ):
            raise EmbeddingError("embedding service returned an invalid vector")
        normalized.append([float(value) for value in embedding])
    return normalized
