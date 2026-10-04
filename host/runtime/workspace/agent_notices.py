"""Compact, resource-specific outcomes for meaningful Kern actions.

Reads and generic MCP calls stay in Activity. Recording is best effort: a
failed notice must never turn a completed mutation into a retryable failure.
"""
from __future__ import annotations

from collections import Counter
import json
import re
from typing import Any
from urllib.parse import unquote

from host.agent_messages import compact
from host.runtime.core import db, host_errors
from host.runtime.workspace.host_api import call_admin_api


def thread_name(thread_id: str) -> str:
    """Resolve a display name without making delivery depend on decoration."""
    match = re.fullmatch(r"(thread|app|schedule)-[1-9][0-9]*", thread_id)
    if match is None:
        return "agent"
    table, key, fallback = {
        "thread": ("chat_threads", "thread_id", "Chat agent"),
        "app": ("web_apps", "app_id", "App"),
        "schedule": ("schedules", "thread_id", "standing agent"),
    }[match[1]]
    try:
        # Deleted/archived resources still have names for their final notices.
        with db.transaction() as cur:
            cur.execute(f"SELECT name FROM {table} WHERE {key} = %s", (thread_id,))
            row = cur.fetchone()
        return compact(row[0], 50) if row and isinstance(row[0], str) and row[0].strip() else fallback
    except Exception as exc:
        host_errors.report_warning("workspace.notice_name", exc, context={"thread_id": thread_id})
        return fallback


def _text(value: Any, fallback: str = "") -> str:
    return compact(value, 100) if isinstance(value, str) and value.strip() else fallback


def _changes(operations: Any) -> str:
    """Counts and a few data paths make batches useful without dumping values."""
    if not isinstance(operations, list):
        return ""
    ops = [op for op in operations if isinstance(op, dict)]
    counts = Counter(op.get("action") for op in ops if isinstance(op.get("action"), str))
    labels = {"set": "set", "append": "appended", "delete": "deleted", "upsert": "saved"}
    parts = [f"{count} {labels[action]}" for action, count in counts.items() if action in labels]
    paths = []
    for op in ops:
        path = op.get("path")
        if isinstance(path, list):
            label = compact(".".join(str(segment) for segment in path), 45)
            if label and label not in paths:
                paths.append(label)
    suffix = ", ".join(paths[:3]) + (f" and {len(paths) - 3} more" if len(paths) > 3 else "")
    return ", ".join(parts) + (f" ({suffix})" if suffix else "")


