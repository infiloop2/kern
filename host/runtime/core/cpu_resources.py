"""CPU capacity visible to this service, including its effective affinity."""

from __future__ import annotations

import os


def available_cpu_count() -> int:
    try:
        return max(1, len(os.sched_getaffinity(0)))
    except (AttributeError, OSError):
        return max(1, os.cpu_count() or 1)
