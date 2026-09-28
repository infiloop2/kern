"""Operator-only browser controls, behind normal admin cookie + CSRF checks."""
from __future__ import annotations
from http import HTTPStatus
from typing import Any
from host.runtime.admin_api.errors import ApiError
from host.runtime.browser import client
from host.runtime.core import state


def control(operation: str, body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise ApiError(HTTPStatus.BAD_REQUEST, "Expected a browser request object.")
    if operation not in {"list", "disconnect", "cancel"} and "browser" not in state.enabled_tool_ids():
        raise ApiError(HTTPStatus.CONFLICT, "Enable Browser in Integrations first.")
    try:
        return client.request("/operator/" + operation, body)
    except client.BrowserError as exc:
        raise ApiError(HTTPStatus.CONFLICT, str(exc)) from exc
