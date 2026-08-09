-- The workspace platform splits the single revision counter into a UI
-- revision (bumped only when the interface bundle is replaced) and a data
-- version (bumped and checked on every data write), and adds four durable
-- systems: always-on instructions, queryable memories, scheduled agent calls,
-- and a restorable history of UI replacements and data operations.
-- Existing workspaces carry over: the old revision becomes the UI revision and
-- data writes start a fresh version chain.

-- migrate:up
SET LOCAL search_path TO app_personal_web_app_builder;

ALTER TABLE web_apps RENAME COLUMN revision TO ui_revision;
ALTER TABLE web_apps ADD COLUMN data_version BIGINT NOT NULL DEFAULT 0
    CHECK (data_version >= 0);
ALTER TABLE web_apps ADD COLUMN instructions_md TEXT NOT NULL DEFAULT '';
ALTER TABLE web_apps ADD COLUMN instructions_updated_by TEXT NOT NULL DEFAULT ''
    CHECK (instructions_updated_by IN ('', 'user', 'agent'));
ALTER TABLE web_apps ADD COLUMN instructions_updated_at TEXT NOT NULL DEFAULT '';

CREATE TABLE web_app_history (
    id BIGSERIAL PRIMARY KEY,
    thread_id TEXT NOT NULL REFERENCES web_apps (thread_id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (
        kind IN ('ui', 'data', 'snapshot', 'instructions', 'memory', 'schedule', 'checkpoint')
    ),
    actor TEXT NOT NULL CHECK (actor IN ('agent', 'app', 'user')),
    ui_revision BIGINT NOT NULL CHECK (ui_revision >= 0),
    data_version BIGINT NOT NULL CHECK (data_version >= 0),
    entry_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX web_app_history_thread_idx
    ON web_app_history (thread_id, id DESC);

-- Existing workspaces get restore anchors matching their current state, the
-- same seed a new workspace receives at creation.
INSERT INTO web_app_history
    (thread_id, kind, actor, ui_revision, data_version, entry_json, created_at)
SELECT thread_id, 'ui', 'user', ui_revision, data_version,
       json_build_object('html', html, 'css', css, 'javascript', javascript)::text,
       updated_at
FROM web_apps;

INSERT INTO web_app_history
    (thread_id, kind, actor, ui_revision, data_version, entry_json, created_at)
SELECT thread_id, 'snapshot', 'user', ui_revision, data_version,
       json_build_object('data', data_json::json)::text,
       updated_at
FROM web_apps;

CREATE TABLE web_app_schedules (
    id BIGSERIAL PRIMARY KEY,
    thread_id TEXT NOT NULL REFERENCES web_apps (thread_id) ON DELETE CASCADE,
    name TEXT NOT NULL CHECK (char_length(name) BETWEEN 1 AND 100),
    message TEXT NOT NULL,
    cadence TEXT NOT NULL CHECK (cadence IN ('interval', 'daily')),
    interval_minutes BIGINT
        CHECK (interval_minutes IS NULL OR interval_minutes BETWEEN 5 AND 10080),
    daily_time TEXT
        CHECK (daily_time IS NULL OR daily_time ~ '^([01][0-9]|2[0-3]):[0-5][0-9]$'),
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_by TEXT NOT NULL CHECK (created_by IN ('user', 'agent')),
    last_run_at TEXT,
    next_run_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (
        (cadence = 'interval' AND interval_minutes IS NOT NULL AND daily_time IS NULL)
        OR (cadence = 'daily' AND daily_time IS NOT NULL AND interval_minutes IS NULL)
    )
);

CREATE INDEX web_app_schedules_due_idx
    ON web_app_schedules (enabled, next_run_at);

CREATE TABLE web_app_memories (
    thread_id TEXT NOT NULL REFERENCES web_apps (thread_id) ON DELETE CASCADE,
    name TEXT NOT NULL CHECK (name ~ '^[a-z0-9][a-z0-9-]{0,63}$'),
    description TEXT NOT NULL CHECK (char_length(description) BETWEEN 1 AND 150),
    body_md TEXT NOT NULL,
    updated_by TEXT NOT NULL CHECK (updated_by IN ('user', 'agent')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (thread_id, name)
);

-- migrate:down
SET LOCAL search_path TO app_personal_web_app_builder;

DROP TABLE web_app_memories;
DROP TABLE web_app_schedules;
DROP TABLE web_app_history;
ALTER TABLE web_apps DROP COLUMN instructions_updated_at;
ALTER TABLE web_apps DROP COLUMN instructions_updated_by;
ALTER TABLE web_apps DROP COLUMN instructions_md;
ALTER TABLE web_apps DROP COLUMN data_version;
ALTER TABLE web_apps RENAME COLUMN ui_revision TO revision;
