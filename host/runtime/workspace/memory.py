"""Host-global, revisioned Workspace memory pages."""

from __future__ import annotations

import base64
import binascii
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
import json
import re
from typing import Any
from urllib.parse import unquote

from host.runtime.core import db
from host.runtime.workspace.host_api import WorkspaceError


PAGE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
INDIVIDUAL_PAGE_ID_RE = re.compile(r"^(?:app|thread|schedule)-")
LINK_RE = re.compile(r"\[\[([a-z0-9][a-z0-9-]{0,63})\]\]")
MAX_DESCRIPTION_CHARS = 100
MAX_CONTENT_CHARS = 1000
MAX_PAGES = 10_000
DEFAULT_PAGE_LIMIT = 50
MAX_PAGE_LIMIT = 100
MAX_REVISION_PAGE_LIMIT = 50
MAX_SEARCH_BYTES = 200
WEAK_SEARCH_LIMIT = 5
FALLBACK_POPULAR_LIMIT = 5
REVISION_RETAINED = 100
DELETED_RETAIN_DAYS = 90
MAX_BIGINT = 2**63 - 1
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def route_browser(
    method: str,
    path: str,
    body: Any,
    query: dict[str, list[str]],
) -> dict[str, Any]:
    if path == "/memory" and method == "GET":
        return list_pages(query)
    if path == "/memory/search" and method == "GET":
        return search_pages(query)
    match = re.fullmatch(r"/memory/pages/([^/]+)", path)
    if match:
        page_id = _page_id(match.group(1))
        if method == "GET":
            return {"page": load_page(page_id, include_deleted=True)}
        if method == "PUT":
            return {"page": save_page(page_id, body, actor="user")}
        if method == "DELETE":
            return delete_page(page_id, query, actor="user")
    match = re.fullmatch(r"/memory/pages/([^/]+)/revisions", path)
    if match and method == "GET":
        return list_revisions(_page_id(match.group(1)), query)
    match = re.fullmatch(r"/memory/pages/([^/]+)/revisions/([1-9][0-9]*)/restore", path)
    if match and method == "POST":
        return {
            "page": restore_revision(
                _page_id(match.group(1)), int(match.group(2)), body
            )
        }
    raise WorkspaceError(HTTPStatus.NOT_FOUND, "memory route not found")


def route_agent(
    method: str,
    path: str,
    body: Any,
    query: dict[str, list[str]],
) -> dict[str, Any]:
    if path == "/agent/memory" and method == "GET":
        return list_swarm_pages(query)
    if path == "/agent/memory/search" and method == "GET":
        return search_swarm_pages(query)
    match = re.fullmatch(r"/agent/memory/pages/([^/]+)", path)
    if match:
        page_id = _page_id(match.group(1))
        _require_swarm_page(page_id)
        if method == "GET":
            return {"page": load_page(page_id)}
        if method == "PUT":
            return {"page": save_page(page_id, body, actor="agent")}
        if method == "DELETE":
            return delete_page(page_id, query, actor="agent")
    raise WorkspaceError(HTTPStatus.NOT_FOUND, "agent memory route not found")


def list_pages(query: dict[str, list[str]]) -> dict[str, Any]:
    _reject_query_keys(
        query,
        {"cursor", "limit", "deleted", "scope"},
        "memory index",
    )
    return _list_pages(
        query,
        deleted=_boolean_query(query, "deleted", default=False),
        scope=_memory_scope(query),
    )


def list_swarm_pages(query: dict[str, list[str]]) -> dict[str, Any]:
    _reject_query_keys(query, {"cursor", "limit"}, "memory index")
    return _list_pages(query, deleted=False, scope="swarm")


