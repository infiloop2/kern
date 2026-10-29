"""Shared integer query validation for operator API routes.

Uses int() parsing and preserves the admin API's bounds and errors.
Single-value extraction lives in request_params; cursor codecs stay
with their route domains and Workspace validators remain separate.
"""

from __future__ import annotations

from http import HTTPStatus

from host.runtime.admin_api.errors import ApiError
from host.runtime.admin_api.request_params import one as _one
from host.runtime.core import state


def _optional_non_negative_int(query: dict[str, list[str]], key: str) -> int | None:
    value = _one(query, key)
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"{key} must be an integer") from exc
    if parsed < 0:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"{key} must be non-negative")
    return parsed


def _optional_bounded_positive_query_int(
    query: dict[str, list[str]],
    key: str,
    maximum: int,
) -> int | None:
    value = _one(query, key)
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"{key} must be an integer") from exc
    if parsed < 1:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"{key} must be positive")
    if parsed > maximum:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"{key} must be at most {maximum}")
    return parsed


def _event_page_limit(query: dict[str, list[str]]) -> int:
    value = _one(query, "limit")
    if value is None:
        return state.EVENT_PAGE_LIMIT
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ApiError(HTTPStatus.BAD_REQUEST, "limit must be an integer") from exc
    if parsed < 1:
        raise ApiError(HTTPStatus.BAD_REQUEST, "limit must be positive")
    if parsed > state.EVENT_PAGE_LIMIT:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"limit must be at most {state.EVENT_PAGE_LIMIT}")
    return parsed


def _reject_query_keys(query: dict[str, list[str]], allowed: frozenset[str], label: str) -> None:
    unexpected = sorted(set(query) - allowed)
    if unexpected:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"unsupported {label} query parameter: {unexpected[0]}")

