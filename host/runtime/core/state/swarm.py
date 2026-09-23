"""Current AI fields and bounded text-free peer-delivery history."""
from __future__ import annotations

from typing import Any

from host.runtime.core import db
from host.runtime.core.state._base import utc_now

_AGENT_PAGE_LIMIT = 250
_PEER_DELIVERY_LIMIT = 50


def swarm_snapshot(search: str = "") -> dict[str, Any]:
    """Bound per-agent lookups before returning the agent snapshot."""
    search = search.strip()[:100]
    with db.transaction() as cur:
        cur.execute("""
            WITH identities AS (
                SELECT app_id AS thread_id, 'app' AS kind, name, purpose,
                       agent_runtime, agent_model AS model,
                       NULL::text AS next_run_at
                FROM web_apps WHERE archived = FALSE
                UNION ALL
                SELECT thread_id, 'chat', COALESCE(name, thread_id), '', NULL, NULL, NULL
                FROM chat_threads WHERE archived = FALSE
                  AND EXISTS (SELECT 1 FROM thread_sessions
                              WHERE thread_sessions.thread_id = chat_threads.thread_id)
                UNION ALL
                SELECT thread_id, 'schedule', name, purpose, agent_runtime, model, next_run_at
                FROM schedules WHERE deleted_at IS NULL AND agent_runtime <> 'script'
            ), selected AS MATERIALIZED (
                SELECT identity.*, session.run_status, session.run_number,
                       session.last_used_at, session.agent_runtime AS session_runtime,
                       session.model AS session_model
                FROM identities AS identity
                LEFT JOIN thread_sessions AS session USING (thread_id)
                WHERE %s = '' OR strpos(lower(identity.name), lower(%s)) > 0
                    OR strpos(lower(identity.purpose), lower(%s)) > 0
                    OR EXISTS (SELECT 1 FROM swarm_agent_ai AS ai_search
                               WHERE ai_search.thread_id = identity.thread_id
                                 AND ai_search.run_number = session.run_number
                                 AND strpos(lower(ai_search.task), lower(%s)) > 0)
                ORDER BY CASE WHEN session.run_status = 'running' THEN 0 ELSE 1 END,
                         COALESCE(session.last_used_at, '') DESC, identity.thread_id
                LIMIT %s
            )
            SELECT selected.thread_id, selected.kind, selected.name, selected.purpose,
                   COALESCE(selected.agent_runtime, selected.session_runtime, ''),
                   COALESCE(selected.model, selected.session_model, ''),
                   COALESCE(selected.run_status, 'idle'), selected.next_run_at,
                   ai.task, ai.needs_human,
                   (SELECT event_type FROM agent_events
                    WHERE agent_events.thread_id = selected.thread_id
                    ORDER BY seq DESC LIMIT 1)
            FROM selected
            LEFT JOIN swarm_agent_ai AS ai ON ai.thread_id = selected.thread_id
                AND ai.run_number = selected.run_number
            ORDER BY selected.kind, lower(selected.name), selected.thread_id
        """, (search, search, search, search, _AGENT_PAGE_LIMIT + 1))
        rows = cur.fetchall()
        has_more = len(rows) > _AGENT_PAGE_LIMIT
        agents = [
            {"thread_id": thread_id, "kind": kind, "name": name, "purpose": purpose or "",
             "agent_runtime": runtime, "model": model,
             "state": "busy" if run_status == "running" else "failed" if latest_event_type == "thread.error" else "idle",
             "next_run_at": next_run_at,
             "task": task, "needs_human": needs_human if run_status != "running" else None}
            for thread_id, kind, name, purpose, runtime, model, run_status,
                next_run_at, task, needs_human, latest_event_type in rows[:_AGENT_PAGE_LIMIT]
        ]
    return {"generated_at": utc_now(), "agents": agents, "has_more": has_more}


def swarm_peer_messages() -> dict[str, Any]:
    """Newest peer deliveries only; message bodies stay in thread history."""
    with db.transaction() as cur:
        cur.execute(
            "SELECT event_seq, sender_thread_id, target_thread_id, created_at"
            " FROM swarm_peer_deliveries ORDER BY event_seq DESC LIMIT %s",
            (_PEER_DELIVERY_LIMIT,),
        )
        peer_deliveries = [
            {"seq": seq, "sender_thread_id": sender,
             "target_thread_id": target, "timestamp": created_at}
            for seq, sender, target, created_at in cur.fetchall()
        ]
    return {"messages": peer_deliveries}


def record_swarm_peer_delivery(
    cur: Any, seq: int, sender_thread_id: str, target_thread_id: str,
) -> None:
    """Retain up to 50 authenticated peer deliveries for scene animation."""
    cur.execute(
        "INSERT INTO swarm_peer_deliveries"
        " (event_seq, sender_thread_id, target_thread_id, created_at)"
        " VALUES (%s, %s, %s, %s)",
        (seq, sender_thread_id, target_thread_id, utc_now()),
    )
    cur.execute(
        "DELETE FROM swarm_peer_deliveries WHERE event_seq <="
        " (SELECT event_seq FROM swarm_peer_deliveries ORDER BY event_seq DESC OFFSET %s LIMIT 1)",
        (_PEER_DELIVERY_LIMIT,),
    )


def reset_swarm_ai(cur: Any, thread_id: str, run_number: int) -> None:
    cur.execute(
        "INSERT INTO swarm_agent_ai (thread_id, run_number) VALUES (%s, %s)"
        " ON CONFLICT (thread_id) DO UPDATE SET run_number = EXCLUDED.run_number,"
        " task = NULL, needs_human = NULL",
        (thread_id, run_number),
    )


def swarm_ai_context(thread_id: str, run_number: int) -> dict[str, Any] | None:
    """Current turn only, with both ends of 24 recent messages/errors."""
    with db.transaction() as cur:
        cur.execute(
            "SELECT session.run_status FROM thread_sessions AS session"
            " JOIN swarm_agent_ai AS ai USING (thread_id, run_number)"
            " WHERE session.thread_id = %s AND session.run_number = %s",
            (thread_id, run_number),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cur.execute(
            "SELECT source, CASE WHEN char_length(COALESCE(message, error_message, '')) > 2000"
            " THEN LEFT(COALESCE(message, error_message, ''), 990)"
            " || E'\\n[Middle omitted.]\\n'"
            " || RIGHT(COALESCE(message, error_message, ''), 990)"
            " ELSE COALESCE(message, error_message, '') END"
            " FROM agent_events WHERE thread_id = %s AND run_number = %s"
            " AND event_type IN ('thread.message', 'thread.error')"
            " ORDER BY seq DESC LIMIT 24",
            (thread_id, run_number),
        )
        history = [{"source": source or "host", "text": text} for source, text in reversed(cur.fetchall())]
    return {"run_status": row[0], "messages": history}


def save_swarm_task(thread_id: str, run_number: int, task: str) -> None:
    with db.transaction() as cur:
        cur.execute(
            "UPDATE swarm_agent_ai SET task = %s WHERE thread_id = %s AND run_number = %s",
            (task, thread_id, run_number),
        )


def save_swarm_needs_human(thread_id: str, run_number: int, needs_human: bool) -> None:
    with db.transaction() as cur:
        cur.execute(
            "UPDATE swarm_agent_ai AS ai SET needs_human = %s"
            " WHERE ai.thread_id = %s AND ai.run_number = %s"
            " AND EXISTS (SELECT 1 FROM thread_sessions AS session"
            " WHERE session.thread_id = ai.thread_id AND session.run_number = ai.run_number"
            " AND session.run_status = 'idle')",
            (needs_human, thread_id, run_number),
        )
