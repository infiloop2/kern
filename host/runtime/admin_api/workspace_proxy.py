"""Authenticated browser proxy to the fixed workspace backend."""

from __future__ import annotations

import http.client
from http import HTTPStatus
import json
from typing import Any
from urllib.parse import urlencode

from host.constants import (
    LOOPBACK,
    MAX_REQUEST_BODY_BYTES,
    MAX_WORKSPACE_RESPONSE_BODY_BYTES,
    WORKSPACE_ADMIN_API_TIMEOUT_SECONDS,
    WORKSPACE_PORT,
)
from host.runtime.admin_api.errors import ApiError
from host.runtime.core import host_errors


PROXY_TIMEOUT_SECONDS = WORKSPACE_ADMIN_API_TIMEOUT_SECONDS + 10
ROUTE_PREFIXES = {
    "/v1/workspace/chat": "/chat",
    "/v1/workspace/web-apps": "/apps",
    "/v1/workspace/memory": "/memory",
    "/v1/workspace/schedules": "/schedules",
}


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
        conn = http.client.HTTPConnection(
            LOOPBACK, WORKSPACE_PORT, timeout=PROXY_TIMEOUT_SECONDS
        )
        conn.request(method, target, body=encoded, headers=headers)
        response = conn.getresponse()
        raw = response.read(MAX_WORKSPACE_RESPONSE_BODY_BYTES + 1)
    except OSError as exc:
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
