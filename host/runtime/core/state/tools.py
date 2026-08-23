"""Bundled-tool configuration, credentials, approvals, and audit state."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hmac
import json
import secrets
import time
from typing import Any

from host.runtime.core import db, secretbox
from host.runtime.core.state._base import (
    EVENT_PAGE_LIMIT,
    HOST_DIAGNOSTIC_COALESCE_SECONDS,
    HOST_DIAGNOSTIC_LIMIT,
    HOST_DIAGNOSTIC_PRUNE_EVERY,
    PRUNE_EVERY,
    TOOL_EVENT_LIMIT,
    mutation,
    utc_now,
)
from host.runtime.core.state.events import _page_before, _prune_events

# -- tools ---------------------------------------------------------------------


# Public "approval_<number>" ids, like "task_<number>" for tasks.
_APPROVAL_ID_PREFIX = "approval_"
_TOOL_APPROVAL_FIELDS = (
    "number, tool_id, action_id, status, summary, payload, check_token, result,"
    " created_at, decided_at, connection_id, account_id, account_label"
)


class PendingToolApprovalLimitReached(Exception):
    """Raised when inserting another pending tool approval would exceed the cap."""


def enabled_tool_ids() -> set[str]:
    with db.transaction() as cur:
        cur.execute("SELECT tool_id FROM enabled_tools")
        return {row[0] for row in cur.fetchall()}


def set_tool_enabled(cur: Any, tool_id: str, enabled: bool) -> None:
    if enabled:
        cur.execute(
            "INSERT INTO enabled_tools (tool_id) VALUES (%s) ON CONFLICT (tool_id) DO NOTHING",
            (tool_id,),
        )
    else:
        cur.execute("DELETE FROM enabled_tools WHERE tool_id = %s", (tool_id,))


def tool_config_keys(tool_id: str) -> set[str]:
    """The configured key names for one tool; values stay in the database
    except for tool_config_values callers building a tool call's config view."""
    with db.transaction() as cur:
        cur.execute("SELECT key FROM tool_config WHERE tool_id = %s", (tool_id,))
        return {row[0] for row in cur.fetchall()}


def tool_config_values(tool_id: str, keys: list[str]) -> dict[str, str]:
    """Configured values for one tool's manifest keys. Config is scoped per
    tool, so a shared key name resolves to this tool's own value. Values are
    secretbox ciphertext at rest and decrypted here for the tool call's config
    view."""
    wanted = set(keys)
    if not wanted:
        return {}
    with db.transaction() as cur:
        cur.execute("SELECT key, value FROM tool_config WHERE tool_id = %s", (tool_id,))
        return {row[0]: secretbox.decrypt(row[1]) for row in cur.fetchall() if row[0] in wanted}


def save_tool_config_value(cur: Any, tool_id: str, key: str, value: str) -> None:
    """Set one tool's deployment config value; an empty value clears the key.
    Stored as secretbox ciphertext so config secrets never sit in the clear."""
    if value:
        cur.execute(
            "INSERT INTO tool_config (tool_id, key, value) VALUES (%s, %s, %s)"
            " ON CONFLICT (tool_id, key) DO UPDATE SET value = EXCLUDED.value",
            (tool_id, key, secretbox.encrypt(value)),
        )
    else:
        cur.execute("DELETE FROM tool_config WHERE tool_id = %s AND key = %s", (tool_id, key))


def tool_credential(tool_id: str, connection_id: str) -> dict[str, Any] | None:
    """One connection's stored OAuth credential (the store behind HostAPI.credentials),
    reassembled into the StoredCredential shape from its columns, or None if
    that connection is absent."""
    with db.transaction() as cur:
        cur.execute(
            "SELECT account_id, account_label, account_scopes, secret, metadata"
            " FROM tool_credentials WHERE tool_id = %s AND connection_id = %s",
            (tool_id, connection_id),
        )
        row = cur.fetchone()
    if row is None:
        return None
    account_id, account_label, account_scopes, secret_ciphertext, metadata = row
    secret = json.loads(secretbox.decrypt(secret_ciphertext))
    return {
        "account": {
            "id": account_id,
            "label": account_label,
            "scopes": [str(scope) for scope in account_scopes] if isinstance(account_scopes, list) else [],
        },
        "secret": secret if isinstance(secret, dict) else {},
        "metadata": metadata if isinstance(metadata, dict) else {},
    }


