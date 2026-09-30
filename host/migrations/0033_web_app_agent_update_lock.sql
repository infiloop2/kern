-- Let the operator temporarily fence agent-authored Web App changes while
-- continuing to use the generated App normally.

-- migrate:up
SET LOCAL search_path TO public;

ALTER TABLE web_apps
    ADD COLUMN agent_updates_locked BOOLEAN NOT NULL DEFAULT FALSE;
