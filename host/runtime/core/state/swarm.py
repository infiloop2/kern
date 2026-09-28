"""Current task titles, approval state, and bounded peer-delivery history."""
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
            ), pending_approvals AS (
                SELECT origin_thread_id AS thread_id, COUNT(*) AS approval_count
                FROM (
                    SELECT origin_thread_id FROM tool_approvals WHERE status = 'pending'
                    UNION ALL
                    SELECT origin_thread_id FROM pending_pushes WHERE status = 'pending'
                ) AS approvals
                WHERE origin_thread_id IS NOT NULL
                GROUP BY origin_thread_id
            ), selected AS MATERIALIZED (
                SELECT identity.*, COALESCE(pending_approvals.approval_count, 0) AS approval_count,
                       session.run_status, session.run_number,
                       session.last_used_at, session.agent_runtime AS session_runtime,
                       session.model AS session_model
                FROM identities AS identity
                LEFT JOIN thread_sessions AS session USING (thread_id)
                LEFT JOIN pending_approvals ON pending_approvals.thread_id = identity.thread_id
                WHERE %s = '' OR strpos(lower(identity.name), lower(%s)) > 0
                    OR strpos(lower(identity.purpose), lower(%s)) > 0
                    OR EXISTS (SELECT 1 FROM swarm_agent_ai AS ai_search
                               WHERE ai_search.thread_id = identity.thread_id
                                 AND ai_search.run_number = session.run_number
                                 AND strpos(lower(ai_search.task), lower(%s)) > 0)
                ORDER BY CASE WHEN pending_approvals.approval_count > 0 THEN 0 ELSE 1 END,
                         CASE WHEN session.run_status = 'running' THEN 0 ELSE 1 END,
                         COALESCE(session.last_used_at, '') DESC, identity.thread_id
                LIMIT %s
            )
            SELECT selected.thread_id, selected.kind, selected.name, selected.purpose,
                   COALESCE(selected.agent_runtime, selected.session_runtime, ''),
                   COALESCE(selected.model, selected.session_model, ''),
                   COALESCE(selected.run_status, 'idle'), selected.next_run_at,
                   ai.task, selected.approval_count,
                   (SELECT event_type FROM agent_events
                    WHERE agent_events.thread_id = selected.thread_id
                    ORDER BY seq DESC LIMIT 1)
            FROM selected
            LEFT JOIN swarm_agent_ai AS ai ON ai.thread_id = selected.thread_id
                AND ai.run_number = selected.run_number
            ORDER BY CASE WHEN selected.approval_count > 0 THEN 0 ELSE 1 END,
                     CASE WHEN selected.run_status = 'running' THEN 0 ELSE 1 END,
                     COALESCE(selected.last_used_at, '') DESC, selected.thread_id
        """, (search, search, search, search, _AGENT_PAGE_LIMIT + 1))
        rows = cur.fetchall()
        has_more = len(rows) > _AGENT_PAGE_LIMIT
        agents = [
            {"thread_id": thread_id, "kind": kind, "name": name, "purpose": purpose or "",
             "agent_runtime": runtime, "model": model,
             "state": "busy" if run_status == "running" else "failed" if latest_event_type == "thread.error" else "idle",
             "next_run_at": next_run_at,
             "task": task, "pending_approval_count": approval_count}
            for thread_id, kind, name, purpose, runtime, model, run_status,
                next_run_at, task, approval_count, latest_event_type in rows[:_AGENT_PAGE_LIMIT]
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
        " task = NULL",
        (thread_id, run_number),
    )


def save_swarm_task(thread_id: str, run_number: int, task: str) -> None:
    with db.transaction() as cur:
        cur.execute(
            "UPDATE swarm_agent_ai SET task = %s WHERE thread_id = %s AND run_number = %s",
            (task, thread_id, run_number),
        )