def tool_connections(tool_id: str) -> list[dict[str, Any]]:
    """List one OAuth tool's non-secret connected accounts in stable order."""
    with db.transaction() as cur:
        cur.execute(
            "SELECT connection_id, account_id, account_label, account_scopes"
            " FROM tool_credentials WHERE tool_id = %s ORDER BY connection_id",
            (tool_id,),
        )
        rows = cur.fetchall()
    return [
        {
            "connection_id": connection_id,
            "account": {
                "id": account_id,
                "label": account_label,
                "scopes": [str(scope) for scope in account_scopes]
                if isinstance(account_scopes, list)
                else [],
            },
        }
        for connection_id, account_id, account_label, account_scopes in rows
    ]


def put_tool_credential(
    tool_id: str, value: dict[str, Any], connection_id: str
) -> None:
    """Store a StoredCredential in its columns. Only the provider token
    material is a secret: it is serialized and secretbox-encrypted; the
    connected-account fields and tool bookkeeping are non-secret by contract
    (host/tools/host_api.py) and stored as plain columns. Malformed records
    are rejected rather than stored partially."""
    account = value.get("account")
    secret = value.get("secret")
    metadata = value.get("metadata")
    if (
        not isinstance(account, dict)
        or not isinstance(account.get("id"), str)
        or not account["id"]
        or not isinstance(account.get("label"), str)
        or not isinstance(account.get("scopes"), list)
        or not isinstance(secret, dict)
        or not isinstance(metadata, dict)
    ):
        raise ValueError(f"malformed stored credential for tool {tool_id}")
    with mutation() as cur:
        cur.execute(
            "SELECT connection_id FROM tool_credentials"
            " WHERE tool_id = %s AND account_id = %s AND connection_id <> %s",
            (tool_id, account["id"], connection_id),
        )
        duplicate = cur.fetchone()
        if duplicate is not None:
            raise ValueError(
                f"{account['label'] or account['id']} is already connected to {tool_id}."
            )
        cur.execute(
            "INSERT INTO tool_credentials"
            " (tool_id, connection_id, account_id, account_label, account_scopes, secret, metadata)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (tool_id, connection_id) DO UPDATE SET"
            " account_label = EXCLUDED.account_label, account_scopes = EXCLUDED.account_scopes,"
            " secret = EXCLUDED.secret, metadata = EXCLUDED.metadata"
            " WHERE tool_credentials.account_id = EXCLUDED.account_id"
            " RETURNING account_id",
            (
                tool_id,
                connection_id,
                account["id"],
                account["label"],
                db.jsonb([str(scope) for scope in account["scopes"]]),
                secretbox.encrypt(json.dumps(secret)),
                db.jsonb(metadata),
            ),
        )
        if cur.fetchone() is None:
            raise ValueError(
                f"Connection {connection_id} is already bound to a different {tool_id} account."
            )


def delete_tool_credential(tool_id: str, connection_id: str) -> None:
    with mutation() as cur:
        cur.execute(
            "DELETE FROM tool_credentials WHERE tool_id = %s AND connection_id = %s",
            (tool_id, connection_id),
        )


# -- tool audit log ------------------------------------------------------------
# The tool-side peer of the agent and network event logs: one row per tool
# event, paged newest-first with the same before-cursor model.

_TOOL_EVENT_FIELDS = (
    "seq, created_at, tool_id, action_id, outcome, detail, arguments,"
    " connection_id, account_id, account_label"
)


def _tool_event_dict(row: Any, *, include_arguments: bool = False) -> dict[str, Any]:
    (
        seq, created_at, tool_id, action_id, outcome, detail, arguments,
        connection_id, account_id, account_label,
    ) = row
    event: dict[str, Any] = {
        "seq": int(seq),
        "timestamp": created_at,
        "event_id": f"tool_event_{seq}",
        "tool_id": tool_id,
        "action_id": action_id,
        "outcome": outcome,
        "detail": detail or "",
        "has_arguments": isinstance(arguments, dict),
        "connection_id": connection_id or "",
        "account_id": account_id or "",
        "account_label": account_label or "",
    }
    if include_arguments:
        event["arguments"] = arguments if isinstance(arguments, dict) else None
    return event


