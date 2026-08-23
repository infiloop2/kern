"""Queryable row collections and revision snapshots for Web Apps."""

from __future__ import annotations

from http import HTTPStatus
import json
import re
from typing import Any, Callable

from host.runtime.core import db
from host.runtime.workspace.host_api import WorkspaceError
from host.runtime.workspace.web_apps.data_shape import utf8_length as _utf8_length

MAX_COLLECTIONS = 64
MAX_COLLECTION_ROWS = 100_000
MAX_COLLECTION_DATA_BYTES = 50 * 1024 * 1024
MAX_COLLECTION_ROW_BYTES = 128 * 1024
MAX_COLLECTION_BATCH_OPERATIONS = 100
MAX_COLLECTION_RESTORE_BATCH_ROWS = 100
MAX_COLLECTION_RESTORE_BATCH_BYTES = 1024 * 1024
MAX_COLLECTION_QUERY_FILTERS = 8
MAX_COLLECTION_QUERY_LIMIT = 100
MAX_COLLECTION_QUERY_OFFSET = 1_000_000
MAX_COLLECTION_FIELD_BYTES = 128
COLLECTION_NAME_RE = re.compile(r"[a-z][a-z0-9_-]{0,63}")
COLLECTION_ROW_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~:-]{0,127}")
STATE_COLUMNS = (
    "revision, html, css, javascript, data_json, updated_at, agent_updates_locked"
)


def _required_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, f"{label} must be an object")
    return value


def _require_keys(
    value: dict[str, Any], allowed: set[str], *, required: set[str]
) -> None:
    missing = sorted(required - set(value))
    if missing:
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, f"missing field: {missing[0]}")
    unexpected = sorted(set(value) - allowed)
    if unexpected:
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, f"unexpected field: {unexpected[0]}")


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, f"{field} must be non-empty text")
    return value


def _required_counter(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, f"{field} must be a non-negative integer")
    return value


def list_collections(
    app_id: str, *, require_web_app: Callable[[str], Any]
) -> dict[str, Any]:
    """Return bounded collection summaries at one coherent App revision."""
    require_web_app(app_id)
    with db.transaction() as cur:
        cur.execute(
            "SELECT revision, updated_at FROM web_apps"
            " WHERE app_id = %s FOR SHARE",
            (app_id,),
        )
        app_state = cur.fetchone()
        if app_state is None:
            raise WorkspaceError(HTTPStatus.INTERNAL_SERVER_ERROR, "app state is unavailable")
        cur.execute(
            "SELECT row_count, data_bytes FROM web_app_collection_state"
            " WHERE app_id = %s",
            (app_id,),
        )
        collection_state = cur.fetchone()
        if collection_state is None:
            raise WorkspaceError(
                HTTPStatus.INTERNAL_SERVER_ERROR, "app collection state is unavailable"
            )
        cur.execute(
            "SELECT collection, COUNT(*), COALESCE(SUM(value_bytes), 0)"
            " FROM web_app_collection_rows WHERE app_id = %s"
            " GROUP BY collection ORDER BY collection",
            (app_id,),
        )
        collections = [
            {"name": str(name), "rows": int(rows), "bytes": int(size)}
            for name, rows, size in cur.fetchall()
        ]
    return {
        "revision": int(app_state[0]),
        "rows": int(collection_state[0]),
        "bytes": int(collection_state[1]),
        "updated_at": app_state[1],
        "items": collections,
    }

