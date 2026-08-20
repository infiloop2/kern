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
# A shape response answers "which branch should I read" and must stay far
# cheaper than the document it describes, so every dimension it walks is
# bounded and every cut it makes is marked where the caller would otherwise
# read absence as completeness.
MAX_SHAPE_DEPTH = 6
MAX_SHAPE_OBJECT_KEYS = 64
MAX_SHAPE_ARRAY_SAMPLE = 200
MAX_SHAPE_NODES = 1000
# A repeated short string is a category worth naming. A string that never
# repeats is an identifier, and copying identifiers would turn the map back
# into the data it exists to avoid returning.
MAX_SHAPE_ENUM_VALUES = 8
MIN_SHAPE_ENUM_OBSERVATIONS = 4
MIN_SHAPE_ENUM_VALUE_OBSERVATIONS = 2
MAX_SHAPE_ENUM_VALUE_BYTES = 40
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
    for attempt in range(SEND_BUSY_RETRIES):
        try:
            return call_admin_api("POST", path, host_request)
        except WorkspaceError as exc:
            transient = exc.status == HTTPStatus.CONFLICT and SEND_RETRY_MARKER in exc.message
            if not transient or attempt == SEND_BUSY_RETRIES - 1:
                raise
            time.sleep(SEND_BUSY_RETRY_DELAY_SECONDS)
    raise WorkspaceError(HTTPStatus.CONFLICT, "the thread stayed busy while sending; retry the message")


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
        "shape": _data_shape([state["data"]], 0, _ShapeBudget(MAX_SHAPE_NODES)),
    }


class _ShapeBudget:
    """Bounds one shape response to a fixed number of described nodes."""

    def __init__(self, limit: int) -> None:
        self.remaining = limit

    def take(self) -> bool:
        if self.remaining <= 0:
            return False
        self.remaining -= 1
        return True


def _data_shape(values: list[Any], depth: int, budget: _ShapeBudget) -> dict[str, Any]:
    """Describe one position from every value observed there.

    Array elements are merged into a single ``items`` node so an array of a
    thousand records costs one record's description. Merging is also what makes
    a category visible: one observation cannot show that a field repeats.
    """
    node = _shape_node(values, depth, budget)
    # A merged position has no single encoded size, so the size belongs to the
    # array that holds it rather than to a representative element.
    if len(values) == 1 and node["type"] in {"object", "array", "string"}:
        node["bytes"] = _encoded_size(values[0])
    return node


def _shape_node(values: list[Any], depth: int, budget: _ShapeBudget) -> dict[str, Any]:
    kinds = sorted({_shape_kind(value) for value in values})
    if len(kinds) > 1:
        return {"type": "mixed", "types": kinds}
    if kinds[0] == "object":
        return _object_shape(values, depth, budget)
    if kinds[0] == "array":
        return _array_shape(values, depth, budget)
    return _scalar_shape(kinds[0], values)


def _shape_kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    return "number"


def _object_shape(
    values: list[Any], depth: int, budget: _ShapeBudget
) -> dict[str, Any]:
    node: dict[str, Any] = {"type": "object"}
    if depth >= MAX_SHAPE_DEPTH:
        node["truncated"] = True
        return node
    observed: dict[str, list[Any]] = {}
    for value in values:
        for key, child in value.items():
            observed.setdefault(key, []).append(child)
    keys: dict[str, Any] = {}
    for key in sorted(observed):
        if len(keys) >= MAX_SHAPE_OBJECT_KEYS or not budget.take():
            node["truncated"] = True
            break
        child_shape = _data_shape(observed[key], depth + 1, budget)
        # Absent from some observed records, so a caller reading it must handle
        # the gap rather than trust the merged description.
        if len(observed[key]) < len(values):
            child_shape["optional"] = True
        # A write validates the path it targets but not the keys inside the
        # value it stores, so a document can hold a key the read route will not
        # traverse. Naming it beats hiding it: the branch exists, and only a
        # full data read can reach it.
        if not _addressable_key(key):
            child_shape["addressable"] = False
        keys[key] = child_shape
    node["keys"] = keys
    return node


def _array_shape(
    values: list[Any], depth: int, budget: _ShapeBudget
) -> dict[str, Any]:
    node: dict[str, Any] = {"type": "array"}
    # A merged position holds one array per observed record, so no single
    # length is true of all of them and a summed one would advertise an index
    # that the record a caller reads does not have. Sizes describe a single
    # observation, exactly as `bytes` does.
    if len(values) == 1:
        node["length"] = len(values[0])
    elements = [element for value in values for element in value]
    if not elements:
        return node
    if depth >= MAX_SHAPE_DEPTH:
        node["truncated"] = True
        return node
    if len(elements) > MAX_SHAPE_ARRAY_SAMPLE:
        # Categories below are drawn from a prefix, so an enum here may be
        # incomplete. Saying so keeps a partial map from reading as a total one.
        node["sampled"] = MAX_SHAPE_ARRAY_SAMPLE
        elements = elements[:MAX_SHAPE_ARRAY_SAMPLE]
    if not budget.take():
        node["truncated"] = True
        return node
    node["items"] = _data_shape(elements, depth + 1, budget)
    return node


def _scalar_shape(kind: str, values: list[Any]) -> dict[str, Any]:
    node: dict[str, Any] = {"type": kind}
    if kind != "string":
        return node
    distinct = sorted(set(values))
    # Categories repeat and identifiers do not, so the position must average at
    # least two observations per distinct value. Merely requiring one repeat
    # would let a field of names with a single coincidental duplicate publish
    # every name it holds.
    if (
        len(values) >= MIN_SHAPE_ENUM_OBSERVATIONS
        and len(distinct) * MIN_SHAPE_ENUM_VALUE_OBSERVATIONS <= len(values)
        and len(distinct) <= MAX_SHAPE_ENUM_VALUES
        and all(_enum_value_fits(value) for value in distinct)
    ):
        node["enum"] = distinct
    return node


def _utf8_length(text: str) -> int | None:
    """Return the UTF-8 size, or None when the string cannot be encoded.

    JSON may escape a lone surrogate, which parses into a ``str`` that no UTF-8
    measurement accepts, so a stored document can hold one. Describing that
    document must not fail on it.
    """
    try:
        return len(text.encode())
    except UnicodeEncodeError:
        return None


def _addressable_key(key: str) -> bool:
    """Whether ``read_app_data_path`` accepts this key as a path segment."""
    size = _utf8_length(key)
    # `_validated_path` measures segments the same way, so a key that cannot be
    # measured is a key the read route refuses.
    return bool(key) and size is not None and size <= MAX_PATH_KEY_BYTES


def _enum_value_fits(value: str) -> bool:
    size = _utf8_length(value)
    return size is not None and size <= MAX_SHAPE_ENUM_VALUE_BYTES


def _encoded_size(value: Any) -> int:
    return len(json.dumps(value, separators=(",", ":")).encode())


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
    """Return bounded collection summaries at one coherent App revision."""
    _require_web_app(app_id)
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


def query_collection(app_id: str, collection: str, body: Any) -> dict[str, Any]:
    """Filter, sort, and page one collection without loading the App document."""
    _require_web_app(app_id)
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


def apply_collection_actions(app_id: str, collection: str, body: Any) -> dict[str, Any]:
    """Apply one bounded row batch as a new whole-App revision."""
    _require_web_app(app_id)
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

    now = _utc_now()
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
        _record_state_row_revision(
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


# --- Unified revisions -------------------------------------------------------


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
