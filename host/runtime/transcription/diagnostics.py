"""Bounded, payload-free dictation warnings in the existing host diagnostics."""

from __future__ import annotations

import threading
import time
from typing import Any

from host.runtime.core import host_errors

SLOW_SECONDS = 3
REPORT_INTERVAL_SECONDS = 60
_lock = threading.Lock()
_last_report: dict[tuple[str, str], float] = {}


def report(component: str, outcome: str, context: dict[str, Any]) -> None:
    # Callers use fixed component/outcome names, never request IDs as keys.
    # Keep failures independent from slow successes so one cannot hide the other.
    now = time.monotonic()
    with _lock:
        key = (component, outcome)
        if now - _last_report.get(key, float("-inf")) < REPORT_INTERVAL_SECONDS:
            return
        _last_report[key] = now
    # logger can block for two seconds. Rate limiting happens before starting
    # this thread, bounding concurrent reporters by the fixed outcome set.
    # Neither the browser reply nor model startup should wait on journald.
    try:
        threading.Thread(
            target=host_errors.report_warning,
            args=(component, f"Dictation {outcome}"),
            kwargs={"context": {"outcome": outcome, **context}},
            daemon=True,
        ).start()
    except RuntimeError:
        # Diagnostics are best effort, including when no thread can start.
        pass
