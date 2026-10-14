"""Operator policies and timestamped checks, without policy lifecycle states."""
from typing import Any

from host.runtime.core import db
from host.runtime.core.state._base import mutation
from host.runtime.core.state.network import _PENDING_PUSH_COLUMNS, _pending_push_row
from host.runtime.core.state.tools import _approval_id, _approval_number, _TOOL_APPROVAL_FIELDS, _tool_approval_dict


def auto_approval_settings() -> dict[str, int]:
    with db.transaction() as cur:
        cur.execute("SELECT sleep_start_minute, sleep_end_minute FROM auto_approval_settings WHERE singleton = TRUE")
        row = cur.fetchone()
    return {"sleep_start_minute": row[0] if row else 0, "sleep_end_minute": row[1] if row else 480}


def set_auto_approval_settings(start: int, end: int) -> None:
    with mutation() as cur:
        cur.execute(
            "INSERT INTO auto_approval_settings (singleton, sleep_start_minute, sleep_end_minute)"
            " VALUES (TRUE, %s, %s) ON CONFLICT (singleton) DO UPDATE"
            " SET sleep_start_minute = EXCLUDED.sleep_start_minute, sleep_end_minute = EXCLUDED.sleep_end_minute",
            (start, end),
        )


def auto_approval_policies() -> list[dict[str, Any]]:
    with db.transaction() as cur:
        cur.execute("SELECT tool_id, action_id, instructions FROM auto_approval_policies ORDER BY tool_id, action_id")
        return [dict(zip(("tool_id", "action_id", "instructions"), row)) for row in cur.fetchall()]


def set_auto_approval_policy(tool_id: str, action_id: str, instructions: str | None) -> None:
    with mutation() as cur:
        if instructions is None:
            cur.execute("DELETE FROM auto_approval_policies WHERE tool_id = %s AND action_id = %s", (tool_id, action_id))
        else:
            cur.execute(
                "INSERT INTO auto_approval_policies VALUES (%s, %s, %s) ON CONFLICT (tool_id, action_id)"
                " DO UPDATE SET instructions = EXCLUDED.instructions", (tool_id, action_id, instructions),
            )


def pending_auto_approvals() -> list[dict[str, Any]]:
    # Take at most 20 requests per batch. Arrivals during a run wait for the
    # next run. A recorded check excludes the request permanently.
    with db.transaction() as cur:
        cur.execute(
            f"SELECT {_TOOL_APPROVAL_FIELDS} FROM tool_approvals a WHERE status = 'pending'"
            " AND NOT EXISTS (SELECT 1 FROM auto_approval_reviews r WHERE r.approval_number = a.number)"
            " ORDER BY number LIMIT 20"
        )
        return [_tool_approval_dict(row) for row in cur.fetchall()]


def save_auto_approval_review(approval_id: str, checked_at: int, policy: str,
                              outcome: str, reason: str, model: str = "") -> None:
    with mutation() as cur:
        cur.execute(
            "INSERT INTO auto_approval_reviews (approval_number, checked_at, policy, model, outcome, reason)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (_approval_number(approval_id), checked_at, policy, model, outcome, reason),
        )


def save_auto_approval_error(approval_id: str, error: str) -> None:
    with mutation() as cur:
        cur.execute("UPDATE auto_approval_reviews SET approval_error = %s WHERE approval_number = %s",
                    (error, _approval_number(approval_id)))


def auto_approval_history(page: int, outcome: str) -> dict[str, Any]:
    predicate = "WHERE r.outcome = %s" if outcome else ""
    args: tuple[Any, ...] = (outcome,) if outcome else ()
    with db.transaction() as cur:
        cur.execute(f"SELECT COUNT(*) FROM auto_approval_reviews r {predicate}", args)
        count = cur.fetchone()
        assert count is not None
        total = int(count[0])
        pages = max(1, (total + 9) // 10)
        page = min(page, pages)
        cur.execute(
            "SELECT r.id, r.checked_at, r.policy, r.model, r.outcome, r.reason,"
            " COALESCE(a.tool_id, 'github'), COALESCE(a.action_id, 'push'),"
            " COALESCE(a.summary, 'Push to ' || p.owner || '/' || p.repo),"
            " COALESCE(a.status, p.status), COALESCE(a.result, p.detail, ''), r.approval_error,"
            " a.number, a.check_token, r.push_id, p.ref_updates, p.changed_paths"
            " FROM auto_approval_reviews r LEFT JOIN tool_approvals a ON a.number = r.approval_number"
            " LEFT JOIN pending_pushes p ON p.id = r.push_id "
            f"{predicate} ORDER BY r.id DESC LIMIT 10 OFFSET %s", (*args, (page - 1) * 10),
        )
        keys = ("id", "checked_at", "policy", "model", "outcome", "reason", "tool_id", "action_id", "summary", "status", "result", "approval_error")
        items = []
        for row in cur.fetchall():
            item: dict[str, Any] = dict(zip(keys, row[:12]))
            item.update(kind="github_push" if row[14] else "tool",
                        approval_id=f"push-{row[14]}" if row[14] else _approval_id(row[12], row[13]))
            if row[14]:
                item["request"] = {"push_id": row[14], "ref_updates": row[15], "changed_paths": row[16]}
            items.append(item)
    return {"items": items, "page": page, "pages": pages, "total": total}


def pending_push_auto_approvals() -> list[dict[str, Any]]:
    with db.transaction() as cur:
        cur.execute(
            f"SELECT {_PENDING_PUSH_COLUMNS} FROM pending_pushes p WHERE status = 'pending'"
            " AND NOT EXISTS (SELECT 1 FROM auto_approval_reviews r WHERE r.push_id = p.id)"
            " ORDER BY requested_at, id LIMIT 10"
        )
        return [_pending_push_row(row) for row in cur.fetchall()]


def save_push_auto_approval_review(push_id: str, checked_at: int, policy: str,
                                   outcome: str, reason: str, model: str = "") -> None:
    with mutation() as cur:
        cur.execute(
            "INSERT INTO auto_approval_reviews (push_id, checked_at, policy, model, outcome, reason)"
            " VALUES (%s, %s, %s, %s, %s, %s)", (push_id, checked_at, policy, model, outcome, reason),
        )


def save_push_auto_approval_error(push_id: str, error: str) -> None:
    with mutation() as cur:
        cur.execute("UPDATE auto_approval_reviews SET approval_error = %s WHERE push_id = %s", (error, push_id))
