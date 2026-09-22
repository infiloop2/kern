"""Paginated operator read model over existing tool and network approvals."""

from __future__ import annotations

from typing import Any

from host.runtime.core import db
from host.runtime.core.state.tools import _approval_id

APPROVAL_PAGE_SIZE = 10

# Only summaries enter the list. Tool payloads remain behind their existing
# authenticated detail endpoint, fetched when the operator expands a request.
_APPROVAL_ROWS = """
    SELECT 'tool' AS kind, number::text AS item_key, number AS sequence, status,
           created_at AS created, COALESCE(NULLIF(decided_at, 0), created_at) AS updated,
           jsonb_strip_nulls(jsonb_build_object(
               'tool_id', tool_id, 'action_id', action_id, 'summary', summary,
               'check_token', check_token, 'result', result,
               'connection_id', connection_id, 'account_label', account_label,
               'risk_scores', assessment.scores
           )) AS detail
    FROM tool_approvals AS approval
    LEFT JOIN tool_approval_risk_assessments AS assessment
      ON assessment.approval_number = approval.number
    UNION ALL
    SELECT 'github_push', id, NULL::bigint, status,
           EXTRACT(EPOCH FROM requested_at::timestamptz)::bigint,
           EXTRACT(EPOCH FROM COALESCE(resolved_at, requested_at)::timestamptz)::bigint,
           jsonb_build_object(
               'owner', owner, 'repo', repo, 'ref_updates', ref_updates,
               'changed_paths', changed_paths, 'result', COALESCE(detail, '')
           )
    FROM pending_pushes
"""


def page_approvals(view: str, page: int) -> dict[str, Any]:
    pending = view == "pending"
    predicate = "status = 'pending'" if pending else "status <> 'pending'"
    order = "created" if pending else "updated"
    with db.transaction() as cur:
        cur.execute(
            f"WITH approvals AS ({_APPROVAL_ROWS}) SELECT"
            " COUNT(*) FILTER (WHERE status = 'pending'),"
            " COUNT(*) FILTER (WHERE status <> 'pending') FROM approvals"
        )
        counts = cur.fetchone()
        assert counts is not None  # Aggregate always returns one row.
        pending_count, history_count = (int(value) for value in counts)
        total = pending_count if pending else history_count
        pages = max(1, (total + APPROVAL_PAGE_SIZE - 1) // APPROVAL_PAGE_SIZE)
        page = min(max(1, page), pages)
        cur.execute(
            f"WITH approvals AS ({_APPROVAL_ROWS})"
            " SELECT kind, item_key, status, created, updated, detail FROM approvals"
            f" WHERE {predicate} ORDER BY {order} DESC, kind, sequence DESC, item_key DESC LIMIT %s OFFSET %s",
            (APPROVAL_PAGE_SIZE, (page - 1) * APPROVAL_PAGE_SIZE),
        )
        rows = cur.fetchall()
    items = []
    for kind, item_key, status, created, updated, detail in rows:
        item = dict(detail)
        item_id = (_approval_id(int(item_key), item.pop("check_token"))
                   if kind == "tool" else str(item_key))
        item.update(kind=kind, id=item_id, status=status, created_at=int(created), updated_at=int(updated))
        items.append(item)
    return {"items": items, "page": page, "pages": pages, "page_size": APPROVAL_PAGE_SIZE,
            "total": total, "pending_count": pending_count, "history_count": history_count}
