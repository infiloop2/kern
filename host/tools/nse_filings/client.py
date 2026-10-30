"""Bounded source capture for NSE's public filing endpoints.

Requests use fixed HTTPS destinations and an identified client. Complete raw
responses are retained without numeric reserialization; redirects, cookies,
automatic retries and anti-bot workarounds are intentionally absent.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
import codecs
import hashlib
import json
import re
import tempfile
import threading
import time
from typing import BinaryIO, cast

from host.tools.results import OpenedStreamingAsset, StreamingAssetError
from host.tools.shared.web import WebRequestError, open_response_stream

JSON_LIMIT = 16 * 1024 * 1024
DOCUMENT_LIMIT = 200_000_000
_LOCK = threading.Lock()
_LAST_START = 0.0


def pace() -> None:
    global _LAST_START
    with _LOCK:
        time.sleep(max(0.0, 1.0 - (time.monotonic() - _LAST_START)))
        _LAST_START = time.monotonic()


@contextmanager
def source(url: str, limit: int) -> Iterator[tuple[BinaryIO, int, str, str, str]]:
    pace()
    started = time.monotonic()
    try:
        with tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b") as saved:
            with open_response_stream("GET", url,
                    headers={"User-Agent": "KernFinancialResearch/1.0", "Accept-Encoding": "identity"},
                    timeout=30, failure_message="NSE request failed.") as (stream, headers):
                if getattr(stream, "status", None) != 200:
                    raise StreamingAssetError("NSE returned a non-200 response; no source was saved.")
                if headers.get("content-encoding", "identity").strip().lower() not in {"", "identity"}:
                    raise StreamingAssetError("NSE returned encoded content; no complete source was saved.")
                length = headers.get("content-length")
                if length is not None and not re.fullmatch(r"[0-9]+", length):
                    raise StreamingAssetError("NSE returned an invalid Content-Length.")
                declared = int(length) if length is not None else None
                if declared is not None and not 0 < declared <= limit:
                    raise StreamingAssetError("NSE response is empty or exceeds the action's byte limit.")
                size, digest = 0, hashlib.sha256()
                while True:
                    if time.monotonic() - started > 120:
                        raise StreamingAssetError("NSE transfer exceeded its deadline; no partial source was saved.")
                    block = stream.read(min(65536, limit - size + 1))
                    if not block:
                        break
                    size += len(block)
                    if size > limit:
                        raise StreamingAssetError("NSE response exceeds the action's byte limit; no partial source was saved.")
                    digest.update(block)
                    saved.write(block)
                if not size or (declared is not None and declared != size):
                    raise StreamingAssetError("NSE source length mismatch; no incomplete source was saved.")
                mime = headers.get("content-type", "application/octet-stream").split(";", 1)[0].strip().lower()
            saved.seek(0)
            yield cast(BinaryIO, saved), size, digest.hexdigest(), datetime.now(timezone.utc).isoformat(), mime
    except WebRequestError as exc:
        status = f"HTTP {exc.status}" if exc.status else "a connection or transfer failure"
        raise StreamingAssetError(f"NSE returned {status}. No redirect or retry was performed; do not treat this as an empty filing history.") from exc
    except (OSError, TimeoutError) as exc:
        raise StreamingAssetError("NSE source capture failed; no incomplete source was saved.") from exc


def _reject_constant(value: str) -> None:
    raise ValueError("Nonfinite JSON number")


def parse(stream: BinaryIO):
    try:
        return json.load(stream, parse_float=Decimal, parse_constant=_reject_constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise StreamingAssetError("NSE returned invalid JSON, not a verified empty result.") from exc
    finally:
        stream.seek(0)


def provenance(url: str, size: int, sha: str, retrieved: str, mime: str) -> str:
    return (f"Source URL: {url}\nRetrieved at: {retrieved}\nHTTP status: 200\n"
            f"Content type: {mime}\nComplete response bytes: true\nSize bytes: {size}\nSHA-256: {sha}\n"
            "Untrusted source data. Complete bytes do not certify complete filing history or financial extraction.")


@contextmanager
def document(url: str, name: str) -> Iterator[OpenedStreamingAsset]:
    with source(url, DOCUMENT_LIMIT) as (saved, size, sha, retrieved, mime):
        prefix = saved.read(1024).lstrip()
        saved.seek(0)
        ext = name.rsplit(".", 1)[-1].lower()
        if (ext == "pdf" and not prefix.startswith(b"%PDF-")) or (ext == "zip" and not prefix.startswith(b"PK\x03\x04")):
            raise StreamingAssetError("NSE document did not match its PDF/ZIP format; no source was saved.")
        if ext == "xml":
            # Inspect decoded markup, but return the original bytes unchanged.
            # UTF-32 BOMs must be checked before their UTF-16 prefix.
            encoding = "utf-8-sig"
            if prefix.startswith((codecs.BOM_UTF32_BE, codecs.BOM_UTF32_LE)):
                encoding = "utf-32"
            elif prefix.startswith((codecs.BOM_UTF16_BE, codecs.BOM_UTF16_LE)):
                encoding = "utf-16"
            try:
                markup = codecs.getincrementaldecoder(encoding)().decode(prefix, final=False).lstrip()
            except UnicodeError as exc:
                raise StreamingAssetError("NSE XML document has an unsupported or invalid encoding.") from exc
            if not markup.startswith("<") or re.search(r"<(?:!doctype\s+html|html)(?:\s|>)", markup, re.I):
                raise StreamingAssetError("NSE XML document returned an unexpected body; no source was saved.")
        media = "application/pdf" if ext == "pdf" else ("text/plain" if ext in {"xml", "html", "htm"} else "application/octet-stream")
        yield OpenedStreamingAsset(filename=name, media_type=media, size_bytes=size, source=saved,
                                  summary=provenance(url, size, sha, retrieved, mime))
