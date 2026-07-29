-- Durable host error diagnostics added after the thread event migrations.

-- migrate:up

-- Structured unexpected host failures captured from journald. Repeated
-- failures within a short collector window update one row instead of letting
-- a crash loop crowd every other diagnostic out of the bounded log.
CREATE TABLE host_errors (
    id BIGSERIAL PRIMARY KEY,
    -- Stable row identity and newest-first ordering are deliberately
    -- separate: a coalesced repeat rotates seq without invalidating an
    -- operator's already-rendered detail link.
    seq BIGSERIAL UNIQUE NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    service TEXT NOT NULL CHECK (octet_length(service) BETWEEN 1 AND 128),
    component TEXT NOT NULL CHECK (octet_length(component) BETWEEN 1 AND 256),
    kind TEXT NOT NULL CHECK (kind IN ('unexpected_exception', 'service_exit', 'invariant_failure')),
    exception_type TEXT NOT NULL CHECK (octet_length(exception_type) <= 256),
    summary TEXT NOT NULL CHECK (octet_length(summary) <= 2048),
    traceback TEXT NOT NULL CHECK (octet_length(traceback) <= 32768),
    context JSONB NOT NULL DEFAULT '{}'::jsonb,
    fingerprint TEXT NOT NULL CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    occurrence_count BIGINT NOT NULL DEFAULT 1 CHECK (occurrence_count > 0),
    host_version TEXT NOT NULL CHECK (octet_length(host_version) <= 64),
    boot_id TEXT NOT NULL CHECK (octet_length(boot_id) <= 64),
    pid BIGINT CHECK (pid IS NULL OR pid > 0)
);
CREATE INDEX host_errors_fingerprint_last_seen_idx
    ON host_errors (fingerprint, last_seen_at DESC, seq DESC);

-- migrate:down

DROP TABLE host_errors;
