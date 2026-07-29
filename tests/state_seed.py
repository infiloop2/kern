"""Test helpers that read/write whole admin-state snapshots.

The runtime uses per-operation storage accessors (host.runtime.core.state); tests
often want to stage or inspect a complete picture instead. load_state() and
save_state() expose a compact test-facing dict: runtime statuses, the
codex_threads/claude_sessions/hermes_sessions maps with their
provider-specific session keys, and the OAuth records. save_state() replaces
the tables to mirror the dict exactly. Turn history is just events in the
thread-only model: tests seed it with state.append_agent_event and read it
back whole with read_agent_events().
"""

from __future__ import annotations

from typing import Any

from host.session_options import SESSION_OPTIONS
from host.runtime.core import state

_SESSION_MAPS = {
    "codex_threads": ("codex", "codex_thread_id"),
    "claude_sessions": ("claude_code", "session_id"),
    "hermes_sessions": ("hermes", "session_id"),
}
_RUNTIME_MAP_KEYS = {runtime: map_key for map_key, (runtime, _) in _SESSION_MAPS.items()}


def _default_session_options(runtime: str) -> tuple[str, str]:
    model = next(iter(SESSION_OPTIONS[runtime]))
    return model, SESSION_OPTIONS[runtime][model][0]


def load_state() -> dict[str, Any]:
    from host.runtime.core import db

    from host.runtime.admin_api import orchestrator

    snapshot: dict[str, Any] = {
        "agent_runtime_statuses": orchestrator.all_runtime_status_records(),
        "codex_threads": {},
        "claude_sessions": {},
        "hermes_sessions": {},
        "codex_oauth": state.oauth_login("codex"),
        "claude_oauth": state.oauth_login("claude"),
    }
    with db.transaction() as cur:
        cur.execute(
            "SELECT agent_runtime, thread_id, provider_session_id, last_used_at, model, effort"
            " FROM thread_sessions ORDER BY thread_id"
        )
        for runtime, thread_id, provider_session_id, last_used_at, model, effort in cur.fetchall():
            map_key = _RUNTIME_MAP_KEYS[str(runtime)]
            session_key = _SESSION_MAPS[map_key][1]
            mapping: dict[str, Any] = {}
            if last_used_at is not None:
                mapping["last_used_at"] = last_used_at
            if provider_session_id is not None:
                mapping[session_key] = provider_session_id
            mapping["model"] = model
            mapping["effort"] = effort
            snapshot[map_key][str(thread_id)] = mapping
    return snapshot


def save_state(snapshot: dict[str, Any]) -> None:
    from host.runtime.admin_api import orchestrator

    with orchestrator._RUNTIME_STATUS_LOCK:
        orchestrator._RUNTIME_STATUSES.clear()
        for runtime, record in snapshot.get("agent_runtime_statuses", {}).items():
            record = dict(record) if isinstance(record, dict) else {}
            record.setdefault("status", "loading")
            orchestrator._RUNTIME_STATUSES.setdefault(runtime, record)

    with state.mutation() as cur:
        cur.execute("DELETE FROM thread_sessions")
        for map_key, (runtime, session_key) in _SESSION_MAPS.items():
            for thread_id, mapping in snapshot.get(map_key, {}).items():
                mapping = mapping if isinstance(mapping, dict) else {}
                model = str(mapping.get("model") or _default_session_options(runtime)[0])
                effort = str(mapping.get("effort") or _default_session_options(runtime)[1])
                state.save_thread_session(
                    cur,
                    runtime,
                    thread_id,
                    mapping.get(session_key),
                    mapping.get("last_used_at"),
                    model,
                    effort,
                )
        state.set_oauth_login(cur, "codex", snapshot.get("codex_oauth"))
        state.set_oauth_login(cur, "claude", snapshot.get("claude_oauth"))


def read_agent_events() -> list[dict[str, Any]]:
    """Every agent event, oldest first (tests inspect whole logs; the runtime
    only ever pages)."""
    from host.runtime.core import db

    with db.transaction() as cur:
        cur.execute(f"SELECT {state._EVENT_FIELDS} FROM agent_events ORDER BY seq")
        return [state._event_dict(row) for row in cur.fetchall()]


def read_network_events() -> list[dict[str, Any]]:
    """Every network event, oldest first (tests inspect whole logs; the
    runtime only ever pages)."""
    from host.runtime.core import db

    with db.transaction() as cur:
        cur.execute(f"SELECT {state._NETWORK_EVENT_FIELDS} FROM network_events ORDER BY seq")
        return [state._network_event_dict(row) for row in cur.fetchall()]
