"""Shared immutable components and row versions for whole-App recovery.

Callers hold the App row lock. Checkpoints remain independent recovery points;
collection intervals describe state at a revision without replaying operations.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def component_versions(
    cur: Any, app_id: str, html: str, css: str, javascript: str, data_json: str,
    kind: str, restored_from: int | None,
) -> tuple[str, str]:
    if restored_from is not None:
        cur.execute(
            "SELECT ui_version, document_version FROM web_app_revisions"
            " WHERE app_id = %s AND revision = %s", (app_id, restored_from),
        )
    else:
        cur.execute(
            "SELECT ui_version, document_version FROM web_app_revisions"
            " WHERE app_id = %s ORDER BY revision DESC LIMIT 1", (app_id,),
        )
    previous = cur.fetchone()
    if previous is not None and (kind == "collection" or restored_from is not None):
        return str(previous[0]), str(previous[1])
    ui_version = str(previous[0]) if previous is not None and kind == "data" else hashlib.sha256(
        json.dumps([html, css, javascript], ensure_ascii=True).encode()
    ).hexdigest()
    document_version = hashlib.sha256(data_json.encode()).hexdigest()
    if previous is None or ui_version != previous[0]:
        cur.execute(
            "INSERT INTO web_app_ui_versions (app_id, version, html, css, javascript)"
            " VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (app_id, ui_version, html, css, javascript),
        )
    if previous is None or document_version != previous[1]:
        cur.execute(
            "INSERT INTO web_app_document_versions (app_id, version, data_json)"
            " VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
            (app_id, document_version, data_json),
        )
    return ui_version, document_version


def record_collection_changes(
    cur: Any, app_id: str, collection: str, revision: int,
    operations: list[tuple[str, str, dict[str, Any] | None, int]],
) -> None:
    from host.runtime.core import db

    for action, row_id, value, _size in operations:
        # Equal-value upserts leave the existing interval open. Deletes close
        # it without inserting a tombstone; absence at a revision means deleted.
        cur.execute(
            "UPDATE web_app_collection_versions SET valid_until = %s"
            " WHERE app_id = %s AND collection = %s AND row_id = %s"
            " AND valid_until IS NULL AND (%s OR value_json IS DISTINCT FROM %s)",
            (revision, app_id, collection, row_id, action == "delete", db.jsonb(value)),
        )
        if action == "upsert":
            cur.execute(
                "INSERT INTO web_app_collection_versions"
                " (app_id, collection, row_id, valid_from, value_json)"
                " SELECT %s, %s, %s, %s, %s WHERE NOT EXISTS"
                " (SELECT 1 FROM web_app_collection_versions WHERE app_id = %s"
                " AND collection = %s AND row_id = %s AND valid_until IS NULL)",
                (app_id, collection, row_id, revision, db.jsonb(value),
                 app_id, collection, row_id),
            )


def collection_snapshot(cur: Any, app_id: str, revision: int) -> str:
    cur.execute(
        "SELECT collection, row_id, value_json FROM web_app_collection_versions"
        " WHERE app_id = %s AND valid_from <= %s"
        " AND (valid_until IS NULL OR valid_until > %s)",
        (app_id, revision, revision),
    )
    rows: dict[str, dict[str, Any]] = {}
    for collection, row_id, value in cur.fetchall():
        rows.setdefault(str(collection), {})[str(row_id)] = value
    return json.dumps(rows, separators=(",", ":"), allow_nan=False)


def record_restored_collections(cur: Any, app_id: str, revision: int) -> None:
    """Version the difference between history and the restored live row store."""
    cur.execute(
        "UPDATE web_app_collection_versions h SET valid_until = %s"
        " WHERE h.app_id = %s AND h.valid_until IS NULL AND NOT EXISTS"
        " (SELECT 1 FROM web_app_collection_rows r WHERE r.app_id = h.app_id"
        " AND r.collection = h.collection AND r.row_id = h.row_id"
        " AND r.value_json = h.value_json)",
        (revision, app_id),
    )
    cur.execute(
        "INSERT INTO web_app_collection_versions"
        " (app_id, collection, row_id, valid_from, value_json)"
        " SELECT r.app_id, r.collection, r.row_id, %s, r.value_json"
        " FROM web_app_collection_rows r WHERE r.app_id = %s AND NOT EXISTS"
        " (SELECT 1 FROM web_app_collection_versions h WHERE h.app_id = r.app_id"
        " AND h.collection = r.collection AND h.row_id = r.row_id"
        " AND h.valid_until IS NULL)",
        (revision, app_id),
    )


def prune_components(cur: Any, app_id: str) -> None:
    # Open collection intervals are current state, even when no recovery point
    # references them yet. Closed intervals survive if any checkpoint needs them.
    cur.execute(
        "DELETE FROM web_app_collection_versions h WHERE h.app_id = %s"
        " AND h.valid_until IS NOT NULL AND NOT EXISTS"
        " (SELECT 1 FROM web_app_revisions r WHERE r.app_id = h.app_id"
        " AND r.revision >= h.valid_from AND r.revision < h.valid_until)",
        (app_id,),
    )
    cur.execute(
        "DELETE FROM web_app_ui_versions v WHERE v.app_id = %s AND NOT EXISTS"
        " (SELECT 1 FROM web_app_revisions r WHERE r.app_id = v.app_id"
        " AND r.ui_version = v.version)",
        (app_id,),
    )
    cur.execute(
        "DELETE FROM web_app_document_versions v WHERE v.app_id = %s AND NOT EXISTS"
        " (SELECT 1 FROM web_app_revisions r WHERE r.app_id = v.app_id"
        " AND r.document_version = v.version)",
        (app_id,),
    )
