"""Redact secret-shaped and numbered machine tokens from inference content."""

from __future__ import annotations

import re
from typing import Any

REDACTED = "<redacted>"
_SECRET_RE = re.compile(
    r"(?i)(?<!\w)(?:bearer\s+\S+|"
    r"(?P<field>[\"']?[A-Za-z0-9_-]*(?:authorization|password|passwd|api[ _-]*key|private[ _-]*key|token|credential|secret)[A-Za-z0-9_-]*[\"']?\s*[:=]\s*)"
    r"(?P<field_value>\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\n,}]+)|"
    r"(?:sk-(?:proj-)?|gh[pousr]_|apify_api_)\S+)"
)
# Eleven characters is the first length the operator wants hidden.
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{11,}")
_SECRET_KEY_RE = re.compile(r"(?i)password|passwd|secret|token|credential|authorization|api[_ -]?key|private[_ -]?key")


def redact_text(value: str) -> str:
    def replace_secret(match: re.Match[str]) -> str:
        field = match.group("field")
        if field is None:
            return REDACTED
        raw_value = match.group("field_value")
        quote = raw_value[0] if raw_value[0] in "\"'" and raw_value[-1] == raw_value[0] else ""
        return f"{field}{quote}{REDACTED}{quote}"

    value = _SECRET_RE.sub(replace_secret, value)

    def replace(match: re.Match[str]) -> str:
        token = match.group()
        return REDACTED if any(char.isdigit() for char in token) else token

    return _TOKEN_RE.sub(replace, value)


def redact_content(value: Any) -> Any:
    """Return a redacted copy of JSON content without dropping hidden fields."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, int) and not isinstance(value, bool) and len(str(abs(value))) > 10:
        return REDACTED
    if isinstance(value, list):
        return [redact_content(item) for item in value]
    if isinstance(value, dict):
        result: dict[str, list[Any]] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("Inference content keys must be strings")
            safe_key = redact_text(key)
            safe_value = (
                REDACTED if _SECRET_KEY_RE.search(key)
                else redact_content(item)
            )
            result.setdefault(safe_key, []).append(safe_value)
        return {key: items[0] if len(items) == 1 else items for key, items in result.items()}
    return value
