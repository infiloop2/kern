"""Historical tool-reported charges. Prices are never recalculated by the host."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from host.runtime.core import db
from host.runtime.core.state._base import mutation


def record_tool_cost(*, tool_id: str, connection_id: str, charge_id: str,
                     execution_id: str, action_id: str, origin_thread_id: str | None,
                     approval_id: str | None, amount_nano_usd: int) -> None:
    # A reported charge is immutable. Advance the daily counter only when
    # this transaction inserted a new charge ID.
    with mutation() as cur:
        cur.execute(
            "INSERT INTO tool_costs (tool_id, connection_id, charge_id, execution_id, "
            "action_id, origin_thread_id, approval_id, amount_nano_usd) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (tool_id, charge_id) DO NOTHING "
            "RETURNING (created_at AT TIME ZONE 'UTC')::date::text",
            (tool_id, connection_id, charge_id, execution_id, action_id,
             origin_thread_id, approval_id, amount_nano_usd),
        )
        inserted = cur.fetchone()
        if inserted is not None:
            cur.execute(
                "INSERT INTO tool_cost_daily (day, tool_id, action_id, amount_nano_usd, charges) "
                "VALUES (%s::date, %s, %s, %s, 1) "
                "ON CONFLICT (day, tool_id, action_id) DO UPDATE SET "
                "amount_nano_usd = tool_cost_daily.amount_nano_usd + EXCLUDED.amount_nano_usd, "
                "charges = tool_cost_daily.charges + 1",
                (inserted[0], tool_id, action_id, amount_nano_usd),
            )


def _usd(nano_usd: int) -> str:
    whole, fraction = divmod(nano_usd, 1_000_000_000)
    return str(whole) if not fraction else f"{whole}.{fraction:09d}".rstrip("0")


def tool_cost_usage() -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    with db.transaction() as cur:
        cur.execute(
            "SELECT tool_id, SUM(amount_nano_usd) FROM tool_cost_daily "
            "WHERE day >= %s::date AND day <= %s::date GROUP BY tool_id "
            "ORDER BY tool_id",
            (start.date().isoformat(), now.date().isoformat()))
        rows = cur.fetchall()
    return {
        "month_to_date": _usd(sum(int(row[1]) for row in rows)),
        "tools": [{"tool_id": row[0], "month_to_date": _usd(int(row[1]))} for row in rows],
    }
