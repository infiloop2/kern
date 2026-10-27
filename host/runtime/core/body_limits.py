"""Shared wire/decoded inspection ceiling, sized for in-memory guards."""

from __future__ import annotations

import os


MAX_INSPECTION_BYTES = 1024 ** 3


def inspection_body_limit() -> int:
    # Only one large body is admitted at once. Budget eight times its size
    # for wire/decoded/frame copies and parsing, leaving half the host's RAM
    # for Admin, PostgreSQL and runtimes. This is capacity planning, not an
    # OS memory reservation; admission also checks current available RAM.
    try:
        memory = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
        if memory <= 0:
            return 0
        return min(MAX_INSPECTION_BYTES, memory // 16)
    except (OSError, ValueError):
        return 0  # unknown capacity must not enable GiB buffering


INSPECTION_BODY_LIMIT = inspection_body_limit()