def _list_pages(
    query: dict[str, list[str]],
    *,
    deleted: bool,
    scope: str,
) -> dict[str, Any]:
    limit = _limit(query)
    after = _decode_cursor(_one(query, "cursor"))
    clause = "deleted_at IS NOT NULL" if deleted else "deleted_at IS NULL"
    clause += _scope_clause(scope)
    params: list[Any] = []
    if after is not None:
        clause += " AND page_id > %s"
        params.append(after)
    with db.transaction() as cur:
        cur.execute(
            "SELECT page_id, description, content, revision, deleted_at,"
            " updated_by, created_at, updated_at FROM memory_pages"
            f" WHERE {clause} ORDER BY page_id LIMIT %s",
            (*params, limit + 1),
        )
        rows = cur.fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    response: dict[str, Any] = {"pages": [_page_summary(row) for row in rows]}
    if more and rows:
        response["next_cursor"] = _encode_cursor(str(rows[-1][0]))
    return response


def search_pages(query: dict[str, list[str]]) -> dict[str, Any]:
    _reject_query_keys(
        query,
        {"q", "cursor", "limit", "scope"},
        "memory search",
    )
    return _search_pages(query, scope=_memory_scope(query), record_top_hit=False)


def search_swarm_pages(query: dict[str, list[str]]) -> dict[str, Any]:
    _reject_query_keys(query, {"q", "cursor", "limit"}, "memory search")
    return _search_pages(query, scope="swarm", record_top_hit=True)


def _search_pages(
    query: dict[str, list[str]],
    *,
    scope: str,
    record_top_hit: bool,
) -> dict[str, Any]:
    needle = _one(query, "q")
    if not needle or len(needle.encode()) > MAX_SEARCH_BYTES:
        raise WorkspaceError(
            HTTPStatus.BAD_REQUEST,
            f"q must be between 1 and {MAX_SEARCH_BYTES} bytes",
        )
    limit = _limit(query, default=20)
    offset = _decode_offset_cursor(_one(query, "cursor"))
    with db.transaction() as cur:
        cur.execute(
            "SELECT page_id, description, content, revision, deleted_at,"
            " updated_by, created_at, updated_at,"
            " ts_rank(to_tsvector('simple', page_id || ' ' || description || ' ' || content),"
            " websearch_to_tsquery('simple', %s)) AS rank"
            " FROM memory_pages WHERE deleted_at IS NULL"
            f"{_scope_clause(scope)}"
            " AND to_tsvector('simple', page_id || ' ' || description || ' ' || content)"
            " @@ websearch_to_tsquery('simple', %s)"
            " ORDER BY rank DESC, page_id LIMIT %s OFFSET %s",
            (needle, needle, limit + 1, offset),
        )
        rows = cur.fetchall()
        if rows and offset == 0 and record_top_hit:
            cur.execute(
                "UPDATE memory_pages SET strong_top_hit_count = CASE"
                " WHEN strong_top_hit_count < %s THEN strong_top_hit_count + 1"
                " ELSE strong_top_hit_count END, last_strong_top_hit_at = %s"
                " WHERE page_id = %s",
                (MAX_BIGINT, _utc_now(), rows[0][0]),
            )
        if not rows and offset == 0:
            weak_rows = _weak_search_rows(cur, needle, scope=scope)
            weak_ids = {str(row[0]) for row in weak_rows}
            popular_rows = [
                row
                for row in _popular_rows(
                    cur,
                    scope=scope,
                    limit=FALLBACK_POPULAR_LIMIT + WEAK_SEARCH_LIMIT,
                )
                if str(row[0]) not in weak_ids
            ][:FALLBACK_POPULAR_LIMIT]
            return {
                "pages": [_page_summary(row[:8]) for row in weak_rows],
                "match_mode": "weak",
                "popular_pages": [_page_summary(row[:8]) for row in popular_rows],
            }
    more = len(rows) > limit
    rows = rows[:limit]
    response: dict[str, Any] = {"pages": [_page_summary(row[:8]) for row in rows]}
    if more:
        response["next_cursor"] = _encode_offset_cursor(offset + limit)
    return response


