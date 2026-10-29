"""Small owner-scoped notebooks alongside self-memory.

Notes store only their current text. They never enter memory history, search,
or embeddings. Writes serialize per owner to enforce the ten-note limit.
Self-memory reads expose note IDs separately from the editable briefing.
"""
from __future__ import annotations

from http import HTTPStatus
import re
from typing import Any

from host.runtime.core import db
from host.runtime.workspace.host_api import WorkspaceError

OWNER_RE = re.compile(r"(?:app|thread|schedule)-[1-9][0-9]*")


def require_owner(owner: str | None) -> str:
    if owner is None or OWNER_RE.fullmatch(owner) is None:
        raise WorkspaceError(HTTPStatus.CONFLICT, "private notes require a current agent identity")
    return owner


def list_notes(owner: str, *, include_content: bool = False) -> list[dict[str, str]]:
    require_owner(owner)
    with db.transaction() as cur:
        cur.execute(
            "SELECT note_id" + (", content" if include_content else "")
            + " FROM private_notes WHERE owner = %s ORDER BY note_id", (owner,),
        )
        return [dict(note_id=row[0], **({"content": row[1]} if include_content else {}))
                for row in cur.fetchall()]


def route(method: str, path: str, body: Any, query: dict[str, list[str]], *, owner: str | None) -> dict[str, Any]:
    owner = require_owner(owner)
    if query:
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, "private notes do not accept query parameters")
    match = re.fullmatch(r"/agent/self/notes/([a-z_]{1,100})", path)
    if match is None or method not in {"GET", "PUT", "DELETE"}:
        raise WorkspaceError(
            HTTPStatus.NOT_FOUND,
            "Use GET, PUT or DELETE /agent/self/notes/{note_id}; note IDs must be 1–100 lowercase letters or underscores.",
        )
    note_id = match.group(1)
    if method != "PUT" and body is not None:
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, "note reads and deletes take no body")
    if method == "PUT" and (
        not isinstance(body, dict) or set(body) != {"content"}
        or not isinstance(body["content"], str) or len(body["content"]) > 20_000
        or "\x00" in body["content"]
    ):
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, "provide only content, a string of at most 20000 characters without NUL")
    with db.transaction() as cur:
        if method == "GET":
            cur.execute("SELECT content FROM private_notes WHERE owner = %s AND note_id = %s", (owner, note_id))
            row = cur.fetchone()
            if row is None:
                raise WorkspaceError(HTTPStatus.NOT_FOUND, "private note not found")
            return {"note": {"note_id": note_id, "content": row[0]}}
        # Serialize changes for this owner, including simultaneous creates.
        cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", ("private_notes:" + owner,))
        if method == "DELETE":
            cur.execute("DELETE FROM private_notes WHERE owner = %s AND note_id = %s RETURNING note_id", (owner, note_id))
            if cur.fetchone() is None:
                raise WorkspaceError(HTTPStatus.NOT_FOUND, "private note not found")
            return {"deleted": True, "note_id": note_id}
        assert isinstance(body, dict)  # PUT payload was validated before opening the transaction.
        cur.execute("SELECT note_id FROM private_notes WHERE owner = %s", (owner,))
        existing = {row[0] for row in cur.fetchall()}
        if note_id not in existing and len(existing) >= 10:
            raise WorkspaceError(HTTPStatus.CONFLICT, "You already have 10 notes; update an existing note or delete one before creating another")
        cur.execute(
            "INSERT INTO private_notes (owner, note_id, content) VALUES (%s, %s, %s)"
            " ON CONFLICT (owner, note_id) DO UPDATE SET content = EXCLUDED.content",
            (owner, note_id, body["content"]),
        )
    return {"note": {"note_id": note_id, "content": body["content"]}}
