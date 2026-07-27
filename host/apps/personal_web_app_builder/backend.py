"""Personal Web App Builder backend.

The app owns one generated UI bundle, one agent-defined JSON document, and one
fixed agent chat. The browser and agent receive separate route namespaces and
authentication markers. Generated browser code never reaches this process as
authority: all durable mutations are validated here and revision checked.
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

from host.constants import APP_BACKEND_ADMIN_SOCKET_PATH, LOOPBACK, MAX_REQUEST_BODY_BYTES as ADMIN_MAX_REQUEST_BODY_BYTES
from host.runtime.core import db
from host.session_options import public_session_options, recorded_session_config, session_config_error


APP_ID = "personal_web_app_builder"
# The builder runs one host thread at a time. A thread's session configuration
# is fixed for its lifetime, so reset moves the app to the next thread rather
# than reconfiguring the current one. Sequence 1 keeps the original id, which
# is what carries an existing conversation through the reset migration.
THREAD_BASE_ID = "builder"
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
CONVERSATION_TASK_LIMIT = 20
CONVERSATION_MESSAGE_BYTES = 12 * 1024
# json.dumps may expand each clipped byte to a six-byte \u00XX escape. Five
# events with both typed text fields at 12 KiB therefore stay below the 1 MiB
# app-backend response cap, including envelope headroom.
CONVERSATION_EVENT_PAGE_LIMIT = 5
MAX_PATH_DEPTH = 16
MAX_PATH_KEY_BYTES = 128
JAVASCRIPT_FORBIDDEN = re.compile(r"\bimport\b")
# Start over rotates the builder onto a new thread while clearing the app, so
# it must not interleave with work that belongs to the old one. Everything that
# could is serialized here rather than each path defending itself: the app is
# one operator's single workspace, so the contention this costs is nil and the
# alternative is a race check in every statement. Held across the host call in
# create_message, as Agent Chat does for its own send path.
BUILDER_LOCK = threading.Lock()
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
    server_version = "KernPersonalWebAppBuilder/0.1"

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

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
        except Exception:
            # App-controlled strings and internal transport details do not
            # belong in a browser or agent error response.
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": {"message": "app request failed"}})

    def _require_host_proxy(self) -> None:
        if self.headers.get("X-Kern-App-Proxy") != APP_ID:
            raise AppError(HTTPStatus.UNAUTHORIZED, "missing host app proxy marker")

    def _require_agent_proxy(self) -> None:
        """Reject anything without the kernel-attributed markers.

        This is the cheap gate. The attributed thread is checked again inside
        the transaction that reads or mutates `app_state`, because Start over
        can revoke a thread between this check and that statement; only the
        in-transaction check is authoritative.
        """
        if (
            self.headers.get("X-Kern-Agent-App-Proxy") != APP_ID
            or self.headers.get("X-Kern-Agent-Thread") != builder_thread_id()
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
    if method == "GET" and path == "/state":
        return {"app": load_app_state()}
    if method == "GET" and path == "/conversation":
        return browser_conversation()
    if method == "GET" and path == "/conversation/events":
        return browser_conversation_events(query or {})
    if method == "POST" and path == "/reset":
        with BUILDER_LOCK:
            return reset_app()
    if method == "POST" and path == "/messages":
        with BUILDER_LOCK:
            return create_message(body, requested_by="user")
    if method == "POST" and path == "/runtime/agent-requests":
        with BUILDER_LOCK:
            return create_message(body, requested_by="app")
    if method == "POST" and path == "/runtime/actions":
        return apply_runtime_action(body)
    match = re.fullmatch(r"/tasks/([^/]+)/(cancel|kill)", path)
    if method == "POST" and match:
        task_id, action = match.groups()
        task_id = _path_segment(task_id)
        return call_admin_api("POST", f"/v1/tasks/{quote(task_id, safe='')}/{action}", body)
    raise AppError(HTTPStatus.NOT_FOUND, "route not found")


def route_agent(method: str, path: str, body: Any, agent_thread: str) -> dict[str, Any]:
    with BUILDER_LOCK:
        # The handler's marker check happens before this lock, so Start over can
        # revoke the thread in between; re-check here, where reset cannot run
        # until the request finishes.
        if agent_thread != builder_thread_id():
            raise AppError(HTTPStatus.UNAUTHORIZED, "missing agent app context")
        if method == "GET" and path == "/agent/state":
            return {"app": load_app_state()}
        if method == "POST" and path == "/agent/actions":
            return apply_agent_action(body)
    raise AppError(HTTPStatus.NOT_FOUND, "agent route not found")


def browser_conversation() -> dict[str, Any]:
    thread_id = builder_thread_id()
    response = call_admin_api(
        "GET",
        f"/v1/threads/{thread_id}/tasks"
        f"?limit={CONVERSATION_TASK_LIMIT}&message_bytes={CONVERSATION_MESSAGE_BYTES}",
    )
    host_tasks = response.get("tasks")
    if not isinstance(host_tasks, list) or not all(isinstance(task, dict) for task in host_tasks):
        raise AppError(HTTPStatus.BAD_GATEWAY, "host admin returned invalid task list")
    tasks: list[dict[str, Any]] = host_tasks
    session = _task_session_config(tasks[0]) if tasks else None
    return {"tasks": tasks, "session": session}


def browser_conversation_events(query: dict[str, list[str]]) -> dict[str, Any]:
    unexpected = sorted(set(query) - {"since"})
    if unexpected:
        raise AppError(
            HTTPStatus.BAD_REQUEST,
            f"unexpected conversation event query fields: {', '.join(unexpected)}",
        )
    since_values = query.get("since") or []
    if len(since_values) > 1:
        raise AppError(HTTPStatus.BAD_REQUEST, "since must be provided once")
    parameters = [
        f"limit={CONVERSATION_EVENT_PAGE_LIMIT}",
        f"message_bytes={CONVERSATION_MESSAGE_BYTES}",
    ]
    if since_values:
        since = since_values[0]
        if not since.isdigit():
            raise AppError(HTTPStatus.BAD_REQUEST, "since must be a non-negative integer")
        parameters.insert(0, f"since={since}")
    path = f"/v1/threads/{builder_thread_id()}/events?{'&'.join(parameters)}"
    response = call_admin_api("GET", path)
    events = response.get("events")
    if not isinstance(events, list) or not all(isinstance(event, dict) for event in events):
        raise AppError(HTTPStatus.BAD_GATEWAY, "host admin returned invalid event list")
    return {"events": events}


def create_message(body: Any, *, requested_by: str) -> dict[str, Any]:
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
    thread_id = builder_thread_id()
    host_request: dict[str, Any] = {"input_message": input_message, "thread_id": thread_id}
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
    response = call_admin_api("POST", "/v1/tasks", host_request)
    task_id = _response_text(response.get("task_id"), "task_id")
    if _response_text(response.get("thread_id"), "thread_id") != thread_id:
        _stop_orphan(task_id)
        raise AppError(HTTPStatus.BAD_GATEWAY, "host admin returned mismatched task reference")
    return response


def _thread_id_for_seq(seq: int) -> str:
    return THREAD_BASE_ID if seq <= 1 else f"{THREAD_BASE_ID}-{seq}"


def builder_thread_id() -> str:
    """The host thread the builder is currently working in."""
    with db.transaction() as cur:
        _set_search_path(cur)
        cur.execute("SELECT thread_seq FROM app_state WHERE singleton = TRUE")
        row = cur.fetchone()
    if row is None:
        raise AppError(HTTPStatus.INTERNAL_SERVER_ERROR, "app state is unavailable")
    return _thread_id_for_seq(row[0])


def _stop_builder_tasks(thread_id: str) -> None:
    """Stop everything active on the thread, or fail without resetting.

    The thread summary carries every active task, so this sees all of them
    rather than a page of recent history — a thread may hold up to the host's
    queued-task limit. A task that cannot be stopped aborts the reset: the
    caller has not touched any state yet, so the operator retries against the
    builder they still have, rather than one that was cleared while hidden work
    kept running.
    """
    response = call_admin_api("GET", "/v1/threads")
    threads = response.get("threads")
    if not isinstance(threads, list):
        raise AppError(HTTPStatus.BAD_GATEWAY, "host admin returned invalid thread list")
    summary = next(
        (
            thread
            for thread in threads
            if isinstance(thread, dict) and thread.get("thread_id") == thread_id
        ),
        None,
    )
    if summary is None:
        return
    active = summary.get("active_tasks")
    if not isinstance(active, list):
        raise AppError(HTTPStatus.BAD_GATEWAY, "host admin returned invalid thread summary")
    for task in active:
        if not isinstance(task, dict):
            raise AppError(HTTPStatus.BAD_GATEWAY, "host admin returned invalid thread summary")
        _stop_task(_response_text(task.get("task_id"), "task_id"))


def reset_app() -> dict[str, Any]:
    """Stop the builder's work, clear the generated app, and start a new thread.

    Start over means exactly that: nothing from before survives it. Anything
    still queued or running is stopped first, because a task left on the
    discarded thread would keep burning a turn on work whose output is thrown
    away and whose conversation the operator can no longer see. Then the bundle
    and its data go, and the app moves to a fresh host thread — a thread's
    session configuration is fixed for its lifetime, so first-run state means a
    new thread rather than a reconfigured one.

    Only the browser can call this; the agent must not discard its own thread.
    The revision keeps counting up rather than returning to zero, so a stale
    writer's `expected_revision` can never match again.
    """
    _stop_builder_tasks(builder_thread_id())
    now = _utc_now()
    with db.transaction() as cur:
        _set_search_path(cur)
        cur.execute(
            "SELECT revision, thread_seq FROM app_state WHERE singleton = TRUE FOR UPDATE"
        )
        row = cur.fetchone()
        if row is None:
            raise AppError(HTTPStatus.INTERNAL_SERVER_ERROR, "app state is unavailable")
        revision, thread_seq = row[0] + 1, row[1] + 1
        cur.execute(
            "UPDATE app_state SET revision = %s, html = '', css = '', javascript = '',"
            " data_json = '{}', thread_seq = %s, updated_at = %s WHERE singleton = TRUE",
            (revision, thread_seq, now),
        )
    return {"app": load_app_state(), "thread_id": _thread_id_for_seq(thread_seq)}


def load_app_state() -> dict[str, Any]:
    with db.transaction() as cur:
        _set_search_path(cur)
        cur.execute(
            "SELECT revision, html, css, javascript, data_json, updated_at"
            " FROM app_state WHERE singleton = TRUE"
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


def apply_agent_action(body: Any) -> dict[str, Any]:
    action = _required_object(body, "agent action")
    name = _required_text(action.get("action"), "action")
    if name in {"set", "delete", "append"}:
        return apply_runtime_action(action)
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
    return {"app": _update_state(revision, values)}


def apply_runtime_action(body: Any) -> dict[str, Any]:
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
        cur.execute(
            "SELECT revision, html, css, javascript, data_json, updated_at"
            " FROM app_state WHERE singleton = TRUE FOR UPDATE"
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
            "UPDATE app_state SET data_json = %s, revision = revision + 1, updated_at = %s"
            " WHERE singleton = TRUE RETURNING revision, html, css, javascript, data_json, updated_at",
            (data_json, now),
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


def _update_state(revision: int, values: dict[str, str]) -> dict[str, Any]:
    assignments = [f"{column} = %s" for column in values]
    now = _utc_now()
    params: list[Any] = [*values.values(), now, revision]
    with db.transaction() as cur:
        _set_search_path(cur)
        cur.execute(
            "SELECT revision, html, css, javascript, data_json, updated_at"
            " FROM app_state WHERE singleton = TRUE FOR UPDATE"
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
            f"UPDATE app_state SET {', '.join(assignments)}, revision = revision + 1, updated_at = %s"
            " WHERE singleton = TRUE AND revision = %s"
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


def _task_session_config(task: dict[str, Any]) -> dict[str, str]:
    """Read back the session configuration the host recorded for a task.

    Read path (`host.session_options`): the recorded model may predate the
    current matrix, so a retained conversation stays readable even though the
    host refuses to run further tasks on its thread. The matrix check belongs
    on the send path.
    """
    config = recorded_session_config(task)
    if config is None:
        raise AppError(HTTPStatus.BAD_GATEWAY, "host admin returned invalid task configuration")
    runtime, model, effort = config
    return {"agent_runtime": runtime, "model": model, "effort": effort}


def _stop_task(task_id: str) -> None:
    """Cancel a queued task, or kill one a worker already claimed."""
    for action in ("cancel", "kill"):
        try:
            call_admin_api("POST", f"/v1/tasks/{quote(task_id, safe='')}/{action}", {})
            return
        except Exception:
            continue
    raise AppError(HTTPStatus.BAD_GATEWAY, "could not stop the builder's active work; try again")


def _stop_orphan(task_id: str) -> None:
    """Best effort: the caller is already failing and owns that error."""
    try:
        _stop_task(task_id)
    except AppError:
        return


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


def _response_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise AppError(HTTPStatus.BAD_GATEWAY, f"host admin returned invalid {label}")
    return value


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
    conn = _UnixHTTPConnection(ADMIN_API_SOCKET, timeout=10)
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
