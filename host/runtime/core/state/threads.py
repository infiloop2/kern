"""Thread, provider-session, and run lifecycle state."""

from __future__ import annotations

from typing import Any

from host.runtime.core import db
from host.runtime.core.state._base import _AGENT_HISTORY_COUNTERS, _read

# -- threads --------------------------------------------------------------------


def _increment_counter(cur: Any, name: str) -> None:
    cur.execute(
        "UPDATE counters SET value = value + 1 WHERE name = %s RETURNING value",
        (name,),
    )
    if cur.fetchone() is None:
        raise RuntimeError(f"counter {name!r} is not initialized")


def agent_history_counts() -> dict[str, int]:
    """Monotonic thread, user-message, and agent-activity totals shown on Home."""
    names = tuple(_AGENT_HISTORY_COUNTERS.values())
    with db.transaction() as cur:
        cur.execute(
            "SELECT name, value FROM counters WHERE name IN (%s, %s, %s)",
            names,
        )
        stored = {str(name): int(value) for name, value in cur.fetchall()}
    if stored.keys() != set(names):
        missing = sorted(set(names) - stored.keys())
        raise RuntimeError(f"agent history counters are not initialized: {missing}")
    return {field: stored[name] for field, name in _AGENT_HISTORY_COUNTERS.items()}


def page_thread_summaries(
    before: tuple[str, str] | None,
    limit: int,
    *,
    thread_prefix: str | None = None,
) -> list[dict[str, Any]]:
    """One newest-first keyset page of canonical thread session rows.

    ``before`` is the last page's ``(last_used_at, thread_id)`` sort key. A
    caller may request a prefix filter as a query optimization; it conveys no
    ownership or authorization.
    """
    clauses: list[str] = []
    params: list[Any] = []
    if thread_prefix is not None:
        clauses.append("LEFT(thread_id, %s) = %s")
        params.extend((len(thread_prefix), thread_prefix))
    if before is not None:
        clauses.append("(COALESCE(last_used_at, ''), thread_id) < (%s, %s)")
        params.extend(before)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    with db.transaction() as cur:
        cur.execute(
            "SELECT thread_id, agent_runtime, model, effort, last_used_at, run_status,"
            " COALESCE((SELECT seq FROM agent_events"
            " WHERE agent_events.thread_id = thread_sessions.thread_id"
            " ORDER BY seq DESC LIMIT 1), 0),"
            " COALESCE((SELECT seq FROM agent_events"
            " WHERE agent_events.thread_id = thread_sessions.thread_id"
            " AND event_type = 'thread.message'"
            " ORDER BY seq DESC LIMIT 1), 0)"
            f" FROM thread_sessions{where}"
            " ORDER BY COALESCE(last_used_at, '') DESC, thread_id DESC LIMIT %s",
            (*params, limit),
        )
        return [
            {
                "thread_id": str(thread_id),
                "agent_runtime": agent_runtime,
                "model": model,
                "effort": effort,
                "last_used_at": last_used_at or "",
                "status": str(run_status),
                "latest_event_seq": int(latest_event_seq),
                "latest_message_seq": int(latest_message_seq),
            }
            for (
                thread_id,
                agent_runtime,
                model,
                effort,
                last_used_at,
                run_status,
                latest_event_seq,
                latest_message_seq,
            ) in cur.fetchall()
        ]


def latest_thread_event_seqs(thread_id: str) -> tuple[int, int]:
    """Newest retained event and message sequences from one database snapshot."""
    with db.transaction() as cur:
        cur.execute(
            "SELECT"
            " COALESCE((SELECT seq FROM agent_events WHERE thread_id = %s"
            " ORDER BY seq DESC LIMIT 1), 0),"
            " COALESCE((SELECT seq FROM agent_events WHERE thread_id = %s"
            " AND event_type = 'thread.message' ORDER BY seq DESC LIMIT 1), 0)",
            (thread_id, thread_id),
        )
        row = cur.fetchone()
    return (int(row[0]), int(row[1])) if row is not None else (0, 0)


