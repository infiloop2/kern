-- migrate:up
ALTER TABLE agent_events
    ADD COLUMN activity JSONB;

-- migrate:down
ALTER TABLE agent_events
    DROP COLUMN activity;
