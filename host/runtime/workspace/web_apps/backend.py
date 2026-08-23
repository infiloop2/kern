"""Agentic Web App backend.

Each workspace owns one coherently revisioned generated UI bundle, JSON
document, and queryable row store, plus a sparse restorable revision history
and one fixed agent thread. Browser calls use the authenticated admin proxy,
while agent calls use the service's peer-authenticated Unix socket.
Generated browser code never reaches this process as authority: all durable
mutations are validated here.

Every durable UI or data write compares and advances one ``revision`` while
the workspace row is locked. The shared token prevents one storage path from
being changed against App state that moved after the caller read it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from http import HTTPStatus
import json
import re
import threading
import time
from typing import Any
from urllib.parse import quote, unquote

from host.constants import MAX_WORKSPACE_RESPONSE_BODY_BYTES
from host.runtime.core import db
from host.runtime.workspace.host_api import WorkspaceError, active_agent_runtimes, call_admin_api
from host.runtime.workspace.busy_retry import post_with_busy_retry
from host.runtime.workspace.web_apps import collections as collection_store
from host.runtime.workspace.web_apps.collections import (
    COLLECTION_NAME_RE,
    COLLECTION_ROW_ID_RE,
    MAX_COLLECTION_BATCH_OPERATIONS,
    MAX_COLLECTION_DATA_BYTES,
    MAX_COLLECTION_FIELD_BYTES,
    MAX_COLLECTION_QUERY_FILTERS,
    MAX_COLLECTION_QUERY_LIMIT,
    MAX_COLLECTION_QUERY_OFFSET,
    MAX_COLLECTION_RESTORE_BATCH_BYTES,
    MAX_COLLECTION_RESTORE_BATCH_ROWS,
    MAX_COLLECTION_ROW_BYTES,
    MAX_COLLECTION_ROWS,
    MAX_COLLECTIONS,
)
from host.runtime.workspace.web_apps.data_shape import (
    MAX_SHAPE_ARRAY_SAMPLE,
    MAX_SHAPE_DEPTH,
    MAX_SHAPE_NODES,
    MAX_SHAPE_OBJECT_KEYS,
    data_shape,
    utf8_length as _utf8_length,
)
from host.session_options import public_session_options, recorded_session_config, session_config_error


APP_ID_RE = re.compile(r"app-([1-9][0-9]*)")
MAX_REQUEST_BODY_BYTES = 768 * 1024
MAX_HTML_BYTES = 128 * 1024
MAX_CSS_BYTES = 64 * 1024
MAX_JAVASCRIPT_BYTES = 128 * 1024
MAX_DATA_BYTES = 10 * 1024 * 1024
# The proxy cap includes the response envelope and json.dumps whitespace. UI
# source can also expand when escaped, so leave a full MiB of transport room.
MAX_STATE_RESPONSE_BYTES = MAX_WORKSPACE_RESPONSE_BODY_BYTES - 1024 * 1024
MAX_CHAT_MESSAGE_BYTES = 50_000
APP_MESSAGE_CONTEXT = "This request is for Web App `{app_id}`.\n\n---\n\n"
MAX_APP_NAME_CHARS = 100
# Apps are durable user projects, so maintenance must not silently delete
# them. A creation quota gives their current state and per-app revision bounds
# a finite aggregate storage ceiling instead.
MAX_WEB_APPS = 100
# Sparse retention caps one App at 17 rows, so one bounded response can present
# every restorable point without a second recovery paging UI.
REVISION_PAGE_LIMIT = 17
REVISION_MAX_RETAINED = 17
REVISION_EXACT_RETAINED = 5
REVISION_RETAIN_DAYS = 7
CONVERSATION_MESSAGE_BYTES = 120 * 1024
THREAD_LIST_PAGE = 100
# A live turn has brief startup and shutdown windows where it cannot accept a
# steer. The host marks those safe-to-retry conflicts explicitly.
SEND_RETRY_MARKER = "retry shortly"
SEND_BUSY_RETRIES = 21
SEND_BUSY_RETRY_DELAY_SECONDS = 0.5
# Match Agent Chat's bounded event window. message_bytes limits the already
# JSON-encoded string size, so six 120 KiB events leave bridge headroom even
# for text that requires escaping.
CONVERSATION_EVENT_PAGE_LIMIT = 6
CONVERSATION_EVENT_TYPES = (
    "thread.message",
    "thread.activity",
    "thread.error",
    "thread.stopped",
)
MAX_PATH_DEPTH = 16
MAX_PATH_KEY_BYTES = 128
MAX_DATA_READ_PATHS = 16
MAX_BATCH_OPERATIONS = 32
# A shape response answers "which branch should I read" and must stay far
# cheaper than the document it describes, so every dimension it walks is
# bounded and every cut it makes is marked where the caller would otherwise
# read absence as completeness.
JAVASCRIPT_FORBIDDEN = re.compile(r"\bimport\b")
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
# Message creation and other writes on one workspace must not interleave.
# A fixed stripe set keeps that coordination bounded while unrelated
# workspaces normally proceed independently.
WORKSPACE_LOCKS = tuple(threading.RLock() for _ in range(64))
STATE_COLUMNS = (
    "revision, html, css, javascript, data_json, updated_at, agent_updates_locked"
)
SUMMARY_COLUMNS = (
    "app_id, name, revision, created_at, updated_at, archived, agent_updates_locked"
)
AGENT_UPDATES_LOCKED_MESSAGE = (
    "The user has locked agent updates for this app. Do not change the app now; "
    "retry again in a while after the user unlocks updates."
)

# Keep these validation seams available to existing route and test callers
# while the collection engine lives in its own module.
_validated_collection_name = collection_store._validated_collection_name
_validated_collection_row = collection_store._validated_collection_row


def route_browser(
    method: str,
    path: str,
    body: Any,
    query: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    app_id = _browser_mutation_app_id(method, path)
    if app_id is None:
        return _route_browser(method, path, body, query)
    with _workspace_lock(app_id):
        return _route_browser(method, path, body, query)


def _browser_mutation_app_id(method: str, path: str) -> str | None:
    """Return the decoded app id whose browser mutation must be serialized."""
    if method == "GET":
        return None
    parts = path.strip("/").split("/")
    if len(parts) <= 1 or parts[0] != "apps":
        return None
    return _path_segment(parts[1])


def _route_browser(
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
    if method == "GET" and path == "/apps":
        return list_web_apps(query or {})
    if method == "POST" and path == "/apps":
        return {"app": create_web_app()}

    match = re.fullmatch(r"/apps/([^/]+)/(state|conversation)", path)
    if method == "GET" and match:
        app_id, resource = _path_segment(match.group(1)), match.group(2)
        if resource == "state":
            return {"app": load_app_state(app_id)}
        return browser_conversation(app_id)

    match = re.fullmatch(r"/apps/([^/]+)/state/(ui|data)", path)
    if method == "GET" and match:
        app_id, resource = _path_segment(match.group(1)), match.group(2)
        return {
            "app": load_app_ui(app_id) if resource == "ui" else load_app_data(app_id)
        }

    match = re.fullmatch(r"/apps/([^/]+)/runtime/data/read", path)
    if method == "POST" and match:
        return {
            "app": read_app_data_path(_path_segment(match.group(1)), body)
        }

    match = re.fullmatch(r"/apps/([^/]+)/runtime/collections/([^/]+)/query", path)
    if method == "POST" and match:
        return {
            "collection": query_collection(
                _path_segment(match.group(1)),
                _collection_path_segment(match.group(2)),
                body,
            )
        }

    match = re.fullmatch(r"/apps/([^/]+)/conversation/events", path)
    if method == "GET" and match:
        return browser_conversation_events(
            _path_segment(match.group(1)), query or {}
        )

    match = re.fullmatch(r"/apps/([^/]+)/name", path)
    if method == "PUT" and match:
        app_id = _path_segment(match.group(1))
        _require_writable_web_app(app_id)
        return {"app": rename_web_app(app_id, body)}

    match = re.fullmatch(r"/apps/([^/]+)/agent-updates", path)
    if method == "PUT" and match:
        app_id = _path_segment(match.group(1))
        _require_writable_web_app(app_id)
        return {"app": set_agent_updates_locked(app_id, body)}

    match = re.fullmatch(r"/apps/([^/]+)/(archive|unarchive)", path)
    if method == "POST" and match:
        app_id = _path_segment(match.group(1))
        with _workspace_lock(app_id):
            return {"app": set_web_app_archived(app_id, match.group(2) == "archive")}

    match = re.fullmatch(
        r"/apps/([^/]+)/(messages|runtime/agent-requests|runtime/actions)", path
    )
    if method == "POST" and match:
        app_id, resource = _path_segment(match.group(1)), match.group(2)
        with _workspace_lock(app_id):
            _require_writable_web_app(app_id)
            if resource == "runtime/actions":
                return apply_runtime_action(body, app_id)
            return create_message(body, app_id=app_id)

    match = re.fullmatch(r"/apps/([^/]+)/revisions", path)
    if method == "GET" and match:
        return list_revisions(_path_segment(match.group(1)), query or {})

    match = re.fullmatch(r"/apps/([^/]+)/revisions/([0-9]{1,18})/restore", path)
    if method == "POST" and match:
        app_id = _path_segment(match.group(1))
        with _workspace_lock(app_id):
            _require_writable_web_app(app_id)
            return restore_revision(app_id, int(match.group(2)))

    match = re.fullmatch(r"/apps/([^/]+)/stop", path)
    if method == "POST" and match:
        app_id = _path_segment(match.group(1))
        _require_web_app(app_id)
        return call_admin_api(
            "POST", f"/v1/threads/{quote(app_id, safe='')}/stop", body
        )
    raise WorkspaceError(HTTPStatus.NOT_FOUND, "route not found")


def route_agent(
    method: str,
    path: str,
    body: Any,
    query: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Route one explicit agent request.

    App access is host-wide for agents: the immutable app id in the route is
    the target. Archived apps remain readable but reject every mutation.
    Revert remains absent because restoring operator-visible state is a human
    control.
    """
    if method == "GET" and path == "/agent/apps":
        if query:
            raise WorkspaceError(HTTPStatus.BAD_REQUEST, "agent app listing takes no query")
        return list_all_web_apps()
    if method == "POST" and path == "/agent/apps":
        if query:
            raise WorkspaceError(HTTPStatus.BAD_REQUEST, "agent app creation takes no query")
        if body not in (None, {}):
            raise WorkspaceError(
                HTTPStatus.BAD_REQUEST,
                "agent app creation accepts no fields",
            )
        return {"app": create_web_app(actor="agent")}

    match = re.fullmatch(r"/agent/apps/([^/]+)(/.*)", path)
    if match is None:
        raise WorkspaceError(HTTPStatus.NOT_FOUND, "agent route not found")
    app_id = _path_segment(match.group(1))
    resource = match.group(2)
    _require_web_app(app_id)
    if query:
        raise WorkspaceError(
            HTTPStatus.BAD_REQUEST,
            "this agent route does not accept query parameters",
        )

    if method == "GET" and resource == "/state/meta":
        return {"app": load_app_state_meta(app_id)}
    if method == "GET" and resource == "/state/ui":
        return {"app": load_app_ui(app_id)}
    if method == "GET" and resource == "/state/data":
        return {"app": load_app_data(app_id)}
    if method == "GET" and resource == "/state/data/shape":
        return {"app": load_app_data_shape(app_id)}
    if method == "POST" and resource == "/state/data/read":
        return {"app": read_app_data_path(app_id, body)}
    if method == "GET" and resource == "/collections":
        return {"collections": list_collections(app_id)}
    collection_match = re.fullmatch(r"/collections/([^/]+)/(query|actions)", resource)
    if collection_match:
        collection = _collection_path_segment(collection_match.group(1))
        collection_resource = collection_match.group(2)
        if method == "POST" and collection_resource == "query":
            return {"collection": query_collection(app_id, collection, body)}
        if method == "POST" and collection_resource == "actions":
            with _workspace_lock(app_id):
                _require_agent_writable_web_app(app_id)
                return apply_collection_actions(app_id, collection, body)
    if method == "POST" and resource == "/actions":
        with _workspace_lock(app_id):
            _require_agent_writable_web_app(app_id)
            return apply_agent_action(body, app_id)
    raise WorkspaceError(HTTPStatus.NOT_FOUND, "agent route not found")