def recover_interrupted_thread_runs(cur: Any) -> list[tuple[str, int]]:
    """Return stale running threads to idle and return their private run ids.

    The admin process is the sole run owner, so every persisted ``running`` row
    at its startup belongs to a process that died before it could settle.
    """
    cur.execute(
        "UPDATE thread_sessions SET run_status = 'idle'"
        " WHERE run_status = 'running'"
        " RETURNING thread_id, run_number"
    )
    return [(str(thread_id), int(run_number)) for thread_id, run_number in cur.fetchall()]


# -- thread -> provider session maps ---------------------------------------------


def save_thread_session(
    cur: Any,
    runtime: str,
    thread_id: str,
    provider_session_id: str | None,
    last_used_at: str | None,
    model: str,
    effort: str,
) -> None:
    # The admin process is the sole thread-session writer and callers hold the
    # mutation lock, so this pre-read and the insert/update below are one
    # creation decision. Count only the first durable row for a thread.
    cur.execute("SELECT 1 FROM thread_sessions WHERE thread_id = %s", (thread_id,))
    creating = cur.fetchone() is None
    cur.execute(
        "INSERT INTO thread_sessions (agent_runtime, thread_id, provider_session_id, last_used_at, model, effort)"
        " VALUES (%s, %s, %s, %s, %s, %s)"
        " ON CONFLICT (thread_id) DO UPDATE SET"
        " provider_session_id = EXCLUDED.provider_session_id,"
        " last_used_at = EXCLUDED.last_used_at"
        " WHERE thread_sessions.agent_runtime = EXCLUDED.agent_runtime"
        " AND thread_sessions.model = EXCLUDED.model"
        " AND thread_sessions.effort = EXCLUDED.effort"
        " RETURNING 1",
        (runtime, thread_id, provider_session_id, last_used_at, model, effort),
    )
    if cur.fetchone() is None:
        raise ValueError(f"thread {thread_id!r} already has another session configuration")
    if creating:
        _increment_counter(cur, _AGENT_HISTORY_COUNTERS["threads"])


def rotate_thread_session(
    cur: Any,
    thread_id: str,
    runtime: str,
    model: str,
    effort: str,
    last_used_at: str,
) -> None:
    """Replace one idle thread's provider configuration.

    Clearing the provider session makes the next run a new provider
    conversation. The idle predicate is the durable race fence: a caller
    cannot switch configuration underneath admitted work.
    """
    cur.execute(
        "UPDATE thread_sessions SET"
        " agent_runtime = %s,"
        " provider_session_id = NULL,"
        " last_used_at = %s,"
        " model = %s,"
        " effort = %s"
        " WHERE thread_id = %s AND run_status = 'idle'"
        " RETURNING 1",
        (runtime, last_used_at, model, effort, thread_id),
    )
    if cur.fetchone() is None:
        raise ValueError(f"thread {thread_id!r} is running or does not exist")


def clear_thread_context(
    cur: Any,
    thread_id: str,
    cleared_seq: int,
    last_used_at: str,
) -> None:
    """Drop one idle thread's provider session and fence its handoff history.

    The next run opens a new provider conversation. ``cleared_seq`` becomes the
    handoff floor so that run is not handed back the events the operator just
    cleared; without it, the missing provider session would itself trigger the
    replay. Nothing is deleted: the events stay readable and only stop being
    forwarded. The idle predicate is the durable race fence, and the floor
    never moves backwards, so a stale caller cannot restore cleared context.
    """
    cur.execute(
        "UPDATE thread_sessions SET"
        " provider_session_id = NULL,"
        " context_cleared_seq = GREATEST(context_cleared_seq, %s),"
        " last_used_at = %s"
        " WHERE thread_id = %s AND run_status = 'idle'"
        " RETURNING 1",
        (cleared_seq, last_used_at, thread_id),
    )
    if cur.fetchone() is None:
        raise ValueError(f"thread {thread_id!r} is running or does not exist")


