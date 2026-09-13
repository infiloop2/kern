"""Turn accounting and the operator's fixed seven-calendar-day report (UTC)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from host.runtime.core import db
from host.runtime.core.state._base import utc_now

FIELDS = ("input_tokens", "cached_input_tokens", "cache_write_tokens", "output_tokens")


def start_turn_usage(cur: Any, thread_id: str, run_number: int, runtime: str, model: str) -> None:
    now = utc_now()
    cur.execute(
        "INSERT INTO turn_usage (thread_id, run_number, agent_runtime, model, started_at, measured_at) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (thread_id, run_number, runtime, model, now, now),
    )


def save_turn_usage(cur: Any, thread_id: str, run_number: int, totals: dict[str, int | None]) -> None:
    # Compare under the same transaction as the detail update: repeated or
    # corrected measurements contribute only their delta to lifetime totals.
    cur.execute(
        "SELECT input_tokens, cached_input_tokens, cache_write_tokens, output_tokens "
        "FROM turn_usage WHERE thread_id = %s AND run_number = %s FOR UPDATE",
        (thread_id, run_number),
    )
    previous = cur.fetchone()
    if previous is None:
        return
    for field, old in zip(FIELDS, previous):
        cur.execute(
            "UPDATE counters SET value = value + %s WHERE name = %s",
            ((totals[field] or 0) - (old or 0), f"token_usage_{field}"),
        )
    cur.execute(
        "UPDATE turn_usage SET measured_at = %s, input_tokens = %s, cached_input_tokens = %s, "
        "cache_write_tokens = %s, output_tokens = %s WHERE thread_id = %s AND run_number = %s",
        (utc_now(), *(totals[field] for field in FIELDS), thread_id, run_number),
    )


def lifetime_token_usage() -> dict[str, int]:
    """Known turn totals across all providers, preserved after detail pruning."""
    names = tuple(f"token_usage_{field}" for field in FIELDS)
    with db.transaction() as cur:
        cur.execute("SELECT name, value FROM counters WHERE name IN (%s, %s, %s, %s)", names)
        stored = dict(cur.fetchall())
    return {field: int(stored[name]) for field, name in zip(FIELDS, names)}


def usage_report(now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=6)
    since = start.strftime("%Y-%m-%dT%H:%M:%SZ")
    until = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    # Group in SQL, not by loading every model response or conversation body.
    sums = ", ".join(f"SUM(u.{field}), COUNT(u.{field})" for field in FIELDS)
    with db.transaction() as cur:
        cur.execute(
            "SELECT u.thread_id, COALESCE(a.name, s.name, c.name, "
            "CASE WHEN c.thread_id IS NOT NULL THEN 'Untitled chat' ELSE 'Deleted thread' END), "
            "u.agent_runtime, u.model, LEFT(u.measured_at, 10), COUNT(*), " + sums +
            ", BOOL_OR((a.app_id IS NOT NULL AND NOT a.archived) OR "
            "(c.thread_id IS NOT NULL AND NOT c.archived) OR "
            "(s.thread_id IS NOT NULL AND s.deleted_at IS NULL))"
            " FROM turn_usage u LEFT JOIN web_apps a ON a.app_id = u.thread_id "
            "LEFT JOIN schedules s ON s.thread_id = u.thread_id "
            "LEFT JOIN chat_threads c ON c.thread_id = u.thread_id "
            "WHERE u.measured_at >= %s AND u.measured_at <= %s GROUP BY u.thread_id, a.name, s.name, c.thread_id, c.name, "
            "u.agent_runtime, u.model, LEFT(u.measured_at, 10)",
            (since, until),
        )
        rows = cur.fetchall()
    groups = []
    for row in rows:
        item: dict[str, Any] = {
            "thread_id": row[0], "name": row[1], "runtime": row[2], "model": row[3],
            "day": row[4], "turns": row[5],
            "active": bool(row[-1]),
            "kind": "apps" if row[0].startswith("app-") else "schedules" if row[0].startswith("schedule-") else "chats",
        }
        item["tokens"] = {field: int(row[6 + i * 2]) if row[6 + i * 2] is not None else None for i, field in enumerate(FIELDS)}
        item["measured_turns"] = {field: int(row[7 + i * 2]) for i, field in enumerate(FIELDS)}
        groups.append(item)
    return {
        "since": since, "until": until,
        "days": [(start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)],
        "groups": groups,
    }


def prune_turn_usage(cur: Any, cutoff: str) -> None:
    cur.execute("DELETE FROM turn_usage WHERE measured_at < %s", (cutoff,))
