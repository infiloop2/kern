-- migrate:up
ALTER TABLE agent_events ADD COLUMN memory_recall_details TEXT;