def record_tool_event(
    tool_id: str,
    action_id: str,
    outcome: str,
    detail: str = "",
    arguments: dict[str, Any] | None = None,
    *,
    connection_id: str = "",
    account_id: str = "",
    account_label: str = "",
) -> None:
    """Append one tool audit event in its own transaction. seq is a serial:
    unique and increasing, with harmless gaps from aborted transactions.
    Prunes to TOOL_EVENT_LIMIT amortized, like the other event logs."""
    with mutation() as cur:
        cur.execute(
            "INSERT INTO tool_events"
            " (created_at, tool_id, action_id, outcome, detail, arguments,"
            " connection_id, account_id, account_label)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING seq",
            (
                utc_now(), tool_id, action_id, outcome, detail,
                db.jsonb(arguments) if arguments is not None else None,
                connection_id, account_id, account_label,
            ),
        )
        if int(cur.fetchone()[0]) % PRUNE_EVERY == 0:
            _prune_events(cur, "tool_events", TOOL_EVENT_LIMIT)


def page_tool_events_before(
    before: int | None, *, limit: int = EVENT_PAGE_LIMIT
) -> list[dict[str, Any]]:
    return _page_before("tool_events", _TOOL_EVENT_FIELDS, _tool_event_dict, before, limit)


def tool_event(seq: int) -> dict[str, Any] | None:
    """Load one audit event with its exact arguments for an operator expansion."""
    with db.transaction() as cur:
        cur.execute(f"SELECT {_TOOL_EVENT_FIELDS} FROM tool_events WHERE seq = %s", (seq,))
        row = cur.fetchone()
    return _tool_event_dict(row, include_arguments=True) if row is not None else None


# -- host diagnostics log ------------------------------------------------------
# A journald collector is the sole writer.

_HOST_DIAGNOSTIC_FIELDS = (
    "id, seq, first_seen_at, last_seen_at, service, component, kind,"
    " severity, exception_type, summary, traceback, context, fingerprint,"
    " occurrence_count, host_version, boot_id, pid"
)


def _host_diagnostic_dict(row: Any, *, include_details: bool = False) -> dict[str, Any]:
    (
        diagnostic_id,
        seq,
        first_seen_at,
        last_seen_at,
        service,
        component,
        kind,
        severity,
        exception_type,
        summary,
        trace,
        context,
        fingerprint,
        occurrence_count,
        host_version,
        boot_id,
        pid,
    ) = row
    diagnostic: dict[str, Any] = {
        "id": int(diagnostic_id),
        "seq": int(seq),
        "diagnostic_id": f"host_diagnostic_{diagnostic_id}",
        "first_seen_at": first_seen_at,
        "last_seen_at": last_seen_at,
        "service": service,
        "component": component,
        "kind": kind,
        "severity": severity,
        "exception_type": exception_type,
        "summary": summary,
        "occurrence_count": int(occurrence_count),
        "host_version": host_version,
        "boot_id": boot_id,
        "pid": int(pid) if pid is not None else None,
        "has_details": bool(trace or context),
    }
    if include_details:
        diagnostic["traceback"] = trace
        diagnostic["context"] = dict(context) if isinstance(context, dict) else {}
        diagnostic["fingerprint"] = fingerprint
    return diagnostic