def list_web_apps(query: dict[str, list[str]]) -> dict[str, Any]:
    unexpected = sorted(set(query) - {"archived"})
    if unexpected:
        raise WorkspaceError(
            HTTPStatus.BAD_REQUEST,
            f"unexpected app query fields: {', '.join(unexpected)}",
        )
    archived_values = query.get("archived") or []
    if len(archived_values) > 1 or (
        archived_values and archived_values[0] not in {"true", "false"}
    ):
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, "archived must be true or false")
    archived = bool(archived_values and archived_values[0] == "true")
    return _list_web_apps(archived)


def list_all_web_apps() -> dict[str, Any]:
    """One database snapshot for the agent's active-plus-archived index."""
    return _list_web_apps(None)


def _list_web_apps(archived: bool | None) -> dict[str, Any]:
    with db.transaction() as cur:
        if archived is None:
            cur.execute(f"SELECT {SUMMARY_COLUMNS} FROM web_apps")
        else:
            cur.execute(
                f"SELECT {SUMMARY_COLUMNS} FROM web_apps WHERE archived = %s",
                (archived,),
            )
        rows = cur.fetchall()
    summaries: dict[str, dict[str, Any]] = {}
    if rows:
        host_threads = _host_thread_summaries()
        summaries = {
            summary["thread_id"]: summary
            for summary in host_threads
            if isinstance(summary, dict)
            and isinstance(summary.get("thread_id"), str)
        }
    apps = [
        _web_app_summary(row, summaries.get(row[0]))
        for row in rows
    ]
    apps.sort(
        key=lambda app: str(app.get("last_used_at") or app["updated_at"]),
        reverse=True,
    )
    return {"apps": apps}


