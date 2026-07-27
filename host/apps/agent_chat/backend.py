"""Agent Chat app backend.

The app owns the Agent Chat thread index, task references, and archive state.
Host task contents and execution remain host-owned and are accessed through the
host admin API by this backend.
"""

from __future__ import annotations

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


HOST = os.environ.get("KERN_APP_HOST", LOOPBACK)
PORT = int(os.environ.get("KERN_APP_PORT", "7450"))
DB_SCHEMA = os.environ.get("KERN_APP_DB_SCHEMA", "app_agent_chat")
ADMIN_API_SOCKET = os.environ.get("KERN_APP_ADMIN_API_SOCKET", APP_BACKEND_ADMIN_SOCKET_PATH)
MAX_REQUEST_BODY_BYTES = 128 * 1024
# Admin API responses (a thread's full task history) can exceed the inbound
# request-body cap; size the response cap to the admin API's own body limit.
MAX_ADMIN_RESPONSE_BYTES = ADMIN_MAX_REQUEST_BODY_BYTES
APP_ID = "agent_chat"
RUNTIME_OPTIONS = {"codex", "claude_code", "hermes"}
# Keep each proxy response comfortably below the fixed 1 MiB bridge cap.
# Six 120 KiB event text budgets leave more than 300 KiB for JSON envelopes
# and bounded activity metadata. The UI drains all pages, so the smaller page
# does not skip events. Full task messages remain stored by the host.
THREAD_TASK_MESSAGE_BYTES = 1024
THREAD_EVENT_MESSAGE_BYTES = 120 * 1024
THREAD_EVENT_PAGE = 6
MESSAGE_SEND_LOCK = threading.Lock()


class AppError(Exception):
    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class Handler(BaseHTTPRequestHandler):
    server_version = "KernAgentChat/0.1"

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
            self._require_host_proxy()
            body = self._read_body()
            response: dict[str, Any]
            path = urlparse(self.path).path
            if method == "GET" and path == "/session-options":
                response = {"session_options": public_session_options()}
            elif method == "GET" and path == "/threads":
                query = parse_qs(urlparse(self.path).query)
                unexpected = sorted(set(query) - {"archived"})
                if unexpected:
                    raise AppError(
                        HTTPStatus.BAD_REQUEST,
                        f"unsupported thread query parameter: {unexpected[0]}",
                    )
                archived_values = query.get("archived") or []
                archived = False
                if archived_values:
                    if archived_values[0] not in {"true", "false"}:
                        raise AppError(HTTPStatus.BAD_REQUEST, "archived must be true or false")
                    archived = archived_values[0] == "true"
                response = list_app_threads(archived=archived)
            elif method == "GET" and path.startswith("/threads/") and path.endswith("/tasks"):
                parts = path.strip("/").split("/")
                if len(parts) != 3:
                    raise AppError(HTTPStatus.NOT_FOUND, "route not found")
                response = list_app_thread_tasks(
                    _path_segment(parts[1]),
                    include_archived=True,
                )
            elif method == "GET" and path.startswith("/threads/") and path.endswith("/events"):
                parts = path.strip("/").split("/")
                if len(parts) != 3:
                    raise AppError(HTTPStatus.NOT_FOUND, "route not found")
                response = list_app_thread_events(
                    _path_segment(parts[1]), parse_qs(urlparse(self.path).query)
                )
            elif (
                method == "POST"
                and path.startswith("/threads/")
                and (path.endswith("/archive") or path.endswith("/unarchive"))
            ):
                parts = path.strip("/").split("/")
                if len(parts) != 3:
                    raise AppError(HTTPStatus.NOT_FOUND, "route not found")
                response = {
                    "thread": set_app_thread_archived(
                        _path_segment(parts[1]),
                        archived=parts[2] == "archive",
                    )
                }
            elif method == "PUT" and path.startswith("/threads/") and path.endswith("/name"):
                parts = path.strip("/").split("/")
                if len(parts) != 3:
                    raise AppError(HTTPStatus.NOT_FOUND, "route not found")
                response = {
                    "thread": rename_app_thread(_path_segment(parts[1]), body)
                }
            elif method == "POST" and path == "/messages":
                response = send_app_message(body)
            elif method == "POST" and path.startswith("/tasks/"):
                parts = path.strip("/").split("/")
                if len(parts) != 3 or parts[2] not in {"cancel", "kill"}:
                    raise AppError(HTTPStatus.NOT_FOUND, "route not found")
                task_id = _path_segment(parts[1])
                _require_app_task(task_id)
                response = call_admin_api("POST", f"/v1/tasks/{quote(task_id, safe='')}/{parts[2]}", body)
            else:
                raise AppError(HTTPStatus.NOT_FOUND, "route not found")
            self._send_json(HTTPStatus.OK, response)
        except AppError as exc:
            self._send_json(exc.status, {"error": {"message": exc.message}})
        except Exception as exc:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": {"message": str(exc)}})

    def _require_host_proxy(self) -> None:
        if self.headers.get("X-Kern-App-Proxy") != APP_ID:
            raise AppError(HTTPStatus.UNAUTHORIZED, "missing host app proxy marker")

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
            raise AppError(HTTPStatus.BAD_REQUEST, f"invalid JSON: {exc}") from exc

    def _send_json(self, status: HTTPStatus, body: Any) -> None:
        data = json.dumps(body, sort_keys=True).encode()
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.end_headers()
        self.wfile.write(data)