def _weak_search_rows(cur: Any, needle: str, *, scope: str) -> list[tuple[Any, ...]]:
    tokens = list(dict.fromkeys(re.findall(r"[^\W_]+", needle.casefold())))
    if not tokens:
        return []
    weak_needle = " OR ".join(tokens)
    cur.execute(
        "SELECT page_id, description, content, revision, deleted_at,"
        " updated_by, created_at, updated_at,"
        " ts_rank(to_tsvector('simple', page_id || ' ' || description || ' ' || content),"
        " websearch_to_tsquery('simple', %s)) AS rank"
        " FROM memory_pages WHERE deleted_at IS NULL"
        f"{_scope_clause(scope)}"
        " AND to_tsvector('simple', page_id || ' ' || description || ' ' || content)"
        " @@ websearch_to_tsquery('simple', %s)"
        " ORDER BY rank DESC, page_id LIMIT %s",
        (weak_needle, weak_needle, WEAK_SEARCH_LIMIT),
    )
    return cur.fetchall()


def _popular_rows(cur: Any, *, scope: str, limit: int) -> list[tuple[Any, ...]]:
    if limit == 0:
        return []
    cur.execute(
        "SELECT page_id, description, content, revision, deleted_at,"
        " updated_by, created_at, updated_at FROM memory_pages"
        " WHERE deleted_at IS NULL AND strong_top_hit_count > 0"
        f"{_scope_clause(scope)}"
        " ORDER BY strong_top_hit_count DESC,"
        " last_strong_top_hit_at DESC NULLS LAST, updated_at DESC, page_id LIMIT %s",
        (limit,),
    )
    return cur.fetchall()


def load_page(
    page_id: str,
    *,
    include_deleted: bool = False,
) -> dict[str, Any]:
    with db.transaction() as cur:
        cur.execute(
            "SELECT page_id, description, content, revision, deleted_at,"
            " updated_by, created_at, updated_at FROM memory_pages WHERE page_id = %s",
            (page_id,),
        )
        row = cur.fetchone()
        if (
            row is not None
            and (include_deleted or row[4] is None)
            and not is_individual_page_id(page_id)
        ):
            cur.execute(
                "SELECT page_id FROM memory_pages WHERE deleted_at IS NULL"
                f"{_scope_clause('swarm')}"
                " AND content LIKE %s ORDER BY page_id LIMIT 101",
                (f"%[[{page_id}]]%",),
            )
            backlinks = [str(item[0]) for item in cur.fetchall()[:100]]
        else:
            backlinks = []
    if row is None or (not include_deleted and row[4] is not None):
        raise WorkspaceError(HTTPStatus.NOT_FOUND, "memory page not found")
    return {**_page_summary(row), "content": row[2], "backlinks": backlinks}


def save_page(
    page_id: str,
    body: Any,
    *,
    actor: str,
) -> dict[str, Any]:
    request = _object(body, "memory page request")
    _require_keys(
        request,
        {"description", "content", "expected_revision"},
        {"description", "content", "expected_revision"},
    )
    description = _description(request["description"])
    content = _content(request["content"])
    expected = _expected_revision(request["expected_revision"])
    now = _utc_now()
    with db.transaction() as cur:
        cur.execute(
            "SELECT description, content, revision, deleted_at, created_at"
            " FROM memory_pages WHERE page_id = %s FOR UPDATE",
            (page_id,),
        )
        current = cur.fetchone()
        if current is None:
            # A missing-row FOR UPDATE does not lock the key. Serialize the
            # uncommon create path so two agents cannot race the global quota
            # or turn a same-id create into an unhandled unique violation.
            cur.execute("LOCK TABLE memory_pages IN SHARE ROW EXCLUSIVE MODE")
            cur.execute(
                "SELECT description, content, revision, deleted_at, created_at"
                " FROM memory_pages WHERE page_id = %s FOR UPDATE",
                (page_id,),
            )
            current = cur.fetchone()
        changed = True
        if current is None:
            if expected != 0:
                raise WorkspaceError(HTTPStatus.CONFLICT, "memory page changed; reload and retry")
            cur.execute("SELECT COUNT(*) FROM memory_pages")
            count_row = cur.fetchone()
            assert count_row is not None
            if int(count_row[0]) >= MAX_PAGES:
                raise WorkspaceError(
                    HTTPStatus.CONFLICT,
                    f"Workspace already retains {MAX_PAGES} memory pages",
                )
            revision = 1
            cur.execute(
                "INSERT INTO memory_pages"
                " (page_id, description, content, revision, deleted_at, created_by,"
                " updated_by, created_at, updated_at)"
                " VALUES (%s, %s, %s, %s, NULL, %s, %s, %s, %s)",
                (page_id, description, content, revision, actor, actor, now, now),
            )
        else:
            if current[3] is not None:
                raise WorkspaceError(
                    HTTPStatus.CONFLICT,
                    "memory page is deleted; restore it from history first",
                )
            if int(current[2]) != expected:
                raise WorkspaceError(HTTPStatus.CONFLICT, "memory page changed; reload and retry")
            if current[0] == description and current[1] == content:
                changed = False
            else:
                revision = expected + 1
                cur.execute(
                    "UPDATE memory_pages SET description = %s, content = %s, revision = %s,"
                    " updated_by = %s, updated_at = %s WHERE page_id = %s",
                    (description, content, revision, actor, now, page_id),
                )
        if changed:
            _insert_revision(cur, page_id, revision, description, content, False, actor, now)
            _prune_revisions(cur, page_id)
    return load_page(page_id, include_deleted=True)


