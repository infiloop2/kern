-- migrate:up
CREATE TABLE thread_recovery (
    thread_id TEXT PRIMARY KEY REFERENCES thread_sessions(thread_id) ON DELETE CASCADE,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 5),
    retry_id UUID,
    retry_kind TEXT CHECK (retry_kind IN ('transient', 'limit', 'restart')),
    error_message TEXT CHECK (char_length(error_message) <= 16000),
    due_at TIMESTAMPTZ CHECK (isfinite(due_at)),
    source_runtime TEXT CHECK (source_runtime <> ''),
    source_model TEXT CHECK (source_model <> ''),
    source_effort TEXT CHECK (source_effort <> ''),
    source_run_number BIGINT CHECK (source_run_number >= 0),
    target_runtime TEXT CHECK (target_runtime <> ''),
    CHECK (
        num_nonnulls(retry_id, retry_kind, error_message, due_at, source_runtime,
                     source_model, source_effort, source_run_number, target_runtime) = 0
        OR (num_nonnulls(retry_id, retry_kind, error_message, due_at, source_runtime,
                        source_model, source_effort, source_run_number, target_runtime) = 9
            AND attempts < 5)
    )
);
CREATE INDEX thread_recovery_due ON thread_recovery(due_at) WHERE retry_id IS NOT NULL;