def save_thread_provider_session(
    cur: Any,
    thread_id: str,
    run_number: int,
    provider_session_id: str,
) -> None:
    """Persist a provider-confirmed non-empty session for exactly one run.

    Matching ``run_number`` prevents a late callback from an old process from
    replacing a newer run's mapping. The lifecycle may already be durably idle
    while its process is finishing, so this deliberately does not require
    ``run_status = 'running'``.
    """
    if not isinstance(provider_session_id, str) or not provider_session_id.strip():
        raise ValueError("provider session id must not be empty")
    provider_session_id = provider_session_id.strip()
    cur.execute(
        "UPDATE thread_sessions SET provider_session_id = %s"
        " WHERE thread_id = %s AND run_number = %s"
        " RETURNING 1",
        (provider_session_id, thread_id, run_number),
    )
    if cur.fetchone() is None:
        raise ValueError(f"thread {thread_id!r} run {run_number} does not exist")


def clear_thread_provider_session(
    cur: Any,
    thread_id: str,
    run_number: int,
    provider_session_id: str,
) -> None:
    """Clear one provider session after the provider confirms it is missing."""
    cur.execute(
        "UPDATE thread_sessions SET provider_session_id = NULL"
        " WHERE thread_id = %s AND run_number = %s AND provider_session_id = %s"
        " RETURNING 1",
        (thread_id, run_number, provider_session_id),
    )
    if cur.fetchone() is None:
        raise ValueError(
            f"thread {thread_id!r} run {run_number} no longer has provider session"
            f" {provider_session_id!r}"
        )


def touch_thread_session(cur: Any, thread_id: str, last_used_at: str) -> None:
    """Refresh one existing thread's recency without changing its provider
    session or current runtime configuration."""
    cur.execute(
        "UPDATE thread_sessions SET last_used_at = %s"
        " WHERE thread_id = %s RETURNING 1",
        (last_used_at, thread_id),
    )
    if cur.fetchone() is None:
        raise ValueError(f"thread {thread_id!r} has no session configuration")


def start_thread_run(cur: Any, thread_id: str) -> int:
    """Atomically mark an idle thread running and allocate its private scope."""
    cur.execute(
        "UPDATE thread_sessions"
        " SET run_status = 'running', run_number = run_number + 1"
        " WHERE thread_id = %s AND run_status = 'idle'"
        " RETURNING run_number",
        (thread_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise ValueError(f"thread {thread_id!r} is already running or does not exist")
    return int(row[0])


def finish_thread_run(cur: Any, thread_id: str, run_number: int) -> None:
    """Return exactly the matching live run to idle."""
    cur.execute(
        "UPDATE thread_sessions SET run_status = 'idle'"
        " WHERE thread_id = %s AND run_status = 'running' AND run_number = %s"
        " RETURNING 1",
        (thread_id, run_number),
    )
    if cur.fetchone() is None:
        raise ValueError(f"thread {thread_id!r} run {run_number} is not running")


def thread_session_config(thread_id: str, cur: Any = None) -> dict[str, Any] | None:
    with _read(cur) as cur:
        cur.execute(
            "SELECT agent_runtime, provider_session_id, last_used_at, model, effort,"
            " run_status, run_number, context_cleared_seq"
            " FROM thread_sessions WHERE thread_id = %s",
            (thread_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "agent_runtime": str(row[0]),
        "provider_session_id": row[1],
        "last_used_at": row[2],
        "model": str(row[3]),
        "effort": str(row[4]),
        "status": str(row[5]),
        "run_number": int(row[6]),
        "context_cleared_seq": int(row[7]),
    }


def prune_thread_sessions(cur: Any, runtime: str, keep: int) -> None:
    """Drop least-recently-used unreferenced threads beyond ``keep``.

    A thread with retained events keeps its canonical row so its history stays
    listed; once event retention drops a thread's last event, the ordinary LRU
    cap applies.
    """
    cur.execute(
        "DELETE FROM thread_sessions AS candidate"
        " WHERE candidate.agent_runtime = %s"
        " AND NOT EXISTS (SELECT 1 FROM agent_events WHERE agent_events.thread_id = candidate.thread_id)"
        " AND candidate.thread_id NOT IN ("
        "  SELECT retained.thread_id FROM thread_sessions AS retained"
        "  WHERE retained.agent_runtime = %s"
        "  AND NOT EXISTS (SELECT 1 FROM agent_events WHERE agent_events.thread_id = retained.thread_id)"
        "  ORDER BY retained.last_used_at DESC NULLS LAST, retained.thread_id LIMIT %s)",
        (runtime, runtime, keep),
    )
