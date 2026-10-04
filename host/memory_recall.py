"""Bounded natural-language recall context."""
from __future__ import annotations

import re
from typing import Any

from host import memory_recall_rules as rules
from host.agent_messages import AUTOMATED_TRIGGER_PREFIX


def strip_message_prefix(message: str) -> str:
    message = message.removeprefix(AUTOMATED_TRIGGER_PREFIX)
    return re.sub(rules.PEER_MESSAGE_PREFIX_PATTERN, "", message, count=1)


def bound_query(query: str) -> str:
    """Clip UTF-8 safely; callers append messages in current/newest-first order."""
    return query.encode("utf-8", errors="replace")[:rules.MAX_QUERY_BYTES].decode("utf-8", errors="ignore").strip()


# Only notices carrying task direction or an action outcome contribute to recall.
# Receipt/tool data, history transfers and memory notices must not feed it back.
RECALL_NOTICE_KINDS = frozenset({"agent_message", "scheduled_trigger", "approval_outcome"})


def context_message(event: dict[str, Any]) -> tuple[str, str] | None:
    payload = event.get("payload", {})
    event_type = event.get("event_type")
    if event_type not in {"thread.message", "thread.notice"}:
        return None
    text = payload.get("message", "")
    notice = payload.get("notice")
    if event_type == "thread.message" and payload.get("source") == "agent":
        return "Assistant", text
    if payload.get("source") != "user":
        return None
    kind = notice.get("kind") if notice else None
    if kind in (None, "operator") and event_type == "thread.message":
        return "User", text
    if kind not in RECALL_NOTICE_KINDS:
        return None
    if kind == "approval_outcome":
        # Full approval messages can contain tool results, JSON and logs.
        return "Approval outcome", notice["summary"]
    return ("Peer" if kind == "agent_message" else "Scheduled request"), strip_message_prefix(text)


def conversation_query(events: list[dict[str, Any]]) -> str:
    """Chronological events in; newest-first, separately bounded roles out."""
    users: list[str] = []
    supporting: list[str] = []
    for event in reversed(events):
        if event.get("event_type") == "thread.memory_cleared":
            break
        item = context_message(event)
        if item is None:
            continue
        role, message = item
        text = " ".join(message.split())
        if text:
            (users if role == "User" else supporting).append(f"{role}: {text}")
    def bucket(messages: list[str], maximum: int) -> str:
        return "\n\n".join(messages).encode("utf-8", errors="replace")[:maximum].decode("utf-8", errors="ignore").strip()
    return bound_query("\n\n".join(part for part in (
        bucket(users, rules.USER_CONTEXT_BYTES), bucket(supporting, rules.SUPPORT_CONTEXT_BYTES),
    ) if part))
