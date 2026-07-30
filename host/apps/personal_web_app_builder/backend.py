"""Agentic Web App backend.

Each workspace owns one generated UI bundle, one agent-defined JSON document,
and one fixed agent thread. The browser and agent receive separate route
namespaces and authentication markers. Generated browser code never reaches
this process as authority: all durable mutations are validated here and
revision checked.
"""

from __future__ import annotations

import copy
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
# Message creation and archive changes on one workspace must not interleave.
# A fixed stripe set keeps that coordination bounded while unrelated
# workspaces normally proceed independently.
WORKSPACE_LOCKS = tuple(threading.Lock() for _ in range(64))
REQUEST_PREFIXES = {
    "user": "Requested by user:",
    "app": "Requested by app:",
}


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

    def log_message(self, format: str, *args: object) -> None:
        return

    def _handle(self, method: str) -> None:
        try:
            parsed = urlparse(self.path)
            body = self._read_body()
            if parsed.path.startswith("/agent/"):
                self._require_agent_proxy()
                response = route_agent(
                    method, parsed.path, body, self.headers.get("X-Kern-Agent-Thread") or ""
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

    match = re.fullmatch(r"/apps/([^/]+)/(archive|unarchive)", path)
    if method == "POST" and match:
        thread_id, action = _path_segment(match.group(1)), match.group(2)
        with _workspace_lock(thread_id):
            return {
                "app": set_web_app_archived(
                    thread_id, archived=action == "archive"
                )
            }

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

    match = re.fullmatch(r"/apps/([^/]+)/stop", path)
    if method == "POST" and match:
        thread_id = _path_segment(match.group(1))
        _require_web_app(thread_id, include_archived=True)
        return call_admin_api(
            "POST", f"/v1/threads/{quote(thread_id, safe='')}/stop", body
        )
    raise AppError(HTTPStatus.NOT_FOUND, "route not found")


def route_agent(method: str, path: str, body: Any, agent_thread: str) -> dict[str, Any]:
    _require_web_app(agent_thread, include_archived=True, agent=True)
    if method == "GET" and path == "/agent/state":
        return {"app": load_app_state(agent_thread)}
    if method == "POST" and path == "/agent/actions":
        return apply_agent_action(body, agent_thread)
    raise AppError(HTTPStatus.NOT_FOUND, "agent route not found")


def list_web_apps(query: dict[str, list[str]]) -> dict[str, Any]:
    unexpected = sorted(set(query) - {"archived"})
    if unexpected:
        raise AppError(
            HTTPStatus.BAD_REQUEST,
            f"unexpected app query fields: {', '.join(unexpected)}",
        )
    archived_values = query.get("archived") or []
    if len(archived_values) > 1 or (
        archived_values and archived_values[0] not in {"true", "false"}
    ):
        raise AppError(HTTPStatus.BAD_REQUEST, "archived must be true or false")
    archived = bool(archived_values and archived_values[0] == "true")
    with db.transaction() as cur:
        _set_search_path(cur)
        cur.execute(
            "SELECT thread_id, name, archived, revision, created_at, updated_at"
            " FROM web_apps WHERE archived = %s",
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
    last_used_at = row[5]
    if host_summary is not None:
        host_status = host_summary.get("status")
        if host_status not in {"idle", "running"}:
            raise AppError(
                HTTPStatus.BAD_GATEWAY, "host admin returned invalid thread summary"
            )
        status = host_status
        session = _thread_session_config(host_summary)
        last_used_at = str(host_summary.get("last_used_at") or row[5])
    return {
        "thread_id": row[0],
        "name": row[1],
        "archived": row[2],
        "revision": row[3],
        "created_at": row[4],
        "updated_at": row[5],
        "last_used_at": last_used_at,
        "session": session,
        "status": status,
    }


def create_web_app() -> dict[str, Any]:
    # Match Agent Chat's allocator. The insert reserves an id across concurrent
    # creators, and archived ids remain counted so a workspace is never reused.
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
                " (thread_id, name, archived, revision, html, css, javascript,"
                " data_json, created_at, updated_at)"
                " VALUES (%s, %s, FALSE, 0, '', '', '', '{}', %s, %s)"
                " ON CONFLICT (thread_id) DO NOTHING"
                " RETURNING thread_id, name, archived, revision, created_at, updated_at",
                (thread_id, thread_id, now, now),
            )
            row = cur.fetchone()
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
            " RETURNING thread_id, name, archived, revision, created_at, updated_at",
            (name, thread_id),
        )
        row = cur.fetchone()
    if row is None:
        raise AppError(HTTPStatus.NOT_FOUND, "app not found")
    return _web_app_summary(row, None)


