"""Best-effort model-turn continuations. All backoff state dies with the host."""

from __future__ import annotations

from dataclasses import dataclass
import threading
import time
from typing import Callable

from host.runtime.core import host_errors

DELAYS = (5 * 60, 20 * 60, 60 * 60, 4 * 60 * 60, 12 * 60 * 60)


@dataclass(frozen=True)
class Retry:
    thread_id: str
    attempt: int
    error: str
    due_at: float


_pending: dict[str, Retry] = {}
_lock = threading.Lock()


def schedule(thread_id: str, attempts: int, error: str) -> None:
    """Schedule the next attempt after a failed model turn."""
    if attempts >= len(DELAYS):
        return
    retry = Retry(thread_id, attempts + 1, error, time.time() + DELAYS[attempts])
    with _lock:
        _pending[thread_id] = retry


def cancel(thread_id: str) -> bool:
    with _lock:
        return _pending.pop(thread_id, None) is not None


def take(retry: Retry) -> bool:
    """Claim this attempt unless cancellation or a newer failure replaced it."""
    with _lock:
        if _pending.get(retry.thread_id) is not retry:
            return False
        del _pending[retry.thread_id]
        return True


def pending(thread_id: str) -> dict[str, int | str] | None:
    with _lock:
        retry = _pending.get(thread_id)
    if retry is None:
        return None
    return {"attempt": retry.attempt, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(retry.due_at))}


def deliver_due(deliver: Callable[[Retry], None]) -> None:
    with _lock:
        due = [retry for retry in _pending.values() if retry.due_at <= time.time()]
    for retry in due:
        try:
            deliver(retry)
        except Exception as exc:
            host_errors.report_warning("agent_runtime.turn_retry", exc, context={"thread_id": retry.thread_id})


def run(deliver: Callable[[Retry], None]) -> None:
    while True:
        time.sleep(15)
        deliver_due(deliver)
