-- One durable counter per limited action, reset lazily on the next UTC day.
-- migrate:up
SET LOCAL search_path TO public;

CREATE TABLE tool_action_runs (
    tool_id TEXT NOT NULL,
    action_id TEXT NOT NULL,
    day DATE NOT NULL,
    runs BIGINT NOT NULL CHECK (runs > 0),
    PRIMARY KEY (tool_id, action_id)
);
GRANT SELECT, INSERT, UPDATE ON tool_action_runs TO "kern-tools";