def _host_thread_summaries() -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    before: str | None = None
    seen_cursors: set[str] = set()
    while True:
        path = f"/v1/threads?limit={THREAD_LIST_PAGE}&prefix=app-"
        if before is not None:
            path += f"&before={quote(before, safe='')}"
        response = call_admin_api("GET", path)
        page = response.get("threads")
        if not isinstance(page, list) or not all(isinstance(item, dict) for item in page):
            raise WorkspaceError(
                HTTPStatus.BAD_GATEWAY, "host admin returned invalid thread list"
            )
        summaries.extend(page)
        next_before = response.get("next_before")
        if next_before is None:
            return summaries
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


def _web_app_summary(
    row: tuple[Any, ...], host_summary: dict[str, Any] | None
) -> dict[str, Any]:
    session: dict[str, str] | None = None
    status = "idle"
    last_used_at = row[4]
    latest_event_seq = 0
    latest_message_seq = 0
    if host_summary is not None:
        host_status = host_summary.get("status")
        if host_status not in {"idle", "running"}:
            raise WorkspaceError(
                HTTPStatus.BAD_GATEWAY, "host admin returned invalid thread summary"
            )
        status = host_status
        session = _thread_session_config(host_summary)
        last_used_at = str(host_summary.get("last_used_at") or row[4])
        latest_event_seq = max(0, int(host_summary.get("latest_event_seq") or 0))
        latest_message_seq = max(0, int(host_summary.get("latest_message_seq") or 0))
    return {
        "app_id": row[0],
        "name": row[1],
        "revision": row[2],
        "created_at": row[3],
        "updated_at": row[4],
        "last_used_at": last_used_at,
        "latest_event_seq": latest_event_seq,
        "latest_message_seq": latest_message_seq,
        "session": session,
        "status": status,
        "archived": bool(row[5]),
        "agent_updates_locked": bool(row[6]),
    }


def create_web_app(*, actor: str = "user") -> dict[str, Any]:
    # Match Agent Chat's allocator. The insert reserves an id across concurrent
    # creators, and every existing id remains counted so one is never reused.
    while True:
        now = _utc_now()
        with db.transaction() as cur:
            # The table lock makes the quota and id reservation one atomic
            # decision across concurrent Workspace request handlers.
            cur.execute("LOCK TABLE web_apps IN SHARE ROW EXCLUSIVE MODE")
            cur.execute("SELECT COUNT(*) FROM web_apps")
            count_row = cur.fetchone()
            if count_row is not None and int(count_row[0]) >= MAX_WEB_APPS:
                raise WorkspaceError(
                    HTTPStatus.CONFLICT,
                    f"Workspace already retains {MAX_WEB_APPS} Web Apps",
                )
            cur.execute("SELECT app_id FROM web_apps")
            rows = cur.fetchall()
            numbers = [
                int(match.group(1))
                for (app_id,) in rows
                if (match := APP_ID_RE.fullmatch(app_id)) is not None
            ]
            app_id = f"app-{max(numbers, default=0) + 1}"
            cur.execute(
                "INSERT INTO web_apps"
                " (app_id, name, revision, html,"
                " css, javascript, data_json, created_at, updated_at)"
                " VALUES (%s, %s, 0, '', '', '', '{}', %s, %s)"
                " ON CONFLICT (app_id) DO NOTHING"
                f" RETURNING {SUMMARY_COLUMNS}",
                (app_id, app_id, now, now),
            )
            row = cur.fetchone()
            if row is not None:
                cur.execute(
                    "INSERT INTO web_app_collection_state (app_id) VALUES (%s)",
                    (app_id,),
                )
                _insert_revision(
                    cur,
                    app_id,
                    revision=0,
                    actor=actor,
                    kind="created",
                    restored_from=None,
                    html="",
                    css="",
                    javascript="",
                    data_json="{}",
                    now=now,
                )
        if row is not None:
            return _web_app_summary(row, None)


def rename_web_app(app_id: str, body: Any) -> dict[str, Any]:
    request = _required_object(body, "rename request")
    _require_keys(request, {"name"}, required={"name"})
    name = _required_text(request.get("name"), "name")
    if len(name) > MAX_APP_NAME_CHARS:
        raise WorkspaceError(
            HTTPStatus.BAD_REQUEST,
            f"name must be at most {MAX_APP_NAME_CHARS} characters",
        )
    with db.transaction() as cur:
        cur.execute(
            "UPDATE web_apps SET name = %s WHERE app_id = %s"
            f" RETURNING {SUMMARY_COLUMNS}",
            (name, app_id),
        )
        row = cur.fetchone()
    if row is None:
        raise WorkspaceError(HTTPStatus.NOT_FOUND, "app not found")
    return _web_app_summary(row, None)


def set_agent_updates_locked(app_id: str, body: Any) -> dict[str, Any]:
    request = _required_object(body, "agent update lock request")
    _require_keys(request, {"locked"}, required={"locked"})
    locked = request.get("locked")
    if not isinstance(locked, bool):
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, "locked must be a boolean")
    with db.transaction() as cur:
        cur.execute(
            "UPDATE web_apps SET agent_updates_locked = %s WHERE app_id = %s"
            f" RETURNING {SUMMARY_COLUMNS}",
            (locked, app_id),
        )
        row = cur.fetchone()
    if row is None:
        raise WorkspaceError(HTTPStatus.NOT_FOUND, "app not found")
    return _web_app_summary(row, None)


def set_web_app_archived(app_id: str, archived: bool) -> dict[str, Any]:
    _require_web_app(app_id)
    if archived:
        try:
            response = call_admin_api(
                "GET", f"/v1/threads/{quote(app_id, safe='')}"
            )
        except WorkspaceError as exc:
            if exc.status != HTTPStatus.NOT_FOUND:
                raise
        else:
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
                    "apps can only be archived while their agent is idle",
                )
    with db.transaction() as cur:
        cur.execute(
            "UPDATE web_apps SET archived = %s WHERE app_id = %s"
            f" RETURNING {SUMMARY_COLUMNS}",
            (archived, app_id),
        )
        row = cur.fetchone()
    if row is None:
        raise WorkspaceError(HTTPStatus.NOT_FOUND, "app not found")
    return _web_app_summary(row, None)


