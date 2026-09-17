-- migrate:up
ALTER TABLE agent_events ADD COLUMN memory_recall_details TEXT;

-- migrate:down
ALTER TABLE agent_events DROP COLUMN memory_recall_details;
