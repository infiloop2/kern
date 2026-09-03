"""Chat workspace backend.

Chat owns its thread index (names and archive state). Thread
contents and execution remain host-owned and are accessed through the host
admin API by this backend: the host synchronously accepts each message into
the thread's current agent session.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from http import HTTPStatus
import json
import re
import threading
import time
from typing import Any
from urllib.parse import quote, unquote

from host.runtime.core import db
from host.runtime.workspace.host_api import WorkspaceError, active_agent_runtimes, call_admin_api
from host.runtime.workspace.busy_retry import post_with_busy_retry
from host.runtime.workspace import seen
from host.session_options import (
    SCRIPT_RUNTIME,
    public_session_options,
    recorded_session_config,
    session_config_error,
)


MAX_REQUEST_BODY_BYTES = 128 * 1024
RUNTIME_OPTIONS = {"codex", "claude_code", "grok", "hermes"}
THREAD_ID_RE = re.compile(r"(?:thread|schedule)-[1-9][0-9]*")
GENERATED_THREAD_ID_RE = re.compile(r"thread-([1-9][0-9]*)")
SCHEDULE_THREAD_ID_RE = re.compile(r"schedule-[1-9][0-9]*")
# Keep each proxy response comfortably below the fixed 1 MiB bridge cap.
# Six 120 KiB event text budgets leave more than 300 KiB for JSON envelopes
# and bounded activity metadata. Full messages remain stored by the host.
THREAD_EVENT_MESSAGE_BYTES = 120 * 1024
THREAD_EVENT_PAGE = 6
THREAD_DISPLAY_EVENT_TYPES = (
    "thread.message",
    "thread.activity",
    "thread.error",
    "thread.stopped",
    # Hiding activity must not hide the working-memory boundary, so this type
    # is deliberately outside the activity filter below.
    "thread.memory_cleared",
)
THREAD_LIST_PAGE = 100
# Chat history is durable user data, so old or archived threads are not
# silently deleted. Bound aggregate thread metadata with admission instead.
MAX_CHAT_THREADS = 10_000
_MESSAGE_SEND_LOCKS_GUARD = threading.Lock()
_MESSAGE_SEND_LOCKS: dict[str, threading.Lock] = {}
_MESSAGE_SEND_LOCK_USERS: dict[str, int] = {}
# A live execution has brief startup and shutdown windows where it cannot
# accept another message. The host marks those safe-to-retry conflicts.
SEND_RETRY_MARKER = "retry shortly"
SEND_BUSY_RETRIES = 11
SEND_BUSY_RETRY_DELAY_SECONDS = 0.5


@contextmanager
def _message_send_lock(thread_id: str) -> Iterator[None]:
    """Serialize delivery-affecting actions within one thread only."""
    with _MESSAGE_SEND_LOCKS_GUARD:
        lock = _MESSAGE_SEND_LOCKS.setdefault(thread_id, threading.Lock())
        _MESSAGE_SEND_LOCK_USERS[thread_id] = _MESSAGE_SEND_LOCK_USERS.get(thread_id, 0) + 1
    try:
        with lock:
            yield
    finally:
        with _MESSAGE_SEND_LOCKS_GUARD:
            remaining = _MESSAGE_SEND_LOCK_USERS[thread_id] - 1
            if remaining:
                _MESSAGE_SEND_LOCK_USERS[thread_id] = remaining
            else:
                del _MESSAGE_SEND_LOCK_USERS[thread_id]
                del _MESSAGE_SEND_LOCKS[thread_id]


def route_browser(
    method: str,
    path: str,
    body: Any,
    query: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    if method == "GET" and path == "/session-options":
        return {
            "session_options": public_session_options(),
            "active_runtimes": active_agent_runtimes(),
        }
    if method == "GET" and path == "/threads":
        query = query or {}
        unexpected = sorted(set(query) - {"archived"})
        if unexpected:
            raise WorkspaceError(
                HTTPStatus.BAD_REQUEST,
                f"unsupported thread query parameter: {unexpected[0]}",
            )
        archived_values = query.get("archived") or []
        archived = False
        if archived_values:
            if archived_values[0] not in {"true", "false"}:
                raise WorkspaceError(HTTPStatus.BAD_REQUEST, "archived must be true or false")
            archived = archived_values[0] == "true"
        return list_chat_threads(archived=archived)
    if method == "GET" and path == "/scheduled-agents":
        if query:
            raise WorkspaceError(
                HTTPStatus.BAD_REQUEST,
                f"unsupported scheduled-agent query parameter: {sorted(query)[0]}",
            )
        return list_scheduled_agent_threads()
    if method == "GET" and path.startswith("/threads/") and path.endswith("/events"):
        parts = path.strip("/").split("/")
        if len(parts) != 3:
            raise WorkspaceError(HTTPStatus.NOT_FOUND, "route not found")
        return list_chat_thread_events(_chat_thread_id(_path_segment(parts[1])), query or {})
    if method == "POST" and path.startswith("/threads/") and path.endswith("/seen"):
        parts = path.strip("/").split("/")
        if len(parts) != 3:
            raise WorkspaceError(HTTPStatus.NOT_FOUND, "route not found")
        return {
            "seen": mark_chat_thread_seen(
                _chat_thread_id(_path_segment(parts[1])), body
            )
        }
    if (
        method == "POST"
        and path.startswith("/threads/")
        and (path.endswith("/archive") or path.endswith("/unarchive"))
    ):
        parts = path.strip("/").split("/")
        if len(parts) != 3:
            raise WorkspaceError(HTTPStatus.NOT_FOUND, "route not found")
        thread_id = _chat_thread_id(_path_segment(parts[1]))
        # Serialize with sends: a send holds this lock from its archived-state
        # check through the host call, so an archive cannot slip between the
        # check and the send and revive a read-only thread.
        with _message_send_lock(thread_id):
            return {
                "thread": set_chat_thread_archived(
                    thread_id,
                    archived=parts[2] == "archive",
                )
            }
    if method == "PUT" and path.startswith("/threads/") and path.endswith("/name"):
        parts = path.strip("/").split("/")
        if len(parts) != 3:
            raise WorkspaceError(HTTPStatus.NOT_FOUND, "route not found")
        return {
            "thread": rename_chat_thread(
                _chat_thread_id(_path_segment(parts[1])), body
            )
        }
    if method == "POST" and path == "/messages":
        return send_chat_message(body)
    if method == "POST" and path.startswith("/threads/") and path.endswith("/stop"):
        parts = path.strip("/").split("/")
        if len(parts) != 3:
            raise WorkspaceError(HTTPStatus.NOT_FOUND, "route not found")
        thread_id = _chat_thread_id(_path_segment(parts[1]))
        # A Stop clicked after Send must follow that send at the host boundary,
        # not race it and accidentally let the retrying send start fresh work
        # after the stop completed.
        with _message_send_lock(thread_id):
            _require_chat_thread(thread_id, include_archived=True)
            return call_admin_api(
                "POST", f"/v1/threads/{quote(thread_id, safe='')}/stop", body
            )
    if method == "POST" and path.startswith("/threads/") and path.endswith("/clear-memory"):
        parts = path.strip("/").split("/")
        if len(parts) != 3:
            raise WorkspaceError(HTTPStatus.NOT_FOUND, "route not found")
        thread_id = _chat_thread_id(_path_segment(parts[1]))
        # Serialize with sends for the same reason Stop does: a clear that
        # interleaved with a send could strip context that send is delivering.
        # Archived threads are read-only, so they are refused here.
        with _message_send_lock(thread_id):
            _require_chat_thread(thread_id)
            # Clear memory sits next to Stop, and a stopped turn stays live
            # while its process closes even though the thread already reads as
            # idle. The host marks that conflict retryable, so absorb it here
            # rather than showing a failure for a clear that just needs a
            # moment.
            return _post_with_busy_retry(
                f"/v1/threads/{quote(thread_id, safe='')}/clear-memory",
                body,
                "the thread stayed busy while clearing; retry in a moment",
            )
    raise WorkspaceError(HTTPStatus.NOT_FOUND, "route not found")


def list_chat_threads(*, archived: bool = False) -> dict[str, Any]:
    """Return ordinary Chat threads joined with live host state."""
    return _list_indexed_threads(prefix="thread-", archived=archived, scheduled=False)


def list_scheduled_agent_threads() -> dict[str, Any]:
    """Return active schedules joined with their stable host threads.

    A schedule is visible before its first delivery because its saved
    configuration is sufficient to open the transcript. Deleted schedules are
    deliberately absent: schedule transcripts never move into Chat.
    """
    return _list_indexed_threads(prefix="schedule-", archived=False, scheduled=True)


def _list_indexed_threads(
    *, prefix: str, archived: bool, scheduled: bool
) -> dict[str, Any]:
    recorded = _recorded_threads(archived=archived, scheduled=scheduled)
    summaries = {
        summary.get("thread_id"): summary
        for summary in _host_thread_summaries(prefix)
        if isinstance(summary, dict) and summary.get("thread_id") in recorded
    }
    chat_threads = []
    for thread_id, metadata in recorded.items():
        summary = summaries.get(thread_id)
        has_session = summary is not None
        if summary is None:
            if not scheduled:
                continue
            summary = _pre_session_schedule_summary(thread_id, metadata)
        chat_threads.append(
            _chat_thread_summary(
                summary,
                metadata=metadata,
                archived=archived,
                has_session=has_session,
            )
        )
    chat_threads.sort(key=lambda item: str(item.get("last_used_at") or ""), reverse=True)
    seen.add_to_items("chat", chat_threads, "thread_id")
    return {"threads": chat_threads}


def mark_chat_thread_seen(thread_id: str, body: Any) -> dict[str, int]:
    """Advance only through the newest message the browser actually rendered."""
    _require_chat_thread(thread_id, include_archived=True)
    requested, _ = seen.request_marker(body, include_revision=False)
    try:
        response = call_admin_api("GET", f"/v1/threads/{quote(thread_id, safe='')}")
    except WorkspaceError as exc:
        if not (
            exc.status == HTTPStatus.NOT_FOUND
            and SCHEDULE_THREAD_ID_RE.fullmatch(thread_id) is not None
        ):
            raise
        current = 0
    else:
        thread = response.get("thread")
        if not isinstance(thread, dict):
            raise WorkspaceError(
                HTTPStatus.BAD_GATEWAY, "host admin returned invalid thread detail"
            )
        current = max(0, int(thread.get("latest_message_seq") or 0))
    return seen.save("chat", thread_id, min(requested, current))


def _pre_session_schedule_summary(
    thread_id: str,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    """Show a scheduled agent before its first host thread event."""
    return {
        "thread_id": thread_id,
        "agent_runtime": metadata["agent_runtime"],
        "model": metadata["model"],
        "effort": metadata["effort"],
        "status": "idle",
        "last_used_at": metadata["created_at"],
        "latest_event_seq": 0,
        "latest_message_seq": 0,
    }


def _host_thread_summaries(prefix: str) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    before: str | None = None
    seen_cursors: set[str] = set()
    while True:
        path = f"/v1/threads?limit={THREAD_LIST_PAGE}&prefix={prefix}"
        if before is not None:
            path += f"&before={quote(before, safe='')}"
        response = call_admin_api("GET", path)
        page = response.get("threads")
        if not isinstance(page, list) or not all(
            isinstance(item, dict) for item in page
        ):
            raise WorkspaceError(
                HTTPStatus.BAD_GATEWAY, "host admin returned invalid thread list"
            )
        summaries.extend(page)
        next_before = response.get("next_before")
        if next_before is None:
            break
        if (
            not isinstance(next_before, str)
            or not next_before
            or next_before in seen_cursors
        ):
            raise WorkspaceError(
                HTTPStatus.BAD_GATEWAY, "host admin returned invalid thread cursor"
            )
        seen_cursors.add(next_before)
        before = next_before
    return summaries


def _recorded_threads(
    *, archived: bool, scheduled: bool
) -> dict[str, dict[str, Any]]:
    """Return the database-owned members and metadata for one thread index."""
    if scheduled:
        query = (
            "SELECT thread_id, name, id, agent_runtime, model, effort,"
            " next_run_at, created_at FROM schedules"
            " WHERE deleted_at IS NULL"
        )
        params: tuple[Any, ...] = ()
    else:
        query = (
            "SELECT thread_id, COALESCE(name, thread_id),"
            " NULL, NULL, NULL, NULL, NULL, NULL FROM chat_threads"
            " WHERE archived = %s"
        )
        params = (archived,)
    with db.transaction() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
    return {
        str(row[0]): {
            "name": str(row[1]),
            "schedule_id": int(row[2]) if row[2] is not None else None,
            "agent_runtime": row[3],
            "model": row[4],
            "effort": row[5],
            "next_run_at": row[6],
            "created_at": row[7],
        }
        for row in rows
    }


def _chat_thread_summary(
    summary: dict[str, Any],
    *,
    metadata: dict[str, Any],
    archived: bool,
    has_session: bool,
) -> dict[str, Any]:
    runtime, model, effort = _host_thread_session_config(summary)
    if metadata["schedule_id"] is not None:
        runtime = metadata["agent_runtime"]
        model = metadata["model"]
        effort = metadata["effort"]
    status = summary.get("status")
    if status not in {"idle", "running"}:
        raise WorkspaceError(HTTPStatus.BAD_GATEWAY, "host admin returned invalid thread summary")
    return {
        "thread_id": _required_response_text(summary.get("thread_id"), "thread_id"),
        "name": metadata["name"],
        "agent_runtime": runtime,
        "model": model,
        "effort": effort,
        "archived": archived,
        "last_used_at": str(summary.get("last_used_at") or ""),
        "latest_event_seq": max(0, int(summary.get("latest_event_seq") or 0)),
        "latest_message_seq": max(0, int(summary.get("latest_message_seq") or 0)),
        "status": status,
        "schedule_id": metadata["schedule_id"],
        "next_run_at": metadata["next_run_at"],
        "has_session": has_session,
    }


def list_chat_thread_events(thread_id: str, query: dict[str, list[str]]) -> dict[str, Any]:
    """One chronological page of the thread's event stream.

    An uncursored request returns the latest page, ``before`` loads earlier
    history, and ``since`` keeps a loaded tail current. Only displayable event
    types cross the Workspace boundary. ``activity=false`` lets the hidden-activity UI
    page conversation events before the host applies its raw event limit.
    """
    _require_chat_thread(thread_id, include_archived=True)
    unexpected = sorted(set(query) - {"since", "before", "activity"})
    if unexpected:
        raise WorkspaceError(
            HTTPStatus.BAD_REQUEST,
            f"unsupported event query parameter: {unexpected[0]}",
        )
    since_values = query.get("since") or []
    before_values = query.get("before") or []
    activity_values = query.get("activity") or []
    if since_values and before_values:
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, "since and before cannot be combined")
    if len(activity_values) > 1 or (
        activity_values and activity_values[0] not in {"true", "false"}
    ):
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, "activity must be true or false")
    include_activity = not activity_values or activity_values[0] == "true"
    path = (
        f"/v1/threads/{quote(thread_id, safe='')}/events"
        f"?limit={THREAD_EVENT_PAGE}&message_bytes={THREAD_EVENT_MESSAGE_BYTES}"
    )
    event_types = THREAD_DISPLAY_EVENT_TYPES if include_activity else tuple(
        event_type
        for event_type in THREAD_DISPLAY_EVENT_TYPES
        if event_type != "thread.activity"
    )
    path += "".join(
        f"&event_type={quote(event_type, safe='')}"
        for event_type in event_types
    )
    cursor_name = "since" if since_values else "before" if before_values else None
    if cursor_name is not None:
        cursor = (since_values if cursor_name == "since" else before_values)[0]
        if not cursor.isdigit():
            raise WorkspaceError(
                HTTPStatus.BAD_REQUEST,
                f"{cursor_name} must be a non-negative integer",
            )
        path += f"&{cursor_name}={cursor}"
    try:
        response = call_admin_api("GET", path)
    except WorkspaceError as exc:
        if (
            exc.status == HTTPStatus.NOT_FOUND
            and SCHEDULE_THREAD_ID_RE.fullmatch(thread_id) is not None
        ):
            return {"events": []}
        raise
    events = response.get("events")
    if not isinstance(events, list):
        raise WorkspaceError(HTTPStatus.BAD_GATEWAY, "host admin returned invalid event list")
    return {"events": events}


def send_chat_message(body: Any) -> dict[str, Any]:
    """Send one message into the thread's agent session. The browser never
    chooses between starting work and directing work already in progress.
    Serializing sends prevents double submissions; safe-to-retry startup and
    shutdown conflicts are retried here so lifecycle races do not surface as
    dropped messages."""
    if not isinstance(body, dict):
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, "message request must be an object")
    message = _required_text(body.get("input_message"), "input_message")
    if "thread_id" in body:
        thread_id = _chat_thread_id(
            _required_text(body.get("thread_id"), "thread_id")
        )
    else:
        # A request without thread_id starts a new thread: Chat owns thread
        # naming, so the operator never types an id. Reservation is already
        # serialized by its database table lock; the generated id then gets
        # the same per-thread delivery lock as every existing conversation.
        thread_id = _reserve_generated_thread_id()
    with _message_send_lock(thread_id):
        schedule_config = _require_sendable_thread(thread_id)
        host_request: dict[str, Any] = {"message": message}
        if schedule_config is None:
            for field in ("agent_runtime", "model", "effort"):
                if field in body:
                    host_request[field] = body[field]
            _requested_session_config(body)
        else:
            if schedule_config["agent_runtime"] == SCRIPT_RUNTIME:
                raise WorkspaceError(
                    HTTPStatus.CONFLICT,
                    "Bash schedule transcripts are read-only",
                )
            host_request.update(schedule_config)
        response = _send_with_busy_retry(thread_id, host_request)
        status = response.get("status")
        if status != "accepted":
            raise WorkspaceError(HTTPStatus.BAD_GATEWAY, "host admin returned invalid send status")
        return {"action": "accepted", "thread_id": thread_id}


def _send_with_busy_retry(thread_id: str, host_request: dict[str, Any]) -> dict[str, Any]:
    return _post_with_busy_retry(
        f"/v1/threads/{quote(thread_id, safe='')}/messages",
        host_request,
        "the thread stayed busy while sending; retry the message",
    )


def _post_with_busy_retry(
    path: str, host_request: Any, exhausted_message: str
) -> dict[str, Any]:
    """Absorb the host's marked, safe-to-retry conflicts.

    A turn's brief startup and shutdown windows reject work with a 409 the
    host marks retryable. Those windows are invisible to the operator — the
    thread already reads as idle — so a single forwarded attempt would surface
    as a failure for an action that simply needs a moment.
    """
    return post_with_busy_retry(
        path,
        host_request,
        attempts=SEND_BUSY_RETRIES,
        exhausted_message=exhausted_message,
        post=call_admin_api,
    )


def _require_sendable_thread(thread_id: str) -> dict[str, str] | None:
    """Ensure the Workspace resource exists and accepts operator messages."""
    if SCHEDULE_THREAD_ID_RE.fullmatch(thread_id) is not None:
        with db.transaction() as cur:
            cur.execute(
                "SELECT agent_runtime, model, effort FROM schedules"
                " WHERE thread_id = %s AND deleted_at IS NULL",
                (thread_id,),
            )
            row = cur.fetchone()
        if row is None:
            raise WorkspaceError(HTTPStatus.NOT_FOUND, "active schedule not found")
        return {"agent_runtime": row[0], "model": row[1], "effort": row[2]}

    # The caller holds this thread's message lock, and archive/unarchive takes
    # the same lock, so the archived state cannot change before the host send.
    with db.transaction() as cur:
        cur.execute("LOCK TABLE chat_threads IN SHARE ROW EXCLUSIVE MODE")
        cur.execute(
            "INSERT INTO chat_threads (thread_id, archived)"
            " SELECT %s, FALSE WHERE (SELECT COUNT(*) FROM chat_threads) < %s"
            " ON CONFLICT (thread_id) DO NOTHING",
            (thread_id, MAX_CHAT_THREADS),
        )
        cur.execute(
            "SELECT archived FROM chat_threads WHERE thread_id = %s FOR UPDATE",
            (thread_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise WorkspaceError(
                HTTPStatus.CONFLICT,
                f"Workspace already retains {MAX_CHAT_THREADS} Chat threads",
            )
        if row[0]:
            raise WorkspaceError(HTTPStatus.CONFLICT, "archived threads are read-only")
        return None


def set_chat_thread_archived(thread_id: str, *, archived: bool) -> dict[str, Any]:
    if SCHEDULE_THREAD_ID_RE.fullmatch(thread_id) is not None:
        raise WorkspaceError(
            HTTPStatus.CONFLICT, "schedule transcripts cannot be archived"
        )
    if archived:
        _require_chat_thread(thread_id, include_archived=True)
        response = call_admin_api(
            "GET", f"/v1/threads/{quote(thread_id, safe='')}"
        )
        thread = response.get("thread")
        if not isinstance(thread, dict) or thread.get("status") not in {
            "idle", "running"
        }:
            raise WorkspaceError(
                HTTPStatus.BAD_GATEWAY, "host admin returned invalid thread"
            )
        if thread["status"] == "running":
            raise WorkspaceError(
                HTTPStatus.CONFLICT,
                "threads can only be archived while their agent is idle",
            )
    with db.transaction() as cur:
        cur.execute(
            "UPDATE chat_threads SET archived = %s WHERE thread_id = %s"
            " RETURNING thread_id, archived",
            (archived, thread_id),
        )
        row = cur.fetchone()
    if not row:
        raise WorkspaceError(HTTPStatus.NOT_FOUND, "thread not found")
    return {
        "thread_id": row[0],
        "archived": row[1],
    }


def archive_chat_thread(thread_id: str) -> dict[str, Any]:
    return set_chat_thread_archived(thread_id, archived=True)


def unarchive_chat_thread(thread_id: str) -> dict[str, Any]:
    return set_chat_thread_archived(thread_id, archived=False)


THREAD_NAME_MAX_CHARS = 100


def rename_chat_thread(thread_id: str, body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, "rename request must be an object")
    name = _required_text(body.get("name"), "name")
    if len(name) > THREAD_NAME_MAX_CHARS:
        raise WorkspaceError(
            HTTPStatus.BAD_REQUEST,
            f"name must be at most {THREAD_NAME_MAX_CHARS} characters",
        )
    if SCHEDULE_THREAD_ID_RE.fullmatch(thread_id) is not None:
        # Imported lazily to avoid making the two Workspace route modules
        # depend on each other during service startup.
        from host.runtime.workspace import schedules

        name = schedules.validate_schedule_name(name)
        renamed = schedules.rename_scheduled_agent(thread_id, name)
        if renamed is None:
            raise WorkspaceError(HTTPStatus.NOT_FOUND, "active schedule not found")
        return renamed
    with db.transaction() as cur:
        cur.execute(
            "UPDATE chat_threads SET name = %s WHERE thread_id = %s"
            " RETURNING thread_id, name",
            (name, thread_id),
        )
        row = cur.fetchone()
    if not row:
        raise WorkspaceError(HTTPStatus.NOT_FOUND, "thread not found")
    return {"thread_id": row[0], "name": row[1]}


def _reserve_generated_thread_id() -> str:
    """Allocate the next successive thread name (thread-1, thread-2, ...).

    The name is reserved by inserting its thread row before the host call:
    the primary key makes concurrent generators take distinct names instead
    of merging two new chats into one thread (the host accepts a matching
    session configuration on an existing thread). Names count over every
    recorded thread, archived included, so a generated id never revives an
    archived thread. A reservation whose host call later fails stays as an
    empty thread: the index hides threads the host has never seen and the
    generator counts it, so its number is skipped rather than reused.
    """
    while True:
        with db.transaction() as cur:
            cur.execute("LOCK TABLE chat_threads IN SHARE ROW EXCLUSIVE MODE")
            cur.execute("SELECT thread_id FROM chat_threads")
            rows = cur.fetchall()
            if len(rows) >= MAX_CHAT_THREADS:
                raise WorkspaceError(
                    HTTPStatus.CONFLICT,
                    f"Workspace already retains {MAX_CHAT_THREADS} Chat threads",
                )
            numbers = [
                int(match.group(1))
                for (thread_id,) in rows
                if (match := GENERATED_THREAD_ID_RE.fullmatch(thread_id)) is not None
            ]
            candidate = f"thread-{max(numbers, default=0) + 1}"
            cur.execute(
                "INSERT INTO chat_threads (thread_id, archived) VALUES (%s, FALSE)"
                " ON CONFLICT (thread_id) DO NOTHING RETURNING thread_id",
                (candidate,),
            )
            if cur.fetchone() is not None:
                return candidate


def _require_chat_thread(thread_id: str, *, include_archived: bool = False) -> None:
    with db.transaction() as cur:
        if SCHEDULE_THREAD_ID_RE.fullmatch(thread_id) is not None:
            query = (
                "SELECT 1 FROM schedules"
                " WHERE thread_id = %s AND deleted_at IS NULL"
            )
        else:
            query = "SELECT 1 FROM chat_threads WHERE thread_id = %s"
            if not include_archived:
                query += " AND archived = FALSE"
        cur.execute(query, (thread_id,))
        row = cur.fetchone()
    if not row:
        raise WorkspaceError(HTTPStatus.NOT_FOUND, "thread not found")

def _requested_session_config(body: dict[str, Any]) -> tuple[str, str, str] | None:
    fields = ("agent_runtime", "model", "effort")
    supplied = [field for field in fields if field in body]
    if not supplied:
        return None
    if len(supplied) != len(fields):
        raise WorkspaceError(
            HTTPStatus.BAD_REQUEST,
            "agent_runtime, model, and effort must be provided together",
        )

    agent_runtime = _required_text(body.get("agent_runtime"), "agent_runtime")
    model = body.get("model")
    effort = body.get("effort")
    error = session_config_error(agent_runtime, model, effort)
    if error is not None:
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, error)
    assert isinstance(model, str) and isinstance(effort, str)
    return agent_runtime, model, effort


def _host_thread_session_config(summary: dict[str, Any]) -> tuple[str, str, str]:
    """Read back the session configuration the host recorded for a thread.

    Read path (`host.session_options`): the recorded model may predate the
    current matrix, so a thread started under an earlier catalog stays listed
    and openable even though the host refuses to run further turns on it. The
    matrix check belongs on the send path (`_requested_session_config`).
    """
    config = recorded_session_config(summary)
    if config is None:
        raise WorkspaceError(HTTPStatus.BAD_GATEWAY, "host admin returned invalid session configuration")
    return config


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, f"{label} must be a non-empty string")
    return value.strip()


def _required_response_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkspaceError(HTTPStatus.BAD_GATEWAY, f"host admin returned invalid {label}")
    return value.strip()


def _path_segment(value: str) -> str:
    decoded = unquote(value)
    if not decoded or "/" in decoded or "\\" in decoded:
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, "invalid path segment")
    return decoded


def _chat_thread_id(value: str) -> str:
    if THREAD_ID_RE.fullmatch(value) is None:
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, "invalid Chat thread id")
    return value


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
