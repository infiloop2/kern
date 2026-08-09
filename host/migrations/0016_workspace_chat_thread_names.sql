-- Let operators give Agent Chat threads a human-readable display name while
-- keeping thread_id stable as the host session key.

-- migrate:up
SET LOCAL search_path TO app_agent_chat;

ALTER TABLE threads ADD COLUMN IF NOT EXISTS name TEXT;

-- migrate:down
SET LOCAL search_path TO app_agent_chat;

ALTER TABLE threads DROP COLUMN IF EXISTS name;
