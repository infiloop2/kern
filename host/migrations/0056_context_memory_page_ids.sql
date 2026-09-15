-- migrate:up
ALTER TABLE agent_events
    ADD COLUMN memory_page_ids JSONB CHECK (jsonb_typeof(memory_page_ids) = 'array');

-- migrate:down
ALTER TABLE agent_events
    DROP COLUMN memory_page_ids;