def _require_web_app(app_id: str) -> None:
    with db.transaction() as cur:
        cur.execute("SELECT 1 FROM web_apps WHERE app_id = %s", (app_id,))
        row = cur.fetchone()
    if row is None:
        raise WorkspaceError(HTTPStatus.NOT_FOUND, "app not found")


def _require_writable_web_app(app_id: str) -> None:
    with db.transaction() as cur:
        cur.execute("SELECT archived FROM web_apps WHERE app_id = %s", (app_id,))
        row = cur.fetchone()
    if row is None:
        raise WorkspaceError(HTTPStatus.NOT_FOUND, "app not found")
    if row[0]:
        raise WorkspaceError(HTTPStatus.CONFLICT, "archived apps are read-only")


def _require_agent_writable_web_app(app_id: str) -> None:
    with db.transaction() as cur:
        cur.execute(
            "SELECT archived, agent_updates_locked FROM web_apps WHERE app_id = %s",
            (app_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise WorkspaceError(HTTPStatus.NOT_FOUND, "app not found")
    if row[0]:
        raise WorkspaceError(HTTPStatus.CONFLICT, "archived apps are read-only")
    if row[1]:
        raise WorkspaceError(HTTPStatus.LOCKED, AGENT_UPDATES_LOCKED_MESSAGE)


def _workspace_lock(app_id: str) -> threading.RLock:
    return WORKSPACE_LOCKS[hash(app_id) % len(WORKSPACE_LOCKS)]


def browser_conversation(app_id: str) -> dict[str, Any]:
    """The workspace's agent session and live status. The conversation
    contents themselves come from the thread event stream."""
    _require_web_app(app_id)
    try:
        response = call_admin_api("GET", f"/v1/threads/{quote(app_id, safe='')}")
    except WorkspaceError as exc:
        if exc.status == HTTPStatus.NOT_FOUND:
            # The host thread appears with the workspace's first message; an
            # unconfigured workspace is simply idle with no session yet.
            return {"session": None, "status": "idle"}
        raise
    thread = response.get("thread")
    if not isinstance(thread, dict):
        raise WorkspaceError(HTTPStatus.BAD_GATEWAY, "host admin returned invalid thread")
    status = thread.get("status")
    if status not in {"idle", "running"}:
        raise WorkspaceError(HTTPStatus.BAD_GATEWAY, "host admin returned invalid thread")
    return {"session": _thread_session_config(thread), "status": status}


def browser_conversation_events(
    app_id: str, query: dict[str, list[str]]
) -> dict[str, Any]:
    _require_web_app(app_id)
    unexpected = sorted(set(query) - {"since", "before", "activity"})
    if unexpected:
        raise WorkspaceError(
            HTTPStatus.BAD_REQUEST,
            f"unexpected conversation event query fields: {', '.join(unexpected)}",
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
    for name, values in (("since", since_values), ("before", before_values)):
        if len(values) > 1:
            raise WorkspaceError(HTTPStatus.BAD_REQUEST, f"{name} must be provided once")
    parameters = [
        f"limit={CONVERSATION_EVENT_PAGE_LIMIT}",
        f"message_bytes={CONVERSATION_MESSAGE_BYTES}",
        *(
            f"event_type={quote(event_type, safe='')}"
            for event_type in CONVERSATION_EVENT_TYPES
            if include_activity or event_type != "thread.activity"
        ),
    ]
    cursor_name = "since" if since_values else "before" if before_values else None
    if cursor_name is not None:
        cursor = (since_values if cursor_name == "since" else before_values)[0]
        if not cursor.isdigit():
            raise WorkspaceError(
                HTTPStatus.BAD_REQUEST,
                f"{cursor_name} must be a non-negative integer",
            )
        parameters.insert(0, f"{cursor_name}={cursor}")
    path = f"/v1/threads/{quote(app_id, safe='')}/events?{'&'.join(parameters)}"
    response = call_admin_api("GET", path)
    events = response.get("events")
    if not isinstance(events, list) or not all(isinstance(event, dict) for event in events):
        raise WorkspaceError(HTTPStatus.BAD_GATEWAY, "host admin returned invalid event list")
    return {"events": events}


def create_message(body: Any, *, app_id: str) -> dict[str, Any]:
    _require_web_app(app_id)
    request = _required_object(body, "message request")
    allowed = {"content", "agent_runtime", "model", "effort"}
    _require_keys(request, allowed, required={"content"})
    context = APP_MESSAGE_CONTEXT.format(app_id=app_id)
    content = _bounded_required_text(
        request.get("content"),
        "content",
        MAX_CHAT_MESSAGE_BYTES - len(context.encode()),
    )
    host_request: dict[str, Any] = {"message": f"{context}{content}"}
    config_fields = ("agent_runtime", "model", "effort")
    supplied = [field for field in config_fields if field in request]
    if supplied:
        if len(supplied) != len(config_fields):
            raise WorkspaceError(HTTPStatus.BAD_REQUEST, "agent_runtime, model, and effort must be provided together")
        runtime = _required_text(request.get("agent_runtime"), "agent_runtime")
        model = request.get("model")
        effort = request.get("effort")
        error = session_config_error(runtime, model, effort)
        if error is not None:
            raise WorkspaceError(HTTPStatus.BAD_REQUEST, error)
        assert isinstance(model, str) and isinstance(effort, str)
        host_request.update({"agent_runtime": runtime, "model": model, "effort": effort})
    response = _send_with_busy_retry(app_id, host_request)
    status = response.get("status")
    if status != "accepted":
        raise WorkspaceError(HTTPStatus.BAD_GATEWAY, "host admin returned invalid send status")
    return {"status": status, "app_id": app_id}


def _send_with_busy_retry(app_id: str, host_request: dict[str, Any]) -> dict[str, Any]:
    path = f"/v1/threads/{quote(app_id, safe='')}/messages"
    return post_with_busy_retry(
        path,
        host_request,
        attempts=SEND_BUSY_RETRIES,
        exhausted_message="the thread stayed busy while sending; retry the message",
        post=call_admin_api,
    )


def load_app_state(app_id: str) -> dict[str, Any]:
    _require_web_app(app_id)
    with db.transaction() as cur:
        cur.execute(
            f"SELECT {STATE_COLUMNS} FROM web_apps WHERE app_id = %s",
            (app_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise WorkspaceError(HTTPStatus.INTERNAL_SERVER_ERROR, "app state is unavailable")
    return _state_row(row)


def load_app_state_meta(app_id: str) -> dict[str, Any]:
    """Return the revision and sizes without copying the bundle or data document
    into agent context. Sizes make the choice between a narrow and full read
    explicit without requiring a probing read first."""
    _require_web_app(app_id)
    with db.transaction() as cur:
        cur.execute(
            "SELECT revision, updated_at, agent_updates_locked,"
            " octet_length(html), octet_length(css), octet_length(javascript),"
            " octet_length(data_json) FROM web_apps WHERE app_id = %s",
            (app_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise WorkspaceError(HTTPStatus.INTERNAL_SERVER_ERROR, "app state is unavailable")
    return {
        "revision": row[0],
        "updated_at": row[1],
        "agent_updates_locked": bool(row[2]),
        "bytes": {
            "html": row[3],
            "css": row[4],
            "javascript": row[5],
            "data": row[6],
        },
    }


def load_app_ui(app_id: str) -> dict[str, Any]:
    _require_web_app(app_id)
    with db.transaction() as cur:
        cur.execute(
            "SELECT revision, html, css, javascript, updated_at,"
            " agent_updates_locked"
            " FROM web_apps WHERE app_id = %s",
            (app_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise WorkspaceError(HTTPStatus.INTERNAL_SERVER_ERROR, "app state is unavailable")
    return {
        "revision": row[0],
        "html": row[1],
        "css": row[2],
        "javascript": row[3],
        "updated_at": row[4],
        "agent_updates_locked": bool(row[5]),
    }


def load_app_data(app_id: str) -> dict[str, Any]:
    _require_web_app(app_id)
    with db.transaction() as cur:
        cur.execute(
            "SELECT revision, data_json, updated_at"
            " FROM web_apps WHERE app_id = %s",
            (app_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise WorkspaceError(HTTPStatus.INTERNAL_SERVER_ERROR, "app state is unavailable")
    return {
        "revision": row[0],
        "data": _decoded_data(row[1]),
        "updated_at": row[2],
    }


def load_app_data_shape(app_id: str) -> dict[str, Any]:
    """Return the data document's structure without returning the document.

    An agent choosing a targeted read must first know which branches exist and
    which are worth the tokens, and reading the whole document to answer that
    is the cost the narrow read routes exist to avoid. The shape is derived
    from the same stored data on every call, so it cannot describe a revision
    that no longer exists; there is deliberately no writable copy to drift.

    Paths in the response are the paths ``read_app_data_path`` accepts, so the
    map's whole purpose is to be spent on a following narrow read.
    """
    state = load_app_data(app_id)
    return {
        "revision": state["revision"],
        "updated_at": state["updated_at"],
        "shape": data_shape(state["data"]),
    }


def read_app_data_path(app_id: str, body: Any) -> dict[str, Any]:
    request = _required_object(body, "data read")
    _require_keys(request, {"path", "paths", "missing"}, required=set())
    has_path = "path" in request
    has_paths = "paths" in request
    if has_path == has_paths:
        raise WorkspaceError(
            HTTPStatus.BAD_REQUEST,
            "data read requires exactly one of path or paths",
        )
    missing = request.get("missing", "error")
    if missing not in {"error", "null"}:
        raise WorkspaceError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "missing must be error or null",
        )

    if has_path:
        paths = [_validated_path(request.get("path"))]
    else:
        raw_paths = request.get("paths")
        if not isinstance(raw_paths, list) or not 1 <= len(raw_paths) <= MAX_DATA_READ_PATHS:
            raise WorkspaceError(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                f"paths must contain 1 to {MAX_DATA_READ_PATHS} paths",
            )
        paths = [_validated_path(path) for path in raw_paths]
        path_keys = [tuple(path) for path in paths]
        if len(set(path_keys)) != len(path_keys):
            raise WorkspaceError(HTTPStatus.UNPROCESSABLE_ENTITY, "paths must be unique")

    state = load_app_data(app_id)
    values = []
    for path in paths:
        value: Any = state["data"]
        try:
            for segment in path:
                value = _child(value, segment)
        except WorkspaceError:
            if missing != "null":
                raise
            value = None
        values.append({"path": path, "value": value})

    if has_path:
        result = {
            "revision": state["revision"],
            **values[0],
            "updated_at": state["updated_at"],
        }
    else:
        result = {
            "revision": state["revision"],
            "values": values,
            "updated_at": state["updated_at"],
        }
    _require_state_response_fits(result)
    return result


def apply_agent_action(body: Any, app_id: str) -> dict[str, Any]:
    action = _required_object(body, "agent action")
    name = _required_text(action.get("action"), "action")
    if name in {"set", "delete", "append", "batch"}:
        result = (
            _apply_data_batch(action, app_id, actor="agent")
            if name == "batch"
            else _apply_data_action(action, app_id, actor="agent")
        )
        # The agent already knows what it wrote; echoing the whole document
        # back into its context on every write is pure token cost.
        return {
            "ok": True,
            "revision": result["revision"],
        }
    if name != "publish_ui":
        raise WorkspaceError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "unsupported agent action; use publish_ui, batch, or set/delete/append",
        )
    _require_keys(
        action,
        {
            "action", "expected_revision", "html", "css", "javascript",
            "data_operations",
        },
        required={"action", "expected_revision", "html", "css", "javascript"},
    )
    revision = _required_counter(action.get("expected_revision"), "expected_revision")
    html = _bounded_string(action.get("html"), "html", MAX_HTML_BYTES)
    css = _bounded_string(action.get("css"), "css", MAX_CSS_BYTES)
    javascript = _bounded_string(action.get("javascript"), "javascript", MAX_JAVASCRIPT_BYTES)
    if JAVASCRIPT_FORBIDDEN.search(javascript):
        raise WorkspaceError(HTTPStatus.UNPROCESSABLE_ENTITY, "javascript cannot use dynamic imports")
    operations = _validated_data_operations(
        action.get("data_operations", []), allow_empty=True
    )
    state = _publish_ui(
        app_id, revision, html, css, javascript, operations, actor="agent"
    )
    return {
        "ok": True,
        "revision": state["revision"],
    }


def _publish_ui(
    app_id: str,
    expected_revision: int,
    html: str,
    css: str,
    javascript: str,
    operations: list[tuple[str, list[str | int], Any]],
    *,
    actor: str,
) -> dict[str, Any]:
    now = _utc_now()
    with db.transaction() as cur:
        cur.execute(
            f"SELECT {STATE_COLUMNS} FROM web_apps WHERE app_id = %s FOR UPDATE",
            (app_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise WorkspaceError(HTTPStatus.INTERNAL_SERVER_ERROR, "app state is unavailable")
        if row[0] != expected_revision:
            raise WorkspaceError(HTTPStatus.CONFLICT, "the app changed; read state and retry")
        current = _state_row(row)
        # The decoded row is transaction-local; mutate it directly instead of
        # cloning a document that may be 10 MiB. A validation failure still
        # rolls back before any SQL update.
        data = current["data"]
        for name, path, value in operations:
            data = _mutate_data(data, name, path, value)
        data_json = _validated_data(data)
        candidate = {
            **current,
            "revision": expected_revision + 1,
            "html": html,
            "css": css,
            "javascript": javascript,
            "data": data,
            "updated_at": now,
        }
        _require_state_response_fits(candidate)
        cur.execute(
            "UPDATE web_apps SET html = %s, css = %s, javascript = %s,"
            " data_json = %s, revision = revision + 1, updated_at = %s"
            " WHERE app_id = %s"
            f" RETURNING {STATE_COLUMNS}",
            (html, css, javascript, data_json, now, app_id),
        )
        changed = cur.fetchone()
        assert changed is not None
        _record_state_row_revision(cur, app_id, changed, actor, "ui", None)
    return {"revision": changed[0], "updated_at": changed[5]}


def apply_runtime_action(body: Any, app_id: str) -> dict[str, Any]:
    state = _apply_data_action(body, app_id, actor="app")
    # The trusted frame and capability worker apply the acknowledged operation
    # to their local full-document compatibility copy. Targeted-data apps do
    # not hold that copy at all. Either way, echoing up to 10 MiB after every
    # mutation is unnecessary.
    return {
        "app": {
            "revision": state["revision"],
            "updated_at": state["updated_at"],
        }
    }


def _apply_data_action(body: Any, app_id: str, *, actor: str) -> dict[str, Any]:
    _require_web_app(app_id)
    action = _required_object(body, "data action")
    version = _required_counter(action.get("expected_revision"), "expected_revision")
    operation = {key: value for key, value in action.items() if key != "expected_revision"}
    name, path, value = _validated_data_operation(operation)
    return _apply_data_operations(
        app_id, version, [(name, path, value)], actor=actor
    )


def _apply_data_batch(body: Any, app_id: str, *, actor: str) -> dict[str, Any]:
    _require_web_app(app_id)
    action = _required_object(body, "batch data action")
    _require_keys(
        action,
        {"action", "expected_revision", "operations"},
        required={"action", "expected_revision", "operations"},
    )
    if action.get("action") != "batch":
        raise WorkspaceError(HTTPStatus.UNPROCESSABLE_ENTITY, "unsupported batch action")
    version = _required_counter(action.get("expected_revision"), "expected_revision")
    operations = _validated_data_operations(action.get("operations"))
    return _apply_data_operations(
        app_id,
        version,
        operations,
        actor=actor,
    )


def _validated_data_operations(
    value: Any, *, allow_empty: bool = False
) -> list[tuple[str, list[str | int], Any]]:
    minimum = 0 if allow_empty else 1
    if (
        not isinstance(value, list)
        or len(value) < minimum
        or len(value) > MAX_BATCH_OPERATIONS
    ):
        raise WorkspaceError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            f"operations must contain {minimum} to {MAX_BATCH_OPERATIONS} data actions",
        )
    operations = []
    for raw_operation in value:
        operation = _required_object(raw_operation, "batch operation")
        name, path, operation_value = _validated_data_operation(operation)
        operations.append((name, path, operation_value))
    return operations


def _validated_data_operation(
    operation: dict[str, Any],
) -> tuple[str, list[str | int], Any]:
    name = _required_text(operation.get("action"), "action")
    allowed = {"action", "path"}
    required = {"action", "path"}
    if name in {"set", "append"}:
        allowed.add("value")
        required.add("value")
    _require_keys(operation, allowed, required=required)
    if name not in {"set", "delete", "append"}:
        raise WorkspaceError(HTTPStatus.UNPROCESSABLE_ENTITY, "unsupported data action")
    path = _validated_path(operation.get("path"))
    value = operation.get("value")
    return name, path, value


def _apply_data_operations(
    app_id: str,
    version: int,
    operations: list[tuple[str, list[str | int], Any]],
    *,
    actor: str,
) -> dict[str, Any]:
    now = _utc_now()
    with db.transaction() as cur:
        cur.execute(
            f"SELECT {STATE_COLUMNS} FROM web_apps"
            " WHERE app_id = %s FOR UPDATE",
            (app_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise WorkspaceError(HTTPStatus.INTERNAL_SERVER_ERROR, "app state is unavailable")
        if row[0] != version:
            raise WorkspaceError(HTTPStatus.CONFLICT, "the app changed; read state and retry")
        current = _state_row(row)
        # This is a private decode of the locked database row, so no defensive
        # 10 MiB clone is needed before applying the transactional operations.
        updated = current["data"]
        for name, path, value in operations:
            updated = _mutate_data(updated, name, path, value)
        data_json = _validated_data(updated)
        cur.execute(
            "UPDATE web_apps SET data_json = %s, revision = revision + 1,"
            " updated_at = %s"
            " WHERE app_id = %s"
            f" RETURNING {STATE_COLUMNS}",
            (data_json, now, app_id),
        )
        changed = cur.fetchone()
        assert changed is not None
        _record_state_row_revision(cur, app_id, changed, actor, "data", None)
    return {"revision": changed[0], "updated_at": changed[5]}


# --- Row-oriented collections -----------------------------------------------


def list_collections(app_id: str) -> dict[str, Any]:
    return collection_store.list_collections(
        app_id, require_web_app=_require_web_app
    )


def query_collection(app_id: str, collection: str, body: Any) -> dict[str, Any]:
    return collection_store.query_collection(
        app_id,
        collection,
        body,
        require_web_app=_require_web_app,
    )


def apply_collection_actions(
    app_id: str, collection: str, body: Any
) -> dict[str, Any]:
    return collection_store.apply_collection_actions(
        app_id,
        collection,
        body,
        require_web_app=_require_web_app,
        record_revision=_record_state_row_revision,
        utc_now=_utc_now,
    )


def _insert_revision(
    cur: Any,
    app_id: str,
    *,
    revision: int,
    actor: str,
    kind: str,
    restored_from: int | None,
    html: str,
    css: str,
    javascript: str,
    data_json: str,
    now: str,
) -> None:
    collections_json = _collection_snapshot_json(cur, app_id)
    cur.execute(
        "INSERT INTO web_app_revisions"
        " (app_id, revision, actor, kind, restored_from, html, css, javascript,"
        " data_json, collections_json, created_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            app_id, revision, actor, kind, restored_from, html, css,
            javascript, data_json, collections_json, now,
        ),
    )


_collection_snapshot_json = collection_store._collection_snapshot_json
_restore_collection_snapshot = collection_store._restore_collection_snapshot


def _record_state_revision(
    cur: Any,
    app_id: str,
    state: dict[str, Any],
    actor: str,
    kind: str,
    restored_from: int | None,
) -> None:
    _insert_revision(
        cur,
        app_id,
        revision=state["revision"],
        actor=actor,
        kind=kind,
        restored_from=restored_from,
        html=state["html"],
        css=state["css"],
        javascript=state["javascript"],
        data_json=_validated_data(state["data"]),
        now=state["updated_at"],
    )
    _prune_revisions(cur, app_id, datetime.now(timezone.utc))


def _record_state_row_revision(
    cur: Any,
    app_id: str,
    row: tuple[Any, ...],
    actor: str,
    kind: str,
    restored_from: int | None,
) -> None:
    """Record an already-validated database row without decoding its data."""
    _insert_revision(
        cur,
        app_id,
        revision=row[0],
        actor=actor,
        kind=kind,
        restored_from=restored_from,
        html=row[1],
        css=row[2],
        javascript=row[3],
        data_json=row[4],
        now=row[5],
    )
    _prune_revisions(cur, app_id, datetime.now(timezone.utc))


def _prune_revisions(cur: Any, app_id: str, now: datetime) -> None:
    """Keep five exact revisions and sparse recovery points for seven days."""
    cur.execute(
        "SELECT revision, created_at FROM web_app_revisions"
        " WHERE app_id = %s ORDER BY revision DESC",
        (app_id,),
    )
    rows = cur.fetchall()
    preferred = {int(row[0]) for row in rows[:REVISION_EXACT_RETAINED]}
    buckets: set[tuple[str, int]] = set()
    for revision, created_at in rows[REVISION_EXACT_RETAINED:]:
        created = datetime.strptime(str(created_at), TIME_FORMAT).replace(
            tzinfo=timezone.utc
        )
        age = max(timedelta(0), now - created)
        if age < timedelta(days=1):
            bucket = ("four-hour", int(age.total_seconds()) // (4 * 3600))
        elif age < timedelta(days=REVISION_RETAIN_DAYS):
            bucket = ("day", age.days)
        else:
            continue
        if bucket in buckets:
            continue
        buckets.add(bucket)
        preferred.add(int(revision))

    keep: set[int] = set()
    for revision, _created_at in rows:
        revision = int(revision)
        if revision not in preferred or len(keep) >= REVISION_MAX_RETAINED:
            continue
        keep.add(revision)
    stale = [
        int(revision)
        for revision, _created_at in rows
        if int(revision) not in keep
    ]
    if stale:
        placeholders = ",".join("%s" for _ in stale)
        cur.execute(
            f"DELETE FROM web_app_revisions WHERE app_id = %s"
            f" AND revision IN ({placeholders})",
            (app_id, *stale),
        )


def prune_revisions(now: datetime | None = None) -> None:
    """Apply age buckets to idle Apps as well as Apps receiving writes."""
    retained_at = now or datetime.now(timezone.utc)
    with db.transaction() as cur:
        cur.execute("SELECT app_id FROM web_apps")
        for (app_id,) in cur.fetchall():
            _prune_revisions(cur, str(app_id), retained_at)


def list_revisions(app_id: str, query: dict[str, list[str]]) -> dict[str, Any]:
    _require_web_app(app_id)
    unexpected = sorted(set(query) - {"before"})
    if unexpected:
        raise WorkspaceError(
            HTTPStatus.BAD_REQUEST,
            f"unexpected revision query fields: {', '.join(unexpected)}",
        )
    before_values = query.get("before") or []
    if len(before_values) > 1 or (
        before_values and not before_values[0].isdigit()
    ):
        raise WorkspaceError(
            HTTPStatus.BAD_REQUEST, "before must be a non-negative integer"
        )
    clause = " AND revision < %s" if before_values else ""
    params: list[Any] = [app_id]
    if before_values:
        params.append(int(before_values[0]))
    with db.transaction() as cur:
        cur.execute(
            "SELECT revision, actor, kind, restored_from, created_at"
            f" FROM web_app_revisions WHERE app_id = %s{clause}"
            " ORDER BY revision DESC LIMIT %s",
            (*params, REVISION_PAGE_LIMIT + 1),
        )
        rows = cur.fetchall()
    more = len(rows) > REVISION_PAGE_LIMIT
    rows = rows[:REVISION_PAGE_LIMIT]
    return {
        "revisions": [_revision_summary(row) for row in rows],
        "next_before": rows[-1][0] if more and rows else None,
    }


def _revision_summary(row: tuple[Any, ...]) -> dict[str, Any]:
    revision, actor, kind, restored_from, created_at = row
    return {
        "revision": revision,
        "actor": actor,
        "kind": kind,
        "restored_from": restored_from,
        "created_at": created_at,
    }


def restore_revision(app_id: str, revision: int) -> dict[str, Any]:
    """Restore one complete App state as a new forward revision."""
    _require_web_app(app_id)
    now = _utc_now()
    with db.transaction() as cur:
        cur.execute(
            f"SELECT {STATE_COLUMNS} FROM web_apps WHERE app_id = %s FOR UPDATE",
            (app_id,),
        )
        current_row = cur.fetchone()
        if current_row is None:
            raise WorkspaceError(HTTPStatus.NOT_FOUND, "app not found")
        current = _state_row(current_row)
        cur.execute(
            "SELECT html, css, javascript, data_json, collections_json"
            " FROM web_app_revisions"
            " WHERE app_id = %s AND revision = %s",
            (app_id, revision),
        )
        source = cur.fetchone()
        if source is None:
            raise WorkspaceError(HTTPStatus.NOT_FOUND, "app revision not found")
        data_json = _validated_data(_decoded_data(source[3]))
        next_revision = current["revision"] + 1
        candidate = {
            "revision": next_revision,
            "html": source[0],
            "css": source[1],
            "javascript": source[2],
            "data": _decoded_data(data_json),
            "updated_at": now,
        }
        _require_state_response_fits(candidate)
        cur.execute(
            "UPDATE web_apps SET revision = %s, html = %s, css = %s,"
            " javascript = %s, data_json = %s, updated_at = %s"
            " WHERE app_id = %s"
            f" RETURNING {STATE_COLUMNS}",
            (
                next_revision, source[0], source[1], source[2], data_json,
                now, app_id,
            ),
        )
        changed_row = cur.fetchone()
        assert changed_row is not None
        changed = _state_row(changed_row)
        _restore_collection_snapshot(cur, app_id, source[4], now)
        _record_state_revision(
            cur, app_id, changed, "user", "restore", revision
        )
    return {"ok": True, "app": changed}


# --- Shared validation and state helpers -------------------------------------


def _validated_data(value: Any) -> str:
    if not isinstance(value, dict):
        raise WorkspaceError(HTTPStatus.UNPROCESSABLE_ENTITY, "data must be a JSON object")
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise WorkspaceError(HTTPStatus.UNPROCESSABLE_ENTITY, "data must contain only JSON values") from exc
    if len(encoded.encode()) > MAX_DATA_BYTES:
        raise WorkspaceError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, f"data exceeds {MAX_DATA_BYTES} bytes")
    return encoded


def _decoded_data(encoded: str) -> dict[str, Any]:
    try:
        data = json.loads(encoded)
    except json.JSONDecodeError as exc:
        raise WorkspaceError(HTTPStatus.INTERNAL_SERVER_ERROR, "stored app data is invalid") from exc
    if not isinstance(data, dict):
        raise WorkspaceError(HTTPStatus.INTERNAL_SERVER_ERROR, "stored app data is invalid")
    return data


def _state_row(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "revision": row[0],
        "html": row[1],
        "css": row[2],
        "javascript": row[3],
        "data": _decoded_data(row[4]),
        "updated_at": row[5],
        "agent_updates_locked": bool(row[6]),
    }


def _require_state_response_fits(state: dict[str, Any]) -> None:
    encoded = json.dumps({"app": state}, sort_keys=True, separators=(",", ":")).encode()
    if len(encoded) > MAX_STATE_RESPONSE_BYTES:
        raise WorkspaceError(
            HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            f"serialized app state exceeds {MAX_STATE_RESPONSE_BYTES} bytes",
        )


def _validated_path(value: Any) -> list[str | int]:
    if not isinstance(value, list) or not value or len(value) > MAX_PATH_DEPTH:
        raise WorkspaceError(HTTPStatus.UNPROCESSABLE_ENTITY, f"path must contain 1 to {MAX_PATH_DEPTH} segments")
    path: list[str | int] = []
    for segment in value:
        if isinstance(segment, bool) or not isinstance(segment, (str, int)):
            raise WorkspaceError(HTTPStatus.UNPROCESSABLE_ENTITY, "path segments must be strings or non-negative integers")
        if isinstance(segment, int) and segment < 0:
            raise WorkspaceError(HTTPStatus.UNPROCESSABLE_ENTITY, "array indexes must be non-negative")
        if isinstance(segment, str) and (not segment or len(segment.encode()) > MAX_PATH_KEY_BYTES):
            raise WorkspaceError(HTTPStatus.UNPROCESSABLE_ENTITY, "object path keys must be bounded non-empty strings")
        path.append(segment)
    return path


def _mutate_data(root: Any, action: str, path: list[str | int], value: Any) -> Any:
    parent = root
    for segment in path[:-1]:
        parent = _child(parent, segment)
    leaf = path[-1]
    if action == "append":
        target = _child(parent, leaf)
        if not isinstance(target, list):
            raise WorkspaceError(HTTPStatus.UNPROCESSABLE_ENTITY, "append target must be an array")
        target.append(value)
        return root
    if isinstance(parent, dict) and isinstance(leaf, str):
        if action == "delete":
            if leaf not in parent:
                raise WorkspaceError(HTTPStatus.UNPROCESSABLE_ENTITY, "data path does not exist")
            del parent[leaf]
        else:
            parent[leaf] = value
        return root
    if isinstance(parent, list) and isinstance(leaf, int):
        if leaf >= len(parent):
            raise WorkspaceError(HTTPStatus.UNPROCESSABLE_ENTITY, "array index is out of range")
        if action == "delete":
            parent.pop(leaf)
        else:
            parent[leaf] = value
        return root
    raise WorkspaceError(HTTPStatus.UNPROCESSABLE_ENTITY, "data path does not match the stored shape")


def _child(parent: Any, segment: str | int) -> Any:
    if isinstance(parent, dict) and isinstance(segment, str) and segment in parent:
        return parent[segment]
    if isinstance(parent, list) and isinstance(segment, int) and segment < len(parent):
        return parent[segment]
    raise WorkspaceError(HTTPStatus.UNPROCESSABLE_ENTITY, "data path does not exist")


def _thread_session_config(thread: dict[str, Any]) -> dict[str, str]:
    """Read back the session configuration the host recorded for a thread.

    Read path (`host.session_options`): the recorded model may predate the
    current matrix, so a retained conversation stays readable even though the
    host refuses to run further turns on its thread. The matrix check belongs
    on the send path.
    """
    config = recorded_session_config(thread)
    if config is None:
        raise WorkspaceError(HTTPStatus.BAD_GATEWAY, "host admin returned invalid thread configuration")
    runtime, model, effort = config
    return {"agent_runtime": runtime, "model": model, "effort": effort}


def _required_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, f"{label} must be an object")
    return value


def _require_keys(value: dict[str, Any], allowed: set[str], *, required: set[str]) -> None:
    missing = sorted(required - set(value))
    extra = sorted(set(value) - allowed)
    if missing or extra:
        details = []
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if extra:
            details.append(f"unsupported {', '.join(extra)}")
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, f"fields are invalid: {'; '.join(details)}")


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, f"{label} must be a non-empty string")
    return value.strip()


def _bounded_required_text(value: Any, label: str, limit: int) -> str:
    text = _required_text(value, label)
    if len(text.encode()) > limit:
        raise WorkspaceError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, f"{label} exceeds {limit} bytes")
    return text


def _required_counter(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, f"{label} must be a non-negative integer")
    return value


def _bounded_string(value: Any, label: str, limit: int) -> str:
    if not isinstance(value, str):
        raise WorkspaceError(HTTPStatus.UNPROCESSABLE_ENTITY, f"{label} must be a string")
    if len(value.encode()) > limit:
        raise WorkspaceError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, f"{label} exceeds {limit} bytes")
    if "\0" in value:
        raise WorkspaceError(HTTPStatus.UNPROCESSABLE_ENTITY, f"{label} must not contain NUL bytes")
    return value


def _path_segment(value: str) -> str:
    decoded = unquote(value)
    if (
        not decoded
        or "/" in decoded
        or "\\" in decoded
        or APP_ID_RE.fullmatch(decoded) is None
    ):
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, "invalid Web App id")
    return decoded


def _collection_path_segment(value: str) -> str:
    decoded = unquote(value)
    if "/" in decoded or "\\" in decoded:
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, "invalid collection")
    return _validated_collection_name(decoded)


def _utc_now() -> str:
    return time.strftime(TIME_FORMAT, time.gmtime())


def _format_ts(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime(TIME_FORMAT)
