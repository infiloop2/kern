-- Generic tool-owned secret JSON, separate from OAuth account credentials.
-- migrate:up
SET LOCAL search_path TO public;

CREATE TABLE tool_secrets (
    tool_id TEXT PRIMARY KEY,
    value TEXT NOT NULL CHECK (value LIKE 'enc:v1:%' AND octet_length(value) <= 24576)
);
GRANT SELECT, INSERT, UPDATE, DELETE ON tool_secrets TO "kern-tools";

-- migrate:down
SET LOCAL search_path TO public;
DROP TABLE tool_secrets;