def action_summary(method: str, path: str, body: Any, response: dict[str, Any],
                   error: Exception | None) -> tuple[str, str] | None:
    """The allowlist and text for each action live together here."""
    request = body if isinstance(body, dict) else {}
    name: str
    kind: str
    label: str
    if method == "POST" and path == "/agent/messages":
        name = thread_name(_text(request.get("thread_id")))
        kind, label = "agent_message_sent", f"Sent message to {name}"
        preview = _text(request.get("message"))
        if preview:
            label += f": {preview}"
    elif method == "POST" and path == "/agent/agents":
        name = _text(request.get("name"), "agent")
        kind, label = "agent_spawned", f"Started {name}"
        model = _text(request.get("model"))
        preview = _text(request.get("message"))
        if model:
            label += f" ({model})"
        if preview:
            label += f": {preview}"
    elif method == "POST" and path == "/agent/agents/archive":
        kind, label = "agent_archived", f"Archived {thread_name(_text(request.get('thread_id')))}"
    elif method == "PUT" and path == "/agent/self/memory":
        kind, label = "self_memory_saved", "Saved self memory"
        description = _text(request.get("description"))
        if description:
            label += f": {description}"
    elif method in {"PUT", "DELETE"} and (match := re.fullmatch(r"/agent/memory/pages/([^/]+)", path)):
        name = unquote(match[1]).replace("-", " ")
        verb = "Saved" if method == "PUT" else "Deleted"
        kind = "shared_memory_saved" if method == "PUT" else "shared_memory_deleted"
        label = f"{verb} {name} memory"
    elif (method == "POST" and path == "/agent/schedules") or (
        method in {"PUT", "DELETE"} and re.fullmatch(r"/agent/schedules/[1-9][0-9]*", path)
    ):
        name = _text(request.get("name"))
        if not name:
            name = thread_name("schedule-" + path.rsplit("/", 1)[-1])
        verb = {"POST": "Created", "PUT": "Updated", "DELETE": "Deleted"}[method]
        kind = {"POST": "standing_agent_created", "PUT": "standing_agent_updated", "DELETE": "standing_agent_deleted"}[method]
        label = f"{verb} standing agent {name}"
        triggers = request.get("triggers")
        if isinstance(triggers, list):
            count = len(triggers)
            label += f": {count} {'trigger' if count == 1 else 'triggers'}"
            if count == 1 and isinstance(triggers[0], dict):
                label += ": " + _text(triggers[0].get("prompt"))
    elif method == "POST" and path == "/agent/apps":
        app = response.get("app", {})
        name = _text(app.get("name"), "App") if isinstance(app, dict) else "App"
        kind, label = "app_created", f"Created {name}"
    elif method in {"PUT", "POST"} and (match := re.fullmatch(r"/agent/apps/(app-[1-9][0-9]*)(/.*)", path)):
        app_id, resource = match.groups()
        # Select supported writes before querying a name. Reads never create notices.
        if method == "PUT" and resource == "/name":
            kind, label = "app_renamed", f"Renamed App to {_text(request.get('name'), 'App')}"
        elif method == "PUT" and resource == "/agent-settings":
            kind, label = "app_agent_updated", f"Updated {thread_name(app_id)} agent"
            settings = " · ".join(_text(request.get(key)) for key in ("agent_runtime", "model", "effort") if _text(request.get(key)))
            if settings:
                label += f": {settings}"
        elif method == "POST" and resource == "/actions":
            name = thread_name(app_id)
            if request.get("action") == "publish_ui":
                kind, label = "app_ui_published", f"Published {name} UI"
                changes = _changes(request.get("data_operations"))
            else:
                kind, label = "app_data_changed", f"Changed {name} data"
                changes = _changes(request.get("operations") if request.get("action") == "batch" else [request])
            if changes:
                label += f": {changes}"
        elif method == "POST" and (collection := re.fullmatch(r"/collections/([^/]+)/actions", resource)):
            kind, label = "app_collection_changed", f"Updated {thread_name(app_id)} / {unquote(collection[1])}"
            changes = _changes(request.get("operations"))
            if changes:
                label += f": {changes}"
        else:
            return None
    else:
        return None
    if error is not None:
        # Prefix first, so truncating a long resource/preview can never hide failure.
        label = f"Failed: {label}. {_text(str(error))}"
    return kind, compact(label)


def record(caller: str | None, method: Any, path: Any, body: Any,
           result: dict[str, Any] | None, error: Exception | None) -> None:
    if caller is None or not isinstance(method, str) or not isinstance(path, str):
        return
    try:
        response = (result or {}).get("body", {})
        action = action_summary(method.upper(), path.split("?", 1)[0], body, response, error)
        if action is None:
            return
        kind, summary = action
        # Outcome first: even a huge request must leave its error/result visible.
        details = summary + "\n\n" + json.dumps({
            **({"error": str(error)} if error else {"result": response}),
            "request": {"method": method, "path": path, "body": body},
        }, ensure_ascii=False, indent=2)
        if len(json.dumps(details).encode()) > 20_000:
            low, high = 0, len(details)
            while low < high:
                mid = (low + high + 1) // 2
                if len(json.dumps(details[:mid]).encode()) <= 19_900:
                    low = mid
                else:
                    high = mid - 1
            details = details[:low] + "\n… [details truncated]"
        call_admin_api("POST", f"/v1/threads/{caller}/notices", {
            "kind": kind, "summary": summary, "details": details,
        })
    except Exception as exc:
        host_errors.report_warning("workspace.agent_notice", exc, context={"thread_id": caller})