def query_collection(
    app_id: str,
    collection: str,
    body: Any,
    *,
    require_web_app: Callable[[str], Any],
) -> dict[str, Any]:
    """Filter, sort, and page one collection without loading the App document."""
    require_web_app(app_id)
    collection = _validated_collection_name(collection)
    request = {} if body is None else _required_object(body, "collection query")
    _require_keys(
        request,
        {"filters", "ids", "sort", "limit", "offset"},
        required=set(),
    )
    raw_filters = request.get("filters", [])
    if (
        not isinstance(raw_filters, list)
        or len(raw_filters) > MAX_COLLECTION_QUERY_FILTERS
    ):
        raise WorkspaceError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            f"filters must contain 0 to {MAX_COLLECTION_QUERY_FILTERS} entries",
        )

    clauses = ["app_id = %s", "collection = %s"]
    parameters: list[Any] = [app_id, collection]
    for raw_filter in raw_filters:
        item = _required_object(raw_filter, "collection filter")
        operation = _required_text(item.get("op"), "filter op")
        required = {"field", "op", "value"} if operation in {"eq", "ne"} else {"field", "op"}
        _require_keys(item, {"field", "op", "value"}, required=required)
        field = _validated_collection_field(item.get("field"))
        if operation == "eq":
            exact_value = _validated_json_value(item.get("value"), "filter value")
            # GIN narrows candidates while the extracted-value comparison
            # preserves exact object and array equality semantics.
            clauses.append("value_json @> %s AND value_json -> %s = %s")
            parameters.extend(
                (
                    db.jsonb({field: exact_value}),
                    field,
                    db.jsonb(exact_value),
                )
            )
        elif operation == "ne":
            clauses.append("value_json -> %s IS DISTINCT FROM %s")
            parameters.extend(
                (
                    field,
                    db.jsonb(
                        _validated_json_value(item.get("value"), "filter value")
                    ),
                )
            )
        elif operation == "exists":
            clauses.append("value_json ? %s")
            parameters.append(field)
        elif operation == "missing":
            clauses.append("NOT value_json ? %s")
            parameters.append(field)
        else:
            raise WorkspaceError(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "filter op must be eq, ne, exists, or missing",
            )

    ids = request.get("ids")
    if ids is not None:
        if not isinstance(ids, list) or not ids or len(ids) > MAX_COLLECTION_QUERY_LIMIT:
            raise WorkspaceError(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                f"ids must contain 1 to {MAX_COLLECTION_QUERY_LIMIT} row ids",
            )
        normalized_ids = [_validated_collection_row_id(value) for value in ids]
        if len(set(normalized_ids)) != len(normalized_ids):
            raise WorkspaceError(HTTPStatus.UNPROCESSABLE_ENTITY, "ids must be unique")
        placeholders = ",".join("%s" for _ in normalized_ids)
        clauses.append(f"row_id IN ({placeholders})")
        parameters.extend(normalized_ids)

    limit = request.get("limit", 50)
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or limit < 1
        or limit > MAX_COLLECTION_QUERY_LIMIT
    ):
        raise WorkspaceError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            f"limit must be from 1 to {MAX_COLLECTION_QUERY_LIMIT}",
        )
    offset = request.get("offset", 0)
    if (
        isinstance(offset, bool)
        or not isinstance(offset, int)
        or offset < 0
        or offset > MAX_COLLECTION_QUERY_OFFSET
    ):
        raise WorkspaceError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            f"offset must be from 0 to {MAX_COLLECTION_QUERY_OFFSET}",
        )

    order_sql = "row_id ASC"
    order_parameters: list[Any] = []
    if request.get("sort") is not None:
        sort = _required_object(request["sort"], "collection sort")
        _require_keys(sort, {"field", "direction"}, required={"field", "direction"})
        field = _validated_collection_field(sort.get("field"))
        direction = _required_text(sort.get("direction"), "sort direction").lower()
        if direction not in {"asc", "desc"}:
            raise WorkspaceError(
                HTTPStatus.UNPROCESSABLE_ENTITY, "sort direction must be asc or desc"
            )
        order_sql = f"value_json -> %s {direction.upper()} NULLS LAST, row_id ASC"
        order_parameters.append(field)

    where_sql = " AND ".join(clauses)
    with db.transaction() as cur:
        # Every storage path shares the web_apps row lock, so the returned App
        # revision, count, and page describe one coherent logical state.
        cur.execute(
            "SELECT revision, updated_at FROM web_apps"
            " WHERE app_id = %s FOR SHARE",
            (app_id,),
        )
        state = cur.fetchone()
        if state is None:
            raise WorkspaceError(
                HTTPStatus.INTERNAL_SERVER_ERROR, "app collection state is unavailable"
            )
        cur.execute(
            f"SELECT COUNT(*) FROM web_app_collection_rows WHERE {where_sql}",
            tuple(parameters),
        )
        count_row = cur.fetchone()
        total = int(count_row[0]) if count_row is not None else 0
        cur.execute(
            "SELECT row_id, value_json FROM web_app_collection_rows"
            f" WHERE {where_sql} ORDER BY {order_sql} LIMIT %s OFFSET %s",
            (*parameters, *order_parameters, limit, offset),
        )
        rows = [
            {"id": str(row_id), "value": value}
            for row_id, value in cur.fetchall()
        ]
    next_offset = offset + len(rows) if offset + len(rows) < total else None
    return {
        "name": collection,
        "revision": int(state[0]),
        "rows": rows,
        "total": total,
        "offset": offset,
        "next_offset": next_offset,
        "updated_at": state[1],
    }