def delete_page(
    page_id: str,
    query: dict[str, list[str]],
    *,
    actor: str,
) -> dict[str, Any]:
    _reject_query_keys(query, {"expected_revision"}, "memory delete")
    expected = _required_query_revision(query)
    now = _utc_now()
    with db.transaction() as cur:
        cur.execute(
            "SELECT description, content, revision, deleted_at"
            " FROM memory_pages WHERE page_id = %s FOR UPDATE",
            (page_id,),
        )
        row = cur.fetchone()
        if row is None or row[3] is not None:
            raise WorkspaceError(HTTPStatus.NOT_FOUND, "memory page not found")
        if int(row[2]) != expected:
            raise WorkspaceError(HTTPStatus.CONFLICT, "memory page changed; reload and retry")
        revision = expected + 1
        cur.execute(
            "UPDATE memory_pages SET revision = %s, deleted_at = %s, updated_by = %s,"
            " updated_at = %s WHERE page_id = %s",
            (revision, now, actor, now, page_id),
        )
        _insert_revision(cur, page_id, revision, row[0], row[1], True, actor, now)
        _prune_revisions(cur, page_id)
    return {"ok": True, "revision": revision}


def list_revisions(page_id: str, query: dict[str, list[str]]) -> dict[str, Any]:
    _reject_query_keys(query, {"before", "limit"}, "memory history")
    limit = _limit(query, default=40, maximum=MAX_REVISION_PAGE_LIMIT)
    before = _optional_positive_int(query, "before")
    clause = " AND id < %s" if before is not None else ""
    params: list[Any] = [page_id]
    if before is not None:
        params.append(before)
    with db.transaction() as cur:
        cur.execute("SELECT 1 FROM memory_pages WHERE page_id = %s", (page_id,))
        if cur.fetchone() is None:
            raise WorkspaceError(HTTPStatus.NOT_FOUND, "memory page not found")
        cur.execute(
            "SELECT id, revision, description, content, deleted, actor, created_at"
            f" FROM memory_page_revisions WHERE page_id = %s{clause}"
            " ORDER BY id DESC LIMIT %s",
            (*params, limit + 1),
        )
        rows = cur.fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    response: dict[str, Any] = {
        "revisions": [
            {
                "id": row[0],
                "revision": row[1],
                "description": row[2],
                "content": row[3],
                "deleted": row[4],
                "actor": row[5],
                "created_at": row[6],
            }
            for row in rows
        ]
    }
    if more and rows:
        response["next_before"] = rows[-1][0]
    return response


