-- Let the operator temporarily fence agent-authored Web App changes while
-- continuing to use the generated App normally.

-- migrate:up
SET LOCAL search_path TO public;

ALTER TABLE web_apps
    ADD COLUMN agent_updates_locked BOOLEAN NOT NULL DEFAULT FALSE;

-- migrate:down
SET LOCAL search_path TO public;

ALTER TABLE web_apps DROP COLUMN agent_updates_locked;