def apply_collection_actions(
    app_id: str,
    collection: str,
    body: Any,
    *,
    require_web_app: Callable[[str], Any],
    record_revision: Callable[..., None],
    utc_now: Callable[[], str],
) -> dict[str, Any]:
    """Apply one bounded row batch as a new whole-App revision."""
    require_web_app(app_id)
    collection = _validated_collection_name(collection)
    request = _required_object(body, "collection action")
    _require_keys(
        request,
        {"expected_revision", "operations"},
        required={"expected_revision", "operations"},
    )
    expected_revision = _required_counter(
        request.get("expected_revision"), "expected_revision"
    )
    raw_operations = request.get("operations")
    if (
        not isinstance(raw_operations, list)
        or not raw_operations
        or len(raw_operations) > MAX_COLLECTION_BATCH_OPERATIONS
    ):
        raise WorkspaceError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "operations must contain 1 to"
            f" {MAX_COLLECTION_BATCH_OPERATIONS} row actions",
        )
    operations: list[tuple[str, str, dict[str, Any] | None, int]] = []
    seen_ids: set[str] = set()
    for raw_operation in raw_operations:
        operation = _required_object(raw_operation, "collection row action")
        name = _required_text(operation.get("action"), "action")
        required = {"action", "id", "value"} if name == "upsert" else {"action", "id"}
        _require_keys(operation, {"action", "id", "value"}, required=required)
        if name not in {"upsert", "delete"}:
            raise WorkspaceError(
                HTTPStatus.UNPROCESSABLE_ENTITY, "row action must be upsert or delete"
            )
        row_id = _validated_collection_row_id(operation.get("id"))
        if row_id in seen_ids:
            raise WorkspaceError(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "a row id may appear only once in one batch",
            )
        seen_ids.add(row_id)
        if name == "upsert":
            row_value, value_bytes = _validated_collection_row(operation.get("value"))
            operations.append((name, row_id, row_value, value_bytes))
        else:
            operations.append((name, row_id, None, 0))

    now = utc_now()
    with db.transaction() as cur:
        cur.execute(
            f"SELECT {STATE_COLUMNS} FROM web_apps"
            " WHERE app_id = %s FOR UPDATE",
            (app_id,),
        )
        app_state = cur.fetchone()
        if app_state is None:
            raise WorkspaceError(HTTPStatus.INTERNAL_SERVER_ERROR, "app state is unavailable")
        if int(app_state[0]) != expected_revision:
            raise WorkspaceError(
                HTTPStatus.CONFLICT, "the app changed; read state and retry"
            )
        cur.execute(
            "SELECT row_count, data_bytes FROM web_app_collection_state"
            " WHERE app_id = %s FOR UPDATE",
            (app_id,),
        )
        collection_state = cur.fetchone()
        if collection_state is None:
            raise WorkspaceError(
                HTTPStatus.INTERNAL_SERVER_ERROR, "app collection state is unavailable"
            )
        placeholders = ",".join("%s" for _ in seen_ids)
        cur.execute(
            "SELECT row_id, value_bytes FROM web_app_collection_rows"
            f" WHERE app_id = %s AND collection = %s AND row_id IN ({placeholders})",
            (app_id, collection, *seen_ids),
        )
        existing = {str(row_id): int(size) for row_id, size in cur.fetchall()}
        row_count = int(collection_state[0])
        data_bytes = int(collection_state[1])
        for name, row_id, _value, value_bytes in operations:
            previous = existing.get(row_id)
            if name == "delete":
                if previous is None:
                    raise WorkspaceError(
                        HTTPStatus.UNPROCESSABLE_ENTITY, "collection row does not exist"
                    )
                row_count -= 1
                data_bytes -= previous
            elif previous is None:
                row_count += 1
                data_bytes += value_bytes
            else:
                data_bytes += value_bytes - previous
        if row_count > MAX_COLLECTION_ROWS:
            raise WorkspaceError(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                f"app collections exceed {MAX_COLLECTION_ROWS} rows",
            )
        if data_bytes > MAX_COLLECTION_DATA_BYTES:
            raise WorkspaceError(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                f"app collections exceed {MAX_COLLECTION_DATA_BYTES} bytes",
            )
        if not existing and any(name == "upsert" for name, *_rest in operations):
            cur.execute(
                "SELECT COUNT(DISTINCT collection) FROM web_app_collection_rows"
                " WHERE app_id = %s",
                (app_id,),
            )
            collection_count_row = cur.fetchone()
            assert collection_count_row is not None
            collection_count = int(collection_count_row[0])
            if collection_count >= MAX_COLLECTIONS:
                cur.execute(
                    "SELECT 1 FROM web_app_collection_rows"
                    " WHERE app_id = %s AND collection = %s LIMIT 1",
                    (app_id, collection),
                )
                if cur.fetchone() is None:
                    raise WorkspaceError(
                        HTTPStatus.CONFLICT,
                        f"an app may retain at most {MAX_COLLECTIONS} collections",
                    )
        for name, row_id, stored_value, value_bytes in operations:
            if name == "delete":
                cur.execute(
                    "DELETE FROM web_app_collection_rows"
                    " WHERE app_id = %s AND collection = %s AND row_id = %s",
                    (app_id, collection, row_id),
                )
            else:
                cur.execute(
                    "INSERT INTO web_app_collection_rows"
                    " (app_id, collection, row_id, value_json, value_bytes, updated_at)"
                    " VALUES (%s, %s, %s, %s, %s, %s)"
                    " ON CONFLICT (app_id, collection, row_id) DO UPDATE SET"
                    " value_json = EXCLUDED.value_json,"
                    " value_bytes = EXCLUDED.value_bytes,"
                    " updated_at = EXCLUDED.updated_at",
                    (
                        app_id,
                        collection,
                        row_id,
                        db.jsonb(stored_value),
                        value_bytes,
                        now,
                    ),
                )
        cur.execute(
            "UPDATE web_app_collection_state SET row_count = %s, data_bytes = %s"
            " WHERE app_id = %s",
            (row_count, data_bytes, app_id),
        )
        cur.execute(
            "UPDATE web_apps SET revision = revision + 1, updated_at = %s"
            " WHERE app_id = %s"
            f" RETURNING {STATE_COLUMNS}",
            (now, app_id),
        )
        changed = cur.fetchone()
        assert changed is not None
        record_revision(
            cur, app_id, changed, "agent", "collection", None
        )
    return {"ok": True, "revision": int(changed[0]), "updated_at": changed[5]}

