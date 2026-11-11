"""Agent-readable token usage, derived from the operator's Analytics report.

The default view ranks threads over the same seven UTC days so an agent can
find heavy spenders without loading every hourly row; `thread_id` returns one
thread's hourly rows unchanged. Counts are best-effort measurements.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Any

from host.runtime.core import state
from host.runtime.core.state.usage import FIELDS
from host.runtime.workspace.host_api import WorkspaceError
from host.runtime.workspace.query import one

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


def route_agent(method: str, query: dict[str, list[str]]) -> dict[str, Any]:
    if method != "GET":
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, "agent usage accepts only GET")
    extra = sorted(set(query) - {"limit", "thread_id"})
    if extra:
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, f"unexpected usage query field: {extra[0]}")
    thread_id = one(query, "thread_id")
    raw_limit = one(query, "limit")
    if thread_id is not None and raw_limit is not None:
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, "limit applies only without thread_id")
    if raw_limit is not None and (not raw_limit.isdigit() or not 1 <= int(raw_limit) <= MAX_LIMIT):
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, f"limit must be between 1 and {MAX_LIMIT}")
    report = state.usage_report()
    window = {key: report[key] for key in ("since", "until", "days")}
    if thread_id is not None:
        groups = [group for group in report["groups"] if group["thread_id"] == thread_id]
        groups.sort(key=lambda group: (group["day"], group["hour"], group["runtime"], group["model"]))
        return {**window, "groups": groups}
    threads = _threads(report)
    limit = int(raw_limit) if raw_limit is not None else DEFAULT_LIMIT
    # Totals cover every thread so a share stays computable from a limited page.
    totals: dict[str, int | None] = dict.fromkeys(FIELDS)
    for row in threads:
        for field in FIELDS:
            if row["tokens"][field] is not None:
                totals[field] = (totals[field] or 0) + row["tokens"][field]
    return {
        **window, "thread_count": len(threads), "turns": sum(row["turns"] for row in threads),
        "tokens": totals, "threads": threads[:limit],
    }


def _threads(report: dict[str, Any]) -> list[dict[str, Any]]:
    """One row per thread, heaviest first, with a known-token total per day."""
    day_index = {day: index for index, day in enumerate(report["days"])}
    threads: dict[str, dict[str, Any]] = {}
    for group in report["groups"]:
        row = threads.setdefault(group["thread_id"], {
            "thread_id": group["thread_id"], "name": group["name"], "kind": group["kind"],
            "active": group["active"], "models": [], "turns": 0,
            "tokens": dict.fromkeys(FIELDS), "daily_tokens": [0] * len(day_index),
        })
        if group["model"] not in row["models"]:
            row["models"].append(group["model"])
        row["turns"] += group["turns"]
        for field in FIELDS:
            count = group["tokens"][field]
            # Unknown stays null until some turn reports that bucket.
            if count is not None:
                row["tokens"][field] = (row["tokens"][field] or 0) + count
                row["daily_tokens"][day_index[group["day"]]] += count
    for row in threads.values():
        row["models"].sort()
    return sorted(threads.values(), key=lambda row: (-sum(row["daily_tokens"]), row["thread_id"]))
