"""Thin agent-facing proxy for host-owned conversation history."""

from __future__ import annotations

from http import HTTPStatus
from typing import Any

from host.runtime.workspace.host_api import WorkspaceError, call_admin_api


ROUTES = {
    "/agent/conversation-history/search": "/v1/conversation-history/search",
    "/agent/conversation-history/read": "/v1/conversation-history/read",
}


def route_agent(
    method: str,
    path: str,
    body: Any,
    query: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Forward the public request unchanged to its authoritative host API."""
    if query:
        raise WorkspaceError(
            HTTPStatus.BAD_REQUEST,
            "conversation history routes do not accept query parameters",
        )
    admin_path = ROUTES.get(path)
    if method != "POST" or admin_path is None:
        raise WorkspaceError(HTTPStatus.NOT_FOUND, "agent conversation route not found")
    return call_admin_api("POST", admin_path, body)
