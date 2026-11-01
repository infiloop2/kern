"""Active agent identities, approval state, and recency-weighted interaction counts."""
from __future__ import annotations

from typing import Any
from datetime import date, datetime, timedelta, timezone

from host.runtime.core import db
from host.runtime.core.state._base import utc_now

# Links within the window are drawn and ranked, each day's count halving per
# half-life of age. Older days are retained for earlier contacts in details.
SWARM_WINDOW_DAYS = 30
SWARM_RETENTION_DAYS = 90
SWARM_HALF_LIFE_DAYS = 7
# Display cap only; ranking metrics always use every link in the window.
SWARM_LINK_LIMIT = 1000


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
    return (today - timedelta(days=SWARM_WINDOW_DAYS - 1)).isoformat(), today.isoformat()


def _retention_start(through: str) -> str:
    return (date.fromisoformat(through) - timedelta(days=SWARM_RETENTION_DAYS - 1)).isoformat()


# Links to archived or deleted agents cannot be drawn, so drop them before any
# cap; otherwise they crowd out links between visible agents. Thread, schedule
# and App ids never collide. Parameters: since, through, retained since and
# half-life, bound once in ``bounds``.
_VISIBLE_LINKS = """
    bounds AS (
        SELECT %s::text AS since, %s::text AS through, %s::text AS retained_since,
               %s::float AS half_life
    ), retired AS (
        SELECT thread_id FROM chat_threads WHERE archived
        UNION ALL
        SELECT app_id FROM web_apps WHERE archived
        UNION ALL
        SELECT thread_id FROM schedules
        WHERE deleted_at IS NOT NULL OR agent_runtime = 'script'
    ), links AS (
        SELECT sender_thread_id AS sender, target_thread_id AS target,
               SUM(interactions) FILTER (WHERE day >= b.since) AS messages,
               SUM(interactions * power(0.5, (b.through::date - day::date) / b.half_life))
                   FILTER (WHERE day >= b.since) AS weight,
               SUM(interactions) AS retained_messages, MAX(day) AS last_day
        FROM swarm_interaction_days CROSS JOIN bounds AS b
        WHERE day >= b.retained_since AND day <= b.through
          AND sender_thread_id NOT IN (SELECT thread_id FROM retired)
          AND target_thread_id NOT IN (SELECT thread_id FROM retired)
        GROUP BY sender_thread_id, target_thread_id
    )
"""


