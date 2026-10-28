"""Durable one-time messages to an existing agent's own conversation."""
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
import re
import time
from typing import Any

from host.agent_messages import AUTOMATED_TRIGGER_PREFIX, compact
from host.runtime.core import db, host_errors
from host.runtime.workspace import agent_messages
from host.runtime.workspace.host_api import WorkspaceError

MAX_PENDING = 5
MAX_HISTORY = 1_000_000
CLEANUP_SECONDS = 24 * 60 * 60
POLL_SECONDS = 30
MAX_DELIVERIES = 4

COLUMNS = "id, thread_id, message, due_at, status, error, created_at, finished_at"
KEYS = COLUMNS.split(", ")


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fields(body: Any) -> tuple[str, str]:
    if (not isinstance(body, dict) or set(body) not in ({"message", "at"}, {"message", "after"})):
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, "provide message and exactly one of at or after")
    message = body["message"]
    if not isinstance(message, str) or not message.strip() or len(message) > 1000:
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, "message must be nonblank and at most 1000 characters")
    now = datetime.now(timezone.utc)
    try:
        if "at" in body:
            due = datetime.fromisoformat(body["at"].replace("Z", "+00:00"))
            if due.tzinfo is None:
                raise ValueError()
        else:
            match = re.fullmatch(r"([1-9][0-9]{0,5})([smhd])", body["after"])
            if match is None:
                raise ValueError()
            seconds = int(match[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[match[2]]
            due = now + timedelta(seconds=seconds)
        if due <= now or due > now + timedelta(days=7):
            raise ValueError()
        return message, _timestamp(due)
    except (ValueError, TypeError, AttributeError, OverflowError):
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, "reminders must be within the next 7 days; at needs a timezone, or use a positive after duration such as 30m or 2h") from None


def _owner(cur: Any, thread_id: str, *, writable: bool) -> None:
    if not isinstance(thread_id, str) or not agent_messages.THREAD_ID_RE.fullmatch(thread_id):
        raise WorkspaceError(HTTPStatus.CONFLICT, "agent thread identity is unavailable")
    if thread_id.startswith("app-"):
        query = "SELECT archived, agent_runtime FROM web_apps WHERE app_id = %s"
    elif thread_id.startswith("schedule-"):
        query = "SELECT deleted_at IS NOT NULL, agent_runtime FROM schedules WHERE thread_id = %s"
    else:
        query = "SELECT archived, 'chat' FROM chat_threads WHERE thread_id = %s"
    # Serialize creation and delivery with archive and automatic inactivity checks.
    cur.execute(query + (" FOR UPDATE" if writable else ""), (thread_id,))
    row = cur.fetchone()
    if row is None:
        raise WorkspaceError(HTTPStatus.NOT_FOUND, "agent not found")
    if row[1] == "script":
        raise WorkspaceError(HTTPStatus.CONFLICT, "Bash agents cannot receive reminders")
    if writable and row[0]:
        raise WorkspaceError(HTTPStatus.CONFLICT, "archived agents cannot change reminders")


def route(method: str, thread_id: str, body: Any,
          query: dict[str, list[str]]) -> dict[str, Any]:
    if method not in {"GET", "POST"}:
        raise WorkspaceError(HTTPStatus.METHOD_NOT_ALLOWED, "reminders support only GET and POST; created reminders cannot be changed or cancelled")
    if set(query) - {"before"} or (query and method != "GET"):
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, "only reminder listing accepts before")
    fields = _fields(body) if method == "POST" else None
    with db.transaction() as cur:
        _owner(cur, thread_id, writable=method != "GET")
        if method == "GET":
            values = query.get("before", ["9223372036854775807"])
            if len(values) != 1 or not re.fullmatch(r"[1-9][0-9]{0,18}", values[0]) or int(values[0]) > 9223372036854775807:
                raise WorkspaceError(HTTPStatus.BAD_REQUEST, "before must be a positive reminder id")
            before = int(values[0])
            cur.execute(f"SELECT {COLUMNS} FROM agent_reminders WHERE thread_id = %s AND status = 'pending'"
                        " ORDER BY due_at, id", (thread_id,))
            pending = [dict(zip(KEYS, row)) for row in cur.fetchall()]
            cur.execute(f"SELECT {COLUMNS} FROM agent_reminders WHERE thread_id = %s AND id < %s"
                        " ORDER BY id DESC LIMIT 101", (thread_id, before))
            rows = cur.fetchall()
            return {"reminders": [dict(zip(KEYS, row)) for row in rows[:100]], "pending": pending,
                    "pending_count": len(pending), "next_before": rows[99][0] if len(rows) > 100 else None}
        now = _timestamp(datetime.now(timezone.utc))
        assert fields is not None
        cur.execute("SELECT count(*) FROM agent_reminders WHERE thread_id = %s AND status = 'pending'", (thread_id,))
        count_row = cur.fetchone()
        assert count_row is not None
        if count_row[0] >= MAX_PENDING:
            raise WorkspaceError(HTTPStatus.CONFLICT, f"at most {MAX_PENDING} reminders may be pending; wait for one to finish before creating another")
        cur.execute("INSERT INTO agent_reminders (thread_id, message, due_at, created_at)"
                    f" VALUES (%s, %s, %s, %s) RETURNING {COLUMNS}", (thread_id, *fields, now))
        row = cur.fetchone()
        assert row is not None
        return {"reminder": dict(zip(KEYS, row))}


