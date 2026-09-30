-- migrate:up
ALTER TABLE agent_events
    ADD COLUMN activity JSONB;
