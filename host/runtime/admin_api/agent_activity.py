"""Provider-independent activity records shown in Agent Chat."""

from __future__ import annotations

import json
from typing import Any

ACTIVITY_TEXT_BYTES = 256 * 1024
ACTIVITY_SHORT_TEXT_BYTES = 2 * 1024
TRUNCATION_SUFFIX = "\n… (truncated)"
ACTIVITY_KINDS = frozenset({
    "reasoning",
    "plan",
    "command",
    "file_change",
    "tool",
    "agent",
    "search",
    "image",
    "wait",
    "status",
})
ACTIVITY_PHASES = frozenset({"started", "completed"})


def clean_text(value: Any) -> str:
    """Return provider text safe for UTF-8 JSON and PostgreSQL JSONB."""
    try:
        text = value if isinstance(value, str) else "" if value is None else str(value)
    except Exception:
        return ""
    # PostgreSQL JSONB rejects \u0000, and provider JSON can contain lone
    # surrogates even though they cannot be encoded as UTF-8. Keep the event
    # readable instead of letting malformed progress fail the whole turn.
    return (
        text.replace("\x00", "\\0")
        .encode("utf-8", errors="replace")
        .decode("utf-8")
    )


def clip_text(value: Any, maximum: int = ACTIVITY_TEXT_BYTES) -> str:
    """Return bounded UTF-8 text and always mark a clipped value explicitly."""
    text = clean_text(value)
    encoded = text.encode()
    if len(encoded) <= maximum:
        return text
    suffix = TRUNCATION_SUFFIX.encode()
    if maximum <= len(suffix):
        return suffix[:maximum].decode(errors="ignore")
    prefix = encoded[: maximum - len(suffix)].decode(errors="ignore")
    return prefix + TRUNCATION_SUFFIX


def json_text(value: Any, maximum: int = ACTIVITY_TEXT_BYTES) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    except Exception:
        text = clean_text(value)
    return clip_text(text, maximum)


def activity(
    provider: str,
    activity_id: str,
    kind: str,
    phase: str,
    title: str,
    *,
    detail: Any = None,
    output: Any = None,
    status: str | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "provider": provider,
        "activity_id": clip_text(activity_id, ACTIVITY_SHORT_TEXT_BYTES),
        "kind": clip_text(kind, 64),
        "phase": clip_text(phase, 64),
        "title": clip_text(title, ACTIVITY_SHORT_TEXT_BYTES),
    }
    if detail not in (None, ""):
        value["detail"] = clip_text(detail)
    if output not in (None, ""):
        value["output"] = clip_text(output)
    if status:
        value["status"] = clip_text(status, 128)
    return value


def normalize_record(value: Any) -> dict[str, Any] | None:
    """Validate and sanitize one provider activity before persistence."""
    if not isinstance(value, dict):
        return None
    provider = clean_text(value.get("provider")).strip()
    activity_id = clean_text(value.get("activity_id")).strip()
    kind = clean_text(value.get("kind")).strip()
    phase = clean_text(value.get("phase")).strip()
    title = clean_text(value.get("title")).strip()
    if (
        not provider
        or not activity_id
        or kind not in ACTIVITY_KINDS
        or phase not in ACTIVITY_PHASES
        or not title
    ):
        return None
    normalized = activity(
        provider,
        activity_id,
        kind,
        phase,
        title,
        detail=value.get("detail"),
        output=value.get("output"),
        status=value.get("status"),
    )
    if value.get("append_output") is True:
        normalized["append_output"] = True
    return normalized
