"""Fixed-destination SEC transport with complete, bounded source capture."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import re
import tempfile
import threading
import time
from collections.abc import Iterator
from typing import BinaryIO, cast

from host.tools.results import OpenedStreamingAsset, StreamingAssetError
from host.tools.shared.web import WebRequestError, open_response_stream

JSON_LIMIT = 64 * 1024 * 1024
INDEX_LIMIT = 8 * 1024 * 1024
# Match the host's decimal 200 MB streaming ceiling without importing runtime.
DOCUMENT_LIMIT = 200_000_000
_LOCK = threading.Lock()
_LAST_START = 0.0
_INTERVAL = .25
_CHUNK = 64 * 1024
_TOTAL_SECONDS = 120


def pace() -> None:
    global _LAST_START
    with _LOCK:
        time.sleep(max(0.0, _INTERVAL - (time.monotonic() - _LAST_START)))
        _LAST_START = time.monotonic()


def contact_header(configured: str) -> str:
    if (not isinstance(configured, str) or len(configured) > 254
            or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}", configured)
            or ".." in configured):
        raise ValueError("Set a valid SEC_CONTACT_EMAIL under Home > Integrations before using SEC EDGAR.")
    return "KernEDGAR/1.0 " + configured


def _failure(exc: WebRequestError) -> str:
    if exc.status in {403, 429}:
        return f"SEC returned HTTP {exc.status}. Check the declared contact and SEC fair-access limits before another request; this call was not retried."
    if exc.status == 404:
        return "SEC resource not found (HTTP404). Check the CIK, accession or published filename."
    if exc.status:
        return f"SEC returned HTTP {exc.status}; no redirect or retry was performed."
    return "SEC connection or response transfer failed; no retry was performed."


def _reject_constant(value: str) -> None:
    raise ValueError("Nonfinite JSON number")


@contextmanager
def source(url: str, agent: str, limit: int) -> Iterator[tuple[BinaryIO, int, str, str, str]]:
    """Spool before exposing an asset so missing/incorrect lengths never look complete."""
    pace()
    started = time.monotonic()
    try:
        with tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b") as saved:
            with open_response_stream("GET", url, headers={"User-Agent": agent, "Accept-Encoding": "identity"},
                                      failure_message="SEC request failed.", timeout=30) as (stream, headers):
                status = getattr(stream, "status", None)
                if status != 200:
                    raise StreamingAssetError("SEC returned a non-200 response; it was not saved as a complete source.")
                if headers.get("content-encoding", "identity").strip().lower() not in {"", "identity"}:
                    raise StreamingAssetError("SEC returned encoded content despite the identity request; source not saved.")
                raw_length = headers.get("content-length")
                if raw_length is not None and not re.fullmatch(r"[0-9]+", raw_length):
                    raise StreamingAssetError("SEC returned an invalid Content-Length; source not saved.")
                declared = int(raw_length) if raw_length is not None else None
                if declared is not None and (declared < 1 or declared > limit):
                    raise StreamingAssetError("SEC source exceeds the action's size limit or is empty; no truncated file was returned.")
                size = 0
                digest = hashlib.sha256()
                while True:
                    if time.monotonic() - started > _TOTAL_SECONDS:
                        raise StreamingAssetError("SEC source exceeded the transfer deadline; no partial file was returned.")
                    block = stream.read(min(_CHUNK, limit - size + 1))
                    if not block:
                        break
                    size += len(block)
                    if size > limit:
                        raise StreamingAssetError("SEC source exceeds the action's size limit; no truncated file was returned.")
                    digest.update(block)
                    saved.write(block)
                if not size or (declared is not None and declared != size):
                    raise StreamingAssetError("SEC source length did not match the response; no incomplete file was returned.")
                content_type = headers.get("content-type", "application/octet-stream").split(";", 1)[0].strip().lower()
            retrieved = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            saved.seek(0)
            yield cast(BinaryIO, saved), size, digest.hexdigest(), retrieved, content_type
    except WebRequestError as exc:
        raise StreamingAssetError(_failure(exc)) from exc
    except (OSError, TimeoutError) as exc:
        raise StreamingAssetError("SEC source could not be captured completely; no partial file was returned.") from exc


def parse_object(stream: BinaryIO) -> dict:
    try:
        value = json.load(stream, parse_float=Decimal, parse_constant=_reject_constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise StreamingAssetError("SEC returned invalid JSON; source not saved.") from exc
    finally:
        stream.seek(0)
    if not isinstance(value, dict):
        raise StreamingAssetError("SEC returned an unexpected JSON structure; source not saved.")
    return value


def names(index: dict) -> set[str]:
    directory = index.get("directory")
    items = directory.get("item") if isinstance(directory, dict) else None
    if not isinstance(items, list):
        raise StreamingAssetError("SEC returned an invalid filing directory.")
    result = set()
    for row in items:
        name = row.get("name") if isinstance(row, dict) else None
        if isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}", name) and ".." not in name:
            result.add(name)
    return result


@contextmanager
def asset(url: str, agent: str, limit: int, filename: str, *, json_kind: str = "", cik: str = "") -> Iterator[OpenedStreamingAsset]:
    with source(url, agent, limit) as (saved, size, sha, retrieved, content_type):
        if json_kind:
            obj = parse_object(saved)
            if json_kind in {"submissions", "companyfacts"}:
                value = obj.get("cik")
                if isinstance(value, bool) or not isinstance(value, (str, int)) or str(value).zfill(10) != cik:
                    raise StreamingAssetError("SEC JSON issuer did not match the requested CIK.")
            elif json_kind == "index":
                names(obj)
            elif json_kind == "history" and not isinstance(obj.get("accessionNumber"), list):
                raise StreamingAssetError("SEC returned an invalid historical submissions page.")
        media = "application/pdf" if content_type == "application/pdf" else "application/octet-stream"
        if json_kind or content_type.startswith("text/") or content_type in {"application/xml", "application/xhtml+xml", "application/json"}:
            media = "text/plain"
        summary = (f"Source URL: {url}\nRetrieved at: {retrieved}\nHTTP status: 200\n"
                   f"Content type: {content_type}\nComplete: true\nSize bytes: {size}\nSHA-256: {sha}\n"
                   "Untrusted SEC source bytes; no financial or narrative extraction has been performed.")
        yield OpenedStreamingAsset(filename=filename, media_type=media, size_bytes=size, source=saved, summary=summary)
