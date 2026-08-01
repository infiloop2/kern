"""Agentic Web App backend.

Each workspace owns one generated UI bundle, one agent-defined JSON document,
always-on instructions, a memory store, scheduled agent calls, a restorable
history, and one fixed agent thread. The browser and agent receive separate
route namespaces and authentication markers. Generated browser code never
reaches this process as authority: all durable mutations are validated here.

Two counters protect concurrent writers. ``ui_revision`` changes only when the
interface bundle is replaced and is what the browser keys rendering and worker
lifecycle on. ``data_version`` changes on every data write and is the
optimistic concurrency token for ``set``/``delete``/``append``. Splitting them
keeps a data-only write from tearing down the rendered app, and keeps data
mutation responses small.
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import http.client
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
import socket
import threading
import time
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

from host.constants import (
    APP_BACKEND_ADMIN_API_TIMEOUT_SECONDS,
    APP_BACKEND_ADMIN_SOCKET_PATH,
    LOOPBACK,
    MAX_REQUEST_BODY_BYTES as ADMIN_MAX_REQUEST_BODY_BYTES,
)
from host.runtime.core import db, host_errors
from host.session_options import public_session_options, recorded_session_config, session_config_error


APP_ID = "personal_web_app_builder"
THREAD_NAME_RE = re.compile(r"app-([1-9][0-9]*)")
HOST = os.environ.get("KERN_APP_HOST", LOOPBACK)
PORT = int(os.environ.get("KERN_APP_PORT", "7456"))
DB_SCHEMA = os.environ.get("KERN_APP_DB_SCHEMA", "app_personal_web_app_builder")
ADMIN_API_SOCKET = os.environ.get("KERN_APP_ADMIN_API_SOCKET", APP_BACKEND_ADMIN_SOCKET_PATH)
MAX_REQUEST_BODY_BYTES = 768 * 1024
MAX_ADMIN_RESPONSE_BYTES = ADMIN_MAX_REQUEST_BODY_BYTES
MAX_HTML_BYTES = 128 * 1024
MAX_CSS_BYTES = 64 * 1024
MAX_JAVASCRIPT_BYTES = 128 * 1024
MAX_DATA_BYTES = 256 * 1024
MAX_STATE_RESPONSE_BYTES = 900 * 1024
MAX_CHAT_MESSAGE_BYTES = 50_000
MAX_APP_NAME_CHARS = 100
MAX_INSTRUCTIONS_BYTES = 8 * 1024
MAX_MEMORY_NAME_CHARS = 64
MAX_MEMORY_DESCRIPTION_CHARS = 150
MAX_MEMORY_BODY_BYTES = 16 * 1024
MAX_MEMORY_COUNT = 100
MEMORY_INDEX_INJECTED = 50
MAX_SCHEDULE_MESSAGE_BYTES = 4000
MAX_SCHEDULES_PER_APP = 20
MIN_SCHEDULE_INTERVAL_MINUTES = 5
MAX_SCHEDULE_INTERVAL_MINUTES = 7 * 24 * 60
SCHEDULE_RETRY_MINUTES = 5
SCHEDULER_POLL_SECONDS = 30
SCHEDULER_DUE_BATCH = 10
HISTORY_SNAPSHOT_EVERY = 20
HISTORY_RETAINED_ENTRIES = 300
HISTORY_PAGE_LIMIT = 40
CHECKPOINT_RETAIN_DAYS = 7
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
    "thread.error",
    "thread.stopped",
)
MAX_PATH_DEPTH = 16
MAX_PATH_KEY_BYTES = 128
JAVASCRIPT_FORBIDDEN = re.compile(r"\bimport\b")
MEMORY_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
DAILY_TIME_RE = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
# Message creation and other writes on one workspace must not interleave.
# A fixed stripe set keeps that coordination bounded while unrelated
# workspaces normally proceed independently.
WORKSPACE_LOCKS = tuple(threading.Lock() for _ in range(64))
REQUEST_PREFIXES = {
    "user": "Requested by user:",
    "app": "Requested by app:",
    "schedule": "Requested by schedule:",
}
# The trusted context block sits directly after the provenance line. The
# browser strips it from displayed user bubbles; only that first block is
# host-composed, so text later in a message cannot impersonate it.
CONTEXT_OPEN = "[Workspace context]"
CONTEXT_CLOSE = "[/Workspace context]"
HISTORY_RESOURCE_LABELS = {
    "ui": "Interface",
    "data": "Data",
    "snapshot": "Data",
    "instructions": "Instructions",
    "memory": "Memory",
    "schedule": "Schedule",
    "checkpoint": "Recovery point",
}

STATE_COLUMNS = "ui_revision, data_version, html, css, javascript, data_json, updated_at"
SUMMARY_COLUMNS = "thread_id, name, ui_revision, created_at, updated_at"
SCHEDULE_COLUMNS = (
    "id, name, message, cadence, interval_minutes, daily_time, enabled,"
    " created_by, last_run_at, next_run_at, created_at, updated_at"
)


class AppError(Exception):
    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class Handler(BaseHTTPRequestHandler):
    server_version = "KernAgenticWebApp/0.1"

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_PUT(self) -> None:
        self._handle("PUT")

    def do_DELETE(self) -> None:
        self._handle("DELETE")

    def log_message(self, format: str, *args: object) -> None:
        return

    def _handle(self, method: str) -> None:
        try:
            parsed = urlparse(self.path)
            body = self._read_body()
            if parsed.path.startswith("/agent/"):
                self._require_agent_proxy()
                response = route_agent(
                    method,
                    parsed.path,
                    body,
                    self.headers.get("X-Kern-Agent-Thread") or "",
                    parse_qs(parsed.query),
                )
            else:
                self._require_host_proxy()
                response = route_browser(method, parsed.path, body, parse_qs(parsed.query))
            self._send_json(HTTPStatus.OK, response)
        except AppError as exc:
            self._send_json(exc.status, {"error": {"message": exc.message}})
        except Exception as exc:
            # App-controlled strings and internal transport details do not
            # belong in a browser or agent error response.
            host_errors.report_unexpected(
                "agentic_web_app.request",
                exc,
                context={"method": method, "route": parsed.path},
            )
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": {"message": "app request failed"}})

    def _require_host_proxy(self) -> None:
        if self.headers.get("X-Kern-App-Proxy") != APP_ID:
            raise AppError(HTTPStatus.UNAUTHORIZED, "missing host app proxy marker")

    def _require_agent_proxy(self) -> None:
        """Reject anything without the kernel-attributed markers.

        This is the cheap gate. ``route_agent`` resolves the attributed thread
        to its exact workspace before any state is returned or changed.
        """
        if (
            self.headers.get("X-Kern-Agent-App-Proxy") != APP_ID
            or not self.headers.get("X-Kern-Agent-Thread")
        ):
            raise AppError(HTTPStatus.UNAUTHORIZED, "missing agent app context")

    def _read_body(self) -> Any:
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError as exc:
            raise AppError(HTTPStatus.BAD_REQUEST, "malformed Content-Length") from exc
        if length < 0:
            raise AppError(HTTPStatus.BAD_REQUEST, "malformed Content-Length")
        if length > MAX_REQUEST_BODY_BYTES:
            raise AppError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request body too large")
        if length == 0:
            return None
        try:
            return json.loads(self.rfile.read(length))
        except json.JSONDecodeError as exc:
            raise AppError(HTTPStatus.BAD_REQUEST, "request body must be valid JSON") from exc

    def _send_json(self, status: HTTPStatus, body: Any) -> None:
        data = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.end_headers()
        self.wfile.write(data)


def route_browser(
    method: str,
    path: str,
    body: Any,
    query: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    if method == "GET" and path == "/session-options":
        return {"session_options": public_session_options()}
    if method == "GET" and path == "/apps":
        return list_web_apps(query or {})
    if method == "POST" and path == "/apps":
        return {"app": create_web_app()}

    match = re.fullmatch(r"/apps/([^/]+)/(state|conversation)", path)
    if method == "GET" and match:
        thread_id, resource = _path_segment(match.group(1)), match.group(2)
        if resource == "state":
            return {"app": load_app_state(thread_id)}
        return browser_conversation(thread_id)

    match = re.fullmatch(r"/apps/([^/]+)/conversation/events", path)
    if method == "GET" and match:
        return browser_conversation_events(
            _path_segment(match.group(1)), query or {}
        )

    match = re.fullmatch(r"/apps/([^/]+)/name", path)
    if method == "PUT" and match:
        return {"app": rename_web_app(_path_segment(match.group(1)), body)}

    match = re.fullmatch(
        r"/apps/([^/]+)/(messages|runtime/agent-requests|runtime/actions)", path
    )
    if method == "POST" and match:
        thread_id, resource = _path_segment(match.group(1)), match.group(2)
        with _workspace_lock(thread_id):
            if resource == "runtime/actions":
                return apply_runtime_action(body, thread_id)
            requested_by = "app" if resource == "runtime/agent-requests" else "user"
            return create_message(body, requested_by=requested_by, thread_id=thread_id)

    match = re.fullmatch(r"/apps/([^/]+)/instructions", path)
    if match:
        thread_id = _path_segment(match.group(1))
        if method == "GET":
            return load_instructions(thread_id)
        if method == "PUT":
            return save_instructions(thread_id, body, actor="user")

    match = re.fullmatch(r"/apps/([^/]+)/memories", path)
    if method == "GET" and match:
        return list_memories(_path_segment(match.group(1)), query or {})

    match = re.fullmatch(r"/apps/([^/]+)/memories/([^/]+)", path)
    if match:
        thread_id = _path_segment(match.group(1))
        name = _memory_name(match.group(2))
        if method == "GET":
            return {"memory": load_memory(thread_id, name)}
        if method == "PUT":
            return {"memory": save_memory(thread_id, name, body, actor="user")}
        if method == "DELETE":
            return delete_memory(thread_id, name)

    match = re.fullmatch(r"/apps/([^/]+)/schedules", path)
    if match:
        thread_id = _path_segment(match.group(1))
        if method == "GET":
            return list_schedules(thread_id)
        if method == "POST":
            with _workspace_lock(thread_id):
                return {"schedule": create_schedule(thread_id, body, actor="user")}

    match = re.fullmatch(r"/apps/([^/]+)/schedules/([1-9][0-9]{0,17})", path)
    if match:
        thread_id = _path_segment(match.group(1))
        schedule_id = int(match.group(2))
        if method == "PUT":
            with _workspace_lock(thread_id):
                return {"schedule": update_schedule(thread_id, schedule_id, body)}
        if method == "DELETE":
            with _workspace_lock(thread_id):
                return delete_schedule(thread_id, schedule_id)

    match = re.fullmatch(r"/apps/([^/]+)/checkpoints", path)
    if match:
        thread_id = _path_segment(match.group(1))
        if method == "GET":
            return list_checkpoints(thread_id)
        if method == "POST":
            with _workspace_lock(thread_id):
                return {"checkpoint": save_workspace_checkpoint(thread_id)}

    match = re.fullmatch(r"/apps/([^/]+)/checkpoints/([1-9][0-9]{0,17})/revert", path)
    if method == "POST" and match:
        thread_id = _path_segment(match.group(1))
        with _workspace_lock(thread_id):
            return revert_workspace_checkpoint(thread_id, int(match.group(2)))

    match = re.fullmatch(r"/apps/([^/]+)/stop", path)
    if method == "POST" and match:
        thread_id = _path_segment(match.group(1))
        _require_web_app(thread_id)
        return call_admin_api(
            "POST", f"/v1/threads/{quote(thread_id, safe='')}/stop", body
        )
    raise AppError(HTTPStatus.NOT_FOUND, "route not found")


def route_agent(
    method: str,
    path: str,
    body: Any,
    agent_thread: str,
    query: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Agent routes run on the kernel-attributed workspace thread only.

    Revert is deliberately absent here: reverting agent changes is a human
    control.
    """
    _require_web_app(agent_thread, agent=True)
    if method == "GET" and path == "/agent/state":
        return {"app": load_app_state(agent_thread)}
    if method == "POST" and path == "/agent/actions":
        return apply_agent_action(body, agent_thread)
    if path == "/agent/instructions":
        if method == "GET":
            return load_instructions(agent_thread, verify=False)
        if method == "PUT":
            return save_instructions(agent_thread, body, actor="agent", verify=False)
    if path == "/agent/memories":
        if method == "GET":
            return list_memories(agent_thread, query or {}, verify=False)
    match = re.fullmatch(r"/agent/memories/([^/]+)", path)
    if match:
        name = _memory_name(match.group(1))
        if method == "GET":
            return {"memory": load_memory(agent_thread, name, verify=False)}
        if method == "PUT":
            return {"memory": save_memory(agent_thread, name, body, actor="agent", verify=False)}
        if method == "DELETE":
            return delete_memory(agent_thread, name, verify=False)
    if path == "/agent/schedules":
        if method == "GET":
            return list_schedules(agent_thread, verify=False)
        if method == "POST":
            with _workspace_lock(agent_thread):
                return {
                    "schedule": create_schedule(
                        agent_thread, body, actor="agent", verify=False
                    )
                }
    match = re.fullmatch(r"/agent/schedules/([1-9][0-9]{0,17})", path)
    if match:
        schedule_id = int(match.group(1))
        if method == "PUT":
            with _workspace_lock(agent_thread):
                return {
                    "schedule": update_schedule(
                        agent_thread, schedule_id, body, verify=False
                    )
                }
        if method == "DELETE":
            with _workspace_lock(agent_thread):
                return delete_schedule(agent_thread, schedule_id, verify=False)
    raise AppError(HTTPStatus.NOT_FOUND, "agent route not found")


