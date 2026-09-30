-- Agentic Web App owns multiple independent web-app workspaces. Existing
-- singleton builder state is intentionally discarded: no deployed users rely
-- on it, and a clean table keeps one workspace, one bundle, and one host thread
-- as the durable unit.

-- migrate:up
SET LOCAL search_path TO app_personal_web_app_builder;

DROP TABLE app_state;

CREATE TABLE web_apps (
    thread_id TEXT PRIMARY KEY,
    name TEXT NOT NULL CHECK (char_length(name) BETWEEN 1 AND 100),
    archived BOOLEAN NOT NULL DEFAULT FALSE,
    revision BIGINT NOT NULL DEFAULT 0 CHECK (revision >= 0),
    html TEXT NOT NULL DEFAULT '',
    css TEXT NOT NULL DEFAULT '',
    javascript TEXT NOT NULL DEFAULT '',
    data_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX web_apps_archive_updated_idx
    ON web_apps (archived, updated_at DESC);
