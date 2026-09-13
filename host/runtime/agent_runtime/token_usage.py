"""Four disjoint token buckets, accumulated within one orchestrator turn.

Provider IDs deduplicate repeated/updated measurements in memory. Only the
turn totals are durable; analytics is best-effort, not a billing ledger.
"""
from __future__ import annotations

from typing import Any

FIELDS = ("input_tokens", "cached_input_tokens", "cache_write_tokens", "output_tokens")


def _count(value: Any) -> int | None:
    return value if type(value) is int and 0 <= value <= 10**15 else None


def record(source_id: Any, raw: Any, provider: str) -> dict[str, Any] | None:
    if not isinstance(source_id, str) or not source_id or not isinstance(raw, dict):
        return None
    keys = {
        "codex": ("inputTokens", "cachedInputTokens", "cacheWriteInputTokens", "outputTokens"),
        "claude": ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens"),
        "grok": ("inputTokens", "cachedReadTokens", "cacheCreationTokens", "outputTokens"),
        "hermes": ("input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens"),
    }[provider]
    values = [_count(raw.get(key)) for key in keys]
    # Codex's protocol defines an omitted cache-write count as zero.
    if provider == "codex" and keys[2] not in raw:
        values[2] = 0
    # Codex/Grok include cached tokens in input; Claude/Hermes split them.
    if provider in {"codex", "grok"}:
        values[0] = max(0, (values[0] or 0) - (values[1] or 0) - (values[2] or 0)) if all(v is not None for v in values[:3]) else None
    if all(value is None for value in values):
        return None
    return {"type": "token_usage", "source_id": source_id, "usage": dict(zip(FIELDS, values))}


class TurnUsage:
    def __init__(self) -> None:
        self.samples: dict[str, tuple[int | None, ...]] = {}

    def add(self, message: dict[str, Any]) -> dict[str, int | None] | None:
        key, raw = message.get("source_id"), message.get("usage")
        if not isinstance(key, str) or not key or not isinstance(raw, dict):
            return None
        values = tuple(_count(raw.get(field)) for field in FIELDS)
        if all(v is None for v in values) or self.samples.get(key) == values:
            return None
        self.samples[key] = values
        return {
            field: None if any(row[i] is None for row in self.samples.values())
            else sum(row[i] or 0 for row in self.samples.values())
            for i, field in enumerate(FIELDS)
        }