def _validated_collection_name(value: Any) -> str:
    if not isinstance(value, str) or COLLECTION_NAME_RE.fullmatch(value) is None:
        raise WorkspaceError(
            HTTPStatus.BAD_REQUEST,
            "collection must start with a lowercase letter and contain only"
            " lowercase letters, numbers, _ or -",
        )
    return value

def _validated_collection_row_id(value: Any) -> str:
    if not isinstance(value, str) or COLLECTION_ROW_ID_RE.fullmatch(value) is None:
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, "invalid collection row id")
    return value

def _validated_collection_field(value: Any) -> str:
    size = _utf8_length(value) if isinstance(value, str) else None
    if not value or size is None or size > MAX_COLLECTION_FIELD_BYTES or "\0" in value:
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, "invalid collection field")
    assert isinstance(value, str)
    return value

def _validated_json_value(value: Any, label: str) -> Any:
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
        canonical = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise WorkspaceError(
            HTTPStatus.UNPROCESSABLE_ENTITY, f"{label} must contain only JSON values"
        ) from exc
    if _json_contains_nul(canonical):
        raise WorkspaceError(
            HTTPStatus.UNPROCESSABLE_ENTITY, f"{label} must not contain NUL characters"
        )
    if _json_contains_invalid_unicode(canonical):
        raise WorkspaceError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            f"{label} must contain valid Unicode text",
        )
    return canonical

def _validated_collection_row(value: Any) -> tuple[dict[str, Any], int]:
    if not isinstance(value, dict):
        raise WorkspaceError(
            HTTPStatus.UNPROCESSABLE_ENTITY, "collection row value must be an object"
        )
    canonical = _validated_json_value(value, "collection row value")
    for field in canonical:
        _validated_collection_field(field)
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    if len(encoded) > MAX_COLLECTION_ROW_BYTES:
        raise WorkspaceError(
            HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            f"collection row exceeds {MAX_COLLECTION_ROW_BYTES} bytes",
        )
    return canonical, len(encoded)

