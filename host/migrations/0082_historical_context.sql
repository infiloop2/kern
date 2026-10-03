-- migrate:up
-- Preserve the bounded session handoff preview shown by context notices.
ALTER TABLE agent_events ADD COLUMN historical_context TEXT;
