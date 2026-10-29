"""Shared conversation input for admission, mid-turn recall and task titles."""
from __future__ import annotations

from collections import OrderedDict
import hashlib
import json
import threading
import time
from typing import Any

from host.memory_recall import conversation_query
from host.runtime.core import host_errors, pgclient, state

MEMORY_BODY_TTL_SECONDS = 3600


class MemoryDelivery:
    """Bounded one-hour body-delivery cache, not a provider context inventory.

    Each thread retains only page IDs, delivery keys and monotonic times.
    Self-memory keys include its content and note IDs so note-index changes are fresh
    even when the stored briefing revision has not changed.
    Explicit context clearing, restart and eviction favor sending again.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._threads: OrderedDict[str, dict[str, tuple[int, float]]] = OrderedDict()

    @staticmethod
    def key(page: dict[str, Any]) -> tuple[str, int]:
        version = page['revision']
        if page.get('scope') == 'self':
            # This transient digest is not a stored revision or prompt field.
            payload = json.dumps([version, page['content'], page.get('saved_notes', [])]).encode('utf-8')
            version = -int.from_bytes(hashlib.sha256(payload).digest(), 'big') - 1
        return page['page_id'], version

    def select(
        self, thread_id: str, pages: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], str]:
        now = time.monotonic()
        with self._lock:
            known = self._threads.get(thread_id, {})
            selected, decisions = [], []
            for page in pages:
                page_id, version = self.key(page)
                last = known.get(page_id)
                elapsed = now - last[1] if last is not None else None
                recent = (last is not None and last[0] == version
                          and elapsed is not None and 0 <= elapsed < MEMORY_BODY_TTL_SECONDS)
                reason = ('new' if last is None else 'changed' if last[0] != version
                          else 'recent' if recent else 'expired')
                if not recent:
                    selected.append(page)
                decisions.append({
                    'page_id': page['page_id'], 'revision': page['revision'],
                    'decision': 'suppressed' if recent else 'prepared', 'reason': reason,
                    'seconds_since_injection': round(elapsed, 3) if elapsed is not None else None,
                })
        detail = {
            'delivery_status': 'pending_readiness', 'ttl_seconds': MEMORY_BODY_TTL_SECONDS,
            'selected_count': len(pages), 'prepared_injection_count': len(selected),
            'suppressed_count': len(pages) - len(selected), 'pages': decisions,
        }
        return selected, 'Memory injection preparation: ' + json.dumps(detail, separators=(',', ':')) + '\n'

    def delivered(
        self, thread_id: str, pages: tuple[tuple[str, int], ...],
    ) -> None:
        now = time.monotonic()
        if not pages:
            return
        with self._lock:
            known = {page: last for page, last in self._threads.get(thread_id, {}).items()
                     if 0 <= now - last[1] < MEMORY_BODY_TTL_SECONDS}
            for page_id, version in pages:
                known.pop(page_id, None)
                known[page_id] = (version, now)
            while len(known) > 64:
                del known[next(iter(known))]
            self._threads[thread_id] = known
            self._threads.move_to_end(thread_id)
            while len(self._threads) > 128:
                self._threads.popitem(last=False)

    def clear(self, thread_id: str) -> None:
        with self._lock:
            self._threads.pop(thread_id, None)


memory_delivery = MemoryDelivery()


def load_query(thread_id: str, incoming: dict[str, Any] | None = None) -> str:
    try:
        history = state.recall_context_events(thread_id)
    except (pgclient.Error, OSError) as exc:
        host_errors.report_warning("memory.context", exc, kind="memory_recall_degraded")
        history = []
    return conversation_query([*history, incoming] if incoming else history)
