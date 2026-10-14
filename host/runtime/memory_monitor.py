"""Best-effort mid-turn memory suggestions. One bounded, serial host worker."""
from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
import threading
import time
from typing import Any

from host.memory_recall import context_message
from host.runtime import memory_context, swarm_annotations
from host.runtime.admin_api import workspace_proxy
from host.runtime.agent_runtime.agent_activity import clip_text
from host.runtime.core import host_errors, state
from host.runtime.host_inference import client

MAX_TURNS = 128
MAX_PAGES = 64
SUGGESTION_INTERVAL = 60.0
SELF_MEMORY_INTERVAL = 300.0
SUGGESTION_EVENTS = 5
SELF_MEMORY_EVENTS = 25
# Global spend/rate bound, in addition to per-turn intervals.
CHECK_INTERVAL = 10.0

Page = tuple[str, int, str]
Key = tuple[str, int]


def page_summary(page: dict[str, Any]) -> Page:
    return page['page_id'], page['revision'], clip_text(page['description'], 256)


@dataclass
class Context:
    last_query: str
    deliver: Callable[[list[Page]], bool]
    known: dict[str, int]
    deliver_self: Callable[[dict[str, Any]], bool]
    next_suggestion: float
    next_self: float
    update_task: Callable[[str, Callable[[], bool]], None] | None = None
    generation: int = 0
    suggestion_events: int = 0
    self_events: int = 0

    def due_at(self) -> float:
        return min(self.next_suggestion if self.suggestion_events >= SUGGESTION_EVENTS else float('inf'),
                   self.next_self if self.self_events >= SELF_MEMORY_EVENTS else float('inf'))


class Monitor:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.turns: dict[Key, Context] = {}
        self.next_check = 0.0
        self.stats: Counter[str] = Counter()

    def seed(self, key: Key, query: str, pages: list[dict[str, Any]],
             deliver: Callable[[list[Page]], bool],
             deliver_self: Callable[[dict[str, Any]], bool],
             update_task: Callable[[str, Callable[[], bool]], None] | None = None) -> None:
        with self.lock:
            if len(self.turns) >= MAX_TURNS:
                self.stats['capacity_skips'] += 1
                return
            known = {p['page_id']: p['revision'] for p in pages[:MAX_PAGES]
                     if isinstance(p.get('page_id'), str) and isinstance(p.get('revision'), int)}
            now = time.monotonic()
            self.turns[key] = Context(query, deliver, known, deliver_self,
                                      now + SUGGESTION_INTERVAL, now + SELF_MEMORY_INTERVAL, update_task)
        self.wake.set()

    def finish(self, key: Key) -> None:
        with self.lock:
            self.turns.pop(key, None)
        self.wake.set()

    def observe(self, key: Key, message: str | dict[str, Any], *, incoming: bool = False) -> None:
        # Count completed operations and context messages, never streaming/log
        # updates or our own notices. Only incoming direction invalidates titles.
        if isinstance(message, dict):
            operation = (message.get('phase') == 'completed'
                         and message.get('kind') in {'command', 'tool', 'search', 'file_change'}
                         and not message.get('append_output') and not message.get('append_detail'))
            if not operation and context_message(message) is None:
                return
        elif not message.strip():
            return
        with self.lock:
            context = self.turns.get(key)
            if context is None:
                return
            context.suggestion_events = min(SUGGESTION_EVENTS, context.suggestion_events + 1)
            context.self_events = min(SELF_MEMORY_EVENTS, context.self_events + 1)
            if incoming:
                context.last_query = ""
                context.generation += 1
        self.wake.set()

    def check_once(self) -> bool:
        """Only the serial worker calls this; producers never wait for I/O."""
        with self.lock:
            now = time.monotonic()
            if now < self.next_check:
                return False
            eligible = [(key, c) for key, c in self.turns.items()
                        if c.due_at() <= now]
            if not eligible:
                return False
            key, context = min(eligible, key=lambda item: item[1].due_at())
            generation = context.generation
            refresh_self = context.next_self <= now and context.self_events >= SELF_MEMORY_EVENTS
            check_suggestions = context.next_suggestion <= now and context.suggestion_events >= SUGGESTION_EVENTS
            if refresh_self:
                context.next_self = now + SELF_MEMORY_INTERVAL
                context.self_events = 0
            if check_suggestions:
                context.next_suggestion = now + SUGGESTION_INTERVAL
                context.suggestion_events = 0
            self.next_check = now + CHECK_INTERVAL
            self.stats['checks'] += 1

        def still_current() -> bool:
            # Title freshness is checked under the turn delivery lock. Never hold our lock
            # across I/O; incoming messages and finish take locks in this order.
            with self.lock:
                return self.turns.get(key) is context and context.generation == generation

        # Full self-memory is independent of query changes and shared recall.
        # Read afresh even at the same revision; failed/busy delivery waits for
        # the next time/event threshold rather than accumulating retries.
        if refresh_self:
            try:
                page = workspace_proxy.self_memory(key[0])
                with self.lock:
                    active = self.turns.get(key) is context
                if active and page is not None and context.deliver_self(page):
                    with self.lock:
                        self.stats['self_refreshes'] += 1
            except Exception as exc:
                self._failure(exc)
        if not check_suggestions:
            return True
        try:
            query = memory_context.load_query(key[0])
            with self.lock:
                if self.turns.get(key) is not context or not query or query == context.last_query:
                    return True
        except Exception as exc:
            self._failure(exc)
            return True
        try:
            response = workspace_proxy.recall_memory(key[0], query)
            with self.lock:
                if self.turns.get(key) is not context:
                    return True
                suggestions = [page_summary(p) for p in response.get('pages', []) if p.get('scope') == 'swarm'
                               and (p['page_id'] not in context.known
                                    or p['revision'] != context.known[p['page_id']])][:3]
                # Stop suggesting when the dedupe budget is full; do not evict and repeat.
                suggestions = suggestions[:max(0, MAX_PAGES - len(context.known))]
                if context.generation == generation:
                    context.last_query = query
            if suggestions:
                delivered = context.deliver(suggestions)
                with self.lock:
                    if self.turns.get(key) is context:
                        if delivered:
                            context.known.update({p[0]: p[1] for p in suggestions})
                        else:
                            # Keep the next interval eligible after a busy drop.
                            context.last_query = ""
                    if delivered:
                        self.stats['suggestions'] += 1
        except Exception as exc:
            self._failure(exc)

        # Title generation is independent of recall ranking; initial and
        # mid-turn titles share a prompt.
        try:
            if context.update_task is not None and still_current():
                task = state.swarm_task_context(*key)
                if task is not None:
                    title = swarm_annotations.task_title(query)
                    if title != task['task_title']:
                        context.update_task(title, still_current)
        except client.HostInferenceError:
            with self.lock:
                self.stats['failures'] += 1
        except Exception as exc:
            self._failure(exc)
        return True

    def _failure(self, exc: Exception) -> None:
        with self.lock:
            self.stats['failures'] += 1
        host_errors.report_warning('memory.monitor', exc, kind='memory_recall_degraded')

    def run(self) -> None:
        while True:
            self.wake.clear()
            self.check_once()
            with self.lock:
                due = min((c.due_at() for c in self.turns.values()), default=float('inf'))
                delay = max(0.01, max(self.next_check, due) - time.monotonic()) if due != float('inf') else None
            self.wake.wait(delay)


monitor = Monitor()