def set_web_app_archived(thread_id: str, *, archived: bool) -> dict[str, Any]:
    with db.transaction() as cur:
        _set_search_path(cur)
        cur.execute(
            "UPDATE web_apps SET archived = %s WHERE thread_id = %s"
            " RETURNING thread_id, name, archived, revision, created_at, updated_at",
            (archived, thread_id),
        )
        row = cur.fetchone()
    if row is None:
        raise AppError(HTTPStatus.NOT_FOUND, "app not found")
    return _web_app_summary(row, None)


def _require_web_app(
    thread_id: str, *, include_archived: bool = False, agent: bool = False
) -> None:
    with db.transaction() as cur:
        _set_search_path(cur)
        query = "SELECT 1 FROM web_apps WHERE thread_id = %s"
        if not include_archived:
            query += " AND archived = FALSE"
        cur.execute(query, (thread_id,))
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
    _require_web_app(thread_id, include_archived=True)
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
    _require_web_app(thread_id, include_archived=True)
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
    content = _bounded_required_text(
        request.get("content"),
        "content",
        MAX_CHAT_MESSAGE_BYTES - len(f"{prefix}\n".encode()),
    )
    input_message = f"{prefix}\n{content}"
    host_request: dict[str, Any] = {"message": input_message}
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
    _require_web_app(thread_id, include_archived=True)
    with db.transaction() as cur:
        _set_search_path(cur)
        cur.execute(
            "SELECT revision, html, css, javascript, data_json, updated_at"
            " FROM web_apps WHERE thread_id = %s",
            (thread_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise AppError(HTTPStatus.INTERNAL_SERVER_ERROR, "app state is unavailable")
    try:
        data = json.loads(row[4])
    except json.JSONDecodeError as exc:
        raise AppError(HTTPStatus.INTERNAL_SERVER_ERROR, "stored app data is invalid") from exc
    return {
        "revision": row[0],
        "html": row[1],
        "css": row[2],
        "javascript": row[3],
        "data": data,
        "updated_at": row[5],
    }


def apply_agent_action(body: Any, thread_id: str) -> dict[str, Any]:
    action = _required_object(body, "agent action")
    name = _required_text(action.get("action"), "action")
    if name in {"set", "delete", "append"}:
        return apply_runtime_action(action, thread_id, allow_archived=True)
    revision = _required_revision(action.get("expected_revision"))
    if name == "replace_app":
        _require_keys(
            action,
            {"action", "expected_revision", "html", "css", "javascript", "data"},
            required={"action", "expected_revision", "html", "css", "javascript", "data"},
        )
        values = _validated_bundle(action)
    elif name == "replace_ui":
        _require_keys(
            action,
            {"action", "expected_revision", "html", "css", "javascript"},
            required={"action", "expected_revision", "html", "css", "javascript"},
        )
        values = _validated_bundle(action, include_data=False)
    elif name == "replace_data":
        _require_keys(
            action,
            {"action", "expected_revision", "data"},
            required={"action", "expected_revision", "data"},
        )
        values = {"data_json": _validated_data(action.get("data"))}
    else:
        raise AppError(HTTPStatus.UNPROCESSABLE_ENTITY, "unsupported agent action")
    return {"app": _update_state(revision, values, thread_id)}


def apply_runtime_action(
    body: Any, thread_id: str, *, allow_archived: bool = False
) -> dict[str, Any]:
    _require_web_app(thread_id, include_archived=allow_archived)
    action = _required_object(body, "runtime action")
    name = _required_text(action.get("action"), "action")
    allowed = {"action", "expected_revision", "path"}
    required = {"action", "expected_revision", "path"}
    if name in {"set", "append"}:
        allowed.add("value")
        required.add("value")
    _require_keys(action, allowed, required=required)
    if name not in {"set", "delete", "append"}:
        raise AppError(HTTPStatus.UNPROCESSABLE_ENTITY, "unsupported runtime action")
    revision = _required_revision(action.get("expected_revision"))
    path = _validated_path(action.get("path"))
    with db.transaction() as cur:
        _set_search_path(cur)
        archive_clause = "" if allow_archived else " AND archived = FALSE"
        cur.execute(
            "SELECT revision, html, css, javascript, data_json, updated_at"
            f" FROM web_apps WHERE thread_id = %s{archive_clause} FOR UPDATE",
            (thread_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise AppError(HTTPStatus.INTERNAL_SERVER_ERROR, "app state is unavailable")
        if row[0] != revision:
            raise AppError(HTTPStatus.CONFLICT, "app state changed; reload and retry")
        try:
            data = json.loads(row[4])
        except json.JSONDecodeError as exc:
            raise AppError(HTTPStatus.INTERNAL_SERVER_ERROR, "stored app data is invalid") from exc
        updated = _mutate_data(copy.deepcopy(data), name, path, action.get("value"))
        data_json = _validated_data(updated)
        now = _utc_now()
        candidate = {
            **_state_row(row),
            "revision": revision + 1,
            "data": updated,
            "updated_at": now,
        }
        _require_state_response_fits(candidate)
        cur.execute(
            "UPDATE web_apps SET data_json = %s, revision = revision + 1, updated_at = %s"
            f" WHERE thread_id = %s{archive_clause}"
            " RETURNING revision, html, css, javascript, data_json, updated_at",
            (data_json, now, thread_id),
        )
        changed = cur.fetchone()
    assert changed is not None
    return {"app": _state_row(changed)}


def _validated_bundle(action: dict[str, Any], *, include_data: bool = True) -> dict[str, str]:
    html = _bounded_string(action.get("html"), "html", MAX_HTML_BYTES)
    css = _bounded_string(action.get("css"), "css", MAX_CSS_BYTES)
    javascript = _bounded_string(action.get("javascript"), "javascript", MAX_JAVASCRIPT_BYTES)
    if JAVASCRIPT_FORBIDDEN.search(javascript):
        raise AppError(HTTPStatus.UNPROCESSABLE_ENTITY, "javascript cannot use dynamic imports")
    result = {"html": html, "css": css, "javascript": javascript}
    if include_data:
        result["data_json"] = _validated_data(action.get("data"))
    return result


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


def _update_state(
    revision: int, values: dict[str, str], thread_id: str
) -> dict[str, Any]:
    assignments = [f"{column} = %s" for column in values]
    now = _utc_now()
    params: list[Any] = [*values.values(), now, thread_id, revision]
    with db.transaction() as cur:
        _set_search_path(cur)
        cur.execute(
            "SELECT revision, html, css, javascript, data_json, updated_at"
            " FROM web_apps WHERE thread_id = %s FOR UPDATE",
            (thread_id,),
        )
        current_row = cur.fetchone()
        if current_row is None:
            raise AppError(HTTPStatus.INTERNAL_SERVER_ERROR, "app state is unavailable")
        if current_row[0] != revision:
            raise AppError(HTTPStatus.CONFLICT, "app state changed; read it and retry")
        current = _state_row(current_row)
        candidate = {
            **current,
            **{key: value for key, value in values.items() if key != "data_json"},
            "revision": revision + 1,
            "updated_at": now,
        }
        if "data_json" in values:
            candidate["data"] = json.loads(values["data_json"])
        _require_state_response_fits(candidate)
        cur.execute(
            f"UPDATE web_apps SET {', '.join(assignments)}, revision = revision + 1, updated_at = %s"
            " WHERE thread_id = %s AND revision = %s"
            " RETURNING revision, html, css, javascript, data_json, updated_at",
            tuple(params),
        )
        row = cur.fetchone()
    if row is None:
        raise AppError(HTTPStatus.CONFLICT, "app state changed; read it and retry")
    return _state_row(row)


def _state_row(row: tuple[Any, ...]) -> dict[str, Any]:
    try:
        data = json.loads(row[4])
    except json.JSONDecodeError as exc:
        raise AppError(HTTPStatus.INTERNAL_SERVER_ERROR, "stored app data is invalid") from exc
    return {
        "revision": row[0], "html": row[1], "css": row[2],
        "javascript": row[3], "data": data, "updated_at": row[5],
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


def _required_revision(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AppError(HTTPStatus.BAD_REQUEST, "expected_revision must be a non-negative integer")
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
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def main() -> int:
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
