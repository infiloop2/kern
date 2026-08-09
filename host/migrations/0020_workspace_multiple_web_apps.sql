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

-- migrate:down
SET LOCAL search_path TO app_personal_web_app_builder;

DROP TABLE web_apps;

CREATE TABLE app_state (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    revision BIGINT NOT NULL DEFAULT 0 CHECK (revision >= 0),
    html TEXT NOT NULL DEFAULT '',
    css TEXT NOT NULL DEFAULT '',
    javascript TEXT NOT NULL DEFAULT '',
    data_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL,
    thread_seq BIGINT NOT NULL DEFAULT 1 CHECK (thread_seq >= 1)
);

INSERT INTO app_state (singleton, updated_at)
VALUES (TRUE, '1970-01-01T00:00:00Z');