def list_app_threads(*, archived: bool = False) -> dict[str, Any]:
    """The thread index: one bulk host call joined against the app's own
    thread bookkeeping. The host's app-scoped `GET /v1/threads` returns
    session config and live status for exactly this app's threads, so the
    index costs one socket round trip regardless of thread count.

    A thread is shown only when it is unarchived and has at least one recorded
    task: the host stays the source of truth for runtime/model/effort and
    active status, but task_count and active ids are taken from the app's own
    `thread_tasks`, so an orphaned host task (created then cancelled when
    `_record_app_task` failed) never inflates a count or resurrects a thread
    the app never finished recording. A reservation that never got a task has
    no `thread_tasks` rows and stays invisible."""
    recorded = _recorded_threads(archived=archived)
    response = call_admin_api("GET", "/v1/threads")
    summaries = response.get("threads")
    if not isinstance(summaries, list):
        raise AppError(HTTPStatus.BAD_GATEWAY, "host admin returned invalid thread list")
    app_threads = [
        _app_thread_summary(
            summary,
            recorded[summary["thread_id"]][0],
            name=recorded[summary["thread_id"]][1],
            archived=archived,
        )
        for summary in summaries
        if isinstance(summary, dict) and summary.get("thread_id") in recorded
    ]
    app_threads.sort(key=lambda item: str(item.get("last_used_at") or ""), reverse=True)
    return {"threads": app_threads}


def _recorded_threads(*, archived: bool) -> dict[str, tuple[set[str], str]]:
    """Threads in one archive state mapped to recorded task ids and names.

    Only threads with at least one recorded task appear, matching the index
    rule. Threads without a custom name keep showing their stable host id.
    """
    with db.transaction() as cur:
        _set_search_path(cur)
        cur.execute(
            "SELECT thread_tasks.thread_id, thread_tasks.task_id,"
            " COALESCE(threads.name, thread_tasks.thread_id)"
            " FROM thread_tasks JOIN threads ON threads.thread_id = thread_tasks.thread_id"
            " WHERE threads.archived = %s",
            (archived,),
        )
        rows = cur.fetchall()
    task_ids: dict[str, set[str]] = {}
    names: dict[str, str] = {}
    for thread_id, task_id, name in rows:
        task_ids.setdefault(thread_id, set()).add(task_id)
        names[thread_id] = name
    return {
        thread_id: (ids, names[thread_id])
        for thread_id, ids in task_ids.items()
    }


def _app_thread_summary(
    summary: dict[str, Any],
    recorded_task_ids: set[str],
    *,
    name: str,
    archived: bool,
) -> dict[str, Any]:
    runtime, model, effort = _host_task_session_config(summary)
    active_tasks = summary.get("active_tasks")
    if not isinstance(active_tasks, list):
        raise AppError(HTTPStatus.BAD_GATEWAY, "host admin returned invalid thread summary")
    return {
        "thread_id": _required_response_text(summary.get("thread_id"), "thread_id"),
        "name": name,
        "agent_runtime": runtime,
        "model": model,
        "effort": effort,
        "archived": archived,
        "last_used_at": str(summary.get("last_used_at") or ""),
        # Count and active ids come from the app's recorded tasks, never the
        # host's raw totals, so an orphaned host task cannot inflate them.
        "task_count": len(recorded_task_ids),
        "active_tasks": [
            {"task_id": task["task_id"], "status": task["status"]}
            for task in active_tasks
            if isinstance(task, dict)
            and isinstance(task.get("task_id"), str)
            and isinstance(task.get("status"), str)
            and task["task_id"] in recorded_task_ids
        ],
    }


