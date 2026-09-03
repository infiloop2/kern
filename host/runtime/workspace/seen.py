"""Backend-owned read markers for Chat, scheduled agents, and Web Apps."""

from __future__ import annotations

from http import HTTPStatus
from typing import Any

from host.runtime.core import db
from host.runtime.workspace.host_api import WorkspaceError


MAX_MARKER = 2**63 - 1


def request_marker(body: Any, *, include_revision: bool) -> tuple[int, int]:
    fields = {"message_seq", "revision"} if include_revision else {"message_seq"}
    if not isinstance(body, dict) or set(body) != fields:
        names = "message_seq and revision" if include_revision else "message_seq"
        raise WorkspaceError(
            HTTPStatus.BAD_REQUEST,
            f"request body must contain exactly {names}",
        )
    values = []
    for field in ("message_seq", "revision"):
        if field not in fields:
            values.append(0)
            continue
        value = body[field]
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_MARKER:
            raise WorkspaceError(
                HTTPStatus.BAD_REQUEST,
                f"{field} must be a non-negative integer",
            )
        values.append(value)
    return values[0], values[1]


def add_to_items(kind: str, items: list[dict[str, Any]], id_field: str) -> None:
    """Attach the persisted marker to index items in place."""
    if not items:
        return
    with db.transaction() as cur:
        cur.execute(
            "SELECT item_id, message_seq, revision"
            " FROM workspace_seen WHERE item_kind = %s",
            (kind,),
        )
        markers = {
            str(item_id): (int(message_seq), int(revision))
            for item_id, message_seq, revision in cur.fetchall()
        }
    for item in items:
        message_seq, revision = markers.get(str(item[id_field]), (0, 0))
        item["seen_message_seq"] = message_seq
        if kind == "apps":
            item["seen_revision"] = revision


def save(kind: str, item_id: str, message_seq: int, revision: int = 0) -> dict[str, int]:
    """Advance one marker monotonically and return the stored values."""
    with db.transaction() as cur:
        cur.execute(
            "INSERT INTO workspace_seen (item_kind, item_id, message_seq, revision)"
            " VALUES (%s, %s, %s, %s)"
            " ON CONFLICT (item_kind, item_id) DO UPDATE SET"
            " message_seq = GREATEST(workspace_seen.message_seq, EXCLUDED.message_seq),"
            " revision = GREATEST(workspace_seen.revision, EXCLUDED.revision)"
            " RETURNING message_seq, revision",
            (kind, item_id, message_seq, revision),
        )
        row = cur.fetchone()
    if row is None:
        raise RuntimeError("workspace read marker was not saved")
    return {"message_seq": int(row[0]), "revision": int(row[1])}
