-- migrate:up
ALTER TABLE web_apps
    ADD COLUMN last_runtime_report TEXT CHECK (octet_length(last_runtime_report) <= 400),
    ADD COLUMN last_runtime_report_at TEXT,
    ADD COLUMN collection_grants JSONB NOT NULL DEFAULT '{}'::jsonb;