def list_app_thread_tasks(
    thread_id: str,
    *,
    include_archived: bool = False,
) -> dict[str, Any]:
    _require_app_thread(thread_id, include_archived=include_archived)
    known_task_ids = _app_task_ids_for_thread(thread_id)
    response = call_admin_api(
        "GET",
        f"/v1/threads/{quote(thread_id, safe='')}/tasks?message_bytes={THREAD_TASK_MESSAGE_BYTES}",
    )
    host_tasks = response.get("tasks", [])
    if not isinstance(host_tasks, list):
        raise AppError(HTTPStatus.BAD_GATEWAY, "host admin returned invalid task list")
    return {"tasks": [task for task in host_tasks if isinstance(task, dict) and task.get("task_id") in known_task_ids]}


def list_app_thread_events(thread_id: str, query: dict[str, list[str]]) -> dict[str, Any]:
    """One chronological page of the thread's event stream.

    An uncursored request returns the latest page, ``before`` loads earlier
    history, and ``since`` keeps a loaded tail current. Every event under the
    app-scoped thread comes from a task this app created, so the stream passes
    through unfiltered; the UI groups events by task and ignores ids it does
    not know.
    """
    _require_app_thread(thread_id, include_archived=True)
    since_values = query.get("since") or []
    before_values = query.get("before") or []
    if since_values and before_values:
        raise AppError(HTTPStatus.BAD_REQUEST, "since and before cannot be combined")
    path = (
        f"/v1/threads/{quote(thread_id, safe='')}/events"
        f"?limit={THREAD_EVENT_PAGE}&message_bytes={THREAD_EVENT_MESSAGE_BYTES}"
    )
    cursor_name = "since" if since_values else "before" if before_values else None
    if cursor_name is not None:
        cursor = (since_values if cursor_name == "since" else before_values)[0]
        if not cursor.isdigit():
            raise AppError(
                HTTPStatus.BAD_REQUEST,
                f"{cursor_name} must be a non-negative integer",
            )
        path += f"&{cursor_name}={cursor}"
    response = call_admin_api("GET", path)
    events = response.get("events")
    if not isinstance(events, list):
        raise AppError(HTTPStatus.BAD_GATEWAY, "host admin returned invalid event list")
    return {"events": events}


def send_app_message(body: Any) -> dict[str, Any]:
    """Steer current work or start one new turn, decided from host state.

    The browser never chooses between those operations. Serializing sends
    prevents double submissions from creating parallel queued tasks, while a
    rejected steer is retried against fresh task state so completion races
    become a new turn instead of losing the operator's message.
    """
    if not isinstance(body, dict):
        raise AppError(HTTPStatus.BAD_REQUEST, "message request must be an object")
    _required_text(body.get("input_message"), "input_message")
    with MESSAGE_SEND_LOCK:
        if "thread_id" not in body:
            return {**create_app_task(body), "action": "created"}
        thread_id = _required_text(body.get("thread_id"), "thread_id")
        for _attempt in range(2):
            thread_tasks = list_app_thread_tasks(thread_id).get("tasks", [])
            running = [
                task for task in thread_tasks
                if isinstance(task, dict) and task.get("status") == "running"
            ]
            if running:
                task = max(
                    running,
                    key=lambda item: str(item.get("created_at") or ""),
                )
                task_id = _required_response_text(task.get("task_id"), "task_id")
                try:
                    call_admin_api(
                        "POST",
                        f"/v1/tasks/{quote(task_id, safe='')}/steer",
                        {"steer_message": body["input_message"]},
                    )
                except AppError as exc:
                    if exc.status == HTTPStatus.CONFLICT:
                        continue
                    raise
                return {
                    "action": "steered",
                    "task_id": task_id,
                    "thread_id": thread_id,
                }
            if any(
                isinstance(task, dict) and task.get("status") == "queued"
                for task in thread_tasks
            ):
                raise AppError(
                    HTTPStatus.CONFLICT,
                    "the task is starting; wait for it to begin before sending a follow-up",
                )
            return {**create_app_task(body), "action": "created"}
        raise AppError(
            HTTPStatus.CONFLICT,
            "the task changed state while sending; retry the message",
        )