def ingest_host_diagnostic(
    realtime_usec: int,
    event: dict[str, Any],
) -> int:
    """Store or briefly coalesce one validated journal diagnostic."""
    from datetime import datetime, timedelta, timezone

    seen_at = (
        datetime.fromtimestamp(realtime_usec / 1_000_000, timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    cutoff = (
        datetime.fromtimestamp(realtime_usec / 1_000_000, timezone.utc)
        - timedelta(seconds=HOST_DIAGNOSTIC_COALESCE_SECONDS)
    ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    with db.transaction() as cur:
        cur.execute(
            "SELECT id FROM host_diagnostics"
            " WHERE fingerprint = %s AND service = %s AND last_seen_at >= %s"
            " ORDER BY seq DESC LIMIT 1 FOR UPDATE",
            (event["fingerprint"], event["service"], cutoff),
        )
        existing = cur.fetchone()
        if existing is not None:
            existing_id = int(existing[0])
            cur.execute(
                "UPDATE host_diagnostics SET seq = nextval('host_diagnostics_seq_seq'),"
                " last_seen_at = %s,"
                " occurrence_count = occurrence_count + 1, summary = %s,"
                " traceback = %s, context = %s, host_version = %s,"
                " boot_id = %s, pid = %s WHERE id = %s RETURNING id, seq",
                (
                    seen_at,
                    event["summary"],
                    event["traceback"],
                    db.jsonb(event["context"]),
                    event["host_version"],
                    event["boot_id"],
                    event["pid"],
                    existing_id,
                ),
            )
            updated = cur.fetchone()
            assert updated is not None
            diagnostic_id, seq = int(updated[0]), int(updated[1])
        else:
            cur.execute(
                "INSERT INTO host_diagnostics"
                " (first_seen_at, last_seen_at, service, component, kind,"
                " severity, exception_type, summary, traceback, context, fingerprint,"
                " occurrence_count, host_version, boot_id, pid)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 1, %s, %s, %s)"
                " RETURNING id, seq",
                (
                    seen_at,
                    seen_at,
                    event["service"],
                    event["component"],
                    event["kind"],
                    event["severity"],
                    event["exception_type"],
                    event["summary"],
                    event["traceback"],
                    db.jsonb(event["context"]),
                    event["fingerprint"],
                    event["host_version"],
                    event["boot_id"],
                    event["pid"],
                ),
            )
            inserted = cur.fetchone()
            assert inserted is not None
            diagnostic_id, seq = int(inserted[0]), int(inserted[1])
        if seq % HOST_DIAGNOSTIC_PRUNE_EVERY == 0:
            prune_host_diagnostics(cur)
    return diagnostic_id


def prune_host_diagnostics(cur: Any) -> None:
    """Keep the newest bounded host diagnostics across both severities."""
    cur.execute(
        "DELETE FROM host_diagnostics WHERE"
        " seq < COALESCE(("
        " SELECT seq FROM host_diagnostics ORDER BY seq DESC OFFSET %s LIMIT 1"
        "), 0)",
        (HOST_DIAGNOSTIC_LIMIT - 1,),
    )


def page_host_diagnostics_before(
    before: int | None,
    *,
    service: str | None = None,
    severity: str | None = None,
    limit: int = EVENT_PAGE_LIMIT,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if service is not None:
        clauses.append("service = %s")
        params.append(service)
    if severity is not None:
        clauses.append("severity = %s")
        params.append(severity)
    return _page_before(
        "host_diagnostics",
        _HOST_DIAGNOSTIC_FIELDS,
        _host_diagnostic_dict,
        before,
        limit,
        extra_clause=" AND ".join(clauses) if clauses else None,
        extra_params=tuple(params),
    )


def host_diagnostic(diagnostic_id: int) -> dict[str, Any] | None:
    with db.transaction() as cur:
        cur.execute(
            f"SELECT {_HOST_DIAGNOSTIC_FIELDS} FROM host_diagnostics WHERE id = %s",
            (diagnostic_id,),
        )
        row = cur.fetchone()
    return _host_diagnostic_dict(row, include_details=True) if row is not None else None


def _approval_id(number: int, check_token: str) -> str:
    # The public id carries the unguessable check token, so the id itself is
    # the agent's poll capability: no separate token to marry back up, and the
    # sequential number alone cannot be enumerated. token_urlsafe has no dots,
    # so the number splits off unambiguously.
    return f"{_APPROVAL_ID_PREFIX}{number}.{check_token}"


def _tool_approval_dict(row: Any) -> dict[str, Any]:
    (
        number, tool_id, action_id, status, summary, payload, check_token,
        result, created_at, decided_at, connection_id, account_id, account_label,
    ) = row
    return {
        "approval_id": _approval_id(number, check_token),
        "tool_id": tool_id,
        "action_id": action_id,
        "status": status,
        "summary": summary,
        "payload": dict(payload) if isinstance(payload, dict) else {},
        "result": result or "",
        "created_at": int(created_at),
        "decided_at": int(decided_at),
        "connection_id": connection_id,
        "account_id": account_id,
        "account_label": account_label,
    }


def _approval_number(approval_id: str) -> int | None:
    if not isinstance(approval_id, str) or not approval_id.startswith(_APPROVAL_ID_PREFIX):
        return None
    number_part = approval_id[len(_APPROVAL_ID_PREFIX):].split(".", 1)[0]
    return int(number_part) if number_part.isdigit() else None


def insert_tool_approval(
    tool_id: str,
    action_id: str,
    summary: str,
    payload: dict[str, Any],
    created_at: int,
    *,
    pending_limit: int,
    connection_id: str = "",
    account_id: str = "",
    account_label: str = "",
) -> dict[str, Any]:
    with mutation() as cur:
        # All inserts run in the tools service under this process's
        # mutation lock, so the count check cannot race another insert.
        # (The admin maintenance pass may expire rows concurrently, which
        # only makes the backpressure count conservative.)
        cur.execute("SELECT COUNT(*) FROM tool_approvals WHERE status = 'pending'")
        if int(cur.fetchone()[0]) >= pending_limit:
            raise PendingToolApprovalLimitReached()
        cur.execute(
            "INSERT INTO tool_approvals"
            " (tool_id, action_id, status, summary, payload, check_token, created_at,"
            " connection_id, account_id, account_label)"
            " VALUES (%s, %s, 'pending', %s, %s, %s, %s, %s, %s, %s)"
            f" RETURNING {_TOOL_APPROVAL_FIELDS}",
            (
                tool_id, action_id, summary, db.jsonb(payload),
                secrets.token_urlsafe(32), created_at, connection_id,
                account_id, account_label,
            ),
        )
        return _tool_approval_dict(cur.fetchone())


def tool_approval(approval_id: str, tool_id: str | None = None) -> dict[str, Any] | None:
    """The approval record for the full ``approval_<n>.<token>`` id, optionally
    restricted to one tool's partition. Returns None unless the id's token
    matches the stored one (constant-time), so a guessed number never
    resolves — verification for every caller lives here."""
    number = _approval_number(approval_id)
    if number is None:
        return None
    with db.transaction() as cur:
        if tool_id is None:
            cur.execute(
                f"SELECT {_TOOL_APPROVAL_FIELDS} FROM tool_approvals WHERE number = %s",
                (number,),
            )
        else:
            cur.execute(
                f"SELECT {_TOOL_APPROVAL_FIELDS} FROM tool_approvals"
                " WHERE number = %s AND tool_id = %s",
                (number, tool_id),
            )
        row = cur.fetchone()
    if row is None:
        return None
    record = _tool_approval_dict(row)
    if not hmac.compare_digest(approval_id, record["approval_id"]):
        return None
    return record


def list_tool_approvals(limit: int, tool_id: str | None = None) -> list[dict[str, Any]]:
    """Newest approvals first, pending before decided so open decisions
    surface at the top of the admin UI. Scoped to one tool when tool_id is set,
    which is how the operator UI shows approvals per tool rather than unified."""
    order = " ORDER BY (status = 'pending') DESC, number DESC LIMIT %s"
    with db.transaction() as cur:
        if tool_id is None:
            cur.execute(f"SELECT {_TOOL_APPROVAL_FIELDS} FROM tool_approvals{order}", (limit,))
        else:
            cur.execute(
                f"SELECT {_TOOL_APPROVAL_FIELDS} FROM tool_approvals WHERE tool_id = %s{order}",
                (tool_id, limit),
            )
        return [_tool_approval_dict(row) for row in cur.fetchall()]


def transition_tool_approval(
    approval_id: str,
    from_status: str,
    to_status: str,
    decided_at: int,
    result: str | None = None,
) -> bool:
    """Atomic conditional status transition; False when the record is absent
    or no longer in from_status, so concurrent decisions cannot both win.
    ``result`` is the terminal outcome text: the approved action's
    user-visible message when it executed, or the error when it failed."""
    number = _approval_number(approval_id)
    if number is None:
        return False
    with mutation() as cur:
        cur.execute(
            "UPDATE tool_approvals SET status = %s, decided_at = %s,"
            " result = COALESCE(%s, result)"
            " WHERE number = %s AND status = %s RETURNING number",
            (to_status, decided_at, result, number, from_status),
        )
        return cur.fetchone() is not None


def fail_approved_tool_approvals(decided_at: int) -> None:
    """Mark every approval stuck in ``approved`` as failed — a direct scan, so
    no record escapes through a listing horizon. Write a failure ``result``
    too, so ``check_tool_approval`` reports the interrupted execution instead
    of an empty outcome."""
    result = "The tools service restarted while executing this approved action; its outcome is unknown."
    with mutation() as cur:
        cur.execute(
            "UPDATE tool_approvals SET status = 'failed', decided_at = %s, result = %s WHERE status = 'approved'",
            (decided_at, result),
        )


def expire_tool_approvals(cutoff: int) -> None:
    """Expire pending approvals created before the cutoff (host expiry
    policy, applied by the maintenance pass)."""
    with mutation() as cur:
        cur.execute(
            "UPDATE tool_approvals SET status = 'expired', decided_at = %s"
            " WHERE status = 'pending' AND created_at < %s",
            (int(time.time()), cutoff),
        )


def prune_tool_approvals(keep: int) -> None:
    """Cap decided-approval history; pending records are never pruned."""
    with mutation() as cur:
        cur.execute(
            "DELETE FROM tool_approvals WHERE status <> 'pending' AND number <="
            " (SELECT COALESCE(MAX(number), 0) FROM tool_approvals) - %s",
            (keep,),
        )
