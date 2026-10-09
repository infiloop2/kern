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
BATCH_SIZE = 5
PRIORITY_CAP = 20
# Global spend/rate bound, independent of each turn's event count.
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
    update_task: Callable[[str, Callable[[], bool]], None] | None = None
    generation: int = 0
    count: int = 0
    waiting_since: float = 0


class Monitor:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.turns: dict[Key, Context] = {}
        self.next_check = 0.0
        self.stats: Counter[str] = Counter()

    def seed(self, key: Key, query: str, pages: list[dict[str, Any]],
             deliver: Callable[[list[Page]], bool],
             update_task: Callable[[str, Callable[[], bool]], None] | None = None) -> None:
        with self.lock:
            if len(self.turns) >= MAX_TURNS:
                self.stats['capacity_skips'] += 1
                return
            known = {p['page_id']: p['revision'] for p in pages[:MAX_PAGES]
                     if isinstance(p.get('page_id'), str) and isinstance(p.get('revision'), int)}
            self.turns[key] = Context(query, deliver, known, update_task)

    def finish(self, key: Key) -> None:
        with self.lock:
            self.turns.pop(key, None)

    def observe(self, key: Key, message: str | dict[str, Any], *, incoming: bool = False) -> None:
        # Completed operations can prompt a check, but their text is never
        # retained or searched. The worker reads only conversation context.
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
            if not context.count:
                context.waiting_since = time.monotonic()
            context.count = min(PRIORITY_CAP, context.count + 1)
            if incoming:
                context.last_query = ""
                context.generation += 1
                context.count = max(BATCH_SIZE, context.count)
        self.wake.set()

    def check_once(self) -> bool:
        """Only the serial worker calls this; producers never wait for I/O."""
        with self.lock:
            now = time.monotonic()
            if now < self.next_check:
                return False
            eligible = [(key, c) for key, c in self.turns.items() if c.count >= BATCH_SIZE]
            if not eligible:
                return False
            key, context = max(eligible, key=lambda item: (item[1].count, -item[1].waiting_since))
            generation = context.generation
            context.count = 0
            self.next_check = now + CHECK_INTERVAL
            self.stats['checks'] += 1

        def still_current() -> bool:
            # Title freshness is checked under the turn delivery lock. Never hold our lock
            # across I/O; incoming messages and finish take locks in this order.
            with self.lock:
                return self.turns.get(key) is context and context.generation == generation

        try:
            query = memory_context.load_query(key[0])
            with self.lock:
                if self.turns.get(key) is not context or not query or query == context.last_query:
                    return True
                context.last_query = query
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
            if suggestions and context.deliver(suggestions):
                with self.lock:
                    if self.turns.get(key) is context:
                        context.known.update({p[0]: p[1] for p in suggestions})
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
                pending = any(c.count >= BATCH_SIZE for c in self.turns.values())
                delay = max(0.01, self.next_check - time.monotonic()) if pending else None
            self.wake.wait(delay)


monitor = Monitor()