def list_web_apps(query: dict[str, list[str]]) -> dict[str, Any]:
    if query:
        raise AppError(
            HTTPStatus.BAD_REQUEST,
            f"unexpected app query fields: {', '.join(sorted(query))}",
        )
    with db.transaction() as cur:
        _set_search_path(cur)
        cur.execute(f"SELECT {SUMMARY_COLUMNS} FROM web_apps")
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
        path = f"/v1/threads?limit={THREAD_LIST_PAGE}"
        if before is not None:
            path += f"&before={quote(before, safe='')}"
        response = call_admin_api("GET", path)
        page = response.get("threads")
        if not isinstance(page, list) or not all(isinstance(item, dict) for item in page):
            raise AppError(
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
            raise AppError(
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
    if host_summary is not None:
        host_status = host_summary.get("status")
        if host_status not in {"idle", "running"}:
            raise AppError(
                HTTPStatus.BAD_GATEWAY, "host admin returned invalid thread summary"
            )
        status = host_status
        session = _thread_session_config(host_summary)
        last_used_at = str(host_summary.get("last_used_at") or row[4])
    return {
        "thread_id": row[0],
        "name": row[1],
        "ui_revision": row[2],
        "created_at": row[3],
        "updated_at": row[4],
        "last_used_at": last_used_at,
        "session": session,
        "status": status,
    }


def create_web_app() -> dict[str, Any]:
    # Match Agent Chat's allocator. The insert reserves an id across concurrent
    # creators, and every existing id remains counted so one is never reused.
    while True:
        now = _utc_now()
        with db.transaction() as cur:
            _set_search_path(cur)
            cur.execute("SELECT thread_id FROM web_apps")
            rows = cur.fetchall()
            numbers = [
                int(match.group(1))
                for (thread_id,) in rows
                if (match := THREAD_NAME_RE.fullmatch(thread_id)) is not None
            ]
            thread_id = f"app-{max(numbers, default=0) + 1}"
            cur.execute(
                "INSERT INTO web_apps"
                " (thread_id, name, ui_revision, data_version, html,"
                " css, javascript, data_json, created_at, updated_at)"
                " VALUES (%s, %s, 0, 0, '', '', '', '{}', %s, %s)"
                " ON CONFLICT (thread_id) DO NOTHING"
                f" RETURNING {SUMMARY_COLUMNS}",
                (thread_id, thread_id, now, now),
            )
            row = cur.fetchone()
            if row is not None:
                # Seed restore anchors so every retained history point can
                # reconstruct both the bundle and the data.
                _insert_history(
                    cur, thread_id, "ui", "user", 0, 0,
                    {"html": "", "css": "", "javascript": ""}, now,
                )
                _insert_history(
                    cur, thread_id, "snapshot", "user", 0, 0, {"data": {}}, now
                )
                _insert_history(
                    cur, thread_id, "checkpoint", "app", 0, 0,
                    {
                        "checkpoint_type": "automatic",
                        "checkpoint_date": now[:10],
                        "name": thread_id,
                        "html": "", "css": "", "javascript": "", "data": {},
                        "instructions_md": "", "memories": [], "schedules": [],
                    },
                    now,
                )
        if row is not None:
            return _web_app_summary(row, None)


def rename_web_app(thread_id: str, body: Any) -> dict[str, Any]:
    request = _required_object(body, "rename request")
    _require_keys(request, {"name"}, required={"name"})
    name = _required_text(request.get("name"), "name")
    if len(name) > MAX_APP_NAME_CHARS:
        raise AppError(
            HTTPStatus.BAD_REQUEST,
            f"name must be at most {MAX_APP_NAME_CHARS} characters",
        )
    with db.transaction() as cur:
        _set_search_path(cur)
        cur.execute(
            "UPDATE web_apps SET name = %s WHERE thread_id = %s"
            f" RETURNING {SUMMARY_COLUMNS}",
            (name, thread_id),
        )
        row = cur.fetchone()
    if row is None:
        raise AppError(HTTPStatus.NOT_FOUND, "app not found")
    return _web_app_summary(row, None)


def _require_web_app(thread_id: str, *, agent: bool = False) -> None:
    with db.transaction() as cur:
        _set_search_path(cur)
        cur.execute("SELECT 1 FROM web_apps WHERE thread_id = %s", (thread_id,))
        row = cur.fetchone()
    if row is None:
        if agent:
            raise AppError(HTTPStatus.UNAUTHORIZED, "missing agent app context")
        raise AppError(HTTPStatus.NOT_FOUND, "app not found")


def _workspace_lock(thread_id: str) -> threading.Lock:
    return WORKSPACE_LOCKS[hash(thread_id) % len(WORKSPACE_LOCKS)]


def browser_conversation(thread_id: str) -> dict[str, Any]:
    """The workspace's agent session and live status. The conversation
    contents themselves come from the thread event stream."""
    _require_web_app(thread_id)
    try:
        response = call_admin_api("GET", f"/v1/threads/{quote(thread_id, safe='')}")
    except AppError as exc:
        if exc.status == HTTPStatus.NOT_FOUND:
            # The host thread appears with the workspace's first message; an
            # unconfigured workspace is simply idle with no session yet.
            return {"session": None, "status": "idle"}
        raise
    thread = response.get("thread")
    if not isinstance(thread, dict):
        raise AppError(HTTPStatus.BAD_GATEWAY, "host admin returned invalid thread")
    status = thread.get("status")
    if status not in {"idle", "running"}:
        raise AppError(HTTPStatus.BAD_GATEWAY, "host admin returned invalid thread")
    return {"session": _thread_session_config(thread), "status": status}


def browser_conversation_events(
    thread_id: str, query: dict[str, list[str]]
) -> dict[str, Any]:
    _require_web_app(thread_id)
    unexpected = sorted(set(query) - {"since", "before"})
    if unexpected:
        raise AppError(
            HTTPStatus.BAD_REQUEST,
            f"unexpected conversation event query fields: {', '.join(unexpected)}",
        )
    since_values = query.get("since") or []
    before_values = query.get("before") or []
    if since_values and before_values:
        raise AppError(HTTPStatus.BAD_REQUEST, "since and before cannot be combined")
    for name, values in (("since", since_values), ("before", before_values)):
        if len(values) > 1:
            raise AppError(HTTPStatus.BAD_REQUEST, f"{name} must be provided once")
    parameters = [
        f"limit={CONVERSATION_EVENT_PAGE_LIMIT}",
        f"message_bytes={CONVERSATION_MESSAGE_BYTES}",
        *(
            f"event_type={quote(event_type, safe='')}"
            for event_type in CONVERSATION_EVENT_TYPES
        ),
    ]
    cursor_name = "since" if since_values else "before" if before_values else None
    if cursor_name is not None:
        cursor = (since_values if cursor_name == "since" else before_values)[0]
        if not cursor.isdigit():
            raise AppError(
                HTTPStatus.BAD_REQUEST,
                f"{cursor_name} must be a non-negative integer",
            )
        parameters.insert(0, f"{cursor_name}={cursor}")
    path = f"/v1/threads/{quote(thread_id, safe='')}/events?{'&'.join(parameters)}"
    response = call_admin_api("GET", path)
    events = response.get("events")
    if not isinstance(events, list) or not all(isinstance(event, dict) for event in events):
        raise AppError(HTTPStatus.BAD_GATEWAY, "host admin returned invalid event list")
    return {"events": events}


def create_message(
    body: Any, *, requested_by: str, thread_id: str
) -> dict[str, Any]:
    _require_web_app(thread_id)
    request = _required_object(body, "message request")
    allowed = {"content", "agent_runtime", "model", "effort"}
    _require_keys(request, allowed, required={"content"})
    prefix = REQUEST_PREFIXES[requested_by]
    context = _workspace_context(thread_id)
    content = _bounded_required_text(
        request.get("content"),
        "content",
        MAX_CHAT_MESSAGE_BYTES - len(f"{prefix}\n{context}".encode()),
    )
    host_request: dict[str, Any] = {"message": f"{prefix}\n{context}{content}"}
    config_fields = ("agent_runtime", "model", "effort")
    supplied = [field for field in config_fields if field in request]
    if supplied:
        if len(supplied) != len(config_fields):
            raise AppError(HTTPStatus.BAD_REQUEST, "agent_runtime, model, and effort must be provided together")
        runtime = _required_text(request.get("agent_runtime"), "agent_runtime")
        model = request.get("model")
        effort = request.get("effort")
        error = session_config_error(runtime, model, effort)
        if error is not None:
            raise AppError(HTTPStatus.BAD_REQUEST, error)
        assert isinstance(model, str) and isinstance(effort, str)
        host_request.update({"agent_runtime": runtime, "model": model, "effort": effort})
    response = _send_with_busy_retry(thread_id, host_request)
    status = response.get("status")
    if status != "accepted":
        raise AppError(HTTPStatus.BAD_GATEWAY, "host admin returned invalid send status")
    return {"status": status, "thread_id": thread_id}


def _workspace_context(thread_id: str) -> str:
    """The always-on block injected after the provenance line of every
    outgoing message: bounded instructions plus the memory index. Memory
    bodies stay out; the agent fetches them on demand."""
    with db.transaction() as cur:
        _set_search_path(cur)
        cur.execute(
            "SELECT instructions_md FROM web_apps WHERE thread_id = %s",
            (thread_id,),
        )
        row = cur.fetchone()
        instructions = row[0] if row else ""
        cur.execute(
            "SELECT name, description FROM web_app_memories"
            " WHERE thread_id = %s ORDER BY updated_at DESC, name",
            (thread_id,),
        )
        memories = cur.fetchall()
    lines = [CONTEXT_OPEN]
    if not instructions and not memories:
        # Always occupy the trusted slot immediately after provenance. User
        # content that starts with the same marker then remains visibly and
        # semantically outside the backend-owned block.
        lines.append("(No saved instructions or memories.)")
    elif instructions:
        lines.append("Always-on instructions:")
        lines.append(instructions)
    if memories:
        lines.append("Memory index (read a body: app_api GET /agent/memories/{name}):")
        lines.extend(
            f"- {name}: {description}"
            for name, description in memories[:MEMORY_INDEX_INJECTED]
        )
        remaining = len(memories) - MEMORY_INDEX_INJECTED
        if remaining > 0:
            lines.append(f"(and {remaining} more; list all: GET /agent/memories)")
    lines.append(CONTEXT_CLOSE)
    return "\n".join(lines) + "\n"


def _send_with_busy_retry(thread_id: str, host_request: dict[str, Any]) -> dict[str, Any]:
    path = f"/v1/threads/{quote(thread_id, safe='')}/messages"
    for attempt in range(SEND_BUSY_RETRIES):
        try:
            return call_admin_api("POST", path, host_request)
        except AppError as exc:
            transient = exc.status == HTTPStatus.CONFLICT and SEND_RETRY_MARKER in exc.message
            if not transient or attempt == SEND_BUSY_RETRIES - 1:
                raise
            time.sleep(SEND_BUSY_RETRY_DELAY_SECONDS)
    raise AppError(HTTPStatus.CONFLICT, "the thread stayed busy while sending; retry the message")


def load_app_state(thread_id: str) -> dict[str, Any]:
    _require_web_app(thread_id)
    with db.transaction() as cur:
        _set_search_path(cur)
        cur.execute(
            f"SELECT {STATE_COLUMNS} FROM web_apps WHERE thread_id = %s",
            (thread_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise AppError(HTTPStatus.INTERNAL_SERVER_ERROR, "app state is unavailable")
    return _state_row(row)


def apply_agent_action(body: Any, thread_id: str) -> dict[str, Any]:
    action = _required_object(body, "agent action")
    name = _required_text(action.get("action"), "action")
    if name in {"set", "delete", "append"}:
        result = _apply_data_action(action, thread_id, actor="agent")
        # The agent already knows what it wrote; echoing the whole document
        # back into its context on every write is pure token cost.
        return {
            "ok": True,
            "ui_revision": result["ui_revision"],
            "data_version": result["data_version"],
        }
    if name != "replace_ui":
        raise AppError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "unsupported agent action; use replace_ui or set/delete/append",
        )
    _require_keys(
        action,
        {"action", "expected_ui_revision", "html", "css", "javascript"},
        required={"action", "expected_ui_revision", "html", "css", "javascript"},
    )
    revision = _required_counter(action.get("expected_ui_revision"), "expected_ui_revision")
    html = _bounded_string(action.get("html"), "html", MAX_HTML_BYTES)
    css = _bounded_string(action.get("css"), "css", MAX_CSS_BYTES)
    javascript = _bounded_string(action.get("javascript"), "javascript", MAX_JAVASCRIPT_BYTES)
    if JAVASCRIPT_FORBIDDEN.search(javascript):
        raise AppError(HTTPStatus.UNPROCESSABLE_ENTITY, "javascript cannot use dynamic imports")
    state = _replace_ui_bundle(thread_id, revision, html, css, javascript, actor="agent")
    return {
        "ok": True,
        "ui_revision": state["ui_revision"],
        "data_version": state["data_version"],
    }


def _replace_ui_bundle(
    thread_id: str,
    expected_ui_revision: int,
    html: str,
    css: str,
    javascript: str,
    *,
    actor: str,
) -> dict[str, Any]:
    now = _utc_now()
    with db.transaction() as cur:
        _set_search_path(cur)
        cur.execute(
            f"SELECT {STATE_COLUMNS} FROM web_apps WHERE thread_id = %s FOR UPDATE",
            (thread_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise AppError(HTTPStatus.INTERNAL_SERVER_ERROR, "app state is unavailable")
        if row[0] != expected_ui_revision:
            raise AppError(HTTPStatus.CONFLICT, "the interface changed; read state and retry")
        candidate = {
            **_state_row(row),
            "ui_revision": expected_ui_revision + 1,
            "html": html,
            "css": css,
            "javascript": javascript,
            "updated_at": now,
        }
        _require_state_response_fits(candidate)
        cur.execute(
            "UPDATE web_apps SET html = %s, css = %s, javascript = %s,"
            " ui_revision = ui_revision + 1, updated_at = %s"
            " WHERE thread_id = %s"
            f" RETURNING {STATE_COLUMNS}",
            (html, css, javascript, now, thread_id),
        )
        changed = cur.fetchone()
        assert changed is not None
        _insert_history(
            cur, thread_id, "ui", actor, changed[0], changed[1],
            {"html": html, "css": css, "javascript": javascript}, now,
        )
        _prune_history(cur, thread_id)
    return _state_row(changed)


def apply_runtime_action(body: Any, thread_id: str) -> dict[str, Any]:
    state = _apply_data_action(body, thread_id, actor="app")
    # The generated UI needs the authoritative document to render from, but
    # never the bundle it is already running.
    return {
        "app": {
            "ui_revision": state["ui_revision"],
            "data_version": state["data_version"],
            "data": state["data"],
            "updated_at": state["updated_at"],
        }
    }


def _apply_data_action(body: Any, thread_id: str, *, actor: str) -> dict[str, Any]:
    _require_web_app(thread_id)
    action = _required_object(body, "data action")
    name = _required_text(action.get("action"), "action")
    allowed = {"action", "expected_data_version", "path"}
    required = {"action", "expected_data_version", "path"}
    if name in {"set", "append"}:
        allowed.add("value")
        required.add("value")
    _require_keys(action, allowed, required=required)
    if name not in {"set", "delete", "append"}:
        raise AppError(HTTPStatus.UNPROCESSABLE_ENTITY, "unsupported data action")
    version = _required_counter(action.get("expected_data_version"), "expected_data_version")
    path = _validated_path(action.get("path"))
    now = _utc_now()
    with db.transaction() as cur:
        _set_search_path(cur)
        cur.execute(
            f"SELECT {STATE_COLUMNS} FROM web_apps"
            " WHERE thread_id = %s FOR UPDATE",
            (thread_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise AppError(HTTPStatus.INTERNAL_SERVER_ERROR, "app state is unavailable")
        if row[1] != version:
            raise AppError(HTTPStatus.CONFLICT, "app data changed; reload and retry")
        current = _state_row(row)
        updated = _mutate_data(copy.deepcopy(current["data"]), name, path, action.get("value"))
        data_json = _validated_data(updated)
        candidate = {
            **current,
            "data_version": version + 1,
            "data": updated,
            "updated_at": now,
        }
        _require_state_response_fits(candidate)
        cur.execute(
            "UPDATE web_apps SET data_json = %s, data_version = data_version + 1,"
            " updated_at = %s"
            " WHERE thread_id = %s"
            f" RETURNING {STATE_COLUMNS}",
            (data_json, now, thread_id),
        )
        changed = cur.fetchone()
        assert changed is not None
        entry: dict[str, Any] = {"action": name, "path": path}
        if name != "delete":
            entry["value"] = action.get("value")
        _insert_history(cur, thread_id, "data", actor, changed[0], changed[1], entry, now)
        _maybe_snapshot(cur, thread_id, changed[0], changed[1], updated, now)
        _prune_history(cur, thread_id)
    return _state_row(changed)


# --- History -----------------------------------------------------------------


def _insert_history(
    cur: Any,
    thread_id: str,
    kind: str,
    actor: str,
    ui_revision: int,
    data_version: int,
    entry: dict[str, Any],
    now: str,
) -> None:
    cur.execute(
        "INSERT INTO web_app_history"
        " (thread_id, kind, actor, ui_revision, data_version, entry_json, created_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (
            thread_id, kind, actor, ui_revision, data_version,
            json.dumps(entry, sort_keys=True, separators=(",", ":")), now,
        ),
    )


def _maybe_snapshot(
    cur: Any,
    thread_id: str,
    ui_revision: int,
    data_version: int,
    data: Any,
    now: str,
) -> None:
    """Insert a full-data checkpoint every N data operations so restores
    replay a bounded op suffix and pruning always keeps a valid anchor."""
    cur.execute(
        "SELECT COUNT(*) FROM web_app_history"
        " WHERE thread_id = %s AND kind = 'data' AND id > COALESCE("
        " (SELECT MAX(id) FROM web_app_history"
        "  WHERE thread_id = %s AND kind = 'snapshot'), 0)",
        (thread_id, thread_id),
    )
    (pending,) = cur.fetchone()
    if pending >= HISTORY_SNAPSHOT_EVERY:
        _insert_history(
            cur, thread_id, "snapshot", "app", ui_revision, data_version,
            {"data": data}, now,
        )


def _prune_history(cur: Any, thread_id: str) -> None:
    """Trim to the retained window without breaking restorability.

    Everything older than the newest snapshot at or before the boundary is
    deleted, except the one UI entry that anchors the bundle for the oldest
    retained points — a workspace that rarely replaces its interface must not
    accumulate unbounded data history behind an old UI entry."""
    cur.execute(
        "SELECT id FROM web_app_history WHERE thread_id = %s AND kind <> 'checkpoint'"
        " ORDER BY id DESC OFFSET %s LIMIT 1",
        (thread_id, HISTORY_RETAINED_ENTRIES - 1),
    )
    boundary = cur.fetchone()
    if boundary is None:
        return
    cur.execute(
        "SELECT MAX(id) FROM web_app_history"
        " WHERE thread_id = %s AND kind = 'snapshot' AND id <= %s",
        (thread_id, boundary[0]),
    )
    snapshot_anchor = cur.fetchone()
    if snapshot_anchor is None or snapshot_anchor[0] is None:
        return
    cur.execute(
        "SELECT MAX(id) FROM web_app_history"
        " WHERE thread_id = %s AND kind = 'ui' AND id <= %s",
        (thread_id, snapshot_anchor[0]),
    )
    ui_anchor = cur.fetchone()
    cur.execute(
        "DELETE FROM web_app_history"
        " WHERE thread_id = %s AND kind <> 'checkpoint'"
        " AND id < %s AND id IS DISTINCT FROM %s",
        (thread_id, snapshot_anchor[0], None if ui_anchor is None else ui_anchor[0]),
    )


def list_history(thread_id: str, query: dict[str, list[str]]) -> dict[str, Any]:
    _require_web_app(thread_id)
    unexpected = sorted(set(query) - {"before"})
    if unexpected:
        raise AppError(
            HTTPStatus.BAD_REQUEST,
            f"unexpected history query fields: {', '.join(unexpected)}",
        )
    before_values = query.get("before") or []
    if len(before_values) > 1 or (before_values and not before_values[0].isdigit()):
        raise AppError(HTTPStatus.BAD_REQUEST, "before must be a non-negative integer")
    clause = " AND id < %s" if before_values else ""
    params: list[Any] = [thread_id]
    if before_values:
        params.append(int(before_values[0]))
    with db.transaction() as cur:
        _set_search_path(cur)
        cur.execute(
            "SELECT id, kind, actor, ui_revision, data_version, entry_json, created_at"
            f" FROM web_app_history WHERE thread_id = %s{clause}"
            " ORDER BY id DESC LIMIT %s",
            (*params, HISTORY_PAGE_LIMIT + 1),
        )
        rows = cur.fetchall()
    more = len(rows) > HISTORY_PAGE_LIMIT
    rows = rows[:HISTORY_PAGE_LIMIT]
    entries = [_history_summary(row) for row in rows]
    return {
        "entries": entries,
        "next_before": rows[-1][0] if more and rows else None,
    }


def list_checkpoints(thread_id: str) -> dict[str, Any]:
    """Return the deliberately small, human-facing recovery list.

    The detailed change log remains an implementation/audit mechanism. The
    browser sees only whole-workspace recovery points: one immutable automatic
    snapshot and at most one movable manual checkpoint for each retained day.
    """
    _require_web_app(thread_id)
    with db.transaction() as cur:
        _set_search_path(cur)
        cur.execute(
            "SELECT id, kind, actor, ui_revision, data_version, entry_json, created_at"
            " FROM web_app_history WHERE thread_id = %s AND kind = 'checkpoint'"
            " ORDER BY created_at DESC, id DESC",
            (thread_id,),
        )
        rows = cur.fetchall()
    return {"checkpoints": [_history_summary(row) for row in rows]}


def save_workspace_checkpoint(
    thread_id: str,
    *,
    automatic: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Create today's automatic recovery point or update today's manual one."""
    _require_web_app(thread_id)
    instant = now or datetime.now(timezone.utc)
    now_ts = _format_ts(instant)
    checkpoint_date = instant.date().isoformat()
    checkpoint_type = "automatic" if automatic else "manual"
    actor = "app" if automatic else "user"
    with db.transaction() as cur:
        _set_search_path(cur)
        payload, counters = _capture_workspace_checkpoint(
            cur, thread_id, checkpoint_type, checkpoint_date
        )
        cur.execute(
            "SELECT id, entry_json FROM web_app_history"
            " WHERE thread_id = %s AND kind = 'checkpoint' ORDER BY id DESC",
            (thread_id,),
        )
        existing_id = None
        checkpoint_rows = cur.fetchall()
        for candidate_id, entry_json in checkpoint_rows:
            try:
                candidate = json.loads(entry_json)
            except json.JSONDecodeError:
                continue
            if (
                candidate.get("checkpoint_type") == checkpoint_type
                and candidate.get("checkpoint_date") == checkpoint_date
            ):
                existing_id = candidate_id
                break
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        if existing_id is not None and automatic:
            cur.execute(
                "SELECT id, kind, actor, ui_revision, data_version, entry_json, created_at"
                " FROM web_app_history WHERE id = %s",
                (existing_id,),
            )
            existing = cur.fetchone()
            assert existing is not None
            return _history_summary(existing)
        if existing_id is None:
            cur.execute(
                "INSERT INTO web_app_history"
                " (thread_id, kind, actor, ui_revision, data_version, entry_json, created_at)"
                " VALUES (%s, 'checkpoint', %s, %s, %s, %s, %s) RETURNING id",
                (thread_id, actor, counters[0], counters[1], encoded, now_ts),
            )
            created = cur.fetchone()
            assert created is not None
            checkpoint_id = created[0]
        else:
            checkpoint_id = existing_id
            cur.execute(
                "UPDATE web_app_history SET actor = %s, ui_revision = %s,"
                " data_version = %s, entry_json = %s, created_at = %s WHERE id = %s",
                (actor, counters[0], counters[1], encoded, now_ts, checkpoint_id),
            )
        _prune_checkpoints(cur, thread_id, instant)
        cur.execute(
            "SELECT id, kind, actor, ui_revision, data_version, entry_json, created_at"
            " FROM web_app_history WHERE id = %s",
            (checkpoint_id,),
        )
        saved = cur.fetchone()
    assert saved is not None
    return _history_summary(saved)


def _capture_workspace_checkpoint(
    cur: Any, thread_id: str, checkpoint_type: str, checkpoint_date: str
) -> tuple[dict[str, Any], tuple[int, int]]:
    cur.execute(
        "SELECT name, ui_revision, data_version, html, css, javascript, data_json,"
        " instructions_md FROM web_apps WHERE thread_id = %s FOR UPDATE",
        (thread_id,),
    )
    app = cur.fetchone()
    if app is None:
        raise AppError(HTTPStatus.NOT_FOUND, "app not found")
    cur.execute(
        "SELECT name, description, body_md FROM web_app_memories"
        " WHERE thread_id = %s ORDER BY name",
        (thread_id,),
    )
    memories = [
        {"name": row[0], "description": row[1], "body_md": row[2]}
        for row in cur.fetchall()
    ]
    cur.execute(
        f"SELECT {SCHEDULE_COLUMNS} FROM web_app_schedules"
        " WHERE thread_id = %s ORDER BY id",
        (thread_id,),
    )
    schedules = [
        {"id": row[0], **_schedule_content(_schedule_row(row))}
        for row in cur.fetchall()
    ]
    return (
        {
            "checkpoint_type": checkpoint_type,
            "checkpoint_date": checkpoint_date,
            "name": app[0],
            "html": app[3],
            "css": app[4],
            "javascript": app[5],
            "data": json.loads(app[6]),
            "instructions_md": app[7],
            "memories": memories,
            "schedules": schedules,
        },
        (app[1], app[2]),
    )


def _prune_checkpoints(cur: Any, thread_id: str, now: datetime) -> None:
    cutoff = (now.date() - timedelta(days=CHECKPOINT_RETAIN_DAYS - 1)).isoformat()
    cur.execute(
        "SELECT id, entry_json FROM web_app_history"
        " WHERE thread_id = %s AND kind = 'checkpoint'",
        (thread_id,),
    )
    expired = []
    for checkpoint_id, entry_json in cur.fetchall():
        try:
            checkpoint_date = str(json.loads(entry_json).get("checkpoint_date", ""))
        except json.JSONDecodeError:
            checkpoint_date = ""
        if checkpoint_date < cutoff:
            expired.append(checkpoint_id)
    if expired:
        placeholders = ",".join("%s" for _ in expired)
        cur.execute(
            f"DELETE FROM web_app_history WHERE id IN ({placeholders})",
            tuple(expired),
        )


def _history_summary(row: tuple[Any, ...]) -> dict[str, Any]:
    entry_id, kind, actor, ui_revision, data_version, entry_json, created_at = row
    try:
        entry = json.loads(entry_json)
    except json.JSONDecodeError:
        entry = {}
    restored_from = entry.get("restored_from")
    if kind == "ui":
        summary = (
            "Restored an earlier interface"
            if restored_from is not None
            else "Replaced the interface"
        )
    elif kind == "snapshot":
        summary = (
            "Restored earlier data"
            if restored_from is not None
            else "Data checkpoint"
        )
    elif kind == "instructions":
        summary = (
            "Reverted always-on instructions"
            if restored_from is not None
            else "Edited always-on instructions"
        )
    elif kind == "memory":
        summary = f"{_change_verb(entry, restored_from)} memory {entry.get('name', '')}".strip()
    elif kind == "schedule":
        content = entry.get("new") or entry.get("old") or {}
        summary = f"{_change_verb(entry, restored_from)} schedule {content.get('name', '')}".strip()
    elif kind == "checkpoint":
        summary = (
            "My saved checkpoint"
            if entry.get("checkpoint_type") == "manual"
            else "Daily snapshot"
        )
    else:
        path = entry.get("path")
        dotted = ".".join(str(segment) for segment in path) if isinstance(path, list) else ""
        verb = {"set": "Set", "delete": "Deleted", "append": "Appended to"}.get(
            str(entry.get("action")), "Changed"
        )
        summary = f"{verb} {dotted}" if dotted else verb
    return {
        "id": entry_id,
        "kind": kind,
        "resource_label": HISTORY_RESOURCE_LABELS.get(kind, "Change"),
        "revert_mode": "workspace" if kind == "checkpoint" else None,
        "revert_prompt": _history_revert_prompt(kind) if kind == "checkpoint" else None,
        "checkpoint_type": entry.get("checkpoint_type"),
        "checkpoint_date": entry.get("checkpoint_date"),
        "actor": actor,
        "ui_revision": ui_revision,
        "data_version": data_version,
        "summary": summary,
        "restored_from": restored_from,
        "created_at": created_at,
    }


def _history_revert_prompt(kind: str) -> str:
    if kind == "checkpoint":
        return (
            "Restore the entire workspace to this checkpoint? Interface, data, "
            "instructions, memories, and schedule definitions will all change."
        )
    raise ValueError(f"{kind} is not a user-facing recovery point")


def _change_verb(entry: dict[str, Any], restored_from: Any) -> str:
    if restored_from is not None:
        return "Reverted"
    if entry.get("old") is None:
        return "Added"
    if entry.get("new") is None:
        return "Deleted"
    return "Edited"


def revert_workspace_checkpoint(thread_id: str, checkpoint_id: int) -> dict[str, Any]:
    """Atomically restore every user-visible workspace resource."""
    _require_web_app(thread_id)
    now = _utc_now()
    with db.transaction() as cur:
        _set_search_path(cur)
        cur.execute(
            "SELECT entry_json FROM web_app_history"
            " WHERE thread_id = %s AND id = %s AND kind = 'checkpoint'",
            (thread_id, checkpoint_id),
        )
        row = cur.fetchone()
        if row is None:
            raise AppError(HTTPStatus.NOT_FOUND, "checkpoint not found")
        try:
            checkpoint = json.loads(row[0])
        except json.JSONDecodeError as exc:
            raise AppError(HTTPStatus.CONFLICT, "checkpoint is unavailable") from exc
        restored = _validated_workspace_checkpoint(checkpoint)
        cur.execute(
            f"SELECT {STATE_COLUMNS} FROM web_apps"
            " WHERE thread_id = %s FOR UPDATE",
            (thread_id,),
        )
        current = cur.fetchone()
        if current is None:
            raise AppError(HTTPStatus.NOT_FOUND, "app not found")
        data_json = _validated_data(restored["data"])
        cur.execute(
            "UPDATE web_apps SET name = %s, html = %s, css = %s, javascript = %s,"
            " data_json = %s, instructions_md = %s, instructions_updated_by = 'user',"
            " instructions_updated_at = %s, ui_revision = ui_revision + 1,"
            " data_version = data_version + 1, updated_at = %s WHERE thread_id = %s"
            f" RETURNING {STATE_COLUMNS}",
            (
                restored["name"], restored["html"], restored["css"],
                restored["javascript"], data_json, restored["instructions_md"],
                now, now, thread_id,
            ),
        )
        changed = cur.fetchone()
        assert changed is not None

        cur.execute("DELETE FROM web_app_memories WHERE thread_id = %s", (thread_id,))
        for memory in restored["memories"]:
            cur.execute(
                "INSERT INTO web_app_memories"
                " (thread_id, name, description, body_md, updated_by, created_at, updated_at)"
                " VALUES (%s, %s, %s, %s, 'user', %s, %s)",
                (
                    thread_id, memory["name"], memory["description"],
                    memory["body_md"], now, now,
                ),
            )

        cur.execute("DELETE FROM web_app_schedules WHERE thread_id = %s", (thread_id,))
        restore_instant = _parse_ts(now)
        for schedule in restored["schedules"]:
            next_run = _format_ts(
                _next_cadence_run(
                    schedule["cadence"], schedule["interval_minutes"],
                    schedule["daily_time"], restore_instant,
                )
            )
            cur.execute(
                "INSERT INTO web_app_schedules"
                " (id, thread_id, name, message, cadence, interval_minutes, daily_time,"
                " enabled, created_by, last_run_at, next_run_at, created_at, updated_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'user', NULL, %s, %s, %s)",
                (
                    schedule["id"], thread_id, schedule["name"], schedule["message"],
                    schedule["cadence"], schedule["interval_minutes"],
                    schedule["daily_time"], schedule["enabled"], next_run, now, now,
                ),
            )

        _insert_history(
            cur, thread_id, "ui", "user", changed[0], changed[1],
            {
                "html": restored["html"], "css": restored["css"],
                "javascript": restored["javascript"], "restored_from": checkpoint_id,
            },
            now,
        )
        _insert_history(
            cur, thread_id, "snapshot", "user", changed[0], changed[1],
            {**restored, "restored_from": checkpoint_id}, now,
        )
        _prune_history(cur, thread_id)
    state = _state_row(changed)
    _require_state_response_fits(state)
    return {"ok": True, "app": state}


def _validated_workspace_checkpoint(value: Any) -> dict[str, Any]:
    checkpoint = _required_object(value, "checkpoint")
    name = _required_text(checkpoint.get("name"), "checkpoint name")
    if len(name) > MAX_APP_NAME_CHARS:
        raise AppError(HTTPStatus.CONFLICT, "checkpoint name is invalid")
    html = _bounded_string(checkpoint.get("html"), "checkpoint html", MAX_HTML_BYTES)
    css = _bounded_string(checkpoint.get("css"), "checkpoint css", MAX_CSS_BYTES)
    javascript = _bounded_string(
        checkpoint.get("javascript"), "checkpoint javascript", MAX_JAVASCRIPT_BYTES
    )
    instructions = _bounded_string(
        checkpoint.get("instructions_md"), "checkpoint instructions", MAX_INSTRUCTIONS_BYTES
    )
    _validated_data(checkpoint.get("data"))
    raw_memories = checkpoint.get("memories")
    if not isinstance(raw_memories, list) or len(raw_memories) > MAX_MEMORY_COUNT:
        raise AppError(HTTPStatus.CONFLICT, "checkpoint memories are invalid")
    memories = []
    seen_memories: set[str] = set()
    for raw in raw_memories:
        memory = _required_object(raw, "checkpoint memory")
        memory_name = _memory_name(str(memory.get("name", "")))
        description = _required_text(memory.get("description"), "description")
        if len(description) > MAX_MEMORY_DESCRIPTION_CHARS or "\n" in description:
            raise AppError(HTTPStatus.CONFLICT, "checkpoint memory is invalid")
        body_md = _bounded_string(memory.get("body_md"), "body_md", MAX_MEMORY_BODY_BYTES)
        if memory_name in seen_memories:
            raise AppError(HTTPStatus.CONFLICT, "checkpoint memories are invalid")
        seen_memories.add(memory_name)
        memories.append(
            {"name": memory_name, "description": description, "body_md": body_md}
        )
    raw_schedules = checkpoint.get("schedules")
    if not isinstance(raw_schedules, list) or len(raw_schedules) > MAX_SCHEDULES_PER_APP:
        raise AppError(HTTPStatus.CONFLICT, "checkpoint schedules are invalid")
    schedules = []
    seen_schedule_ids: set[int] = set()
    for raw in raw_schedules:
        schedule = _required_object(raw, "checkpoint schedule")
        schedule_id = schedule.get("id")
        if (
            isinstance(schedule_id, bool) or not isinstance(schedule_id, int)
            or schedule_id < 1 or schedule_id in seen_schedule_ids
        ):
            raise AppError(HTTPStatus.CONFLICT, "checkpoint schedule id is invalid")
        fields = _validated_schedule_fields(
            {key: schedule.get(key) for key in (
                "name", "message", "cadence", "interval_minutes", "daily_time", "enabled"
            )}
        )
        seen_schedule_ids.add(schedule_id)
        schedules.append({"id": schedule_id, **fields})
    return {
        "name": name,
        "html": html,
        "css": css,
        "javascript": javascript,
        "data": checkpoint.get("data"),
        "instructions_md": instructions,
        "memories": memories,
        "schedules": schedules,
    }


def restore_workspace(thread_id: str, body: Any) -> dict[str, Any]:
    """Rewind the bundle and/or data to a retained history entry.

    A restore is a new forward write (both counters bump), never a history
    rewrite, so an agent turn racing a restore fails its version check
    instead of silently resurrecting the pre-restore state.
    """
    _require_web_app(thread_id)
    request = _required_object(body, "restore request")
    _require_keys(request, {"history_id", "scope"}, required={"history_id"})
    history_id = request.get("history_id")
    if isinstance(history_id, bool) or not isinstance(history_id, int) or history_id < 1:
        raise AppError(HTTPStatus.BAD_REQUEST, "history_id must be a positive integer")
    scope = request.get("scope", "both")
    if scope not in {"both", "data", "ui"}:
        raise AppError(HTTPStatus.BAD_REQUEST, "scope must be both, data, or ui")
    now = _utc_now()
    with db.transaction() as cur:
        _set_search_path(cur)
        cur.execute(
            f"SELECT {STATE_COLUMNS} FROM web_apps"
            " WHERE thread_id = %s FOR UPDATE",
            (thread_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise AppError(HTTPStatus.NOT_FOUND, "app not found")
        cur.execute(
            "SELECT id FROM web_app_history WHERE thread_id = %s AND id = %s",
            (thread_id, history_id),
        )
        if cur.fetchone() is None:
            raise AppError(HTTPStatus.NOT_FOUND, "history entry not found")
        # Reconstruct only what the scope needs: a UI-only restore must not
        # fail because the data anchors behind an old UI entry were pruned.
        bundle = _bundle_at(cur, thread_id, history_id) if scope != "data" else None
        data = _data_at(cur, thread_id, history_id) if scope != "ui" else None
        assignments = []
        params: list[Any] = []
        if bundle is not None:
            assignments.append(
                "html = %s, css = %s, javascript = %s, ui_revision = ui_revision + 1"
            )
            params.extend([bundle["html"], bundle["css"], bundle["javascript"]])
        if data is not None:
            assignments.append("data_json = %s, data_version = data_version + 1")
            params.append(_validated_data(data))
        cur.execute(
            f"UPDATE web_apps SET {', '.join(assignments)}, updated_at = %s"
            " WHERE thread_id = %s"
            f" RETURNING {STATE_COLUMNS}",
            (*params, now, thread_id),
        )
        changed = cur.fetchone()
        assert changed is not None
        if bundle is not None:
            _insert_history(
                cur, thread_id, "ui", "user", changed[0], changed[1],
                {**bundle, "restored_from": history_id}, now,
            )
        if data is not None:
            _insert_history(
                cur, thread_id, "snapshot", "user", changed[0], changed[1],
                {"data": data, "restored_from": history_id}, now,
            )
        _prune_history(cur, thread_id)
    state = _state_row(changed)
    _require_state_response_fits(state)
    return {"app": state}


def _bundle_at(cur: Any, thread_id: str, history_id: int) -> dict[str, str]:
    cur.execute(
        "SELECT entry_json FROM web_app_history"
        " WHERE thread_id = %s AND kind = 'ui' AND id <= %s"
        " ORDER BY id DESC LIMIT 1",
        (thread_id, history_id),
    )
    row = cur.fetchone()
    if row is None:
        raise AppError(HTTPStatus.CONFLICT, "this history point is no longer restorable")
    entry = json.loads(row[0])
    return {
        "html": str(entry.get("html", "")),
        "css": str(entry.get("css", "")),
        "javascript": str(entry.get("javascript", "")),
    }


def _data_at(cur: Any, thread_id: str, history_id: int) -> Any:
    cur.execute(
        "SELECT id, entry_json FROM web_app_history"
        " WHERE thread_id = %s AND kind = 'snapshot' AND id <= %s"
        " ORDER BY id DESC LIMIT 1",
        (thread_id, history_id),
    )
    row = cur.fetchone()
    if row is None:
        raise AppError(HTTPStatus.CONFLICT, "this history point is no longer restorable")
    snapshot_id, entry_json = row
    data = json.loads(entry_json).get("data", {})
    cur.execute(
        "SELECT entry_json FROM web_app_history"
        " WHERE thread_id = %s AND kind = 'data' AND id > %s AND id <= %s"
        " ORDER BY id",
        (thread_id, snapshot_id, history_id),
    )
    for (op_json,) in cur.fetchall():
        op = json.loads(op_json)
        data = _mutate_data(
            data, str(op.get("action")), list(op.get("path", [])), op.get("value")
        )
    return data


# --- Instructions and memory -------------------------------------------------


def load_instructions(thread_id: str, *, verify: bool = True) -> dict[str, Any]:
    if verify:
        _require_web_app(thread_id)
    with db.transaction() as cur:
        _set_search_path(cur)
        cur.execute(
            "SELECT instructions_md, instructions_updated_by, instructions_updated_at"
            " FROM web_apps WHERE thread_id = %s",
            (thread_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise AppError(HTTPStatus.NOT_FOUND, "app not found")
    return {
        "instructions_md": row[0],
        "updated_by": row[1],
        "updated_at": row[2],
    }


def save_instructions(
    thread_id: str, body: Any, *, actor: str, verify: bool = True
) -> dict[str, Any]:
    request = _required_object(body, "instructions request")
    _require_keys(request, {"instructions_md"}, required={"instructions_md"})
    instructions = _bounded_string(
        request.get("instructions_md"), "instructions_md", MAX_INSTRUCTIONS_BYTES
    )
    if verify:
        _require_web_app(thread_id)
    return _apply_instructions(thread_id, instructions, actor=actor)


def _apply_instructions(
    thread_id: str, instructions: str, *, actor: str
) -> dict[str, Any]:
    now = _utc_now()
    with db.transaction() as cur:
        _set_search_path(cur)
        cur.execute(
            "SELECT instructions_md, instructions_updated_by, instructions_updated_at,"
            " ui_revision, data_version"
            " FROM web_apps WHERE thread_id = %s FOR UPDATE",
            (thread_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise AppError(HTTPStatus.NOT_FOUND, "app not found")
        old = row[0]
        if old == instructions:
            return {"instructions_md": row[0], "updated_by": row[1], "updated_at": row[2]}
        cur.execute(
            "UPDATE web_apps SET instructions_md = %s, instructions_updated_by = %s,"
            " instructions_updated_at = %s WHERE thread_id = %s"
            " RETURNING instructions_md, instructions_updated_by, instructions_updated_at",
            (instructions, actor, now, thread_id),
        )
        changed = cur.fetchone()
        assert changed is not None
        entry: dict[str, Any] = {"old": old, "new": instructions}
        _insert_history(cur, thread_id, "instructions", actor, row[3], row[4], entry, now)
        _prune_history(cur, thread_id)
    return {
        "instructions_md": changed[0],
        "updated_by": changed[1],
        "updated_at": changed[2],
    }


def list_memories(
    thread_id: str, query: dict[str, list[str]], *, verify: bool = True
) -> dict[str, Any]:
    if verify:
        _require_web_app(thread_id)
    unexpected = sorted(set(query) - {"q"})
    if unexpected:
        raise AppError(
            HTTPStatus.BAD_REQUEST,
            f"unexpected memory query fields: {', '.join(unexpected)}",
        )
    q_values = query.get("q") or []
    if len(q_values) > 1:
        raise AppError(HTTPStatus.BAD_REQUEST, "q must be provided once")
    clause = ""
    params: list[Any] = [thread_id]
    if q_values and q_values[0]:
        needle = q_values[0]
        if len(needle.encode()) > 200:
            raise AppError(HTTPStatus.BAD_REQUEST, "q exceeds 200 bytes")
        clause = " AND (name ILIKE %s OR description ILIKE %s OR body_md ILIKE %s)"
        like = f"%{_escape_like(needle)}%"
        params.extend([like, like, like])
    with db.transaction() as cur:
        _set_search_path(cur)
        cur.execute(
            "SELECT name, description, updated_by, updated_at FROM web_app_memories"
            f" WHERE thread_id = %s{clause} ORDER BY updated_at DESC, name",
            params,
        )
        rows = cur.fetchall()
    return {
        "memories": [
            {
                "name": row[0],
                "description": row[1],
                "updated_by": row[2],
                "updated_at": row[3],
            }
            for row in rows
        ]
    }


def load_memory(thread_id: str, name: str, *, verify: bool = True) -> dict[str, Any]:
    if verify:
        _require_web_app(thread_id)
    with db.transaction() as cur:
        _set_search_path(cur)
        cur.execute(
            "SELECT name, description, body_md, updated_by, updated_at"
            " FROM web_app_memories WHERE thread_id = %s AND name = %s",
            (thread_id, name),
        )
        row = cur.fetchone()
    if row is None:
        raise AppError(HTTPStatus.NOT_FOUND, "memory not found")
    return {
        "name": row[0],
        "description": row[1],
        "body_md": row[2],
        "updated_by": row[3],
        "updated_at": row[4],
    }


def save_memory(
    thread_id: str, name: str, body: Any, *, actor: str, verify: bool = True
) -> dict[str, Any]:
    if verify:
        _require_web_app(thread_id)
    request = _required_object(body, "memory request")
    _require_keys(request, {"description", "body_md"}, required={"description", "body_md"})
    description = _required_text(request.get("description"), "description")
    if len(description) > MAX_MEMORY_DESCRIPTION_CHARS or "\n" in description:
        raise AppError(
            HTTPStatus.BAD_REQUEST,
            f"description must be one line of at most {MAX_MEMORY_DESCRIPTION_CHARS} characters",
        )
    body_md = _bounded_string(request.get("body_md"), "body_md", MAX_MEMORY_BODY_BYTES)
    return _apply_memory(
        thread_id, name, {"description": description, "body_md": body_md}, actor=actor
    )


def _apply_memory(
    thread_id: str,
    name: str,
    value: dict[str, str] | None,
    *,
    actor: str,
) -> dict[str, Any]:
    """Upsert (value set) or delete (value None) one memory, recording the
    change as an individually undoable history entry."""
    now = _utc_now()
    with db.transaction() as cur:
        _set_search_path(cur)
        counters = _thread_counters(cur, thread_id)
        cur.execute(
            "SELECT description, body_md FROM web_app_memories"
            " WHERE thread_id = %s AND name = %s FOR UPDATE",
            (thread_id, name),
        )
        old_row = cur.fetchone()
        old = None if old_row is None else {"description": old_row[0], "body_md": old_row[1]}
        if value is None:
            if old_row is None:
                raise AppError(HTTPStatus.NOT_FOUND, "memory not found")
            cur.execute(
                "DELETE FROM web_app_memories WHERE thread_id = %s AND name = %s",
                (thread_id, name),
            )
            result: dict[str, Any] = {"ok": True}
        else:
            if old == value:
                cur.execute(
                    "SELECT name, description, body_md, updated_by, updated_at"
                    " FROM web_app_memories WHERE thread_id = %s AND name = %s",
                    (thread_id, name),
                )
                unchanged = cur.fetchone()
                assert unchanged is not None
                return _memory_row(unchanged)
            if old_row is None:
                cur.execute(
                    "SELECT COUNT(*) FROM web_app_memories WHERE thread_id = %s",
                    (thread_id,),
                )
                count_row = cur.fetchone()
                if count_row is not None and count_row[0] >= MAX_MEMORY_COUNT:
                    raise AppError(
                        HTTPStatus.CONFLICT,
                        f"this app already has {MAX_MEMORY_COUNT} memories; delete one first",
                    )
            cur.execute(
                "INSERT INTO web_app_memories"
                " (thread_id, name, description, body_md, updated_by, created_at, updated_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (thread_id, name) DO UPDATE SET"
                " description = EXCLUDED.description, body_md = EXCLUDED.body_md,"
                " updated_by = EXCLUDED.updated_by, updated_at = EXCLUDED.updated_at"
                " RETURNING name, description, body_md, updated_by, updated_at",
                (thread_id, name, value["description"], value["body_md"], actor, now, now),
            )
            row = cur.fetchone()
            assert row is not None
            result = _memory_row(row)
        entry: dict[str, Any] = {"name": name, "old": old, "new": value}
        _insert_history(
            cur, thread_id, "memory", actor, counters[0], counters[1], entry, now
        )
        _prune_history(cur, thread_id)
    return result


def _memory_row(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "name": row[0],
        "description": row[1],
        "body_md": row[2],
        "updated_by": row[3],
        "updated_at": row[4],
    }


def _thread_counters(cur: Any, thread_id: str) -> tuple[int, int]:
    cur.execute(
        "SELECT ui_revision, data_version FROM web_apps WHERE thread_id = %s FOR UPDATE",
        (thread_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise AppError(HTTPStatus.NOT_FOUND, "app not found")
    return (row[0], row[1])


def delete_memory(thread_id: str, name: str, *, verify: bool = True) -> dict[str, Any]:
    if verify:
        _require_web_app(thread_id)
    return _apply_memory(thread_id, name, None, actor="user" if verify else "agent")


def _memory_name(value: str) -> str:
    decoded = unquote(value)
    if not MEMORY_NAME_RE.fullmatch(decoded):
        raise AppError(
            HTTPStatus.BAD_REQUEST,
            "memory names are lowercase letters, digits, and dashes (max 64)",
        )
    return decoded


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# --- Schedules ---------------------------------------------------------------


def list_schedules(thread_id: str, *, verify: bool = True) -> dict[str, Any]:
    if verify:
        _require_web_app(thread_id)
    with db.transaction() as cur:
        _set_search_path(cur)
        cur.execute(
            f"SELECT {SCHEDULE_COLUMNS} FROM web_app_schedules"
            " WHERE thread_id = %s ORDER BY id",
            (thread_id,),
        )
        rows = cur.fetchall()
    return {"schedules": [_schedule_row(row) for row in rows]}


def create_schedule(
    thread_id: str, body: Any, *, actor: str, verify: bool = True
) -> dict[str, Any]:
    if verify:
        _require_web_app(thread_id)
    request = _required_object(body, "schedule request")
    _require_keys(
        request,
        {"name", "message", "cadence", "interval_minutes", "daily_time", "enabled"},
        required={"name", "message", "cadence"},
    )
    fields = _validated_schedule_fields(request)
    now = _utc_now()
    next_run = _format_ts(
        _next_cadence_run(
            fields["cadence"], fields["interval_minutes"], fields["daily_time"], _parse_ts(now)
        )
    )
    with db.transaction() as cur:
        _set_search_path(cur)
        counters = _thread_counters(cur, thread_id)
        cur.execute(
            "SELECT COUNT(*) FROM web_app_schedules WHERE thread_id = %s",
            (thread_id,),
        )
        count_row = cur.fetchone()
        if count_row is not None and count_row[0] >= MAX_SCHEDULES_PER_APP:
            raise AppError(
                HTTPStatus.CONFLICT,
                f"this app already has {MAX_SCHEDULES_PER_APP} schedules; delete one first",
            )
        cur.execute(
            "INSERT INTO web_app_schedules"
            " (thread_id, name, message, cadence, interval_minutes, daily_time,"
            " enabled, created_by, next_run_at, created_at, updated_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
            f" RETURNING {SCHEDULE_COLUMNS}",
            (
                thread_id, fields["name"], fields["message"], fields["cadence"],
                fields["interval_minutes"], fields["daily_time"], fields["enabled"],
                actor, next_run, now, now,
            ),
        )
        row = cur.fetchone()
        assert row is not None
        _insert_history(
            cur, thread_id, "schedule", actor, counters[0], counters[1],
            {"schedule_id": row[0], "old": None, "new": _schedule_content(fields)},
            now,
        )
        _prune_history(cur, thread_id)
    return _schedule_row(row)


def update_schedule(
    thread_id: str, schedule_id: int, body: Any, *, verify: bool = True
) -> dict[str, Any]:
    if verify:
        _require_web_app(thread_id)
    request = _required_object(body, "schedule request")
    allowed = {"name", "message", "cadence", "interval_minutes", "daily_time", "enabled"}
    _require_keys(request, allowed, required=set())
    if not request:
        raise AppError(HTTPStatus.BAD_REQUEST, "schedule update must change at least one field")
    now = _utc_now()
    with db.transaction() as cur:
        _set_search_path(cur)
        counters = _thread_counters(cur, thread_id)
        cur.execute(
            f"SELECT {SCHEDULE_COLUMNS} FROM web_app_schedules"
            " WHERE thread_id = %s AND id = %s FOR UPDATE",
            (thread_id, schedule_id),
        )
        row = cur.fetchone()
        if row is None:
            raise AppError(HTTPStatus.NOT_FOUND, "schedule not found")
        current = _schedule_row(row)
        merged = {
            "name": request.get("name", current["name"]),
            "message": request.get("message", current["message"]),
            "cadence": request.get("cadence", current["cadence"]),
            "interval_minutes": request.get(
                "interval_minutes",
                current["interval_minutes"] if request.get("cadence", current["cadence"]) == "interval" else None,
            ),
            "daily_time": request.get(
                "daily_time",
                current["daily_time"] if request.get("cadence", current["cadence"]) == "daily" else None,
            ),
            "enabled": request.get("enabled", current["enabled"]),
        }
        fields = _validated_schedule_fields(merged)
        cadence_changed = any(
            fields[key] != current[key]
            for key in ("cadence", "interval_minutes", "daily_time")
        )
        re_enabled = fields["enabled"] and not current["enabled"]
        next_run = current["next_run_at"]
        if cadence_changed or re_enabled:
            next_run = _format_ts(
                _next_cadence_run(
                    fields["cadence"], fields["interval_minutes"], fields["daily_time"],
                    _parse_ts(now),
                )
            )
        cur.execute(
            "UPDATE web_app_schedules SET name = %s, message = %s, cadence = %s,"
            " interval_minutes = %s, daily_time = %s, enabled = %s,"
            " next_run_at = %s, updated_at = %s"
            " WHERE thread_id = %s AND id = %s"
            f" RETURNING {SCHEDULE_COLUMNS}",
            (
                fields["name"], fields["message"], fields["cadence"],
                fields["interval_minutes"], fields["daily_time"], fields["enabled"],
                next_run, now, thread_id, schedule_id,
            ),
        )
        changed = cur.fetchone()
        assert changed is not None
        old_content = _schedule_content(current)
        new_content = _schedule_content(fields)
        if old_content != new_content:
            _insert_history(
                cur, thread_id, "schedule", "user" if verify else "agent",
                counters[0], counters[1],
                {"schedule_id": schedule_id, "old": old_content, "new": new_content},
                now,
            )
            _prune_history(cur, thread_id)
    return _schedule_row(changed)


def delete_schedule(
    thread_id: str, schedule_id: int, *, verify: bool = True
) -> dict[str, Any]:
    if verify:
        _require_web_app(thread_id)
    now = _utc_now()
    with db.transaction() as cur:
        _set_search_path(cur)
        counters = _thread_counters(cur, thread_id)
        cur.execute(
            f"DELETE FROM web_app_schedules WHERE thread_id = %s AND id = %s"
            f" RETURNING {SCHEDULE_COLUMNS}",
            (thread_id, schedule_id),
        )
        row = cur.fetchone()
        if row is None:
            raise AppError(HTTPStatus.NOT_FOUND, "schedule not found")
        _insert_history(
            cur, thread_id, "schedule", "user" if verify else "agent",
            counters[0], counters[1],
            {
                "schedule_id": schedule_id,
                "old": _schedule_content(_schedule_row(row)),
                "new": None,
            },
            now,
        )
        _prune_history(cur, thread_id)
    return {"ok": True}


def _schedule_content(schedule: dict[str, Any]) -> dict[str, Any]:
    """The undoable content of a schedule: its definition, not its run
    bookkeeping."""
    return {
        key: schedule[key]
        for key in ("name", "message", "cadence", "interval_minutes", "daily_time", "enabled")
    }


def _validated_schedule_fields(request: dict[str, Any]) -> dict[str, Any]:
    name = _required_text(request.get("name"), "name")
    if len(name) > MAX_APP_NAME_CHARS or "\n" in name:
        raise AppError(
            HTTPStatus.BAD_REQUEST,
            f"name must be one line of at most {MAX_APP_NAME_CHARS} characters",
        )
    message = _bounded_required_text(
        request.get("message"), "message", MAX_SCHEDULE_MESSAGE_BYTES
    )
    cadence = request.get("cadence")
    interval_minutes = request.get("interval_minutes")
    daily_time = request.get("daily_time")
    if cadence == "interval":
        if (
            isinstance(interval_minutes, bool)
            or not isinstance(interval_minutes, int)
            or not MIN_SCHEDULE_INTERVAL_MINUTES <= interval_minutes <= MAX_SCHEDULE_INTERVAL_MINUTES
        ):
            raise AppError(
                HTTPStatus.BAD_REQUEST,
                "interval_minutes must be an integer between"
                f" {MIN_SCHEDULE_INTERVAL_MINUTES} and {MAX_SCHEDULE_INTERVAL_MINUTES}",
            )
        if daily_time is not None:
            raise AppError(HTTPStatus.BAD_REQUEST, "daily_time does not apply to interval cadence")
        daily_time = None
    elif cadence == "daily":
        if not isinstance(daily_time, str) or not DAILY_TIME_RE.fullmatch(daily_time):
            raise AppError(HTTPStatus.BAD_REQUEST, "daily_time must be HH:MM in UTC")
        if interval_minutes is not None:
            raise AppError(HTTPStatus.BAD_REQUEST, "interval_minutes does not apply to daily cadence")
        interval_minutes = None
    else:
        raise AppError(HTTPStatus.BAD_REQUEST, "cadence must be interval or daily")
    enabled = request.get("enabled", True)
    if not isinstance(enabled, bool):
        raise AppError(HTTPStatus.BAD_REQUEST, "enabled must be a boolean")
    return {
        "name": name,
        "message": message,
        "cadence": cadence,
        "interval_minutes": interval_minutes,
        "daily_time": daily_time,
        "enabled": enabled,
    }


def _schedule_row(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "id": row[0],
        "name": row[1],
        "message": row[2],
        "cadence": row[3],
        "interval_minutes": row[4],
        "daily_time": row[5],
        "enabled": row[6],
        "created_by": row[7],
        "last_run_at": row[8],
        "next_run_at": row[9],
        "created_at": row[10],
        "updated_at": row[11],
    }


def _next_cadence_run(
    cadence: str,
    interval_minutes: int | None,
    daily_time: str | None,
    after: datetime,
) -> datetime:
    if cadence == "interval":
        assert interval_minutes is not None
        return after + timedelta(minutes=interval_minutes)
    assert daily_time is not None
    hour, minute = int(daily_time[:2]), int(daily_time[3:])
    candidate = after.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= after:
        candidate += timedelta(days=1)
    return candidate


def run_due_schedules(now: datetime | None = None) -> int:
    """Fire every due, enabled workspace schedule.

    A fire is one ordinary provenance-prefixed message on the workspace's
    fixed thread. A running turn defers the fire instead of steering work the
    human is watching; a workspace whose thread has no session yet skips to
    the next cadence occurrence (schedules never choose a session).
    """
    now = now or datetime.now(timezone.utc)
    now_ts = _format_ts(now)
    with db.transaction() as cur:
        _set_search_path(cur)
        cur.execute(
            "SELECT s.thread_id, s.id FROM web_app_schedules s"
            " WHERE s.enabled AND s.next_run_at <= %s"
            " ORDER BY s.next_run_at LIMIT %s",
            (now_ts, SCHEDULER_DUE_BATCH),
        )
        due = cur.fetchall()
    fired = 0
    for thread_id, schedule_id in due:
        fired += _fire_schedule(thread_id, schedule_id, now)
    return fired


def run_daily_workspace_snapshots(now: datetime | None = None) -> int:
    """Ensure every active workspace has one immutable snapshot for today."""
    instant = now or datetime.now(timezone.utc)
    checkpoint_date = instant.date().isoformat()
    with db.transaction() as cur:
        _set_search_path(cur)
        cur.execute(
            "SELECT thread_id FROM web_apps ORDER BY thread_id"
        )
        thread_ids = [row[0] for row in cur.fetchall()]
    created = 0
    for thread_id in thread_ids:
        try:
            with _workspace_lock(thread_id):
                before = list_checkpoints(thread_id)["checkpoints"]
                already_exists = any(
                    checkpoint.get("checkpoint_type") == "automatic"
                    and checkpoint.get("checkpoint_date") == checkpoint_date
                    for checkpoint in before
                )
                if already_exists:
                    continue
                save_workspace_checkpoint(thread_id, automatic=True, now=instant)
                created += 1
        except AppError as exc:
            if exc.status != HTTPStatus.NOT_FOUND:
                raise
    return created


def _fire_schedule(
    thread_id: str,
    schedule_id: int,
    now: datetime,
) -> int:
    try:
        status = _thread_status(thread_id)
        # Keep the potentially slow first status lookup outside the lock so a
        # concurrent pause, delete, or edit can commit. Then serialize the
        # current definition with browser/app sends and recheck an idle
        # verdict before delivery, closing the stale-idle race without making
        # running or sessionless workspaces pay for a second host request.
        with _workspace_lock(thread_id):
            schedule = _current_due_schedule(thread_id, schedule_id, now)
            if schedule is None:
                return 0
            if status == "idle":
                status = _thread_status(thread_id)
            next_cadence = _format_ts(
                _next_cadence_run(
                    schedule["cadence"],
                    schedule["interval_minutes"],
                    schedule["daily_time"],
                    now,
                )
            )
            retry = _format_ts(now + timedelta(minutes=SCHEDULE_RETRY_MINUTES))
            if status == "running":
                _reschedule(thread_id, schedule_id, next_run_at=retry, ran_at=None)
                return 0
            if status is None:
                # No session yet: a schedule cannot pick one. Skip this occurrence.
                _reschedule(thread_id, schedule_id, next_run_at=next_cadence, ran_at=None)
                return 0
            create_message(
                {"content": schedule["message"]},
                requested_by="schedule",
                thread_id=thread_id,
            )
            _reschedule(
                thread_id,
                schedule_id,
                next_run_at=next_cadence,
                ran_at=_format_ts(now),
            )
        return 1
    except AppError:
        # Transient host trouble (busy thread, admin restart): retry shortly
        # instead of dropping the occurrence. Recheck here too: a concurrent
        # edit may have moved this schedule to a later cadence while the host
        # request was failing.
        with _workspace_lock(thread_id):
            if _current_due_schedule(thread_id, schedule_id, now) is not None:
                _reschedule(
                    thread_id,
                    schedule_id,
                    next_run_at=_format_ts(
                        now + timedelta(minutes=SCHEDULE_RETRY_MINUTES)
                    ),
                    ran_at=None,
                )
        return 0


def _current_due_schedule(
    thread_id: str, schedule_id: int, now: datetime
) -> dict[str, Any] | None:
    """Return the current definition only while it is still eligible to fire."""
    with db.transaction() as cur:
        _set_search_path(cur)
        cur.execute(
            f"SELECT {', '.join(f's.{column.strip()}' for column in SCHEDULE_COLUMNS.split(','))}"
            " FROM web_app_schedules s"
            " WHERE s.thread_id = %s AND s.id = %s AND s.enabled"
            " AND s.next_run_at <= %s",
            (thread_id, schedule_id, _format_ts(now)),
        )
        row = cur.fetchone()
    return None if row is None else _schedule_row(row)


def _thread_status(thread_id: str) -> str | None:
    try:
        response = call_admin_api("GET", f"/v1/threads/{quote(thread_id, safe='')}")
    except AppError as exc:
        if exc.status == HTTPStatus.NOT_FOUND:
            return None
        raise
    thread = response.get("thread")
    if not isinstance(thread, dict) or thread.get("status") not in {"idle", "running"}:
        raise AppError(HTTPStatus.BAD_GATEWAY, "host admin returned invalid thread")
    return str(thread["status"])


def _reschedule(
    thread_id: str, schedule_id: int, *, next_run_at: str, ran_at: str | None
) -> None:
    assignment = ", last_run_at = %s" if ran_at is not None else ""
    params: list[Any] = [next_run_at, _utc_now()]
    if ran_at is not None:
        params.append(ran_at)
    params.extend([thread_id, schedule_id])
    with db.transaction() as cur:
        _set_search_path(cur)
        cur.execute(
            f"UPDATE web_app_schedules SET next_run_at = %s, updated_at = %s{assignment}"
            " WHERE thread_id = %s AND id = %s AND enabled",
            params,
        )


def scheduler_loop() -> None:
    snapshot_date: str | None = None
    while True:
        time.sleep(SCHEDULER_POLL_SECONDS)
        try:
            today = datetime.now(timezone.utc).date().isoformat()
            if today != snapshot_date:
                run_daily_workspace_snapshots()
                snapshot_date = today
            run_due_schedules()
        except Exception as exc:
            host_errors.report_unexpected("agentic_web_app.scheduler", exc)


# --- Shared validation and state helpers -------------------------------------


def _validated_data(value: Any) -> str:
    if not isinstance(value, dict):
        raise AppError(HTTPStatus.UNPROCESSABLE_ENTITY, "data must be a JSON object")
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise AppError(HTTPStatus.UNPROCESSABLE_ENTITY, "data must contain only JSON values") from exc
    if len(encoded.encode()) > MAX_DATA_BYTES:
        raise AppError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, f"data exceeds {MAX_DATA_BYTES} bytes")
    return encoded


def _state_row(row: tuple[Any, ...]) -> dict[str, Any]:
    try:
        data = json.loads(row[5])
    except json.JSONDecodeError as exc:
        raise AppError(HTTPStatus.INTERNAL_SERVER_ERROR, "stored app data is invalid") from exc
    return {
        "ui_revision": row[0],
        "data_version": row[1],
        "html": row[2],
        "css": row[3],
        "javascript": row[4],
        "data": data,
        "updated_at": row[6],
    }


def _require_state_response_fits(state: dict[str, Any]) -> None:
    encoded = json.dumps({"app": state}, sort_keys=True, separators=(",", ":")).encode()
    if len(encoded) > MAX_STATE_RESPONSE_BYTES:
        raise AppError(
            HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            f"serialized app state exceeds {MAX_STATE_RESPONSE_BYTES} bytes",
        )


def _validated_path(value: Any) -> list[str | int]:
    if not isinstance(value, list) or not value or len(value) > MAX_PATH_DEPTH:
        raise AppError(HTTPStatus.UNPROCESSABLE_ENTITY, f"path must contain 1 to {MAX_PATH_DEPTH} segments")
    path: list[str | int] = []
    for segment in value:
        if isinstance(segment, bool) or not isinstance(segment, (str, int)):
            raise AppError(HTTPStatus.UNPROCESSABLE_ENTITY, "path segments must be strings or non-negative integers")
        if isinstance(segment, int) and segment < 0:
            raise AppError(HTTPStatus.UNPROCESSABLE_ENTITY, "array indexes must be non-negative")
        if isinstance(segment, str) and (not segment or len(segment.encode()) > MAX_PATH_KEY_BYTES):
            raise AppError(HTTPStatus.UNPROCESSABLE_ENTITY, "object path keys must be bounded non-empty strings")
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
            raise AppError(HTTPStatus.UNPROCESSABLE_ENTITY, "append target must be an array")
        target.append(value)
        return root
    if isinstance(parent, dict) and isinstance(leaf, str):
        if action == "delete":
            if leaf not in parent:
                raise AppError(HTTPStatus.UNPROCESSABLE_ENTITY, "data path does not exist")
            del parent[leaf]
        else:
            parent[leaf] = value
        return root
    if isinstance(parent, list) and isinstance(leaf, int):
        if leaf >= len(parent):
            raise AppError(HTTPStatus.UNPROCESSABLE_ENTITY, "array index is out of range")
        if action == "delete":
            parent.pop(leaf)
        else:
            parent[leaf] = value
        return root
    raise AppError(HTTPStatus.UNPROCESSABLE_ENTITY, "data path does not match the stored shape")


def _child(parent: Any, segment: str | int) -> Any:
    if isinstance(parent, dict) and isinstance(segment, str) and segment in parent:
        return parent[segment]
    if isinstance(parent, list) and isinstance(segment, int) and segment < len(parent):
        return parent[segment]
    raise AppError(HTTPStatus.UNPROCESSABLE_ENTITY, "data path does not exist")


def _thread_session_config(thread: dict[str, Any]) -> dict[str, str]:
    """Read back the session configuration the host recorded for a thread.

    Read path (`host.session_options`): the recorded model may predate the
    current matrix, so a retained conversation stays readable even though the
    host refuses to run further turns on its thread. The matrix check belongs
    on the send path.
    """
    config = recorded_session_config(thread)
    if config is None:
        raise AppError(HTTPStatus.BAD_GATEWAY, "host admin returned invalid thread configuration")
    runtime, model, effort = config
    return {"agent_runtime": runtime, "model": model, "effort": effort}


def _required_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AppError(HTTPStatus.BAD_REQUEST, f"{label} must be an object")
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
        raise AppError(HTTPStatus.BAD_REQUEST, f"fields are invalid: {'; '.join(details)}")


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AppError(HTTPStatus.BAD_REQUEST, f"{label} must be a non-empty string")
    return value.strip()


def _bounded_required_text(value: Any, label: str, limit: int) -> str:
    text = _required_text(value, label)
    if len(text.encode()) > limit:
        raise AppError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, f"{label} exceeds {limit} bytes")
    return text


def _required_counter(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AppError(HTTPStatus.BAD_REQUEST, f"{label} must be a non-negative integer")
    return value


def _bounded_string(value: Any, label: str, limit: int) -> str:
    if not isinstance(value, str):
        raise AppError(HTTPStatus.UNPROCESSABLE_ENTITY, f"{label} must be a string")
    if len(value.encode()) > limit:
        raise AppError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, f"{label} exceeds {limit} bytes")
    if "\0" in value:
        raise AppError(HTTPStatus.UNPROCESSABLE_ENTITY, f"{label} must not contain NUL bytes")
    return value


def _path_segment(value: str) -> str:
    decoded = unquote(value)
    if not decoded or "/" in decoded or "\\" in decoded:
        raise AppError(HTTPStatus.BAD_REQUEST, "invalid path segment")
    return decoded


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: float) -> None:
        super().__init__("kern-admin-api", timeout=timeout)
        self._socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._socket_path)
        self.sock = sock


def call_admin_api(method: str, path: str, body: Any = None) -> dict[str, Any]:
    encoded = None if body is None else json.dumps(body, sort_keys=True).encode()
    headers = {"X-Kern-App-Backend": APP_ID}
    if encoded is not None:
        headers["Content-Type"] = "application/json"
    conn = _UnixHTTPConnection(
        ADMIN_API_SOCKET,
        timeout=APP_BACKEND_ADMIN_API_TIMEOUT_SECONDS,
    )
    try:
        conn.request(method, path, body=encoded, headers=headers)
        response = conn.getresponse()
        status = response.status
        raw = response.read(MAX_ADMIN_RESPONSE_BYTES + 1)
    except (OSError, http.client.HTTPException) as exc:
        raise AppError(HTTPStatus.BAD_GATEWAY, "host admin request failed") from exc
    finally:
        conn.close()
    if len(raw) > MAX_ADMIN_RESPONSE_BYTES:
        raise AppError(HTTPStatus.BAD_GATEWAY, "host admin response too large")
    try:
        payload = json.loads(raw.decode() or "{}")
    except json.JSONDecodeError as exc:
        raise AppError(HTTPStatus.BAD_GATEWAY, "host admin returned invalid JSON") from exc
    if status >= 400:
        message = payload.get("error", {}).get("message") if isinstance(payload, dict) else None
        raise AppError(HTTPStatus(status), message or "host admin request failed")
    if not isinstance(payload, dict):
        raise AppError(HTTPStatus.BAD_GATEWAY, "host admin returned invalid response")
    return payload


def _set_search_path(cur: Any) -> None:
    cur.execute(f'SET LOCAL search_path TO "{DB_SCHEMA.replace(chr(34), chr(34) * 2)}"')


def _utc_now() -> str:
    return time.strftime(TIME_FORMAT, time.gmtime())


def _format_ts(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime(TIME_FORMAT)


def _parse_ts(value: str) -> datetime:
    return datetime.strptime(value, TIME_FORMAT).replace(tzinfo=timezone.utc)


def main() -> int:
    threading.Thread(target=scheduler_loop, name="schedule-runner", daemon=True).start()
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
