"""Shared conversation input for admission, mid-turn recall and task titles."""
from __future__ import annotations

from typing import Any

from host.memory_recall import conversation_query
from host.runtime.core import host_errors, pgclient, state


def load_query(thread_id: str, incoming: dict[str, Any] | None = None) -> str:
    try:
        history = state.recall_context_events(thread_id)
    except (pgclient.Error, OSError) as exc:
        host_errors.report_warning("memory.context", exc, kind="memory_recall_degraded")
        history = []
    return conversation_query([*history, incoming] if incoming else history)
