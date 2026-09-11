"""Bounded, request-only Streamable HTTP for Upwork.

This is transport plumbing, not an agent-callable MCP proxy. The owning package
pins the endpoint and constructs each reviewed tool name and argument object.
No roots, sampling, elicitation, resource fetching, redirects, or replay.
"""

from __future__ import annotations

import json
import http.client
import math
import re
import time
import uuid
from typing import BinaryIO, cast

from host.tools.json_types import JSONObject
from host.tools.shared.web import _request_bytes_and_headers, open_response_stream, WebRequestError
from host.tools.upwork.oauth import ENDPOINT, USER_AGENT

MAX_BYTES = 1024 * 1024
PROTOCOL = "2025-06-18"
SUPPORTED_PROTOCOLS = frozenset((PROTOCOL, "2025-03-26", "2025-11-25"))
FAILURE = "The MCP provider request failed."
INVALID = "The MCP provider returned an unsupported or invalid response."


def result_text(result: JSONObject) -> str:
    """Validate MCP content, returning text without forwarding other blocks."""
    content = result.get("content")
    if not isinstance(content, list) or not isinstance(result.get("isError", False), bool):
        raise RuntimeError(INVALID)
    if result.get("isError") is True:
        raise RuntimeError(FAILURE)
    text = []
    for row in content:
        if not isinstance(row, dict):
            raise RuntimeError(INVALID)
        kind = row.get("type")
        if kind == "text":
            if not isinstance(row.get("text"), str):
                raise RuntimeError(INVALID)
            text.append(cast(str, row["text"]))
        elif kind in ("image", "audio"):
            if not isinstance(row.get("data"), str) or not isinstance(row.get("mimeType"), str):
                raise RuntimeError(INVALID)
        elif kind == "resource_link":
            if not isinstance(row.get("name"), str) or not isinstance(row.get("uri"), str):
                raise RuntimeError(INVALID)
        elif kind == "resource":
            resource = row.get("resource")
            if not isinstance(resource, dict) or not isinstance(resource.get("uri"), str) or not (
                isinstance(resource.get("text"), str) or isinstance(resource.get("blob"), str)
            ):
                raise RuntimeError(INVALID)
        else:
            raise RuntimeError(INVALID)
    return "\n".join(text)


def _object(raw: str) -> JSONObject:
    def reject_constant(value: str) -> None:
        raise ValueError(INVALID)

    def pairs(rows):
        result = {}
        for key, value in rows:
            if key in result:
                raise ValueError(INVALID)
            result[key] = value
        return result

    try:
        value = json.loads(raw, parse_constant=reject_constant, object_pairs_hook=pairs)
        # Also rejects finite-looking literals that overflow into infinity.
        json.dumps(value, allow_nan=False)
    except (ValueError, RecursionError) as exc:
        raise RuntimeError(INVALID) from exc
    if not isinstance(value, dict):
        raise RuntimeError(INVALID)
    return cast(JSONObject, value)


def _result(raw: bytes, content_type: str, request_id: str) -> JSONObject:
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError(INVALID) from exc
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        raise RuntimeError(INVALID)
    message = _object(decoded)
    if message.get("jsonrpc") != "2.0":
        raise RuntimeError(INVALID)
    # Never perform server-initiated requests; the client advertises none.
    if "method" in message:
        raise RuntimeError("The MCP provider requested an unsupported client capability.")
    if message.get("id") != request_id:
        raise RuntimeError(INVALID)
    if "error" in message:
        # Provider messages/data can echo credentials or request content.
        raise RuntimeError(FAILURE)
    result = message.get("result")
    if not isinstance(result, dict):
        raise RuntimeError(INVALID)
    return cast(JSONObject, result)


def _read(stream: BinaryIO, size: int, deadline: float) -> bytes:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RuntimeError("The Upwork request deadline expired. Check Upwork before retrying a write.")
    if isinstance(stream, http.client.HTTPResponse) and stream.fp is not None:
        # urllib exposes the HTTPResponse; constrain each socket read to the
        # remaining wall-clock budget, including servers that trickle bytes.
        getattr(stream.fp.raw, "_sock").settimeout(remaining)
    chunk = getattr(stream, "read1", stream.read)(size)
    if time.monotonic() >= deadline:
        raise RuntimeError("The Upwork request deadline expired. Check Upwork before retrying a write.")
    return chunk


