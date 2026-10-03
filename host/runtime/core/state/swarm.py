"""Active agent identities, approval state, and weekly interaction counts."""
from __future__ import annotations

from typing import Any
from datetime import datetime, timedelta, timezone

from host.runtime.core import db
from host.runtime.core.state._base import utc_now

def is_on_demand_agent(thread_id: str) -> bool:
    if not thread_id.startswith("thread-"):
        return False
    with db.transaction() as cur:
        cur.execute("SELECT spawned_by_thread_id IS NULL FROM chat_threads WHERE thread_id = %s", (thread_id,))
        row = cur.fetchone()
    return bool(row and row[0])


def swarm_snapshot(search: str = "") -> dict[str, Any]:
    """All active agents, including idle and disconnected nodes."""
    search = search.strip()[:100]
    with db.transaction() as cur:
        cur.execute("""
            WITH identities AS (
                SELECT app_id AS thread_id, 'app' AS kind, name, purpose,
                       agent_runtime, agent_model AS model,
                       NULL::text AS next_run_at
                FROM web_apps WHERE archived = FALSE
                UNION ALL
                SELECT thread_id, CASE WHEN spawned_by_thread_id IS NULL THEN 'on-demand' ELSE 'spawned' END,
                       COALESCE(name, thread_id), '', NULL, NULL, NULL
                FROM chat_threads WHERE archived = FALSE
                  AND EXISTS (SELECT 1 FROM thread_sessions
                              WHERE thread_sessions.thread_id = chat_threads.thread_id)
                UNION ALL
                SELECT thread_id, 'standing', name, purpose, agent_runtime, model, next_run_at
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
            )
            SELECT selected.thread_id, selected.kind, selected.name, selected.purpose,
                   COALESCE(selected.agent_runtime, selected.session_runtime, ''),
                   COALESCE(selected.model, selected.session_model, ''),
                   COALESCE(selected.run_status, 'idle'), selected.next_run_at,
                   CASE WHEN selected.kind = 'on-demand' THEN ai.task END, selected.approval_count,
                   (SELECT spawned_by_thread_id FROM chat_threads WHERE chat_threads.thread_id = selected.thread_id),
                   (SELECT event_type FROM agent_events
                    WHERE agent_events.thread_id = selected.thread_id
                    ORDER BY seq DESC LIMIT 1)
            FROM selected
            LEFT JOIN swarm_agent_ai AS ai ON ai.thread_id = selected.thread_id
                AND ai.run_number = selected.run_number
            ORDER BY CASE WHEN selected.approval_count > 0 THEN 0 ELSE 1 END,
                     CASE WHEN selected.run_status = 'running' THEN 0 ELSE 1 END,
                     COALESCE(selected.last_used_at, '') DESC, selected.thread_id
        """, (search, search, search, search))
        rows = cur.fetchall()
        agents = [
            {"thread_id": thread_id, "kind": kind, "name": name, "purpose": purpose or "",
             "agent_runtime": runtime, "model": model,
             "state": "busy" if run_status == "running" else "failed" if latest_event_type == "thread.error" else "idle",
             "next_run_at": next_run_at,
             "task": task, "pending_approval_count": approval_count, "spawned_by_thread_id": parent}
            for thread_id, kind, name, purpose, runtime, model, run_status,
                next_run_at, task, approval_count, parent, latest_event_type in rows
        ]
    return {"generated_at": utc_now(), "agents": agents, "has_more": False}


def _interaction_window() -> tuple[str, str]:
    today = datetime.now(timezone.utc).date()
    return (today - timedelta(days=6)).isoformat(), today.isoformat()


def swarm_interactions() -> dict[str, Any]:
    """Weekly ranking metrics and up to 500 display links, never message bodies."""
    since, through = _interaction_window()
    with db.transaction() as cur:
        cur.execute(
            "SELECT sender_thread_id, target_thread_id, SUM(interactions) AS message_count"
            " FROM swarm_interaction_days WHERE day >= %s AND day <= %s"
            " GROUP BY sender_thread_id, target_thread_id"
            " ORDER BY message_count DESC, sender_thread_id, target_thread_id LIMIT 500",
            (since, through),
        )
        interactions = [
            {"sender_thread_id": sender, "target_thread_id": target, "count": int(count)}
            for sender, target, count in cur.fetchall()
        ]
        # Aggregate before the display cap: weak links still count toward breadth
        # and operator attention. Reciprocal links count as one distinct peer.
        cur.execute("""
            WITH weekly AS (
                SELECT sender_thread_id AS sender, target_thread_id AS target,
                       SUM(interactions) AS messages
                FROM swarm_interaction_days WHERE day >= %s AND day <= %s
                GROUP BY sender_thread_id, target_thread_id
            ), endpoints AS (
                SELECT target AS thread_id, sender AS peer,
                       CASE WHEN sender = 'operator' THEN messages ELSE 0 END AS operator_messages
                FROM weekly WHERE sender <> target
                UNION ALL
                SELECT sender, target, 0 FROM weekly WHERE sender <> target
            ), communication AS (
                SELECT thread_id, SUM(operator_messages) AS operator_messages,
                       COUNT(DISTINCT peer) FILTER (WHERE peer <> 'operator') AS agent_peers
                FROM endpoints WHERE thread_id <> 'operator' GROUP BY thread_id
            ), tokens AS (
                SELECT thread_id,
                       SUM(CASE WHEN input_tokens IS NOT NULL OR cached_input_tokens IS NOT NULL
                                      OR cache_write_tokens IS NOT NULL OR output_tokens IS NOT NULL
                           THEN COALESCE(input_tokens, 0) + COALESCE(cached_input_tokens, 0)
                              + COALESCE(cache_write_tokens, 0) + COALESCE(output_tokens, 0)
                           END) AS total_tokens,
                       BOOL_OR(input_tokens IS NULL OR cached_input_tokens IS NULL
                               OR cache_write_tokens IS NULL OR output_tokens IS NULL) AS tokens_partial
                FROM turn_usage WHERE measured_at >= %s AND measured_at < %s
                GROUP BY thread_id
            )
            SELECT COALESCE(c.thread_id, t.thread_id), COALESCE(c.operator_messages, 0),
                   COALESCE(c.agent_peers, 0), t.total_tokens, t.tokens_partial
            FROM communication c FULL OUTER JOIN tokens t USING (thread_id)
        """, (since, through, since + 'T00:00:00Z',
              (datetime.fromisoformat(through) + timedelta(days=1)).strftime('%Y-%m-%dT00:00:00Z')))
        metrics = {
            thread_id: {"operator_messages": int(messages), "agent_peers": int(peers),
                        "total_tokens": int(tokens) if tokens is not None else None,
                        "tokens_partial": partial if partial is not None else True}
            for thread_id, messages, peers, tokens, partial in cur.fetchall()
        }
    return {"interactions": interactions, "metrics": metrics, "since": since, "through": through}


def record_swarm_interaction(cur: Any, sender_thread_id: str, target_thread_id: str) -> None:
    """Count each accepted delivery in its admission transaction."""
    since, today = _interaction_window()
    cur.execute(
        "INSERT INTO swarm_interaction_days (day, sender_thread_id, target_thread_id, interactions)"
        " VALUES (%s, %s, %s, 1) ON CONFLICT (day, sender_thread_id, target_thread_id)"
        " DO UPDATE SET interactions = swarm_interaction_days.interactions + 1",
        (today, sender_thread_id, target_thread_id),
    )
    cur.execute("DELETE FROM swarm_interaction_days WHERE day < %s", (since,))


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
