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
from host.session_options import public_session_options, recorded_session_config, session_config_error


MAX_REQUEST_BODY_BYTES = 128 * 1024
RUNTIME_OPTIONS = {"codex", "claude_code", "grok", "hermes"}
THREAD_ID_RE = re.compile(r"thread-([1-9][0-9]*)")
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
    if method == "GET" and path.startswith("/threads/") and path.endswith("/events"):
        parts = path.strip("/").split("/")
        if len(parts) != 3:
            raise WorkspaceError(HTTPStatus.NOT_FOUND, "route not found")
        return list_chat_thread_events(_chat_thread_id(_path_segment(parts[1])), query or {})
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
    """The thread index: one bulk host call joined against Chat's own
    thread bookkeeping. A prefix-filtered host `GET /v1/threads` returns
    session config and live status for this product's direct thread ids, so the
    index costs one socket round trip regardless of thread count.

    A thread is shown only when it is unarchived and known to the host: the
    host row appears with the thread's first message, so a name reservation
    whose send never went through stays invisible. The host stays the source
    of truth for runtime/model/effort and live status; Chat contributes
    names and archive state."""
    recorded = _recorded_threads(archived=archived)
    summaries = _host_thread_summaries()
    chat_threads = [
        _chat_thread_summary(
            summary,
            name=recorded[summary["thread_id"]],
            archived=archived,
        )
        for summary in summaries
        if isinstance(summary, dict) and summary.get("thread_id") in recorded
    ]
    chat_threads.sort(key=lambda item: str(item.get("last_used_at") or ""), reverse=True)
    return {"threads": chat_threads}


def _host_thread_summaries() -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    before: str | None = None
    seen_cursors: set[str] = set()
    while True:
        path = f"/v1/threads?limit={THREAD_LIST_PAGE}&prefix=thread-"
        if before is not None:
            path += f"&before={quote(before, safe='')}"
        response = call_admin_api("GET", path)
        page = response.get("threads")
        if not isinstance(page, list) or not all(isinstance(item, dict) for item in page):
            raise WorkspaceError(HTTPStatus.BAD_GATEWAY, "host admin returned invalid thread list")
        summaries.extend(page)
        next_before = response.get("next_before")
        if next_before is None:
            return summaries
        if (
            not isinstance(next_before, str)
            or not next_before
            or next_before in seen_cursors
        ):
            raise WorkspaceError(HTTPStatus.BAD_GATEWAY, "host admin returned invalid thread cursor")
        seen_cursors.add(next_before)
        before = next_before


def _recorded_threads(*, archived: bool) -> dict[str, str]:
    """Threads in one archive state mapped to their display names. Threads
    without a custom name keep showing their stable host id."""
    with db.transaction() as cur:
        cur.execute(
            "SELECT thread_id, COALESCE(name, thread_id) FROM chat_threads WHERE archived = %s",
            (archived,),
        )
        rows = cur.fetchall()
    return {thread_id: name for thread_id, name in rows}


def _chat_thread_summary(
    summary: dict[str, Any],
    *,
    name: str,
    archived: bool,
) -> dict[str, Any]:
    runtime, model, effort = _host_thread_session_config(summary)
    status = summary.get("status")
    if status not in {"idle", "running"}:
        raise WorkspaceError(HTTPStatus.BAD_GATEWAY, "host admin returned invalid thread summary")
    return {
        "thread_id": _required_response_text(summary.get("thread_id"), "thread_id"),
        "name": name,
        "agent_runtime": runtime,
        "model": model,
        "effort": effort,
        "archived": archived,
        "last_used_at": str(summary.get("last_used_at") or ""),
        "latest_event_seq": max(0, int(summary.get("latest_event_seq") or 0)),
        "latest_message_seq": max(0, int(summary.get("latest_message_seq") or 0)),
        "status": status,
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
    response = call_admin_api("GET", path)
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
        _require_sendable_thread(thread_id)
        host_request: dict[str, Any] = {"message": message}
        for field in ("agent_runtime", "model", "effort"):
            if field in body:
                host_request[field] = body[field]
        _requested_session_config(body)
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
    for attempt in range(SEND_BUSY_RETRIES):
        try:
            return call_admin_api("POST", path, host_request)
        except WorkspaceError as exc:
            transient = exc.status == HTTPStatus.CONFLICT and SEND_RETRY_MARKER in exc.message
            if not transient or attempt == SEND_BUSY_RETRIES - 1:
                raise
            time.sleep(SEND_BUSY_RETRY_DELAY_SECONDS)
    raise WorkspaceError(HTTPStatus.CONFLICT, exhausted_message)


def _require_sendable_thread(thread_id: str) -> None:
    """Ensure Chat's thread row exists and is not archived before the host
    call. The caller holds this thread's message lock, and archive/unarchive
    updates take the same lock, so the archived state checked here cannot
    change before the host send completes."""
    with db.transaction() as cur:
        cur.execute("LOCK TABLE chat_threads IN SHARE ROW EXCLUSIVE MODE")
        cur.execute(
            "INSERT INTO chat_threads (thread_id, archived)"
            " SELECT %s, FALSE WHERE (SELECT COUNT(*) FROM chat_threads) < %s"
            " ON CONFLICT (thread_id) DO NOTHING",
            (thread_id, MAX_CHAT_THREADS),
        )
        cur.execute("SELECT archived FROM chat_threads WHERE thread_id = %s FOR UPDATE", (thread_id,))
        row = cur.fetchone()
        if row is None:
            raise WorkspaceError(
                HTTPStatus.CONFLICT,
                f"Workspace already retains {MAX_CHAT_THREADS} Chat threads",
            )
        if row[0]:
            raise WorkspaceError(HTTPStatus.CONFLICT, "archived threads are read-only")


def set_chat_thread_archived(thread_id: str, *, archived: bool) -> dict[str, Any]:
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
                if (match := THREAD_ID_RE.fullmatch(thread_id)) is not None
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
