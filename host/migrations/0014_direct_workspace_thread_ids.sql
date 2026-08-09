-- Chat and Apps now own their host thread ids directly. Their immutable
-- product ids already carry distinct prefixes (`thread-` and `app-`), so the
-- former admin-owned namespace layer is redundant. Visible workspace ids, names,
-- events, and workspace data do not change.

-- migrate:up
SET LOCAL search_path TO public;

DO $$
BEGIN
    IF EXISTS (
        WITH old_ids AS (
            SELECT DISTINCT
                thread_id AS source_id,
                CASE
                    WHEN left(thread_id, length('agent_chat__')) = 'agent_chat__'
                        THEN substring(thread_id FROM length('agent_chat__') + 1)
                    ELSE substring(thread_id FROM length('personal_web_app_builder__') + 1)
                END AS direct_id
            FROM (
                SELECT thread_id FROM thread_sessions
                UNION ALL
                SELECT thread_id FROM agent_events WHERE thread_id IS NOT NULL
            ) AS old_threads
            WHERE left(thread_id, length('agent_chat__')) = 'agent_chat__'
               OR left(thread_id, length('personal_web_app_builder__')) =
                  'personal_web_app_builder__'
        ), existing_direct_ids AS (
            SELECT thread_id
            FROM thread_sessions
            WHERE left(thread_id, length('agent_chat__')) <> 'agent_chat__'
              AND left(thread_id, length('personal_web_app_builder__')) <>
                  'personal_web_app_builder__'
            UNION
            SELECT thread_id
            FROM agent_events
            WHERE thread_id IS NOT NULL
              AND left(thread_id, length('agent_chat__')) <> 'agent_chat__'
              AND left(thread_id, length('personal_web_app_builder__')) <>
                  'personal_web_app_builder__'
        )
        SELECT 1 FROM old_ids
        JOIN existing_direct_ids AS existing
          ON existing.thread_id = old_ids.direct_id
    ) THEN
        RAISE EXCEPTION 'direct workspace thread id collides with existing host state';
    END IF;
    IF EXISTS (
        WITH old_ids AS (
            SELECT DISTINCT
                thread_id AS source_id,
                CASE
                    WHEN left(thread_id, length('agent_chat__')) = 'agent_chat__'
                        THEN substring(thread_id FROM length('agent_chat__') + 1)
                    ELSE substring(thread_id FROM length('personal_web_app_builder__') + 1)
                END AS direct_id
            FROM (
                SELECT thread_id FROM thread_sessions
                UNION ALL
                SELECT thread_id FROM agent_events WHERE thread_id IS NOT NULL
            ) AS old_threads
            WHERE left(thread_id, length('agent_chat__')) = 'agent_chat__'
               OR left(thread_id, length('personal_web_app_builder__')) =
                  'personal_web_app_builder__'
        )
        SELECT 1
        FROM old_ids
        GROUP BY direct_id
        HAVING count(*) > 1
    ) THEN
        RAISE EXCEPTION 'legacy workspace thread ids collide after prefix removal';
    END IF;
END
$$;

-- Retain an exact map until the legacy workspace migrations are consolidated
-- in 0026. This makes a manual rollback restore only ids this migration
-- changed; unrelated host threads named thread-* or app-* stay untouched.
CREATE TABLE workspace_thread_id_migrations (
    direct_id TEXT PRIMARY KEY,
    legacy_id TEXT NOT NULL UNIQUE
);

INSERT INTO workspace_thread_id_migrations (direct_id, legacy_id)
SELECT DISTINCT
    CASE
        WHEN left(thread_id, length('agent_chat__')) = 'agent_chat__'
            THEN substring(thread_id FROM length('agent_chat__') + 1)
        ELSE substring(thread_id FROM length('personal_web_app_builder__') + 1)
    END,
    thread_id
FROM (
    SELECT thread_id FROM thread_sessions
    UNION ALL
    SELECT thread_id FROM agent_events WHERE thread_id IS NOT NULL
) AS old_threads
WHERE left(thread_id, length('agent_chat__')) = 'agent_chat__'
   OR left(thread_id, length('personal_web_app_builder__')) =
      'personal_web_app_builder__';

-- Provider-session pointers deliberately remain unchanged. This migration
-- renames Kern's durable thread key only; it does not invalidate the opaque
-- provider session associated with that thread. Existing agents discover the
-- current Workspace API without sacrificing their provider context or cache.

UPDATE agent_events
SET thread_id = CASE
    WHEN left(thread_id, length('agent_chat__')) = 'agent_chat__'
        THEN substring(thread_id FROM length('agent_chat__') + 1)
    ELSE substring(thread_id FROM length('personal_web_app_builder__') + 1)
END
WHERE left(thread_id, length('agent_chat__')) = 'agent_chat__'
   OR left(thread_id, length('personal_web_app_builder__')) =
      'personal_web_app_builder__';

UPDATE thread_sessions
SET thread_id = CASE
    WHEN left(thread_id, length('agent_chat__')) = 'agent_chat__'
        THEN substring(thread_id FROM length('agent_chat__') + 1)
    ELSE substring(thread_id FROM length('personal_web_app_builder__') + 1)
END
WHERE left(thread_id, length('agent_chat__')) = 'agent_chat__'
   OR left(thread_id, length('personal_web_app_builder__')) =
      'personal_web_app_builder__';

-- migrate:down
SET LOCAL search_path TO public;

UPDATE agent_events AS events
SET thread_id = mapping.legacy_id
FROM workspace_thread_id_migrations AS mapping
WHERE events.thread_id = mapping.direct_id;

UPDATE thread_sessions AS sessions
SET thread_id = mapping.legacy_id
FROM workspace_thread_id_migrations AS mapping
WHERE sessions.thread_id = mapping.direct_id;

DROP TABLE workspace_thread_id_migrations;