def _json_contains_nul(value: Any) -> bool:
    if isinstance(value, str):
        return "\0" in value
    if isinstance(value, list):
        return any(_json_contains_nul(item) for item in value)
    if isinstance(value, dict):
        return any("\0" in key or _json_contains_nul(item) for key, item in value.items())
    return False

def _json_contains_invalid_unicode(value: Any) -> bool:
    if isinstance(value, str):
        try:
            value.encode()
        except UnicodeEncodeError:
            return True
        return False
    if isinstance(value, list):
        return any(_json_contains_invalid_unicode(item) for item in value)
    if isinstance(value, dict):
        return any(
            _json_contains_invalid_unicode(key) or _json_contains_invalid_unicode(item)
            for key, item in value.items()
        )
    return False

def _collection_snapshot_json(cur: Any, app_id: str) -> str:
    """Encode the complete row store for one retained App revision."""
    cur.execute(
        "SELECT collection, row_id, value_json FROM web_app_collection_rows"
        " WHERE app_id = %s ORDER BY collection, row_id",
        (app_id,),
    )
    collections: dict[str, dict[str, Any]] = {}
    for collection, row_id, value in cur.fetchall():
        collections.setdefault(str(collection), {})[str(row_id)] = value
    return json.dumps(
        collections, sort_keys=True, separators=(",", ":"), allow_nan=False
    )

def _restore_collection_snapshot(
    cur: Any, app_id: str, encoded: str, now: str
) -> None:
    """Replace the current row store with one complete retained copy."""
    try:
        collections = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise WorkspaceError(
            HTTPStatus.INTERNAL_SERVER_ERROR,
            "app revision collection snapshot is invalid",
        ) from exc
    if not isinstance(collections, dict):
        raise WorkspaceError(
            HTTPStatus.INTERNAL_SERVER_ERROR,
            "app revision collection snapshot is invalid",
        )

    rows: list[tuple[str, str, dict[str, Any], int]] = []
    data_bytes = 0
    for raw_collection, raw_values in collections.items():
        collection = _validated_collection_name(raw_collection)
        if not isinstance(raw_values, dict):
            raise WorkspaceError(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "app revision collection snapshot is invalid",
            )
        for raw_row_id, raw_value in raw_values.items():
            row_id = _validated_collection_row_id(raw_row_id)
            value, value_bytes = _validated_collection_row(raw_value)
            rows.append((collection, row_id, value, value_bytes))
            data_bytes += value_bytes
    if (
        len(collections) > MAX_COLLECTIONS
        or len(rows) > MAX_COLLECTION_ROWS
        or data_bytes > MAX_COLLECTION_DATA_BYTES
    ):
        raise WorkspaceError(
            HTTPStatus.INTERNAL_SERVER_ERROR,
            "app revision collection snapshot exceeds collection limits",
        )

    cur.execute("DELETE FROM web_app_collection_rows WHERE app_id = %s", (app_id,))
    batch: list[tuple[str, str, dict[str, Any], int]] = []
    batch_bytes = 0
    for row in rows:
        if batch and (
            len(batch) >= MAX_COLLECTION_RESTORE_BATCH_ROWS
            or batch_bytes + row[3] > MAX_COLLECTION_RESTORE_BATCH_BYTES
        ):
            _insert_collection_snapshot_batch(cur, app_id, batch, now)
            batch = []
            batch_bytes = 0
        batch.append(row)
        batch_bytes += row[3]
    if batch:
        _insert_collection_snapshot_batch(cur, app_id, batch, now)
    cur.execute(
        "UPDATE web_app_collection_state SET row_count = %s, data_bytes = %s"
        " WHERE app_id = %s",
        (len(rows), data_bytes, app_id),
    )

def _insert_collection_snapshot_batch(
    cur: Any,
    app_id: str,
    rows: list[tuple[str, str, dict[str, Any], int]],
    now: str,
) -> None:
    placeholders = ",".join("(%s, %s, %s, %s, %s, %s)" for _row in rows)
    parameters: list[Any] = []
    for collection, row_id, value, value_bytes in rows:
        parameters.extend(
            (app_id, collection, row_id, db.jsonb(value), value_bytes, now)
        )
    cur.execute(
        "INSERT INTO web_app_collection_rows"
        " (app_id, collection, row_id, value_json, value_bytes, updated_at)"
        f" VALUES {placeholders}",
        tuple(parameters),
    )
