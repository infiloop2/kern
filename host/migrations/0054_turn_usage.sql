-- migrate:up
SET LOCAL search_path TO public;

-- One row per host execution. Steering keeps the same run_number. No backfill.
CREATE TABLE turn_usage (
    thread_id TEXT NOT NULL,
    run_number BIGINT NOT NULL,
    agent_runtime TEXT NOT NULL,
    model TEXT NOT NULL,
    started_at TEXT NOT NULL,
    measured_at TEXT NOT NULL,
    input_tokens BIGINT CHECK (input_tokens >= 0),
    cached_input_tokens BIGINT CHECK (cached_input_tokens >= 0),
    cache_write_tokens BIGINT CHECK (cache_write_tokens >= 0),
    output_tokens BIGINT CHECK (output_tokens >= 0),
    PRIMARY KEY (thread_id, run_number)
);
CREATE INDEX turn_usage_measured_at_idx ON turn_usage (measured_at);

-- Lifetime totals start at deployment and survive detailed usage retention.
INSERT INTO counters (name, value) VALUES
    ('token_usage_input_tokens', 0),
    ('token_usage_cached_input_tokens', 0),
    ('token_usage_cache_write_tokens', 0),
    ('token_usage_output_tokens', 0);

-- migrate:down
SET LOCAL search_path TO public;
DROP TABLE turn_usage;
DELETE FROM counters WHERE name IN (
    'token_usage_input_tokens', 'token_usage_cached_input_tokens',
    'token_usage_cache_write_tokens', 'token_usage_output_tokens'
);
