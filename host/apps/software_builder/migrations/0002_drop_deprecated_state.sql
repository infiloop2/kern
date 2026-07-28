-- Software Builder is deprecated. Its manifest retains this migration history
-- so upgrades can remove app-owned state without loading an app backend.

-- migrate:up

DROP TABLE IF EXISTS tools;
DROP TABLE IF EXISTS memories;
DROP TABLE IF EXISTS artifacts;
DROP TABLE IF EXISTS schedules;
DROP TABLE IF EXISTS runs;
DROP TABLE IF EXISTS messages;
DROP TABLE IF EXISTS workspace;

-- migrate:down

-- Deprecated app data deletion is intentionally irreversible.
SELECT 1;