def _stream_result(stream: BinaryIO, request_id: str, deadline: float | None = None) -> JSONObject:
    """Read bounded SSE events and stop at the matching result, not at EOF."""
    if deadline is None:
        deadline = time.monotonic() + 30
    total = messages = 0
    line = bytearray()
    data: list[bytes] = []
    previous_cr = False
    while time.monotonic() < deadline:
        chunk = _read(stream, min(4096, MAX_BYTES + 1 - total), deadline)
        total += len(chunk)
        if total > MAX_BYTES or not chunk or time.monotonic() >= deadline:
            break
        for byte in chunk:
            if previous_cr and byte == 10:
                previous_cr = False
                continue
            previous_cr = byte == 13
            if byte not in (10, 13):
                line.append(byte)
                continue
            if line:
                if line.startswith(b"data:"):
                    data.append(bytes(line[5:]).removeprefix(b" "))
                line.clear()
            elif data:
                raw = b"\n".join(data)
                data.clear()
                messages += 1
                if messages > 100:
                    raise RuntimeError(INVALID)
                try:
                    message = _object(raw.decode("utf-8"))
                except UnicodeDecodeError:
                    raise RuntimeError(INVALID) from None
                if message.get("jsonrpc") == "2.0" and "method" in message and "id" not in message:
                    continue
                return _result(raw, "application/json", request_id)
    raise RuntimeError(INVALID)


class MCPConnection:
    """One short-lived session; no session identifier survives the tool call."""

    def __init__(self, token: str, *, deadline: float | None = None) -> None:
        self.deadline = deadline if deadline is not None else time.monotonic() + 210
        self.headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }

    def initialize(self) -> None:
        result = self.request("initialize", {
            "protocolVersion": PROTOCOL,
            "capabilities": {},
            "clientInfo": {"name": "Kern", "version": "1.0"},
        })
        version = result.get("protocolVersion")
        if version not in SUPPORTED_PROTOCOLS:
            raise RuntimeError("The MCP provider requires an unsupported protocol version.")
        self.headers["MCP-Protocol-Version"] = str(version)
        self.request("notifications/initialized", {}, notification=True)

    def request(self, method: str, params: JSONObject, *, notification: bool = False) -> JSONObject:
        deadline = min(self.deadline, time.monotonic() + 30)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("The Upwork request deadline expired. Check Upwork before retrying a write.")
        request_id = uuid.uuid4().hex
        payload: JSONObject = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notification:
            payload["id"] = request_id
        with open_response_stream(
            "POST", ENDPOINT, headers=self.headers,
            data=json.dumps(payload, separators=(",", ":"), allow_nan=False).encode(),
            failure_message=FAILURE, timeout=max(1, math.ceil(remaining)),
        ) as (stream, headers):
            session_id = headers.get("mcp-session-id")
            if session_id is not None:
                if not re.fullmatch(r"[\x21-\x7e]{1,1024}", session_id):
                    raise RuntimeError(INVALID)
                if method != "initialize" and session_id != self.headers.get("Mcp-Session-Id"):
                    raise RuntimeError(INVALID)
                self.headers["Mcp-Session-Id"] = session_id
            if notification:
                if _read(stream, 1, deadline):
                    raise RuntimeError(INVALID)
                return {}
            content_type = headers.get("content-type", "")
            if content_type.split(";", 1)[0].strip().lower() == "text/event-stream":
                return _stream_result(stream, request_id, deadline)
            raw = bytearray()
            while True:
                chunk = _read(stream, min(4096, MAX_BYTES + 1 - len(raw)), deadline)
                if not chunk:
                    break
                raw.extend(chunk)
                if len(raw) > MAX_BYTES:
                    raise RuntimeError(INVALID)
            return _result(bytes(raw), content_type, request_id)

    def close(self) -> None:
        if "Mcp-Session-Id" not in self.headers:
            return
        headers = dict(self.headers)
        self.headers.pop("Mcp-Session-Id")
        try:
            _request_bytes_and_headers("DELETE", ENDPOINT, headers=headers,
                failure_message=FAILURE, max_bytes=4096, timeout=5)
        except (WebRequestError, OSError, RuntimeError):
            # Termination is best effort (servers may return 405). Never turn
            # a completed operation into a retry because cleanup failed.
            pass

    def call(self, name: str, arguments: JSONObject) -> JSONObject:
        result = self.request("tools/call", {"name": name, "arguments": arguments})
        result_text(result)
        return result