def create_app_task(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise AppError(HTTPStatus.BAD_REQUEST, "task request must be an object")
    if "thread_id" in body:
        thread_id = _required_text(body.get("thread_id"), "thread_id")
    else:
        # A request without thread_id starts a new thread: the app owns thread
        # naming, so the operator never types an id.
        thread_id = _reserve_generated_thread_id()
        body = {**body, "thread_id": thread_id}
    requested_config = _requested_session_config(body)
    response = call_admin_api("POST", "/v1/tasks", body)
    task_id = _required_response_text(response.get("task_id"), "task_id")
    try:
        response_thread_id = _required_response_text(response.get("thread_id"), "thread_id")
        response_config = _host_task_session_config(response)
        if response_thread_id != thread_id or (
            requested_config is not None and response_config != requested_config
        ):
            raise AppError(HTTPStatus.BAD_GATEWAY, "host admin returned mismatched task reference")
        _record_app_task(thread_id, task_id)
    except Exception:
        _cancel_orphaned_host_task(task_id)
        raise
    return response


def _cancel_orphaned_host_task(task_id: str) -> None:
    # Cancel covers a still-queued task; kill covers one a worker claimed in
    # the create-to-conflict window, so the orphan never keeps executing. A
    # task that already finished cannot be revoked — the app request still
    # got its conflict error either way.
    for action in ("cancel", "kill"):
        try:
            call_admin_api("POST", f"/v1/tasks/{quote(task_id, safe='')}/{action}", {})
            return
        except Exception:
            continue


def set_app_thread_archived(thread_id: str, *, archived: bool) -> dict[str, Any]:
    with db.transaction() as cur:
        _set_search_path(cur)
        cur.execute(
            "UPDATE threads SET archived = %s WHERE thread_id = %s"
            " RETURNING thread_id, archived",
            (archived, thread_id),
        )
        row = cur.fetchone()
    if not row:
        raise AppError(HTTPStatus.NOT_FOUND, "thread not found")
    return {
        "thread_id": row[0],
        "archived": row[1],
    }


def archive_app_thread(thread_id: str) -> dict[str, Any]:
    return set_app_thread_archived(thread_id, archived=True)


def unarchive_app_thread(thread_id: str) -> dict[str, Any]:
    return set_app_thread_archived(thread_id, archived=False)


THREAD_NAME_MAX_CHARS = 100


def rename_app_thread(thread_id: str, body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise AppError(HTTPStatus.BAD_REQUEST, "rename request must be an object")
    name = _required_text(body.get("name"), "name")
    if len(name) > THREAD_NAME_MAX_CHARS:
        raise AppError(
            HTTPStatus.BAD_REQUEST,
            f"name must be at most {THREAD_NAME_MAX_CHARS} characters",
        )
    with db.transaction() as cur:
        _set_search_path(cur)
        cur.execute(
            "UPDATE threads SET name = %s WHERE thread_id = %s"
            " RETURNING thread_id, name",
            (name, thread_id),
        )
        row = cur.fetchone()
    if not row:
        raise AppError(HTTPStatus.NOT_FOUND, "thread not found")
    return {"thread_id": row[0], "name": row[1]}


THREAD_NAME_RE = re.compile(r"thread-([1-9][0-9]*)")


def _reserve_generated_thread_id() -> str:
    """Allocate the next successive thread name (thread-1, thread-2, ...).

    The name is reserved by inserting its thread row before the host call:
    the primary key makes concurrent generators take distinct names instead
    of merging two new chats into one thread (the host accepts a matching
    session configuration on an existing thread). Names count over every
    recorded thread, archived included, so a generated id never revives an
    archived thread. A reservation whose host call later fails stays as an
    empty thread: the index hides threads without tasks and the generator
    counts it, so its number is skipped rather than reused.
    """
    while True:
        with db.transaction() as cur:
            _set_search_path(cur)
            cur.execute("SELECT thread_id FROM threads")
            rows = cur.fetchall()
            numbers = [
                int(match.group(1))
                for (thread_id,) in rows
                if (match := THREAD_NAME_RE.fullmatch(thread_id)) is not None
            ]
            candidate = f"thread-{max(numbers, default=0) + 1}"
            cur.execute(
                "INSERT INTO threads (thread_id, archived) VALUES (%s, FALSE)"
                " ON CONFLICT (thread_id) DO NOTHING RETURNING thread_id",
                (candidate,),
            )
            if cur.fetchone() is not None:
                return candidate


def _record_app_task(thread_id: str, task_id: str) -> None:
    with db.transaction() as cur:
        _set_search_path(cur)
        cur.execute(
            """
            INSERT INTO threads (thread_id, archived)
            VALUES (%s, FALSE)
            ON CONFLICT (thread_id) DO NOTHING
            """,
            (thread_id,),
        )
        # Serialize against archive/unarchive. If archive won the row lock,
        # fail the send and let create_app_task cancel the just-created host
        # task instead of silently reviving a read-only thread.
        cur.execute(
            "SELECT archived FROM threads WHERE thread_id = %s FOR UPDATE",
            (thread_id,),
        )
        row = cur.fetchone()
        if row is None or row[0]:
            raise AppError(HTTPStatus.CONFLICT, "archived threads are read-only")
        cur.execute(
            """
            INSERT INTO thread_tasks (task_id, thread_id)
            VALUES (%s, %s)
            ON CONFLICT (task_id) DO NOTHING
            """,
            (task_id, thread_id),
        )


def _require_app_thread(thread_id: str, *, include_archived: bool = False) -> None:
    with db.transaction() as cur:
        _set_search_path(cur)
        query = "SELECT 1 FROM threads WHERE thread_id = %s"
        if not include_archived:
            query += " AND archived = FALSE"
        cur.execute(query, (thread_id,))
        row = cur.fetchone()
    if not row:
        raise AppError(HTTPStatus.NOT_FOUND, "thread not found")


def _requested_session_config(body: dict[str, Any]) -> tuple[str, str, str] | None:
    fields = ("agent_runtime", "model", "effort")
    supplied = [field for field in fields if field in body]
    if not supplied:
        return None
    if len(supplied) != len(fields):
        raise AppError(
            HTTPStatus.BAD_REQUEST,
            "agent_runtime, model, and effort must be provided together",
        )

    agent_runtime = _required_text(body.get("agent_runtime"), "agent_runtime")
    model = body.get("model")
    effort = body.get("effort")
    error = session_config_error(agent_runtime, model, effort)
    if error is not None:
        raise AppError(HTTPStatus.BAD_REQUEST, error)
    assert isinstance(model, str) and isinstance(effort, str)
    return agent_runtime, model, effort


def _host_task_session_config(task: dict[str, Any]) -> tuple[str, str, str]:
    """Read back the session configuration the host recorded for a thread.

    Read path (`host.session_options`): the recorded model may predate the
    current matrix, so a thread started under an earlier catalog stays listed
    and openable even though the host refuses to run further tasks on it. The
    matrix check belongs on the create path (`_requested_session_config`).
    """
    config = recorded_session_config(task)
    if config is None:
        raise AppError(HTTPStatus.BAD_GATEWAY, "host admin returned invalid session configuration")
    return config


def _app_task_ids_for_thread(thread_id: str) -> set[str]:
    with db.transaction() as cur:
        _set_search_path(cur)
        cur.execute("SELECT task_id FROM thread_tasks WHERE thread_id = %s", (thread_id,))
        rows = cur.fetchall()
    return {row[0] for row in rows}


def _require_app_task(task_id: str) -> None:
    with db.transaction() as cur:
        _set_search_path(cur)
        cur.execute(
            "SELECT 1 FROM thread_tasks"
            " JOIN threads ON threads.thread_id = thread_tasks.thread_id"
            " WHERE thread_tasks.task_id = %s AND threads.archived = FALSE",
            (task_id,),
        )
        row = cur.fetchone()
    if not row:
        raise AppError(HTTPStatus.NOT_FOUND, "task not found")


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AppError(HTTPStatus.BAD_REQUEST, f"{label} must be a non-empty string")
    return value.strip()


def _required_response_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AppError(HTTPStatus.BAD_GATEWAY, f"host admin returned invalid {label}")
    return value.strip()


class _UnixHTTPConnection(http.client.HTTPConnection):
    """http.client over the admin API's Unix socket: the standard client with
    only connect() replaced."""

    def __init__(self, socket_path: str, timeout: float) -> None:
        super().__init__("kern-admin-api", timeout=timeout)
        self._socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._socket_path)
        self.sock = sock


def call_admin_api(method: str, path: str, body: Any = None) -> dict[str, Any]:
    encoded_body = None if body is None else json.dumps(body, sort_keys=True).encode()
    headers = {"X-Kern-App-Backend": APP_ID}
    if encoded_body is not None:
        headers["Content-Type"] = "application/json"
    conn = _UnixHTTPConnection(ADMIN_API_SOCKET, timeout=10)
    try:
        conn.request(method, path, body=encoded_body, headers=headers)
        response = conn.getresponse()
        status = response.status
        raw = response.read(MAX_ADMIN_RESPONSE_BYTES + 1)
    except (OSError, http.client.HTTPException) as exc:
        raise AppError(HTTPStatus.BAD_GATEWAY, f"host admin request failed: {exc}") from exc
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
    cur.execute(f"SET LOCAL search_path TO {_quote_ident(DB_SCHEMA)}")


def _quote_ident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _path_segment(value: str) -> str:
    decoded = unquote(value)
    if not decoded or "/" in decoded or "\\" in decoded:
        raise AppError(HTTPStatus.BAD_REQUEST, "invalid path segment")
    return decoded


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def main() -> int:
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
