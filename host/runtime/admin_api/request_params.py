"""Small protocol helpers shared by admin API route modules."""

from __future__ import annotations

from http import HTTPStatus
import json

from host.runtime.agent_runtime import agent_activity
from host.runtime.admin_api.errors import ApiError


def one(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    if not values:
        return None
    if len(values) != 1:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"{key} must appear once")
    return values[0]


def clip_json_encoded_text(value: str, maximum: int) -> str:
    """Bound text's encoded JSON string, including escaped control bytes."""
    def encoded_size(text: str) -> int:
        return len(json.dumps(text).encode())

    if encoded_size(value) <= maximum:
        return value
    suffix = "\n… (truncated)"
    if encoded_size(suffix) > maximum:
        return agent_activity.clip_text(value, maximum)
    low, high = 0, len(value)
    while low < high:
        middle = (low + high + 1) // 2
        if encoded_size(value[:middle] + suffix) <= maximum:
            low = middle
        else:
            high = middle - 1
    return value[:low] + suffix