def restore_revision(page_id: str, revision: int, body: Any) -> dict[str, Any]:
    request = _object(body, "memory restore request")
    _require_keys(request, {"expected_revision"}, {"expected_revision"})
    expected = _expected_revision(request["expected_revision"])
    now = _utc_now()
    with db.transaction() as cur:
        cur.execute(
            "SELECT revision FROM memory_pages WHERE page_id = %s FOR UPDATE",
            (page_id,),
        )
        current = cur.fetchone()
        if current is None:
            raise WorkspaceError(HTTPStatus.NOT_FOUND, "memory page not found")
        if int(current[0]) != expected:
            raise WorkspaceError(HTTPStatus.CONFLICT, "memory page changed; reload and retry")
        cur.execute(
            "SELECT description, content, deleted FROM memory_page_revisions"
            " WHERE page_id = %s AND revision = %s",
            (page_id, revision),
        )
        source = cur.fetchone()
        if source is None:
            raise WorkspaceError(HTTPStatus.NOT_FOUND, "memory revision not found")
        new_revision = expected + 1
        deleted_at = now if source[2] else None
        cur.execute(
            "UPDATE memory_pages SET description = %s, content = %s, revision = %s,"
            " deleted_at = %s, updated_by = 'user', updated_at = %s WHERE page_id = %s",
            (source[0], source[1], new_revision, deleted_at, now, page_id),
        )
        _insert_revision(
            cur, page_id, new_revision, source[0], source[1], bool(source[2]), "user", now
        )
        _prune_revisions(cur, page_id)
    return load_page(page_id, include_deleted=True)


def prune_deleted(now: datetime | None = None) -> int:
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=DELETED_RETAIN_DAYS)
    with db.transaction() as cur:
        cur.execute(
            "DELETE FROM memory_pages WHERE deleted_at IS NOT NULL AND deleted_at < %s"
            " RETURNING page_id",
            (_format_ts(cutoff),),
        )
        return len(cur.fetchall())


def _page_summary(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "page_id": row[0],
        "description": row[1],
        "revision": row[3],
        "deleted": row[4] is not None,
        "links": _page_links(str(row[0]), str(row[2])),
        "updated_by": row[5],
        "created_at": row[6],
        "updated_at": row[7],
    }


def _page_links(page_id: str, content: str) -> list[str]:
    if is_individual_page_id(page_id):
        return []
    return [
        target
        for target in dict.fromkeys(LINK_RE.findall(content))
        if not is_individual_page_id(target)
    ][:100]


def _insert_revision(
    cur: Any,
    page_id: str,
    revision: int,
    description: str,
    content: str,
    deleted: bool,
    actor: str,
    now: str,
) -> None:
    cur.execute(
        "INSERT INTO memory_page_revisions"
        " (page_id, revision, description, content, deleted, actor, created_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (page_id, revision, description, content, deleted, actor, now),
    )


def _prune_revisions(cur: Any, page_id: str) -> None:
    cur.execute(
        "DELETE FROM memory_page_revisions WHERE id IN ("
        " SELECT id FROM memory_page_revisions WHERE page_id = %s"
        " ORDER BY id DESC OFFSET %s)",
        (page_id, REVISION_RETAINED),
    )


def _page_id(value: str) -> str:
    decoded = unquote(value)
    if PAGE_ID_RE.fullmatch(decoded) is None:
        raise WorkspaceError(
            HTTPStatus.BAD_REQUEST,
            "page_id must be a lowercase slug of at most 64 characters",
        )
    return decoded


def is_individual_page_id(page_id: str) -> bool:
    return INDIVIDUAL_PAGE_ID_RE.match(page_id) is not None


def individual_page_id(value: str) -> str:
    page_id = _page_id(value)
    if not is_individual_page_id(page_id):
        raise WorkspaceError(
            HTTPStatus.CONFLICT,
            "self-memory is unavailable for this thread identity",
        )
    return page_id


def _require_swarm_page(page_id: str) -> None:
    if is_individual_page_id(page_id):
        # Do not reveal whether an identity-owned page exists through the
        # ordinary shared-memory API.
        raise WorkspaceError(HTTPStatus.NOT_FOUND, "memory page not found")