def swarm_interactions() -> dict[str, Any]:
    """Recency-weighted ranking metrics and capped display links, never message bodies.

    Each day's messages and tokens count half as much for every half-life of
    age. Links active in the window are drawn; older retained links are
    returned separately as earlier contacts for details.
    """
    since, through = _interaction_window()
    bounds = (since, through, _retention_start(through), SWARM_HALF_LIFE_DAYS)
    with db.transaction() as cur:
        cur.execute(f"""
            WITH {_VISIBLE_LINKS}
            SELECT sender, target, messages, weight, last_day FROM links
            WHERE messages IS NOT NULL
            ORDER BY weight DESC, messages DESC, sender, target LIMIT %s
        """, (*bounds, SWARM_LINK_LIMIT))
        interactions = [
            {"sender_thread_id": sender, "target_thread_id": target, "count": int(count),
             "weight": round(float(weight), 4), "last_day": last_day}
            for sender, target, count, weight, last_day in cur.fetchall()
        ]
        cur.execute(f"""
            WITH {_VISIBLE_LINKS}
            SELECT sender, target, retained_messages, last_day FROM links
            WHERE messages IS NULL
            ORDER BY last_day DESC, retained_messages DESC, sender, target LIMIT %s
        """, (*bounds, SWARM_LINK_LIMIT))
        earlier = [
            {"sender_thread_id": sender, "target_thread_id": target, "count": int(count), "last_day": last_day}
            for sender, target, count, last_day in cur.fetchall()
        ]
        # Aggregate before the display cap: weak links still count toward breadth
        # and operator attention. Reciprocal links count as one distinct peer,
        # weighted by the most recent contact in either direction.
        cur.execute("""
            WITH bounds AS (
                SELECT %s::text AS since, %s::text AS through, %s::float AS half_life,
                       %s::text AS since_at, %s::text AS until_at
            ), recent AS (
                SELECT sender_thread_id AS sender, target_thread_id AS target,
                       SUM(interactions) AS messages,
                       SUM(interactions * power(0.5, (b.through::date - day::date) / b.half_life)) AS weight,
                       MAX(power(0.5, (b.through::date - day::date) / b.half_life)) AS recency
                FROM swarm_interaction_days CROSS JOIN bounds AS b
                WHERE day >= b.since AND day <= b.through
                GROUP BY sender_thread_id, target_thread_id
            ), endpoints AS (
                SELECT target AS thread_id, sender AS peer,
                       CASE WHEN sender = 'operator' THEN messages ELSE 0 END AS operator_messages,
                       CASE WHEN sender = 'operator' THEN weight ELSE 0 END AS weighted_operator_messages,
                       recency
                FROM recent WHERE sender <> target
                UNION ALL
                SELECT sender, target, 0, 0, recency FROM recent WHERE sender <> target
            ), peers AS (
                SELECT thread_id, peer, SUM(operator_messages) AS operator_messages,
                       SUM(weighted_operator_messages) AS weighted_operator_messages,
                       MAX(recency) AS recency
                FROM endpoints WHERE thread_id NOT IN ('operator', 'kern-host')
                GROUP BY thread_id, peer
            ), communication AS (
                SELECT thread_id, SUM(operator_messages) AS operator_messages,
                       SUM(weighted_operator_messages) AS weighted_operator_messages,
                       COUNT(*) FILTER (WHERE peer NOT IN ('operator', 'kern-host')) AS agent_peers,
                       COALESCE(SUM(recency) FILTER (WHERE peer NOT IN ('operator', 'kern-host')), 0)
                           AS weighted_agent_peers
                FROM peers GROUP BY thread_id
            ), turns AS (
                SELECT thread_id,
                       CASE WHEN input_tokens IS NOT NULL OR cached_input_tokens IS NOT NULL
                                 OR cache_write_tokens IS NOT NULL OR output_tokens IS NOT NULL
                           THEN COALESCE(input_tokens, 0) + COALESCE(cached_input_tokens, 0)
                              + COALESCE(cache_write_tokens, 0) + COALESCE(output_tokens, 0)
                       END AS total,
                       input_tokens IS NULL OR cached_input_tokens IS NULL
                           OR cache_write_tokens IS NULL OR output_tokens IS NULL AS partial,
                       power(0.5, (b.through::date - left(measured_at, 10)::date) / b.half_life) AS decay
                FROM turn_usage CROSS JOIN bounds AS b
                WHERE measured_at >= b.since_at AND measured_at < b.until_at
            ), tokens AS (
                SELECT thread_id, SUM(total) AS total_tokens, SUM(total * decay) AS weighted_tokens,
                       BOOL_OR(partial) AS tokens_partial
                FROM turns GROUP BY thread_id
            )
            SELECT COALESCE(c.thread_id, t.thread_id), COALESCE(c.operator_messages, 0),
                   COALESCE(c.agent_peers, 0), t.total_tokens, t.tokens_partial,
                   COALESCE(c.weighted_operator_messages, 0), COALESCE(c.weighted_agent_peers, 0),
                   t.weighted_tokens
            FROM communication c FULL OUTER JOIN tokens t USING (thread_id)
        """, (since, through, SWARM_HALF_LIFE_DAYS, since + 'T00:00:00Z',
              (date.fromisoformat(through) + timedelta(days=1)).strftime('%Y-%m-%dT00:00:00Z')))
        metrics = {
            thread_id: {"operator_messages": int(messages), "agent_peers": int(peers),
                        "total_tokens": int(tokens) if tokens is not None else None,
                        "tokens_partial": partial if partial is not None else True,
                        "weighted": {"operator_messages": round(float(weighted_messages), 4),
                                     "agent_peers": round(float(weighted_peers), 4),
                                     "total_tokens": round(float(weighted_tokens), 4)
                                     if weighted_tokens is not None else None}}
            for thread_id, messages, peers, tokens, partial,
                weighted_messages, weighted_peers, weighted_tokens in cur.fetchall()
        }
    return {"interactions": interactions, "earlier": earlier, "metrics": metrics,
            "since": since, "through": through}


def record_swarm_interaction(cur: Any, sender_thread_id: str, target_thread_id: str) -> None:
    """Count each accepted delivery in its admission transaction."""
    _, today = _interaction_window()
    cur.execute(
        "INSERT INTO swarm_interaction_days (day, sender_thread_id, target_thread_id, interactions)"
        " VALUES (%s, %s, %s, 1) ON CONFLICT (day, sender_thread_id, target_thread_id)"
        " DO UPDATE SET interactions = swarm_interaction_days.interactions + 1",
        (today, sender_thread_id, target_thread_id),
    )
    cur.execute("DELETE FROM swarm_interaction_days WHERE day < %s", (_retention_start(today),))


def reset_swarm_ai(cur: Any, thread_id: str, run_number: int) -> None:
    cur.execute(
        "INSERT INTO swarm_agent_ai (thread_id, run_number) VALUES (%s, %s)"
        " ON CONFLICT (thread_id) DO UPDATE SET run_number = EXCLUDED.run_number,"
        " task = NULL",
        (thread_id, run_number),
    )


def swarm_task_context(thread_id: str, run_number: int) -> dict[str, Any] | None:
    """Current title, including an empty title, only for an on-demand turn."""
    if not thread_id.startswith('thread-'):
        return None
    with db.transaction() as cur:
        cur.execute(
            "SELECT ai.task FROM swarm_agent_ai ai JOIN chat_threads chat USING (thread_id)"
            " WHERE ai.thread_id = %s AND ai.run_number = %s"
            " AND chat.spawned_by_thread_id IS NULL AND NOT chat.archived",
            (thread_id, run_number),
        )
        row = cur.fetchone()
    return {'task_title': row[0]} if row else None


def save_swarm_task(thread_id: str, run_number: int, task: str, *, only_if_empty: bool = False) -> None:
    with db.transaction() as cur:
        cur.execute(
            "UPDATE swarm_agent_ai SET task = %s WHERE thread_id = %s AND run_number = %s"
            " AND EXISTS (SELECT 1 FROM chat_threads chat WHERE chat.thread_id = swarm_agent_ai.thread_id"
            " AND chat.spawned_by_thread_id IS NULL AND NOT chat.archived)"
            + (" AND task IS NULL" if only_if_empty else ""),
            (task, thread_id, run_number),
        )
