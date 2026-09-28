"""Bounded natural-language recall context."""
from __future__ import annotations

import re

from host import memory_recall_rules as rules
from host.agent_scripts import AUTOMATED_TRIGGER_PREFIX, LEGACY_AUTOMATED_TRIGGER_PREFIX


def strip_message_prefix(message: str) -> str:
    for prefix in (AUTOMATED_TRIGGER_PREFIX, LEGACY_AUTOMATED_TRIGGER_PREFIX):
        message = message.removeprefix(prefix)
    return re.sub(rules.PEER_MESSAGE_PREFIX_PATTERN, "", message, count=1)


def task_query(message: str) -> str:
    """One message per paragraph, preserving wording, case and negation."""
    return bound_query(" ".join(strip_message_prefix(message).split()))


def bound_query(query: str) -> str:
    """Clip UTF-8 safely; callers append messages in current/newest-first order."""
    return query.encode("utf-8", errors="replace")[:rules.MAX_QUERY_BYTES].decode("utf-8", errors="ignore").strip()
