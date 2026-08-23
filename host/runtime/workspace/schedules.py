"""Host-global scheduled agent work with one fresh host thread per run."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from http import HTTPStatus
import re
import threading
from typing import Any
from urllib.parse import quote

from host.agent_scripts import script_path_error
from host.runtime.core import db, host_errors
from host.runtime.workspace.host_api import WorkspaceError, active_agent_runtimes, call_admin_api
from host.runtime.workspace.query import one as _one
from host.session_options import SCRIPT_RUNTIME, schedule_session_options


MAX_SCHEDULES = 100
MAX_NAME_CHARS = 100
MAX_MESSAGE_CHARS = 12_000
MAX_SESSION_VALUE_CHARS = 100
MIN_INTERVAL_MINUTES = 5
MAX_INTERVAL_MINUTES = 7 * 24 * 60
DEFAULT_PAGE_LIMIT = 40
MAX_PAGE_LIMIT = 100
MAX_REVISION_PAGE_LIMIT = 10
REVISION_RETAINED = 100
RUN_RETAINED = 1000
RETAIN_DAYS = 90
DUE_BATCH = 10
POLL_SECONDS = 30
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
DAILY_TIME_RE = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
ID_CAPTURE = r"([1-9][0-9]{0,18})"
MAX_BIGINT = 2**63 - 1
_SCHEDULER_WAKE = threading.Event()

SCHEDULE_COLUMNS = (
    "id, name, message, cadence, interval_minutes, daily_time, agent_runtime,"
    " model, effort, revision, deleted_at, last_run_at, next_run_at,"
    " created_at, updated_at"
)
RUN_COLUMNS = (
    "id, schedule_id, thread_id, message, agent_runtime, model, effort, status,"
    " error_message, scheduled_for, finished_at"
)
QUALIFIED_RUN_COLUMNS = (
    "schedule_runs.id, schedule_runs.schedule_id, schedule_runs.thread_id,"
    " schedule_runs.message, schedule_runs.agent_runtime, schedule_runs.model,"
    " schedule_runs.effort, schedule_runs.status, schedule_runs.error_message,"
    " schedule_runs.scheduled_for, schedule_runs.finished_at"
)


def route_browser(
    method: str,
    path: str,
    body: Any,
    query: dict[str, list[str]],
) -> dict[str, Any]:
    if path == "/schedules/session-options" and method == "GET":
        if query:
            raise WorkspaceError(HTTPStatus.BAD_REQUEST, "session options do not accept query parameters")
        return {
            "session_options": schedule_session_options(),
            "active_runtimes": active_agent_runtimes(),
        }
    if path == "/schedules":
        if method == "GET":
            return list_schedules(query)
        if method == "POST":
            return {"schedule": create_schedule(body, actor="user")}
    match = re.fullmatch(rf"/schedules/{ID_CAPTURE}", path)
    if match:
        schedule_id = _positive_id(match.group(1))
        if method == "GET":
            return {"schedule": load_schedule(schedule_id, include_deleted=True)}
        if method == "PUT":
            return {"schedule": update_schedule(schedule_id, body, actor="user")}
        if method == "DELETE":
            return delete_schedule(schedule_id, query, actor="user")
    match = re.fullmatch(rf"/schedules/{ID_CAPTURE}/revisions", path)
    if match and method == "GET":
        return list_revisions(_positive_id(match.group(1)), query)
    match = re.fullmatch(
        rf"/schedules/{ID_CAPTURE}/revisions/{ID_CAPTURE}/restore", path
    )
    if match and method == "POST":
        return {
            "schedule": restore_revision(
                _positive_id(match.group(1)), _positive_id(match.group(2)), body
            )
        }
    match = re.fullmatch(rf"/schedules/{ID_CAPTURE}/runs", path)
    if match and method == "GET":
        return list_runs(_positive_id(match.group(1)), query)
    match = re.fullmatch(rf"/schedules/{ID_CAPTURE}/runs/{ID_CAPTURE}", path)
    if match and method == "GET":
        return {
            "run": load_run(
                _positive_id(match.group(1)), _positive_id(match.group(2))
            )
        }
    match = re.fullmatch(
        rf"/schedules/{ID_CAPTURE}/runs/{ID_CAPTURE}/events", path
    )
    if match and method == "GET":
        return run_events(
            _positive_id(match.group(1)), _positive_id(match.group(2)), query
        )
    raise WorkspaceError(HTTPStatus.NOT_FOUND, "schedule route not found")


def route_agent(
    method: str,
    path: str,
    body: Any,
    query: dict[str, list[str]],
) -> dict[str, Any]:
    if path == "/agent/schedules/session-options" and method == "GET":
        if query:
            raise WorkspaceError(
                HTTPStatus.BAD_REQUEST,
                "session options do not accept query parameters",
            )
        return {"session_options": schedule_session_options()}
    if path == "/agent/schedules":
        if method == "GET":
            return list_active_schedules(query)
        if method == "POST":
            return {"schedule": create_schedule(body, actor="agent")}
    if path == "/agent/schedules/recent-failures" and method == "GET":
        return list_recent_failures(query)
    match = re.fullmatch(rf"/agent/schedules/{ID_CAPTURE}", path)
    if match:
        schedule_id = _positive_id(match.group(1))
        if method == "GET":
            return {"schedule": load_schedule(schedule_id)}
        if method == "PUT":
            return {"schedule": update_schedule(schedule_id, body, actor="agent")}
        if method == "DELETE":
            return delete_schedule(schedule_id, query, actor="agent")
    raise WorkspaceError(HTTPStatus.NOT_FOUND, "agent schedule route not found")


def list_schedules(query: dict[str, list[str]]) -> dict[str, Any]:
    _reject_query_keys(query, {"before", "limit", "deleted"}, "schedule list")
    return _list_schedules(
        query,
        deleted=_boolean_query(query, "deleted", default=False),
    )


def list_active_schedules(query: dict[str, list[str]]) -> dict[str, Any]:
    _reject_query_keys(query, {"before", "limit"}, "schedule list")
    return _list_schedules(query, deleted=False)


def _list_schedules(
    query: dict[str, list[str]],
    *,
    deleted: bool,
) -> dict[str, Any]:
    before = _optional_positive_int(query, "before")
    limit = _limit(query)
    clause = "deleted_at IS NOT NULL" if deleted else "deleted_at IS NULL"
    params: list[Any] = []
    if before is not None:
        clause += " AND id < %s"
        params.append(before)
    with db.transaction() as cur:
        cur.execute(
            f"SELECT {SCHEDULE_COLUMNS} FROM schedules WHERE {clause}"
            " ORDER BY id DESC LIMIT %s",
            (*params, limit + 1),
        )
        rows = cur.fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    response: dict[str, Any] = {
        "schedules": [_schedule_summary(_schedule_row(row)) for row in rows]
    }
    if more and rows:
        response["next_before"] = rows[-1][0]
    return response


def load_schedule(schedule_id: int, *, include_deleted: bool = False) -> dict[str, Any]:
    with db.transaction() as cur:
        cur.execute(f"SELECT {SCHEDULE_COLUMNS} FROM schedules WHERE id = %s", (schedule_id,))
        row = cur.fetchone()
    if row is None or (not include_deleted and row[10] is not None):
        raise WorkspaceError(HTTPStatus.NOT_FOUND, "schedule not found")
    return _schedule_row(row)


def create_schedule(body: Any, *, actor: str) -> dict[str, Any]:
    request = _object(body, "schedule request")
    required = {
        "name", "message", "cadence", "agent_runtime", "model", "effort"
    }
    _require_keys(
        request,
        required | {"interval_minutes", "daily_time"},
        required,
    )
    fields = _validated_fields(request)
    now = datetime.now(timezone.utc)
    now_ts = _format_ts(now)
    next_run = _format_ts(_next_run(fields, now))
    with db.transaction() as cur:
        # Serialize only schedule creation so concurrent agents cannot both
        # observe the last free quota slot. Edits and scheduler claims remain
        # independently row-locked.
        cur.execute("LOCK TABLE schedules IN SHARE ROW EXCLUSIVE MODE")
        cur.execute("SELECT COUNT(*) FROM schedules")
        count_row = cur.fetchone()
        assert count_row is not None
        if int(count_row[0]) >= MAX_SCHEDULES:
            raise WorkspaceError(
                HTTPStatus.CONFLICT, f"Workspace already retains {MAX_SCHEDULES} schedules"
            )
        cur.execute(
            "INSERT INTO schedules"
            " (name, message, cadence, interval_minutes, daily_time, agent_runtime,"
            " model, effort, revision, deleted_at, next_run_at, created_at, updated_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 1, NULL, %s, %s, %s)"
            f" RETURNING {SCHEDULE_COLUMNS}",
            (
                fields["name"], fields["message"], fields["cadence"],
                fields["interval_minutes"], fields["daily_time"],
                fields["agent_runtime"], fields["model"], fields["effort"],
                next_run, now_ts, now_ts,
            ),
        )
        row = cur.fetchone()
        assert row is not None
        _insert_revision(cur, _schedule_row(row), actor, now_ts)
    _SCHEDULER_WAKE.set()
    return _schedule_row(row)


def update_schedule(schedule_id: int, body: Any, *, actor: str) -> dict[str, Any]:
    request = _object(body, "schedule request")
    required = {
        "expected_revision", "name", "message", "cadence", "agent_runtime",
        "model", "effort",
    }
    _require_keys(request, required | {"interval_minutes", "daily_time"}, required)
    expected = _expected_revision(request["expected_revision"])
    fields = _validated_fields(request)
    now = datetime.now(timezone.utc)
    now_ts = _format_ts(now)
    with db.transaction() as cur:
        cur.execute(
            f"SELECT {SCHEDULE_COLUMNS} FROM schedules WHERE id = %s FOR UPDATE",
            (schedule_id,),
        )
        row = cur.fetchone()
        if row is None or row[10] is not None:
            raise WorkspaceError(HTTPStatus.NOT_FOUND, "schedule not found")
        current = _schedule_row(row)
        if current["revision"] != expected:
            raise WorkspaceError(HTTPStatus.CONFLICT, "schedule changed; reload and retry")
        cadence_changed = any(
            fields[key] != current[key]
            for key in ("cadence", "interval_minutes", "daily_time")
        )
        next_run = (
            _format_ts(_next_run(fields, now))
            if cadence_changed
            else current["next_run_at"]
        )
        revision = expected + 1
        cur.execute(
            "UPDATE schedules SET name = %s, message = %s, cadence = %s,"
            " interval_minutes = %s, daily_time = %s, agent_runtime = %s, model = %s,"
            " effort = %s, revision = %s, next_run_at = %s,"
            " updated_at = %s WHERE id = %s"
            f" RETURNING {SCHEDULE_COLUMNS}",
            (
                fields["name"], fields["message"], fields["cadence"],
                fields["interval_minutes"], fields["daily_time"], fields["agent_runtime"],
                fields["model"], fields["effort"], revision,
                next_run, now_ts, schedule_id,
            ),
        )
        changed = cur.fetchone()
        assert changed is not None
        _insert_revision(cur, _schedule_row(changed), actor, now_ts)
        _prune_revisions(cur, schedule_id)
    _SCHEDULER_WAKE.set()
    return _schedule_row(changed)


def delete_schedule(
    schedule_id: int, query: dict[str, list[str]], *, actor: str
) -> dict[str, Any]:
    _reject_query_keys(query, {"expected_revision"}, "schedule delete")
    expected = _required_query_revision(query)
    now = _utc_now()
    with db.transaction() as cur:
        cur.execute(
            f"SELECT {SCHEDULE_COLUMNS} FROM schedules WHERE id = %s FOR UPDATE",
            (schedule_id,),
        )
        row = cur.fetchone()
        if row is None or row[10] is not None:
            raise WorkspaceError(HTTPStatus.NOT_FOUND, "schedule not found")
        current = _schedule_row(row)
        if current["revision"] != expected:
            raise WorkspaceError(HTTPStatus.CONFLICT, "schedule changed; reload and retry")
        revision = expected + 1
        cur.execute(
            "UPDATE schedules SET revision = %s, deleted_at = %s,"
            " updated_at = %s WHERE id = %s"
            f" RETURNING {SCHEDULE_COLUMNS}",
            (revision, now, now, schedule_id),
        )
        changed = cur.fetchone()
        assert changed is not None
        _insert_revision(cur, _schedule_row(changed), actor, now)
        _prune_revisions(cur, schedule_id)
    return {"ok": True, "revision": revision}


def list_revisions(schedule_id: int, query: dict[str, list[str]]) -> dict[str, Any]:
    _reject_query_keys(query, {"before", "limit"}, "schedule history")
    before = _optional_positive_int(query, "before")
    limit = _limit(query, default=MAX_REVISION_PAGE_LIMIT, maximum=MAX_REVISION_PAGE_LIMIT)
    clause = " AND id < %s" if before is not None else ""
    params: list[Any] = [schedule_id]
    if before is not None:
        params.append(before)
    with db.transaction() as cur:
        cur.execute("SELECT 1 FROM schedules WHERE id = %s", (schedule_id,))
        if cur.fetchone() is None:
            raise WorkspaceError(HTTPStatus.NOT_FOUND, "schedule not found")
        cur.execute(
            "SELECT id, revision, name, message, cadence, interval_minutes, daily_time,"
            " agent_runtime, model, effort, deleted, actor, created_at"
            f" FROM schedule_revisions WHERE schedule_id = %s{clause}"
            " ORDER BY id DESC LIMIT %s",
            (*params, limit + 1),
        )
        rows = cur.fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    response: dict[str, Any] = {
        "revisions": [
            {
                "id": row[0], "revision": row[1], "name": row[2], "message": row[3],
                "cadence": row[4], "interval_minutes": row[5], "daily_time": row[6],
                "agent_runtime": row[7], "model": row[8], "effort": row[9],
                "deleted": row[10], "actor": row[11], "created_at": row[12],
            }
            for row in rows
        ]
    }
    if more and rows:
        response["next_before"] = rows[-1][0]
    return response


def restore_revision(schedule_id: int, revision: int, body: Any) -> dict[str, Any]:
    request = _object(body, "schedule restore request")
    _require_keys(request, {"expected_revision"}, {"expected_revision"})
    expected = _expected_revision(request["expected_revision"])
    now = datetime.now(timezone.utc)
    now_ts = _format_ts(now)
    with db.transaction() as cur:
        cur.execute(
            f"SELECT {SCHEDULE_COLUMNS} FROM schedules WHERE id = %s FOR UPDATE",
            (schedule_id,),
        )
        current_row = cur.fetchone()
        if current_row is None:
            raise WorkspaceError(HTTPStatus.NOT_FOUND, "schedule not found")
        current = _schedule_row(current_row)
        if current["revision"] != expected:
            raise WorkspaceError(HTTPStatus.CONFLICT, "schedule changed; reload and retry")
        cur.execute(
            "SELECT name, message, cadence, interval_minutes, daily_time, agent_runtime,"
            " model, effort, deleted FROM schedule_revisions"
            " WHERE schedule_id = %s AND revision = %s",
            (schedule_id, revision),
        )
        source = cur.fetchone()
        if source is None:
            raise WorkspaceError(HTTPStatus.NOT_FOUND, "schedule revision not found")
        fields = {
            "name": source[0], "message": source[1], "cadence": source[2],
            "interval_minutes": source[3], "daily_time": source[4],
            "agent_runtime": source[5], "model": source[6], "effort": source[7],
        }
        fields = _validated_fields(fields)
        new_revision = expected + 1
        deleted_at = now_ts if source[8] else None
        next_run = _format_ts(_next_run(fields, now))
        cur.execute(
            "UPDATE schedules SET name = %s, message = %s, cadence = %s,"
            " interval_minutes = %s, daily_time = %s, agent_runtime = %s, model = %s,"
            " effort = %s, revision = %s, deleted_at = %s,"
            " next_run_at = %s, updated_at = %s WHERE id = %s"
            f" RETURNING {SCHEDULE_COLUMNS}",
            (
                fields["name"], fields["message"], fields["cadence"],
                fields["interval_minutes"], fields["daily_time"], fields["agent_runtime"],
                fields["model"], fields["effort"], new_revision,
                deleted_at, next_run, now_ts, schedule_id,
            ),
        )
        changed = cur.fetchone()
        assert changed is not None
        _insert_revision(cur, _schedule_row(changed), "user", now_ts)
        _prune_revisions(cur, schedule_id)
    _SCHEDULER_WAKE.set()
    return _schedule_row(changed)


def list_runs(schedule_id: int, query: dict[str, list[str]]) -> dict[str, Any]:
    _reject_query_keys(query, {"before", "limit"}, "schedule runs")
    before = _optional_positive_int(query, "before")
    limit = _limit(query)
    clause = " AND id < %s" if before is not None else ""
    params: list[Any] = [schedule_id]
    if before is not None:
        params.append(before)
    with db.transaction() as cur:
        cur.execute("SELECT 1 FROM schedules WHERE id = %s", (schedule_id,))
        if cur.fetchone() is None:
            raise WorkspaceError(HTTPStatus.NOT_FOUND, "schedule not found")
        cur.execute(
            f"SELECT {RUN_COLUMNS} FROM schedule_runs WHERE schedule_id = %s{clause}"
            " ORDER BY id DESC LIMIT %s",
            (*params, limit + 1),
        )
        rows = cur.fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    response: dict[str, Any] = {"runs": [_run_summary(_run_row(row)) for row in rows]}
    if more and rows:
        response["next_before"] = rows[-1][0]
    return response


def list_recent_failures(query: dict[str, list[str]]) -> dict[str, Any]:
    """Return retained failed runs for schedules that are still active."""
    _reject_query_keys(query, {"before", "limit"}, "recent schedule failures")
    before = _optional_positive_int(query, "before")
    limit = _limit(query)
    clause = " AND schedule_runs.id < %s" if before is not None else ""
    params: list[Any] = []
    if before is not None:
        params.append(before)
    with db.transaction() as cur:
        cur.execute(
            f"SELECT {QUALIFIED_RUN_COLUMNS}, schedules.name"
            " FROM schedule_runs JOIN schedules ON schedules.id = schedule_runs.schedule_id"
            " WHERE schedules.deleted_at IS NULL AND schedule_runs.status = 'failed'"
            f"{clause} ORDER BY schedule_runs.id DESC LIMIT %s",
            (*params, limit + 1),
        )
        rows = cur.fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    failures = []
    for row in rows:
        failure = _run_summary(_run_row(row[:11]))
        failure["schedule_name"] = row[11]
        failures.append(failure)
    response: dict[str, Any] = {"failures": failures}
    if more and rows:
        response["next_before"] = rows[-1][0]
    return response


def load_run(schedule_id: int, run_id: int) -> dict[str, Any]:
    with db.transaction() as cur:
        cur.execute(
            f"SELECT {RUN_COLUMNS} FROM schedule_runs WHERE schedule_id = %s AND id = %s",
            (schedule_id, run_id),
        )
        row = cur.fetchone()
    if row is None:
        raise WorkspaceError(HTTPStatus.NOT_FOUND, "schedule run not found")
    return _run_row(row)


def run_events(
    schedule_id: int, run_id: int, query: dict[str, list[str]]
) -> dict[str, Any]:
    run = load_run(schedule_id, run_id)
    _reject_query_keys(query, {"before", "since"}, "schedule run events")
    before = _optional_non_negative_int(query, "before")
    since = _optional_non_negative_int(query, "since")
    if before is not None and since is not None:
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, "before and since cannot be combined")
    path = (
        f"/v1/threads/{quote(run['thread_id'], safe='')}/events"
        "?limit=20&message_bytes=32768&event_type=thread.message"
        "&event_type=thread.error&event_type=thread.stopped"
    )
    if before is not None:
        path += f"&before={before}"
    if since is not None:
        path += f"&since={since}"
    try:
        response = call_admin_api("GET", path)
    except WorkspaceError as exc:
        if exc.status == HTTPStatus.NOT_FOUND:
            return {"events": [], "retained": False}
        raise
    events = response.get("events")
    if not isinstance(events, list):
        raise WorkspaceError(HTTPStatus.BAD_GATEWAY, "host admin returned invalid events")
    # Every admitted schedule thread records its user message. A terminal run
    # whose newest display-event page is empty has therefore outlived the
    # host's retained event window even if its lightweight thread row remains.
    retained = bool(events) or run["status"] in {"pending", "running"}
    return {"events": events, "retained": retained}


def run_due(now: datetime | None = None) -> int:
    instant = now or datetime.now(timezone.utc)
    now_ts = _format_ts(instant)
    with db.transaction() as cur:
        cur.execute(
            "SELECT schedules.id FROM schedules"
            " WHERE deleted_at IS NULL AND next_run_at <= %s"
            " AND NOT EXISTS (SELECT 1 FROM schedule_runs"
            "   WHERE schedule_runs.schedule_id = schedules.id"
            "   AND schedule_runs.status IN ('pending', 'running'))"
            " ORDER BY next_run_at, schedules.id LIMIT %s",
            (now_ts, DUE_BATCH),
        )
        candidates = [int(row[0]) for row in cur.fetchall()]
    launched = 0
    for schedule_id in candidates:
        run = _claim_run(schedule_id, instant)
        if run is None:
            continue
        _launch_run(run)
        launched += 1
    return launched


def refresh_active_runs() -> int:
    # At most one run per schedule can be active, and schedules are globally
    # capped, so scanning every active run is already bounded.
    with db.transaction() as cur:
        cur.execute(
            f"SELECT {RUN_COLUMNS} FROM schedule_runs"
            " WHERE status IN ('pending', 'running') ORDER BY id"
        )
        rows = cur.fetchall()
    changed = 0
    for row in rows:
        run = _run_row(row)
        try:
            response = call_admin_api(
                "GET", f"/v1/threads/{quote(run['thread_id'], safe='')}"
            )
            thread = response.get("thread")
            if not isinstance(thread, dict) or thread.get("status") not in {"idle", "running"}:
                raise WorkspaceError(HTTPStatus.BAD_GATEWAY, "host admin returned invalid thread")
            if thread["status"] == "running":
                if run["status"] == "pending":
                    _start_run(run["id"])
                    changed += 1
                continue
            terminal = call_admin_api(
                "GET",
                f"/v1/threads/{quote(run['thread_id'], safe='')}/events"
                "?limit=10&message_bytes=32768&event_type=thread.error&event_type=thread.stopped",
            ).get("events")
            if not isinstance(terminal, list):
                raise WorkspaceError(HTTPStatus.BAD_GATEWAY, "host admin returned invalid events")
        except WorkspaceError as exc:
            if exc.status == HTTPStatus.NOT_FOUND:
                if run["status"] == "pending":
                    _finish_run(
                        run["id"],
                        "failed",
                        "schedule launch was interrupted before the host thread was created",
                    )
                else:
                    _finish_run(
                        run["id"],
                        "failed",
                        "schedule host thread is no longer available",
                    )
                changed += 1
            # Once a launch was accepted, a transient admin outage must not
            # declare the run terminal: the agent may still be active. Retry
            # observation on the next scheduler pass to preserve no-overlap.
            continue
        if run["status"] == "pending":
            _start_run(run["id"])
        status = "succeeded"
        error = None
        if terminal:
            latest = terminal[-1]
            event_type = latest.get("event_type") if isinstance(latest, dict) else None
            if event_type == "thread.stopped":
                status = "stopped"
            elif event_type == "thread.error":
                status = "failed"
                payload = latest.get("payload") if isinstance(latest, dict) else None
                if isinstance(payload, dict) and isinstance(payload.get("error_message"), str):
                    error = payload["error_message"][:4000]
        _finish_run(run["id"], status, error)
        changed += 1
    return changed


def _claim_run(schedule_id: int, now: datetime) -> dict[str, Any] | None:
    now_ts = _format_ts(now)
    with db.transaction() as cur:
        cur.execute(
            f"SELECT {SCHEDULE_COLUMNS} FROM schedules WHERE id = %s"
            " AND deleted_at IS NULL AND next_run_at <= %s FOR UPDATE",
            (schedule_id, now_ts),
        )
        row = cur.fetchone()
        if row is None:
            return None
        schedule = _schedule_row(row)
        cur.execute(
            "SELECT 1 FROM schedule_runs WHERE schedule_id = %s"
            " AND status IN ('pending', 'running')",
            (schedule_id,),
        )
        if cur.fetchone() is not None:
            return None
        cur.execute(
            "WITH allocated AS (SELECT nextval('schedule_runs_id_seq') AS id)"
            " INSERT INTO schedule_runs"
            " (id, schedule_id, thread_id, message, agent_runtime, model, effort, status, scheduled_for)"
            " SELECT id, %s, 'schedule-' || %s::text || '-run-' || id::text,"
            " %s, %s, %s, %s, 'pending', %s"
            " FROM allocated"
            f" RETURNING {RUN_COLUMNS}",
            (
                schedule_id, schedule_id, schedule["message"], schedule["agent_runtime"],
                schedule["model"], schedule["effort"], schedule["next_run_at"],
            ),
        )
        run_row = cur.fetchone()
        assert run_row is not None
        cur.execute(
            "UPDATE schedules SET next_run_at = %s, last_run_at = %s WHERE id = %s",
            (_format_ts(_next_run(schedule, now)), now_ts, schedule_id),
        )
    return _run_row(run_row)


def _launch_run(run: dict[str, Any]) -> None:
    body = {
        "message": run["message"],
        "agent_runtime": run["agent_runtime"],
        "model": run["model"],
        "effort": run["effort"],
    }
    try:
        response = call_admin_api(
            "POST", f"/v1/threads/{quote(run['thread_id'], safe='')}/messages", body
        )
        thread = response.get("thread")
        if response.get("status") != "accepted" or not isinstance(thread, dict):
            raise WorkspaceError(HTTPStatus.BAD_GATEWAY, "host admin returned invalid acceptance")
    except WorkspaceError as exc:
        # Occurrences are attempted once. Capacity, configuration, transport,
        # and host failures are visible in run history and are never queued or
        # retried behind the operator's back.
        _finish_run(run["id"], "failed", exc.message[:4000])
        return
    _start_run(run["id"])


def _start_run(run_id: int) -> None:
    with db.transaction() as cur:
        cur.execute(
            "UPDATE schedule_runs SET status = 'running'"
            " WHERE id = %s AND status = 'pending'",
            (run_id,),
        )


def _finish_run(run_id: int, status: str, error: str | None) -> None:
    now = _utc_now()
    with db.transaction() as cur:
        cur.execute(
            "UPDATE schedule_runs SET status = %s, error_message = %s, finished_at = %s"
            " WHERE id = %s AND status IN ('pending', 'running')",
            (status, error, now, run_id),
        )


def prune_retained(now: datetime | None = None) -> None:
    cutoff = _format_ts((now or datetime.now(timezone.utc)) - timedelta(days=RETAIN_DAYS))
    with db.transaction() as cur:
        cur.execute(
            "DELETE FROM schedule_runs WHERE status NOT IN ('pending', 'running')"
            " AND (finished_at < %s OR id IN ("
            "   SELECT id FROM schedule_runs AS older"
            "   WHERE older.schedule_id = schedule_runs.schedule_id"
            "   ORDER BY id DESC OFFSET %s"
            " ))",
            (cutoff, RUN_RETAINED),
        )
        cur.execute(
            "DELETE FROM schedules WHERE deleted_at IS NOT NULL AND deleted_at < %s"
            " AND NOT EXISTS (SELECT 1 FROM schedule_runs"
            " WHERE schedule_runs.schedule_id = schedules.id)",
            (cutoff,),
        )


def scheduler_loop() -> None:
    while True:
        _SCHEDULER_WAKE.wait(POLL_SECONDS)
        _SCHEDULER_WAKE.clear()
        try:
            refresh_active_runs()
            run_due()
        except Exception as exc:
            host_errors.report_unexpected("workspace.scheduler", exc)


def _schedule_row(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "id": row[0], "name": row[1], "message": row[2], "cadence": row[3],
        "interval_minutes": row[4], "daily_time": row[5], "agent_runtime": row[6],
        "model": row[7], "effort": row[8], "revision": row[9],
        "deleted": row[10] is not None, "last_run_at": row[11],
        "next_run_at": row[12], "created_at": row[13], "updated_at": row[14],
    }


def _run_row(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "id": row[0], "schedule_id": row[1], "thread_id": row[2],
        "message": row[3], "agent_runtime": row[4], "model": row[5],
        "effort": row[6], "status": row[7], "error_message": row[8],
        "scheduled_for": row[9], "finished_at": row[10],
    }


def _schedule_summary(schedule: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in schedule.items() if key != "message"}


def _run_summary(run: dict[str, Any]) -> dict[str, Any]:
    summary = {key: value for key, value in run.items() if key != "message"}
    if isinstance(summary["error_message"], str):
        summary["error_message"] = summary["error_message"][:500]
    return summary


def _insert_revision(cur: Any, schedule: dict[str, Any], actor: str, now: str) -> None:
    cur.execute(
        "INSERT INTO schedule_revisions"
        " (schedule_id, revision, name, message, cadence, interval_minutes, daily_time,"
        " agent_runtime, model, effort, deleted, actor, created_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            schedule["id"], schedule["revision"], schedule["name"], schedule["message"],
            schedule["cadence"], schedule["interval_minutes"], schedule["daily_time"],
            schedule["agent_runtime"], schedule["model"], schedule["effort"],
            schedule["deleted"], actor, now,
        ),
    )


def _prune_revisions(cur: Any, schedule_id: int) -> None:
    cur.execute(
        "DELETE FROM schedule_revisions WHERE id IN ("
        " SELECT id FROM schedule_revisions WHERE schedule_id = %s"
        " ORDER BY id DESC OFFSET %s)",
        (schedule_id, REVISION_RETAINED),
    )


def _validated_fields(value: dict[str, Any]) -> dict[str, Any]:
    name = value.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > MAX_NAME_CHARS or "\n" in name or "\r" in name:
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, f"name must be one line of at most {MAX_NAME_CHARS} characters")
    message = value.get("message")
    if not isinstance(message, str) or not message.strip() or len(message) > MAX_MESSAGE_CHARS:
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, f"message must be between 1 and {MAX_MESSAGE_CHARS} characters")
    cadence = value.get("cadence")
    interval = value.get("interval_minutes")
    daily = value.get("daily_time")
    if cadence == "interval":
        if isinstance(interval, bool) or not isinstance(interval, int) or not MIN_INTERVAL_MINUTES <= interval <= MAX_INTERVAL_MINUTES:
            raise WorkspaceError(HTTPStatus.BAD_REQUEST, f"interval_minutes must be between {MIN_INTERVAL_MINUTES} and {MAX_INTERVAL_MINUTES}")
        if daily is not None:
            raise WorkspaceError(HTTPStatus.BAD_REQUEST, "daily_time does not apply to interval cadence")
    elif cadence == "daily":
        if not isinstance(daily, str) or DAILY_TIME_RE.fullmatch(daily) is None:
            raise WorkspaceError(HTTPStatus.BAD_REQUEST, "daily_time must be HH:MM in UTC")
        if interval is not None:
            raise WorkspaceError(HTTPStatus.BAD_REQUEST, "interval_minutes does not apply to daily cadence")
    else:
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, "cadence must be interval or daily")
    session = {
        "agent_runtime": value.get("agent_runtime"),
        "model": value.get("model"),
        "effort": value.get("effort"),
    }
    for label, item in session.items():
        if not isinstance(item, str) or not item or len(item) > MAX_SESSION_VALUE_CHARS:
            raise WorkspaceError(
                HTTPStatus.BAD_REQUEST,
                f"{label} must be between 1 and {MAX_SESSION_VALUE_CHARS} characters",
            )
    if session["agent_runtime"] == SCRIPT_RUNTIME:
        # A script schedule's message is the script's path, so the one thing
        # this layer can check about it changes shape entirely. Whether the
        # file exists is the launcher's decision at run time — the workspace
        # cannot read the agent's private home — but a path that could never
        # run is rejected here, while the operator is still editing the form,
        # instead of becoming a failed run in an hour.
        error = script_path_error(message)
        if error is not None:
            raise WorkspaceError(HTTPStatus.BAD_REQUEST, error)
    return {
        "name": name, "message": message, "cadence": cadence,
        "interval_minutes": interval, "daily_time": daily,
        **session,
    }


def _next_run(schedule: dict[str, Any], after: datetime) -> datetime:
    if schedule["cadence"] == "interval":
        return after + timedelta(minutes=int(schedule["interval_minutes"]))
    hour, minute = (int(part) for part in str(schedule["daily_time"]).split(":"))
    candidate = after.astimezone(timezone.utc).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    return candidate if candidate > after else candidate + timedelta(days=1)


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


def _positive_id(value: str) -> int:
    parsed = int(value)
    if parsed > MAX_BIGINT:
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, "resource id is out of range")
    return parsed


def _required_query_revision(query: dict[str, list[str]]) -> int:
    value = _one(query, "expected_revision")
    if value is None or not value.isdigit():
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, "expected_revision must be a non-negative integer")
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
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, f"limit must be between 1 and {maximum}")
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


def _optional_non_negative_int(query: dict[str, list[str]], key: str) -> int | None:
    raw = _one(query, key)
    if raw is None:
        return None
    if not raw.isdigit() or int(raw) > MAX_BIGINT:
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, f"{key} must be non-negative")
    return int(raw)


def _reject_query_keys(query: dict[str, list[str]], allowed: set[str], label: str) -> None:
    extra = sorted(set(query) - allowed)
    if extra:
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, f"unexpected {label} query field: {extra[0]}")


def _utc_now() -> str:
    return _format_ts(datetime.now(timezone.utc))


def _format_ts(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime(TIME_FORMAT)
