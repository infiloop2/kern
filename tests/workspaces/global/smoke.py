"""Deterministic global Memory and Schedules mock plus Playwright journeys."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
import json
import re
import threading
from typing import Any

from host.runtime.workspace import memory as memory_backend
from host.runtime.workspace import schedules as schedule_backend
from host.runtime.workspace.host_api import WorkspaceError
from host.session_options import schedule_session_options


LOCK = threading.RLock()
MEMORY: dict[str, dict[str, Any]] = {}
MEMORY_HISTORY: dict[str, list[dict[str, Any]]] = {}
SCHEDULES: dict[int, dict[str, Any]] = {}
SCHEDULE_HISTORY: dict[int, list[dict[str, Any]]] = {}
NEXT_SCHEDULE_ID = 1
DEMO_MODE = False


def configure_mock(*, demo_mode: bool = False) -> None:
    global NEXT_SCHEDULE_ID, DEMO_MODE
    DEMO_MODE = demo_mode
    with LOCK:
        MEMORY.clear()
        MEMORY_HISTORY.clear()
        SCHEDULES.clear()
        SCHEDULE_HISTORY.clear()
        NEXT_SCHEDULE_ID = 1
        if demo_mode:
            _seed_demo()


def route_workspace_api(
    method: str,
    relative: str,
    query: dict[str, list[str]],
    body: Any,
    api_error: Any,
    host_api: Any,
) -> dict[str, Any]:
    del host_api
    with LOCK:
        if relative == "memory" and method == "GET":
            return _list_memory(query, api_error)
        if relative == "memory/search" and method == "GET":
            return _search_memory(query, api_error)
        memory_match = re.fullmatch(r"memory/pages/([a-z0-9][a-z0-9-]{0,63})", relative)
        if memory_match:
            page_id = memory_match.group(1)
            if method == "GET":
                return {"page": _memory_detail(page_id, api_error)}
            if method == "PUT":
                return {"page": _save_memory(page_id, body, api_error)}
            if method == "DELETE":
                return _delete_memory(page_id, query, api_error)
        history_match = re.fullmatch(r"memory/pages/([^/]+)/revisions", relative)
        if history_match and method == "GET":
            return {"revisions": deepcopy(list(reversed(MEMORY_HISTORY.get(history_match.group(1), []))))}
        restore_match = re.fullmatch(r"memory/pages/([^/]+)/revisions/([1-9][0-9]*)/restore", relative)
        if restore_match and method == "POST":
            return {"page": _restore_memory(restore_match.group(1), int(restore_match.group(2)), body, api_error)}

        if relative == "schedules/session-options" and method == "GET":
            # Hermes is deliberately left deactivated so the smoke covers the
            # gated rendering for real; no journey selects it.
            return {
                "session_options": schedule_session_options(),
                "active_runtimes": ["codex"] if DEMO_MODE else ["claude_code", "codex"],
            }
        if relative == "schedules":
            if method == "GET":
                return _list_schedules(query, api_error)
            if method == "POST":
                return {"schedule": _create_schedule(body, api_error)}
        schedule_match = re.fullmatch(r"schedules/([1-9][0-9]*)", relative)
        if schedule_match:
            schedule_id = int(schedule_match.group(1))
            if method == "GET":
                return {"schedule": deepcopy(_schedule(schedule_id, api_error))}
            if method == "PUT":
                return {"schedule": _update_schedule(schedule_id, body, api_error)}
            if method == "DELETE":
                return _delete_schedule(schedule_id, query, api_error)
        revision_match = re.fullmatch(r"schedules/([1-9][0-9]*)/revisions", relative)
        if revision_match and method == "GET":
            return {"revisions": deepcopy(list(reversed(SCHEDULE_HISTORY.get(int(revision_match.group(1)), []))))}
        restore_schedule = re.fullmatch(r"schedules/([1-9][0-9]*)/revisions/([1-9][0-9]*)/restore", relative)
        if restore_schedule and method == "POST":
            return {"schedule": _restore_schedule(int(restore_schedule.group(1)), int(restore_schedule.group(2)), body, api_error)}
    raise api_error(HTTPStatus.NOT_FOUND, "global Workspace route not found")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _one(query: dict[str, list[str]], key: str, *, optional: bool = False) -> str:
    values = query.get(key) or []
    if not values:
        if optional:
            return ""
        raise ValueError(f"missing {key}")
    return values[0]


def _translate(api_error: Any, operation: Any, *args: Any, **kwargs: Any) -> Any:
    try:
        return operation(*args, **kwargs)
    except WorkspaceError as exc:
        raise api_error(exc.status, exc.message) from exc


def _memory_summary(page: dict[str, Any]) -> dict[str, Any]:
    summary = {
        key: deepcopy(page[key])
        for key in ("page_id", "description", "revision", "deleted", "links", "updated_by", "created_at", "updated_at")
    }
    summary["links"] = memory_backend._page_links(page["page_id"], page["content"])
    return summary


def _list_memory(query: dict[str, list[str]], api_error: Any) -> dict[str, Any]:
    _translate(api_error, memory_backend._reject_query_keys, query, {"cursor", "limit", "deleted", "scope"}, "memory index")
    deleted = _translate(api_error, memory_backend._boolean_query, query, "deleted", default=False)
    scope = _translate(api_error, memory_backend._memory_scope, query)
    limit = _translate(api_error, memory_backend._limit, query)
    after = _translate(api_error, memory_backend._decode_cursor, memory_backend._one(query, "cursor"))
    pages = sorted(
        (_memory_summary(page) for page in MEMORY.values() if page["deleted"] == deleted and memory_backend.is_individual_page_id(page["page_id"]) == (scope == "individual") and (after is None or page["page_id"] > after)),
        key=lambda page: page["page_id"],
    )
    response: dict[str, Any] = {"pages": pages[:limit]}
    if len(pages) > limit:
        response["next_cursor"] = memory_backend._encode_cursor(pages[limit - 1]["page_id"])
    return response


def _search_memory(query: dict[str, list[str]], api_error: Any) -> dict[str, Any]:
    _translate(api_error, memory_backend._reject_query_keys, query, {"q", "cursor", "limit", "scope"}, "memory search")
    scope = _translate(api_error, memory_backend._memory_scope, query)
    needle = _translate(api_error, memory_backend._one, query, "q")
    if not needle or len(needle.encode()) > memory_backend.MAX_SEARCH_BYTES:
        raise api_error(HTTPStatus.BAD_REQUEST, f"q must be between 1 and {memory_backend.MAX_SEARCH_BYTES} bytes")
    limit = _translate(api_error, memory_backend._limit, query, default=20)
    offset = _translate(api_error, memory_backend._decode_offset_cursor, memory_backend._one(query, "cursor"))
    terms = [term.lower() for term in re.findall(r"[a-z0-9]+", needle)]
    matches = []
    for page in MEMORY.values():
        haystack = f"{page['page_id']} {page['description']} {page['content']}".lower()
        if not page["deleted"] and memory_backend.is_individual_page_id(page["page_id"]) == (scope == "individual") and terms and all(term in haystack for term in terms):
            matches.append(_memory_summary(page))
    matches.sort(key=lambda page: page["page_id"])
    response: dict[str, Any] = {"pages": matches[offset:offset + limit]}
    if len(matches) > offset + limit:
        response["next_cursor"] = memory_backend._encode_offset_cursor(offset + limit)
    return response


def _memory_detail(page_id: str, api_error: Any) -> dict[str, Any]:
    page = MEMORY.get(page_id)
    if page is None:
        raise api_error(HTTPStatus.NOT_FOUND, "memory page not found")
    links = memory_backend._page_links(page_id, page["content"])
    backlinks = [] if memory_backend.is_individual_page_id(page_id) else sorted(
        item["page_id"] for item in MEMORY.values()
        if not item["deleted"]
        and not memory_backend.is_individual_page_id(item["page_id"])
        and page_id in memory_backend._page_links(item["page_id"], item["content"])
    )
    return {**deepcopy(page), "links": links, "backlinks": backlinks}


def _save_memory(page_id: str, body: Any, api_error: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise api_error(HTTPStatus.BAD_REQUEST, "memory page request must be an object")
    _translate(
        api_error,
        memory_backend._require_keys,
        body,
        {"description", "content", "expected_revision"},
        {"description", "content", "expected_revision"},
    )
    description = _translate(api_error, memory_backend._description, body["description"])
    content = _translate(api_error, memory_backend._content, body["content"])
    expected = _translate(api_error, memory_backend._expected_revision, body["expected_revision"])
    current = MEMORY.get(page_id)
    if current is None and len(MEMORY) >= memory_backend.MAX_PAGES:
        raise api_error(
            HTTPStatus.CONFLICT,
            f"Workspace already retains {memory_backend.MAX_PAGES} memory pages",
        )
    if expected != (0 if current is None else current["revision"]):
        raise api_error(HTTPStatus.CONFLICT, "memory page changed; reload and retry")
    if current is not None and current["deleted"]:
        raise api_error(HTTPStatus.CONFLICT, "memory page is deleted; restore it from history first")
    if current is not None and current["description"] == description and current["content"] == content:
        return _memory_detail(page_id, api_error)
    now = _now()
    page = {
        "page_id": page_id,
        "description": description,
        "content": content,
        "revision": 1 if current is None else current["revision"] + 1,
        "deleted": False,
        "links": memory_backend._page_links(page_id, content),
        "updated_by": "user",
        "created_at": now if current is None else current["created_at"],
        "updated_at": now,
    }
    MEMORY[page_id] = page
    MEMORY_HISTORY.setdefault(page_id, []).append({
        "id": page["revision"], "revision": page["revision"],
        "description": page["description"], "content": page["content"],
        "deleted": False, "actor": "user", "created_at": now,
    })
    return _memory_detail(page_id, api_error)


def _delete_memory(page_id: str, query: dict[str, list[str]], api_error: Any) -> dict[str, Any]:
    _translate(api_error, memory_backend._reject_query_keys, query, {"expected_revision"}, "memory delete")
    page = MEMORY.get(page_id)
    if page is None or page["deleted"]:
        raise api_error(HTTPStatus.NOT_FOUND, "memory page not found")
    if int(_one(query, "expected_revision")) != page["revision"]:
        raise api_error(HTTPStatus.CONFLICT, "memory page changed; reload and retry")
    page["revision"] += 1
    page["deleted"] = True
    page["updated_at"] = _now()
    MEMORY_HISTORY[page_id].append({
        "id": page["revision"], "revision": page["revision"],
        "description": page["description"], "content": page["content"],
        "deleted": True, "actor": "user", "created_at": page["updated_at"],
    })
    return {"ok": True, "revision": page["revision"]}


def _restore_memory(page_id: str, revision: int, body: Any, api_error: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise api_error(HTTPStatus.BAD_REQUEST, "memory restore request must be an object")
    _translate(
        api_error,
        memory_backend._require_keys,
        body,
        {"expected_revision"},
        {"expected_revision"},
    )
    page = MEMORY.get(page_id)
    source = next((item for item in MEMORY_HISTORY.get(page_id, []) if item["revision"] == revision), None)
    if page is None or source is None:
        raise api_error(HTTPStatus.NOT_FOUND, "memory revision not found")
    expected = _translate(api_error, memory_backend._expected_revision, body["expected_revision"])
    if expected != page["revision"]:
        raise api_error(HTTPStatus.CONFLICT, "memory page changed; reload and retry")
    now = _now()
    page.update({
        "description": source["description"],
        "content": source["content"],
        "revision": expected + 1,
        "deleted": source["deleted"],
        "links": list(dict.fromkeys(memory_backend.LINK_RE.findall(source["content"])))[:100],
        "updated_by": "user",
        "updated_at": now,
    })
    MEMORY_HISTORY[page_id].append({
        "id": page["revision"], "revision": page["revision"],
        "description": page["description"], "content": page["content"],
        "deleted": page["deleted"], "actor": "user", "created_at": now,
    })
    return _memory_detail(page_id, api_error)


def _schedule(schedule_id: int, api_error: Any) -> dict[str, Any]:
    schedule = SCHEDULES.get(schedule_id)
    if schedule is None:
        raise api_error(HTTPStatus.NOT_FOUND, "schedule not found")
    return schedule


def _schedule_fields(body: Any, api_error: Any, *, update: bool = False) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise api_error(HTTPStatus.BAD_REQUEST, "schedule request must be an object")
    required = {
        "name", "message", "cadence", "agent_runtime", "model", "effort",
    }
    if update:
        required.add("expected_revision")
    allowed = required | {"interval_minutes", "daily_time"}
    try:
        schedule_backend._require_keys(body, allowed, required)
        return schedule_backend._validated_fields(body)
    except WorkspaceError as exc:
        raise api_error(exc.status, exc.message) from exc


def _list_schedules(query: dict[str, list[str]], api_error: Any) -> dict[str, Any]:
    _translate(api_error, schedule_backend._reject_query_keys, query, {"before", "limit", "deleted"}, "schedule list")
    deleted = _translate(api_error, schedule_backend._boolean_query, query, "deleted", default=False)
    before = _translate(api_error, schedule_backend._optional_positive_int, query, "before")
    limit = _translate(api_error, schedule_backend._limit, query)
    rows = sorted(
        (
            deepcopy(item)
            for item in SCHEDULES.values()
            if item["deleted"] == deleted
            and (before is None or item["id"] < before)
        ),
        key=lambda item: item["id"],
        reverse=True,
    )
    response: dict[str, Any] = {"schedules": rows[:limit]}
    if len(rows) > limit:
        response["next_before"] = rows[limit - 1]["id"]
    return response


def _revision(schedule: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": schedule["revision"], "revision": schedule["revision"],
        **{key: deepcopy(schedule[key]) for key in (
            "name", "message", "cadence", "interval_minutes", "daily_time",
            "agent_runtime", "model", "effort", "deleted",
        )},
        "actor": "user", "created_at": schedule["updated_at"],
    }


def _create_schedule(body: Any, api_error: Any) -> dict[str, Any]:
    global NEXT_SCHEDULE_ID
    fields = _schedule_fields(body, api_error)
    active_count = sum(not schedule["deleted"] for schedule in SCHEDULES.values())
    if active_count >= schedule_backend.MAX_SCHEDULES:
        raise api_error(
            HTTPStatus.CONFLICT,
            f"Workspace already has {schedule_backend.MAX_SCHEDULES} active schedules",
        )
    schedule_id = NEXT_SCHEDULE_ID
    NEXT_SCHEDULE_ID += 1
    instant = datetime.now(timezone.utc)
    now = schedule_backend._format_ts(instant)
    next_run = schedule_backend._format_ts(schedule_backend._next_run(fields, instant))
    schedule = {
        "id": schedule_id, **fields, "revision": 1,
        "deleted": False,
        "last_run_at": now, "next_run_at": next_run, "created_at": now, "updated_at": now,
        "thread_id": f"schedule-{schedule_id}",
    }
    SCHEDULES[schedule_id] = schedule
    SCHEDULE_HISTORY[schedule_id] = [_revision(schedule)]
    return deepcopy(schedule)


def _update_schedule(schedule_id: int, body: Any, api_error: Any) -> dict[str, Any]:
    schedule = _schedule(schedule_id, api_error)
    fields = _schedule_fields(body, api_error, update=True)
    assert isinstance(body, dict)
    if body.get("expected_revision") != schedule["revision"]:
        raise api_error(HTTPStatus.CONFLICT, "schedule changed; reload and retry")
    cadence_changed = any(fields[key] != schedule[key] for key in ("cadence", "interval_minutes", "daily_time"))
    schedule.update(fields)
    if cadence_changed:
        schedule["next_run_at"] = schedule_backend._format_ts(
            schedule_backend._next_run(fields, datetime.now(timezone.utc))
        )
    schedule["revision"] += 1
    schedule["updated_at"] = _now()
    SCHEDULE_HISTORY[schedule_id].append(_revision(schedule))
    return deepcopy(schedule)


def _delete_schedule(schedule_id: int, query: dict[str, list[str]], api_error: Any) -> dict[str, Any]:
    schedule = _schedule(schedule_id, api_error)
    if int(_one(query, "expected_revision")) != schedule["revision"]:
        raise api_error(HTTPStatus.CONFLICT, "schedule changed; reload and retry")
    schedule["revision"] += 1
    schedule["deleted"] = True
    schedule["updated_at"] = _now()
    SCHEDULE_HISTORY[schedule_id].append(_revision(schedule))
    return {
        "ok": True,
        "revision": schedule["revision"],
        "thread_id": schedule["thread_id"],
    }


def _restore_schedule(schedule_id: int, revision: int, body: Any, api_error: Any) -> dict[str, Any]:
    schedule = _schedule(schedule_id, api_error)
    source = next((item for item in SCHEDULE_HISTORY[schedule_id] if item["revision"] == revision), None)
    if source is None:
        raise api_error(HTTPStatus.NOT_FOUND, "schedule revision not found")
    if body.get("expected_revision") != schedule["revision"]:
        raise api_error(HTTPStatus.CONFLICT, "schedule changed; reload and retry")
    schedule.update({key: deepcopy(source[key]) for key in (
        "name", "message", "cadence", "interval_minutes", "daily_time",
        "agent_runtime", "model", "effort",
    )})
    schedule["deleted"] = source["deleted"]
    schedule["next_run_at"] = schedule_backend._format_ts(
        schedule_backend._next_run(schedule, datetime.now(timezone.utc))
    )
    schedule["revision"] += 1
    schedule["updated_at"] = _now()
    SCHEDULE_HISTORY[schedule_id].append(_revision(schedule))
    return deepcopy(schedule)


def _seed_demo() -> None:
    global NEXT_SCHEDULE_ID
    now = datetime.now(timezone.utc)
    older = schedule_backend._format_ts(now - timedelta(days=2))
    recent = schedule_backend._format_ts(now - timedelta(minutes=18))
    daily_last = now.replace(hour=9, minute=0, second=0, microsecond=0)
    if daily_last > now:
        daily_last -= timedelta(days=1)
    daily_finished = daily_last + timedelta(minutes=8)
    daily_next = daily_last + timedelta(days=1)
    interval_next = now + timedelta(hours=6)
    MEMORY.update({
        "release-preferences": {
            "page_id": "release-preferences", "description": "Use before planning a release",
            "content": "Keep release notes concise. Review [[rollback-plan]] before deployment.",
            "revision": 2, "deleted": False, "links": ["rollback-plan"], "updated_by": "user",
            "created_at": older, "updated_at": recent,
        },
        "rollback-plan": {
            "page_id": "rollback-plan", "description": "Fallback steps for production deployments",
            "content": "Confirm the previous image is available, then verify health after rollback.",
            "revision": 1, "deleted": False, "links": [], "updated_by": "agent",
            "created_at": older, "updated_at": older,
        },
        "retired-note": {
            "page_id": "retired-note", "description": "An example deleted memory page",
            "content": "This page can be restored from revision history.",
            "revision": 2, "deleted": True, "links": [], "updated_by": "user",
            "created_at": older, "updated_at": recent,
        },
        "thread-7": {
            "page_id": "thread-7", "description": "Private context for chat thread 7",
            "content": "Prefer a bounded release checklist.",
            "revision": 1, "deleted": False, "links": [], "updated_by": "agent",
            "created_at": older, "updated_at": recent,
        },
    })
    for page in MEMORY.values():
        first = {"id": 1, "revision": 1, "description": page["description"], "content": page["content"], "deleted": False, "actor": page["updated_by"], "created_at": page["created_at"]}
        MEMORY_HISTORY[page["page_id"]] = [first]
        if page["revision"] == 2:
            MEMORY_HISTORY[page["page_id"]].append({**first, "id": 2, "revision": 2, "deleted": page["deleted"], "created_at": page["updated_at"]})
    schedules = [
        {
            "id": 1, "name": "Morning release review", "message": "Summarize open release work and identify blockers.",
            "cadence": "daily", "interval_minutes": None, "daily_time": "09:00",
            "agent_runtime": "codex", "model": "gpt-5.6-terra", "effort": "high",
            "revision": 2, "deleted": False,
            "thread_id": "schedule-1",
            "last_run_at": schedule_backend._format_ts(daily_last),
            "next_run_at": schedule_backend._format_ts(daily_next),
            "created_at": older, "updated_at": schedule_backend._format_ts(daily_finished),
        },
        {
            "id": 2, "name": "Dependency snapshot", "message": "/mnt/kern-agent/agent-home/scripts/dependency-snapshot.sh",
            "cadence": "interval", "interval_minutes": 360, "daily_time": None,
            "agent_runtime": "script", "model": "bash", "effort": "fixed",
            "revision": 1, "deleted": False, "last_run_at": recent,
            "thread_id": "schedule-2",
            "next_run_at": schedule_backend._format_ts(interval_next),
            "created_at": older, "updated_at": recent,
        },
    ]
    for schedule in schedules:
        SCHEDULES[schedule["id"]] = schedule
        SCHEDULE_HISTORY[schedule["id"]] = [_revision(schedule)]
    NEXT_SCHEDULE_ID = 3


def desktop_smoke(page: Any) -> None:
    from playwright.sync_api import expect

    page.get_by_role("button", name="Memory", exact=True).click()
    surface = page.locator("#panel-workspace-global")
    expect(surface).to_be_visible()
    expect(surface.locator("#global-title")).to_have_text("Memory")
    expect(page).to_have_url(re.compile(r"#memory$"))
    surface.get_by_role("button", name="New page", exact=True).click()
    surface.locator("#memory-page-id").fill("release-preferences")
    surface.locator("#memory-description").fill("Use before planning a release")
    memory_content = surface.locator("#memory-content")
    memory_content.fill(
        "Read [[rollback-plan]] and keep notes concise.\n" + "unbroken-memory-content-" * 18
    )
    expect(memory_content).to_have_css("overflow-x", "hidden")
    expect(memory_content).to_have_css("overflow-y", "hidden")
    expect(memory_content).to_have_css("white-space", "pre-wrap")
    dimensions = memory_content.evaluate(
        "element => ({scrollHeight: element.scrollHeight, clientHeight: element.clientHeight, "
        "scrollWidth: element.scrollWidth, clientWidth: element.clientWidth})"
    )
    if dimensions["scrollHeight"] > dimensions["clientHeight"] + 1:
        raise AssertionError(f"memory content should grow instead of scrolling: {dimensions}")
    if dimensions["scrollWidth"] > dimensions["clientWidth"] + 1:
        raise AssertionError(f"memory content should wrap instead of scrolling: {dimensions}")
    original_viewport = page.viewport_size
    if original_viewport:
        page.set_viewport_size({"width": 720, "height": original_viewport["height"]})
        page.wait_for_timeout(50)
        narrowed = memory_content.evaluate(
            "element => ({scrollHeight: element.scrollHeight, clientHeight: element.clientHeight})"
        )
        if narrowed["scrollHeight"] > narrowed["clientHeight"] + 1:
            raise AssertionError(f"memory content clipped after its width changed: {narrowed}")
        page.set_viewport_size(original_viewport)
    surface.get_by_role("button", name="Save page", exact=True).click()
    expect(surface.locator("#global-list")).to_contain_text("release-preferences")
    expect(surface.locator("#memory-links")).to_contain_text("rollback-plan")
    expect(page).to_have_url(re.compile(r"#memory/release-preferences$"))
    page.reload(wait_until="domcontentloaded")
    expect(surface).to_be_visible()
    expect(surface.locator("#memory-page-id")).to_have_value("release-preferences")
    expect(page).to_have_url(re.compile(r"#memory/release-preferences$"))
    surface.get_by_role("button", name="Cancel", exact=True).click()
    expect(surface.locator("#global-empty")).to_be_visible()
    expect(page).to_have_url(re.compile(r"#memory$"))
    surface.locator("[data-item-id='release-preferences']").click()
    surface.locator("#memory-content").fill("Keep release notes concise.")
    surface.get_by_role("button", name="Save page", exact=True).click()
    expect(surface.locator("#memory-history")).to_contain_text("Revision 1")
    surface.get_by_role("button", name="Individual", exact=True).click()
    expect(surface.locator("#global-intro")).to_contain_text("Private memory")
    expect(surface.locator("#global-list")).not_to_contain_text("release-preferences")
    surface.get_by_role("button", name="New page", exact=True).click()
    expect(surface.locator("#memory-link-graph")).to_be_hidden()
    surface.locator("#memory-page-id").fill("thread-7")
    surface.locator("#memory-description").fill("Private context for chat thread 7")
    surface.locator("#memory-content").fill("Prefer a bounded release checklist.")
    surface.get_by_role("button", name="Save page", exact=True).click()
    expect(surface.locator("#global-list")).to_contain_text("thread-7")
    surface.get_by_role("button", name="Swarm", exact=True).click()
    expect(surface.locator("#global-list")).to_contain_text("release-preferences")
    expect(surface.locator("#global-list")).not_to_contain_text("thread-7")

    page.get_by_role("button", name="Scheduled agents", exact=True).click()
    expect(surface.locator("#global-title")).to_have_text("Schedules")
    expect(page).to_have_url(re.compile(r"#scheduled-agents$"))
    surface.get_by_role("button", name="New schedule", exact=True).click()
    expect(surface.locator("#schedule-enabled")).to_have_count(0)
    expect(surface.locator("#schedule-runs-section")).to_have_count(0)
    surface.locator("#schedule-name").fill("Morning review")
    schedule_message = surface.locator("#schedule-message")
    schedule_message.fill("Summarize open release work.\n" + "unbroken-schedule-message-" * 24)
    expect(schedule_message).to_have_css("overflow-x", "hidden")
    expect(schedule_message).to_have_css("overflow-y", "hidden")
    expect(schedule_message).to_have_css("white-space", "pre-wrap")
    dimensions = schedule_message.evaluate(
        "element => ({scrollHeight: element.scrollHeight, clientHeight: element.clientHeight, "
        "scrollWidth: element.scrollWidth, clientWidth: element.clientWidth})"
    )
    if dimensions["scrollHeight"] > dimensions["clientHeight"] + 1:
        raise AssertionError(f"schedule message should grow instead of scrolling: {dimensions}")
    if dimensions["scrollWidth"] > dimensions["clientWidth"] + 1:
        raise AssertionError(f"schedule message should wrap instead of scrolling: {dimensions}")
    expect(surface.locator("#schedule-message-label")).to_have_text("Message")
    expect(surface.locator("#schedule-runtime option[value='script']")).to_have_count(1)
    # A provider the operator has not activated stays visible but unusable.
    hermes = surface.locator("#schedule-runtime option[value='hermes']")
    expect(hermes).to_have_text("hermes (not activated)")
    if not hermes.evaluate("option => option.disabled"):
        raise AssertionError("a deactivated runtime must not be selectable")
    surface.locator("#schedule-cadence").select_option("daily")
    surface.locator("#schedule-time").fill("09:00")
    surface.get_by_role("button", name="Save schedule", exact=True).click()
    chat = page.locator("#panel-workspace-chat")
    expect(chat).to_be_visible()
    expect(chat.locator("#thread-title")).to_have_text("Morning review")
    expect(page.locator("#scheduled-agents-nav-items")).to_contain_text("Morning review")
    expect(page).to_have_url(re.compile(r"#chat/schedule-1$"))
    expect(chat.locator("#schedule-settings")).to_be_visible()
    expect(chat.locator("#thread-memory")).to_be_visible()
    expect(chat.locator("#new-task-runtime")).to_be_hidden()
    expect(chat.locator("#archive-thread")).to_be_hidden()
    chat.get_by_role("button", name="Rename scheduled agent", exact=True).click()
    chat.locator("#rename-thread-input").fill("Daily release review")
    chat.locator("#rename-thread-save").click()
    expect(chat.locator("#thread-title")).to_have_text("Daily release review")
    expect(page.locator("#scheduled-agents-nav-items")).to_contain_text(
        "Daily release review"
    )
    chat.locator("#schedule-settings").click()
    expect(surface).to_be_visible()
    expect(surface.locator("#global-title")).to_have_text("Scheduled agent")
    expect(surface.locator("#schedule-name")).to_have_value("Daily release review")
    expect(surface.locator("#schedule-runs-section")).to_have_count(0)
    expect(page).to_have_url(re.compile(r"#scheduled-agents/1$"))
    page.evaluate("""() => {
      window.__kernSavedRefreshNavigation = window.KernHost.refreshNavigation;
      window.KernHost.refreshNavigation = async () => {
        throw new Error("navigation unavailable");
      };
    }""")
    surface.get_by_role("button", name="Save schedule", exact=True).click()
    expect(surface.locator("#global-status")).to_contain_text(
        "Schedule saved, but Chat could not be opened: navigation unavailable"
    )
    expect(surface.locator("#schedule-name")).to_have_value("Daily release review")
    page.evaluate("""() => {
      window.KernHost.refreshNavigation = window.__kernSavedRefreshNavigation;
      delete window.__kernSavedRefreshNavigation;
    }""")
    page.evaluate("""() => {
      window.__kernSavedRefreshNavigation = window.KernHost.refreshNavigation;
      window.__kernDelayedRefreshStarted = false;
      window.KernHost.refreshNavigation = () => new Promise(resolve => {
        window.__kernDelayedRefreshStarted = true;
        window.__kernReleaseDelayedRefresh = resolve;
      });
    }""")
    surface.get_by_role("button", name="Save schedule", exact=True).click()
    page.wait_for_function("() => window.__kernDelayedRefreshStarted === true")
    page.get_by_role("button", name="Memory", exact=True).click()
    expect(page).to_have_url(re.compile(r"#memory$"))
    page.evaluate("window.__kernReleaseDelayedRefresh()")
    expect(page).to_have_url(re.compile(r"#memory$"))
    page.evaluate("""() => {
      window.KernHost.refreshNavigation = window.__kernSavedRefreshNavigation;
      delete window.__kernSavedRefreshNavigation;
      delete window.__kernDelayedRefreshStarted;
      delete window.__kernReleaseDelayedRefresh;
    }""")
    page.evaluate("window.location.hash = '#schedules/1'")
    expect(page).to_have_url(re.compile(r"#scheduled-agents/1$"))
    page.go_back()
    expect(page).to_have_url(re.compile(r"#memory$"))
    page.go_forward()
    expect(page).to_have_url(re.compile(r"#scheduled-agents/1$"))
    expect(surface.locator("#global-title")).to_have_text("Scheduled agent")

    trigger_message = "This is an automated trigger.\n\nSummarize open release work."
    chat_index = page.evaluate(
        "() => window.KernHost.api('GET', '/v1/workspace/chat/scheduled-agents')"
    )
    scheduled_item = next(
        item for item in chat_index["threads"] if item["thread_id"] == "schedule-1"
    )
    scheduled_item["latest_event_seq"] = 1
    scheduled_item["latest_message_seq"] = 1
    scheduled_item["seen_message_seq"] = 0
    scheduled_item["status"] = "running"
    index_body = json.dumps(chat_index)
    event_body = json.dumps({
        "events": [{
            "seq": 1,
            "timestamp": "2026-08-07T10:00:00Z",
            "event_type": "thread.message",
            "thread_id": "schedule-1",
            "payload": {"message": trigger_message, "source": "user"},
        }],
    })

    def unread_schedule_index(route: Any) -> None:
        route.fulfill(status=200, content_type="application/json", body=index_body)

    def scheduled_events(route: Any) -> None:
        route.fulfill(status=200, content_type="application/json", body=event_body)

    def mark_schedule_seen(route: Any) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body='{"seen":{"message_seq":1,"revision":0}}',
        )

    page.route("**/v1/workspace/chat/scheduled-agents", unread_schedule_index)
    page.route("**/v1/workspace/chat/threads/schedule-1/events*", scheduled_events)
    page.route("**/v1/workspace/chat/threads/schedule-1/seen", mark_schedule_seen)
    page.evaluate("window.KernHost.refreshNavigation()")
    scheduled_row = page.locator(
        "#scheduled-agents-nav-items [data-action='open-chat'][data-item-id='schedule-1']"
    )
    expect(scheduled_row.locator(".workspace-nav-unseen")).to_have_count(1)
    scheduled_row.click()
    expect(chat.locator("#thread-detail")).to_contain_text(trigger_message)
    expect(scheduled_row.locator(".workspace-nav-unseen")).to_have_count(0)
    page.unroute("**/v1/workspace/chat/scheduled-agents", unread_schedule_index)
    page.unroute("**/v1/workspace/chat/threads/schedule-1/events*", scheduled_events)
    page.unroute("**/v1/workspace/chat/threads/schedule-1/seen", mark_schedule_seen)

    schedule_before_switch = page.evaluate(
        "() => window.KernHost.api('GET', '/v1/workspace/schedules/1')"
    )["schedule"]
    script_update = {
        key: schedule_before_switch[key]
        for key in (
            "name", "message", "cadence", "interval_minutes", "daily_time",
            "agent_runtime", "model", "effort",
        )
    }
    script_update["expected_revision"] = schedule_before_switch["revision"]
    script_update.update({
        "message": "/mnt/kern-agent/agent-home/scripts/release-review.sh",
        "agent_runtime": "script",
        "model": "bash",
        "effort": "fixed",
    })
    page.evaluate(
        "body => window.KernHost.api('PUT', '/v1/workspace/schedules/1', body)",
        script_update,
    )
    page.evaluate("window.KernChat.refresh()")
    expect(chat.locator("#thread-title")).to_have_text("Daily release review")
    expect(chat.locator("#composer")).to_be_hidden()
    expect(chat.locator("#thread-memory")).to_be_hidden()
    expect(page).to_have_url(re.compile(r"#chat/schedule-1$"))
    model_update = {
        **script_update,
        "message": schedule_before_switch["message"],
        "agent_runtime": schedule_before_switch["agent_runtime"],
        "model": schedule_before_switch["model"],
        "effort": schedule_before_switch["effort"],
        "expected_revision": script_update["expected_revision"] + 1,
    }
    page.evaluate(
        "body => window.KernHost.api('PUT', '/v1/workspace/schedules/1', body)",
        model_update,
    )

    page.get_by_role("button", name="Scheduled agents", exact=True).click()
    expect(surface.locator("#global-title")).to_have_text("Schedules")
    expect(page).to_have_url(re.compile(r"#scheduled-agents$"))
    surface.get_by_role("button", name="New schedule", exact=True).click()
    surface.locator("#schedule-runtime").select_option("script")
    surface.locator("#schedule-name").fill("Dependency snapshot")
    surface.locator("#schedule-message").fill(
        "/mnt/kern-agent/agent-home/scripts/dependency-snapshot.sh"
    )
    expect(surface.locator("#schedule-message-label")).to_have_text("Script path")
    expect(surface.locator("#schedule-runtime")).to_have_value("script")
    expect(surface.locator("#schedule-model")).to_have_value("bash")
    expect(surface.locator("#schedule-effort")).to_have_value("fixed")
    surface.locator("#schedule-cadence").select_option("daily")
    surface.locator("#schedule-time").fill("09:00")
    surface.get_by_role("button", name="Save schedule", exact=True).click()
    expect(page).to_have_url(re.compile(r"#chat/schedule-2$"))
    expect(chat.locator("#thread-title")).to_have_text("Dependency snapshot")
    expect(chat.locator("#composer")).to_be_hidden()
    expect(chat.locator("#thread-memory")).to_be_hidden()
    expect(page.locator("#scheduled-agents-nav-items")).to_contain_text(
        "Dependency snapshot"
    )
    page.get_by_role("button", name="Scheduled agents", exact=True).click()
    surface.locator("[data-item-id='2']").click()
    expect(surface.locator("#schedule-name")).to_have_value("Dependency snapshot")
    expect(page).to_have_url(re.compile(r"#scheduled-agents/2$"))
    surface.get_by_role("button", name="Cancel", exact=True).click()
    expect(surface.locator("#global-empty")).to_be_visible()
    expect(page).to_have_url(re.compile(r"#scheduled-agents$"))
    surface.locator("[data-item-id='2']").click()
    expect(surface.locator("#global-title")).to_have_text("Script schedule")
    expect(page.locator("#scheduled-agents-nav-items")).to_contain_text(
        "Dependency snapshot"
    )

    surface.locator("[data-item-id='1']").click()
    expect(surface.locator("#global-title")).to_have_text("Scheduled agent")
    page.evaluate("""() => {
      window.__kernSavedRefreshNavigation = window.KernHost.refreshNavigation;
      window.KernHost.refreshNavigation = async () => {
        throw new Error("navigation unavailable");
      };
    }""")
    page.once("dialog", lambda dialog: dialog.accept())
    surface.get_by_role("button", name="Delete", exact=True).click()
    expect(surface.locator("#global-status")).to_contain_text(
        "Schedule moved to Deleted, but navigation could not refresh: navigation unavailable"
    )
    page.evaluate("""() => {
      window.KernHost.refreshNavigation = window.__kernSavedRefreshNavigation;
      delete window.__kernSavedRefreshNavigation;
    }""")
    page.evaluate("() => window.KernHost.refreshNavigation()")
    expect(page.locator("#scheduled-agents-nav-items")).not_to_contain_text(
        "Daily release review"
    )
    expect(page.locator("#chat-nav-items")).not_to_contain_text(
        "Daily release review"
    )

    # A slow item fetch cannot overwrite a newer host-level navigation after
    # the operator has left the global Workspace panel.
    page.get_by_role("button", name="Memory", exact=True).click()
    page.evaluate(
        """() => {
          const nativeApi = window.KernHost.api;
          window.KernHost.api = (method, path, body) => {
            if (method !== "GET" || path !== "/v1/workspace/memory/pages/release-preferences") {
              return nativeApi(method, path, body);
            }
            return new Promise((resolve, reject) => {
              window.__releaseMemoryDetail = () => {
                window.KernHost.api = nativeApi;
                return nativeApi(method, path, body).then(resolve, reject);
              };
            });
          };
        }"""
    )
    surface.locator("[data-item-id='release-preferences']").click()
    expect(page).to_have_url(re.compile(r"#memory/release-preferences$"))
    page.get_by_role("button", name="Home", exact=True).click()
    page.evaluate("window.__releaseMemoryDetail()")
    page.wait_for_timeout(150)
    expect(page.locator("#panel-home")).to_be_visible()
    expect(page).to_have_url(re.compile(r"#home$"))

    # A slow save that completes after leaving Memory must not replace Home's
    # URL with the item it saved.
    page.get_by_role("button", name="Memory", exact=True).click()
    surface.locator("[data-item-id='release-preferences']").click()
    expect(surface.locator("#memory-page-id")).to_have_value("release-preferences")
    page.evaluate(
        """() => {
          const nativeApi = window.KernHost.api;
          window.KernHost.api = (method, path, body) => {
            if (method !== "PUT" || path !== "/v1/workspace/memory/pages/release-preferences") {
              return nativeApi(method, path, body);
            }
            return new Promise((resolve, reject) => {
              window.__releaseMemorySave = () => {
                window.KernHost.api = nativeApi;
                return nativeApi(method, path, body).then(resolve, reject);
              };
            });
          };
        }"""
    )
    surface.locator("#memory-content").fill("Saved after leaving Memory.")
    surface.get_by_role("button", name="Save page", exact=True).click()
    page.wait_for_function("() => typeof window.__releaseMemorySave === 'function'")
    page.get_by_role("button", name="Home", exact=True).click()
    page.evaluate("window.__releaseMemorySave()")
    page.wait_for_timeout(150)
    expect(page.locator("#panel-home")).to_be_visible()
    expect(page).to_have_url(re.compile(r"#home$"))

    # Restoring a deep link also loads its collection before selecting the
    # item. Leaving during that first request must cancel the later selection.
    page.evaluate(
        """() => {
          const nativeApi = window.KernHost.api;
          window.KernHost.api = (method, path, body) => {
            if (method !== "GET" || !path.startsWith("/v1/workspace/memory?")) {
              return nativeApi(method, path, body);
            }
            return new Promise((resolve, reject) => {
              window.__releaseMemoryRouteList = () => {
                window.KernHost.api = nativeApi;
                return nativeApi(method, path, body).then(resolve, reject);
              };
            });
          };
          history.pushState(
            { kernWorkspaceRoute: "memory", itemId: "release-preferences" },
            "",
            "#memory/release-preferences",
          );
          dispatchEvent(new PopStateEvent("popstate", { state: history.state }));
        }"""
    )
    page.wait_for_function("() => typeof window.__releaseMemoryRouteList === 'function'")
    page.get_by_role("button", name="Home", exact=True).click()
    page.evaluate("window.__releaseMemoryRouteList()")
    page.wait_for_timeout(150)
    expect(page.locator("#panel-home")).to_be_visible()
    expect(page).to_have_url(re.compile(r"#home$"))

    # A transient detail failure preserves the bookmark for retry instead of
    # treating the item as deleted and replacing it with Home.
    page.evaluate(
        """() => {
          const nativeApi = window.KernHost.api;
          window.KernHost.api = (method, path, body) => {
            if (method !== "GET" || path !== "/v1/workspace/memory/pages/release-preferences") {
              return nativeApi(method, path, body);
            }
            window.KernHost.api = nativeApi;
            const error = new Error("Memory is temporarily unavailable.");
            error.status = 503;
            return Promise.reject(error);
          };
          history.pushState(
            { kernWorkspaceRoute: "memory", itemId: "release-preferences" },
            "",
            "#memory/release-preferences",
          );
          dispatchEvent(new PopStateEvent("popstate", { state: history.state }));
        }"""
    )
    expect(page).to_have_url(re.compile(r"#memory/release-preferences$"))
    expect(page.locator("#notice")).to_have_text("Memory is temporarily unavailable.")
    page.get_by_role("button", name="Home", exact=True).click()

    page.evaluate(
        """() => {
          history.pushState(
            { kernWorkspaceRoute: "memory", itemId: "missing-memory-page" },
            "",
            "#memory/missing-memory-page",
          );
          dispatchEvent(new PopStateEvent("popstate", { state: history.state }));
        }"""
    )
    expect(page.locator("#panel-home")).to_be_visible()
    expect(page).to_have_url(re.compile(r"#home$"))
    expect(page.locator("#notice")).to_have_text("That Workspace item is no longer available.")


def mobile_smoke(page: Any) -> None:
    from playwright.sync_api import expect

    page.locator("#mobile-nav-toggle").click()
    page.get_by_role("button", name="Memory", exact=True).click()
    surface = page.locator("#panel-workspace-global")
    expect(surface.locator("#global-title")).to_have_text("Memory")
    expect(surface.locator("#global-list")).to_contain_text("release-preferences")
    overflow = surface.locator(".global-workspace").evaluate(
        "element => element.scrollWidth - element.clientWidth"
    )
    if overflow > 1:
        raise AssertionError(f"global Workspace UI overflows horizontally by {overflow}px")
