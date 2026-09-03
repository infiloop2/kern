-- Persist the agent configuration selected for each Web App independently of
-- whether the operator has sent the next message that starts that session.

-- migrate:up
SET LOCAL search_path TO public;

ALTER TABLE web_apps
    ADD COLUMN agent_runtime TEXT,
    ADD COLUMN agent_model TEXT,
    ADD COLUMN agent_effort TEXT;

-- A Web App's immutable id is also its host thread id. Preserve the exact
-- configuration of every App that has already started an agent session.
UPDATE web_apps AS app
SET agent_runtime = session.agent_runtime,
    agent_model = session.model,
    agent_effort = session.effort
FROM thread_sessions AS session
WHERE session.thread_id = app.app_id;

-- Sessionless legacy Apps get the same pinned fallback used when no runtime
-- is active. Runtime activation is host state and cannot be embedded in SQL.
UPDATE web_apps
SET agent_runtime = 'codex',
    agent_model = 'gpt-5.6-sol',
    agent_effort = 'high'
WHERE agent_runtime IS NULL;

ALTER TABLE web_apps
    ALTER COLUMN agent_runtime SET NOT NULL,
    ALTER COLUMN agent_model SET NOT NULL,
    ALTER COLUMN agent_effort SET NOT NULL,
    ADD CONSTRAINT web_apps_agent_settings_complete CHECK (
        agent_runtime <> '' AND agent_model <> '' AND agent_effort <> ''
    );

-- migrate:down
SET LOCAL search_path TO public;

ALTER TABLE web_apps
    DROP CONSTRAINT web_apps_agent_settings_complete,
    DROP COLUMN agent_effort,
    DROP COLUMN agent_model,
    DROP COLUMN agent_runtime;
