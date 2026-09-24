"""Authenticated browser proxy to the fixed workspace backend."""

from __future__ import annotations

import http.client
from http import HTTPStatus
import json
import os
import socket
from typing import Any
from urllib.parse import urlencode

from host.constants import (
    MAX_REQUEST_BODY_BYTES,
    MAX_WORKSPACE_RESPONSE_BODY_BYTES,
    WORKSPACE_ADMIN_API_TIMEOUT_SECONDS,
    WORKSPACE_BROWSER_SOCKET_PATH,
)
from host.runtime.admin_api.errors import ApiError
from host.runtime.core import host_errors


PROXY_TIMEOUT_SECONDS = WORKSPACE_ADMIN_API_TIMEOUT_SECONDS + 10
RECALL_TIMEOUT_SECONDS = 3
BROWSER_SOCKET = os.environ.get("KERN_WORKSPACE_BROWSER_SOCKET", WORKSPACE_BROWSER_SOCKET_PATH)
ROUTE_PREFIXES = {
    "/v1/workspace/getting-started": "/getting-started",
    "/v1/workspace/chat": "/chat",
    "/v1/workspace/web-apps": "/apps",
    "/v1/workspace/memory": "/memory",
    "/v1/workspace/schedules": "/schedules",
}


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: float) -> None:
        super().__init__("kern-workspace", timeout=timeout)
        self._socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self._socket_path)
        except OSError:
            sock.close()
            raise
        self.sock = sock


def send_message(thread_id: str, message: str) -> dict[str, Any]:
    """Deliver a Kern notice through Workspace's ordinary destination checks."""
    return _proxy("POST", f"/messages/{thread_id}", {}, {"message": message})


def recall_memory(
    thread_id: str,
    message: str,
) -> dict[str, Any]:
    """Fetch Workspace-owned context for one newly admitted model turn."""
    return _proxy(
        "POST",
        "/memory/recall",
        {},
        {
            "thread_id": thread_id,
            "message": message,
        },
        timeout_seconds=RECALL_TIMEOUT_SECONDS,
    )


def route_request(
    method: str,
    path: str,
    query: dict[str, list[str]],
    body: Any,
) -> Any:
    for prefix, backend_prefix in ROUTE_PREFIXES.items():
        if path == prefix or path.startswith(prefix + "/"):
            suffix = path.removeprefix(prefix)
            return _proxy(method, backend_prefix + suffix, query, body)
    raise ApiError(HTTPStatus.NOT_FOUND, "workspace route not found")


def _proxy(
    method: str,
    path: str,
    query: dict[str, list[str]],
    body: Any,
    *,
    timeout_seconds: float = PROXY_TIMEOUT_SECONDS,
) -> Any:
    encoded = None if body is None else json.dumps(body, sort_keys=True).encode()
    headers: dict[str, str] = {}
    if encoded is not None:
        headers["Content-Type"] = "application/json"
        headers["Content-Length"] = str(len(encoded))
    target = path
    if query:
        target += "?" + urlencode(
            [(key, value) for key, values in query.items() for value in values]
        )
    conn: http.client.HTTPConnection | None = None
    try:
        conn = _UnixHTTPConnection(BROWSER_SOCKET, timeout=timeout_seconds)
        conn.request(method, target, body=encoded, headers=headers)
        response = conn.getresponse()
        raw = response.read(MAX_WORKSPACE_RESPONSE_BODY_BYTES + 1)
    except TimeoutError as exc:
        raise ApiError(
            HTTPStatus.GATEWAY_TIMEOUT, "workspaces backend timed out"
        ) from exc
    except (OSError, http.client.HTTPException) as exc:
        raise ApiError(
            HTTPStatus.BAD_GATEWAY, "workspaces backend unavailable"
        ) from exc
    finally:
        if conn is not None:
            conn.close()
    if len(raw) > MAX_WORKSPACE_RESPONSE_BODY_BYTES:
        host_errors.report_warning(
            "admin_api.workspace_proxy",
            "Workspace service returned an oversized response.",
            context={"method": method, "route": path, "http_status": response.status},
        )
        raise ApiError(HTTPStatus.BAD_GATEWAY, "Workspace service response too large")
    try:
        data = json.loads(raw.decode() or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        host_errors.report_warning(
            "admin_api.workspace_proxy",
            exc,
            context={"method": method, "route": path, "http_status": response.status},
        )
        raise ApiError(
            HTTPStatus.BAD_GATEWAY, "Workspace service returned invalid JSON"
        ) from exc
    if response.status >= 400:
        message = data.get("error", {}).get("message") if isinstance(data, dict) else None
        raise ApiError(
            HTTPStatus(response.status), message or "Workspace service request failed"
        )
    return data
