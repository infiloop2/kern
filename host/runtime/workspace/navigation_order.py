"""Stable operator-owned App and scheduled-agent navigation order."""

from __future__ import annotations

from http import HTTPStatus
import json
import re
from typing import Any

from host.runtime.core import db
from host.runtime.workspace.host_api import WorkspaceError


def ordered_ids(ids: list[str], saved: list[str]) -> list[str]:
    """Retain saved positions; append new identities in creation order."""
    remaining = set(ids)
    ordered = []
    for item_id in saved:
        if item_id in remaining:
            ordered.append(item_id)
            remaining.remove(item_id)
    ordered.extend(sorted(remaining, key=lambda value: int(value.rsplit("-", 1)[1])))
    return ordered


def sort_items(kind: str, items: list[dict[str, Any]], id_field: str) -> None:
    if not items:
        return
    with db.transaction() as cur:
        cur.execute(
            "SELECT item_ids FROM workspace_navigation_order WHERE item_kind = %s",
            (kind,),
        )
        row = cur.fetchone()
    saved = row[0] if row else []
    positions = {item_id: index for index, item_id in enumerate(
        ordered_ids([item[id_field] for item in items], saved)
    )}
    items.sort(key=lambda item: positions[item[id_field]])


def move(kind: str, body: Any) -> dict[str, str]:
    """Apply one relative move against current state, serialized per list.

    A move never replaces a browser's potentially stale copy of the full list.
    Archived Apps keep their positions and new items cannot be lost.
    """
    prefix = "app" if kind == "apps" else "schedule"
    if not isinstance(body, dict) or set(body) != {"item_id", "before_id"}:
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, "provide item_id and before_id")
    item_id, before_id = body["item_id"], body["before_id"]
    for value in ([item_id] if before_id is None else [item_id, before_id]):
        if not isinstance(value, str) or not re.fullmatch(rf"{prefix}-[1-9][0-9]*", value):
            raise WorkspaceError(HTTPStatus.BAD_REQUEST, "invalid navigation item id")
    if item_id == before_id:
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, "an item cannot precede itself")
    with db.transaction() as cur:
        cur.execute(
            "INSERT INTO workspace_navigation_order (item_kind) VALUES (%s)"
            " ON CONFLICT DO NOTHING", (kind,),
        )
        cur.execute(
            "SELECT item_ids FROM workspace_navigation_order"
            " WHERE item_kind = %s FOR UPDATE", (kind,),
        )
        row = cur.fetchone()
        if row is None:
            raise RuntimeError("navigation order was not initialized")
        saved = row[0]
        if kind == "apps":
            cur.execute("SELECT app_id, NOT archived FROM web_apps")
        else:
            cur.execute("SELECT thread_id, TRUE FROM schedules WHERE deleted_at IS NULL")
        rows = cur.fetchall()
        active = {record[0] for record in rows if record[1]}
        if item_id not in active or (before_id is not None and before_id not in active):
            raise WorkspaceError(HTTPStatus.CONFLICT, "the list changed; refresh and try again")
        order = ordered_ids([record[0] for record in rows], saved)
        order.remove(item_id)
        order.insert(len(order) if before_id is None else order.index(before_id), item_id)
        cur.execute(
            "UPDATE workspace_navigation_order SET item_ids = %s::jsonb WHERE item_kind = %s",
            (json.dumps(order), kind),
        )
    return {"status": "ok"}
