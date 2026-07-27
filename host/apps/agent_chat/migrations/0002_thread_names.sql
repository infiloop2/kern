-- Let operators give Agent Chat threads a human-readable display name while
-- keeping thread_id stable as the host session key.

-- migrate:up

ALTER TABLE threads ADD COLUMN IF NOT EXISTS name TEXT;

-- migrate:down

ALTER TABLE threads DROP COLUMN IF EXISTS name;
