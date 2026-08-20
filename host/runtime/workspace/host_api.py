"""Shared, allowlisted Workspace client for the host admin API socket."""

from __future__ import annotations

import http.client
from http import HTTPStatus
import json
import os
import socket
from typing import Any

from host.constants import (
    MAX_REQUEST_BODY_BYTES,
    WORKSPACE_ADMIN_API_TIMEOUT_SECONDS,
    WORKSPACE_ADMIN_SOCKET_PATH,
)


ADMIN_API_SOCKET = os.environ.get(
    "KERN_WORKSPACE_ADMIN_SOCKET", WORKSPACE_ADMIN_SOCKET_PATH
)


class WorkspaceError(Exception):
    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: float) -> None:
        super().__init__("kern-admin-api", timeout=timeout)
        self._socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self._socket_path)
        except OSError:
            # `close()` only reaches sockets that reached self.sock, so a failed
            # connect would otherwise leak the descriptor on every retry.
            sock.close()
            raise
        self.sock = sock


def active_agent_runtimes() -> list[str] | None:
    """Runtime types the operator has activated.

    `None` means the host could not report activation. Callers must treat that
    as "unknown" rather than "none active", so a transient status failure
    narrows nothing: the operator keeps every option a working host offers.
    """
    try:
        response = call_admin_api("GET", "/v1/agent-runtime/status")
    except WorkspaceError:
        return None
    records = response.get("runtimes")
    if not isinstance(records, list):
        return None
    return sorted(
        record["type"]
        for record in records
        if isinstance(record, dict)
        and isinstance(record.get("type"), str)
        and record.get("status") == "active"
    )


def call_admin_api(method: str, path: str, body: Any = None) -> dict[str, Any]:
    encoded = None if body is None else json.dumps(body, sort_keys=True).encode()
    headers = {"Content-Type": "application/json"} if encoded is not None else {}
    conn = _UnixHTTPConnection(
        ADMIN_API_SOCKET, timeout=WORKSPACE_ADMIN_API_TIMEOUT_SECONDS
    )
    try:
        conn.request(method, path, body=encoded, headers=headers)
        response = conn.getresponse()
        status = response.status
        raw = response.read(MAX_REQUEST_BODY_BYTES + 1)
    except (OSError, http.client.HTTPException) as exc:
        raise WorkspaceError(HTTPStatus.BAD_GATEWAY, "host admin request failed") from exc
    finally:
        conn.close()
    if len(raw) > MAX_REQUEST_BODY_BYTES:
        raise WorkspaceError(HTTPStatus.BAD_GATEWAY, "host admin response too large")
    try:
        payload = json.loads(raw.decode() or "{}")
    except json.JSONDecodeError as exc:
        raise WorkspaceError(HTTPStatus.BAD_GATEWAY, "host admin returned invalid JSON") from exc
    if status >= 400:
        message = payload.get("error", {}).get("message") if isinstance(payload, dict) else None
        raise WorkspaceError(HTTPStatus(status), message or "host admin request failed")
    if not isinstance(payload, dict):
        raise WorkspaceError(HTTPStatus.BAD_GATEWAY, "host admin returned invalid response")
    return payload