def _memory_scope(query: dict[str, list[str]]) -> str:
    scope = _one(query, "scope") or "swarm"
    if scope not in {"swarm", "individual"}:
        raise WorkspaceError(
            HTTPStatus.BAD_REQUEST,
            "scope must be swarm or individual",
        )
    return scope


def _scope_clause(scope: str | None) -> str:
    if scope is None:
        return ""
    prefixes = (
        "(page_id LIKE 'app-%' OR page_id LIKE 'thread-%'"
        " OR page_id LIKE 'schedule-%')"
    )
    return f" AND {prefixes}" if scope == "individual" else f" AND NOT {prefixes}"


def _description(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_DESCRIPTION_CHARS:
        raise WorkspaceError(
            HTTPStatus.BAD_REQUEST,
            f"description must be between 1 and {MAX_DESCRIPTION_CHARS} characters",
        )
    if "\n" in value or "\r" in value:
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, "description must be one line")
    return value


def _content(value: Any) -> str:
    if not isinstance(value, str) or len(value) > MAX_CONTENT_CHARS:
        raise WorkspaceError(
            HTTPStatus.BAD_REQUEST,
            f"content must be at most {MAX_CONTENT_CHARS} characters",
        )
    return value


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, f"{label} must be an object")
    return value


def _require_keys(value: dict[str, Any], allowed: set[str], required: set[str]) -> None:
    extra = sorted(set(value) - allowed)
    missing = sorted(required - set(value))
    if extra:
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, f"unsupported field: {extra[0]}")
    if missing:
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, f"missing field: {missing[0]}")


def _expected_revision(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, "expected_revision must be non-negative")
    return value


def _required_query_revision(query: dict[str, list[str]]) -> int:
    value = _one(query, "expected_revision")
    if value is None or not value.isdigit():
        raise WorkspaceError(
            HTTPStatus.BAD_REQUEST, "expected_revision must be a non-negative integer"
        )
    return int(value)


def _limit(
    query: dict[str, list[str]],
    *,
    default: int = DEFAULT_PAGE_LIMIT,
    maximum: int = MAX_PAGE_LIMIT,
) -> int:
    raw = _one(query, "limit")
    if raw is None:
        return default
    if not raw.isdigit() or not 1 <= int(raw) <= maximum:
        raise WorkspaceError(
            HTTPStatus.BAD_REQUEST, f"limit must be between 1 and {maximum}"
        )
    return int(raw)


def _boolean_query(query: dict[str, list[str]], key: str, *, default: bool) -> bool:
    raw = _one(query, key)
    if raw is None:
        return default
    if raw not in {"true", "false"}:
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, f"{key} must be true or false")
    return raw == "true"


def _optional_positive_int(query: dict[str, list[str]], key: str) -> int | None:
    raw = _one(query, key)
    if raw is None:
        return None
    if not raw.isdigit() or not 1 <= int(raw) <= MAX_BIGINT:
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, f"{key} must be a positive integer")
    return int(raw)


def _one(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    if not values:
        return None
    if len(values) != 1:
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, f"{key} must be provided once")
    return values[0]


def _reject_query_keys(
    query: dict[str, list[str]], allowed: set[str], label: str
) -> None:
    extra = sorted(set(query) - allowed)
    if extra:
        raise WorkspaceError(
            HTTPStatus.BAD_REQUEST, f"unexpected {label} query field: {extra[0]}"
        )


def _encode_cursor(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def _decode_cursor(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode()
    except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, "cursor is invalid") from exc
    if PAGE_ID_RE.fullmatch(decoded) is None:
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, "cursor is invalid")
    return decoded


def _encode_offset_cursor(offset: int) -> str:
    return _encode_cursor(str(offset))


def _decode_offset_cursor(value: str | None) -> int:
    if value is None:
        return 0
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode()
    except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, "cursor is invalid") from exc
    if not decoded.isdigit() or int(decoded) > MAX_PAGES:
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, "cursor is invalid")
    return int(decoded)


def _utc_now() -> str:
    return _format_ts(datetime.now(timezone.utc))


def _format_ts(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime(TIME_FORMAT)