def prune_history() -> None:
    """Daily global retention; never remove pending reminders."""
    with db.transaction() as cur:
        cur.execute("DELETE FROM agent_reminders WHERE id IN ("
                    "SELECT id FROM agent_reminders WHERE status <> 'pending'"
                    " ORDER BY finished_at DESC, id DESC OFFSET %s)", (MAX_HISTORY,))


def run_due(executor: ThreadPoolExecutor, active: dict[str, Future[None]],
            now: datetime | None = None) -> None:
    for thread_id, future in list(active.items()):
        if future.done():
            del active[thread_id]
            try:
                future.result()
            except Exception as exc:
                host_errors.report_unexpected("workspace.reminder", exc)
    if len(active) >= MAX_DELIVERIES:
        return
    due = _timestamp(now or datetime.now(timezone.utc))
    excluded = " AND thread_id NOT IN (" + ", ".join("%s" for _ in active) + ")" if active else ""
    with db.transaction() as cur:
        cur.execute("SELECT id, thread_id FROM agent_reminders WHERE status = 'pending'"
                    " AND due_at <= %s" + excluded + " ORDER BY due_at, id LIMIT 100", (due, *active))
        candidates = cur.fetchall()
    for reminder_id, thread_id in candidates:
        # Same-thread attempts share a parent lock; never fill workers waiting on it.
        if thread_id not in active:
            active[thread_id] = executor.submit(_deliver, reminder_id, thread_id, due)
            if len(active) >= MAX_DELIVERIES:
                break


def _deliver(reminder_id: int, thread_id: str, due: str) -> None:
    with db.transaction() as cur:
        try:
            _owner(cur, thread_id, writable=True)
        except WorkspaceError as exc:
            cur.execute("UPDATE agent_reminders SET status = 'failed', error = %s, finished_at = %s"
                        " WHERE id = %s AND status = 'pending' AND due_at <= %s",
                        (exc.message[:1000], _timestamp(datetime.now(timezone.utc)), reminder_id, due))
            return
        cur.execute(f"SELECT {COLUMNS} FROM agent_reminders WHERE id = %s AND status = 'pending'"
                    " AND due_at <= %s FOR UPDATE SKIP LOCKED", (reminder_id, due))
        row = cur.fetchone()
        if row is None:
            return
        reminder = dict(zip(KEYS, row))
        message = (AUTOMATED_TRIGGER_PREFIX + f"Reminder you scheduled for {reminder['due_at']}:\n\n"
                   + reminder["message"])
        notice = {"kind": "reminder", "summary": compact(f"Reminder: {reminder['message']}")}
        status, error = "delivered", None
        try:
            agent_messages._deliver_message(cur, thread_id, {"message": message, "kern_notice": notice})
        except WorkspaceError as exc:
            status, error = "failed", exc.message[:1000]
        cur.execute("UPDATE agent_reminders SET status = %s, error = %s, finished_at = %s WHERE id = %s",
                    (status, error, _timestamp(datetime.now(timezone.utc)), reminder_id))


def delivery_loop() -> None:
    """Keep reminder admission independent of slow recurring deliveries."""
    next_cleanup = 0.0
    active: dict[str, Future[None]] = {}
    with ThreadPoolExecutor(max_workers=MAX_DELIVERIES, thread_name_prefix="reminder") as executor:
        while True:
            try:
                run_due(executor, active)
                if time.monotonic() >= next_cleanup:
                    next_cleanup = time.monotonic() + CLEANUP_SECONDS
                    prune_history()
            except Exception as exc:
                host_errors.report_unexpected("workspace.reminders", exc)
            time.sleep(POLL_SECONDS)
