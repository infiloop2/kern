-- Move durable memory and scheduled work out of individual generated Web Apps
-- and into the host-global Workspace. The migration intentionally imports only
-- current state: old per-app history, deleted values, and prior executions do
-- not become global history.

-- migrate:up
SET LOCAL search_path TO public;

CREATE TABLE memory_pages (
    page_id TEXT PRIMARY KEY
        CHECK (page_id ~ '^[a-z0-9][a-z0-9-]{0,63}$'),
    description TEXT NOT NULL
        CHECK (char_length(description) BETWEEN 1 AND 100 AND description !~ E'[\r\n]'),
    content TEXT NOT NULL CHECK (char_length(content) <= 1000),
    revision BIGINT NOT NULL DEFAULT 1 CHECK (revision >= 1),
    deleted_at TEXT,
    created_by TEXT NOT NULL CHECK (created_by IN ('user', 'agent', 'migration')),
    updated_by TEXT NOT NULL CHECK (updated_by IN ('user', 'agent', 'migration')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE memory_page_revisions (
    id BIGSERIAL PRIMARY KEY,
    page_id TEXT NOT NULL REFERENCES memory_pages (page_id) ON DELETE CASCADE,
    revision BIGINT NOT NULL CHECK (revision >= 1),
    description TEXT NOT NULL
        CHECK (char_length(description) BETWEEN 1 AND 100 AND description !~ E'[\r\n]'),
    content TEXT NOT NULL CHECK (char_length(content) <= 1000),
    deleted BOOLEAN NOT NULL,
    actor TEXT NOT NULL CHECK (actor IN ('user', 'agent', 'migration')),
    created_at TEXT NOT NULL,
    UNIQUE (page_id, revision)
);

-- Page ids used to be unique only within an app. Keep one reproducible winner:
-- newest update first, then the lowest immutable app id as the tie-breaker.
WITH winners AS (
    SELECT DISTINCT ON (name)
        name AS page_id,
        left(regexp_replace(description, E'[\r\n]+', ' ', 'g'), 100) AS description,
        left(body_md, 1000) AS content,
        created_at,
        updated_at
    FROM web_app_memories
    ORDER BY name, updated_at DESC,
             substring(app_id FROM '^app-([1-9][0-9]*)$')::numeric
), retained AS (
    SELECT * FROM winners ORDER BY updated_at DESC, page_id LIMIT 1000
)
INSERT INTO memory_pages
    (page_id, description, content, revision, deleted_at,
     created_by, updated_by, created_at, updated_at)
SELECT page_id, description, content, 1, NULL,
       'migration', 'migration', created_at, updated_at
FROM retained;

INSERT INTO memory_page_revisions
    (page_id, revision, description, content, deleted, actor, created_at)
SELECT page_id, revision, description, content, FALSE, 'migration', updated_at
FROM memory_pages;

CREATE SEQUENCE schedules_id_seq;

CREATE TABLE schedules (
    id BIGINT PRIMARY KEY DEFAULT nextval('schedules_id_seq'),
    name TEXT NOT NULL CHECK (char_length(name) BETWEEN 1 AND 100 AND name !~ E'[\r\n]'),
    message TEXT NOT NULL CHECK (char_length(message) BETWEEN 1 AND 4000),
    cadence TEXT NOT NULL CHECK (cadence IN ('interval', 'daily')),
    interval_minutes BIGINT
        CHECK (interval_minutes IS NULL OR interval_minutes BETWEEN 5 AND 10080),
    daily_time TEXT
        CHECK (daily_time IS NULL OR daily_time ~ '^([01][0-9]|2[0-3]):[0-5][0-9]$'),
    agent_runtime TEXT NOT NULL CHECK (char_length(agent_runtime) BETWEEN 1 AND 100),
    model TEXT NOT NULL CHECK (char_length(model) BETWEEN 1 AND 100),
    effort TEXT NOT NULL CHECK (char_length(effort) BETWEEN 1 AND 100),
    revision BIGINT NOT NULL DEFAULT 1 CHECK (revision >= 1),
    deleted_at TEXT,
    last_run_at TEXT,
    next_run_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (
        (cadence = 'interval' AND interval_minutes IS NOT NULL AND daily_time IS NULL)
        OR (cadence = 'daily' AND daily_time IS NOT NULL AND interval_minutes IS NULL)
    )
);

ALTER SEQUENCE schedules_id_seq OWNED BY schedules.id;

CREATE TABLE schedule_revisions (
    id BIGSERIAL PRIMARY KEY,
    schedule_id BIGINT NOT NULL REFERENCES schedules (id) ON DELETE CASCADE,
    revision BIGINT NOT NULL CHECK (revision >= 1),
    name TEXT NOT NULL
        CHECK (char_length(name) BETWEEN 1 AND 100 AND name !~ E'[\r\n]'),
    message TEXT NOT NULL CHECK (char_length(message) BETWEEN 1 AND 4000),
    cadence TEXT NOT NULL CHECK (cadence IN ('interval', 'daily')),
    interval_minutes BIGINT
        CHECK (interval_minutes IS NULL OR interval_minutes BETWEEN 5 AND 10080),
    daily_time TEXT
        CHECK (daily_time IS NULL OR daily_time ~ '^([01][0-9]|2[0-3]):[0-5][0-9]$'),
    agent_runtime TEXT NOT NULL CHECK (char_length(agent_runtime) BETWEEN 1 AND 100),
    model TEXT NOT NULL CHECK (char_length(model) BETWEEN 1 AND 100),
    effort TEXT NOT NULL CHECK (char_length(effort) BETWEEN 1 AND 100),
    deleted BOOLEAN NOT NULL,
    actor TEXT NOT NULL CHECK (actor IN ('user', 'agent', 'migration')),
    created_at TEXT NOT NULL,
    UNIQUE (schedule_id, revision),
    CHECK (
        (cadence = 'interval' AND interval_minutes IS NOT NULL AND daily_time IS NULL)
        OR (cadence = 'daily' AND daily_time IS NOT NULL AND interval_minutes IS NULL)
    )
);

CREATE TABLE schedule_runs (
    id BIGSERIAL PRIMARY KEY,
    schedule_id BIGINT NOT NULL REFERENCES schedules (id) ON DELETE CASCADE,
    thread_id TEXT NOT NULL UNIQUE
        CHECK (thread_id ~ '^schedule-[1-9][0-9]*-run-[1-9][0-9]*$'),
    message TEXT NOT NULL CHECK (char_length(message) BETWEEN 1 AND 4000),
    agent_runtime TEXT NOT NULL CHECK (char_length(agent_runtime) BETWEEN 1 AND 100),
    model TEXT NOT NULL CHECK (char_length(model) BETWEEN 1 AND 100),
    effort TEXT NOT NULL CHECK (char_length(effort) BETWEEN 1 AND 100),
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'running', 'succeeded', 'failed', 'stopped')
    ),
    error_message TEXT,
    scheduled_for TEXT NOT NULL,
    finished_at TEXT
);

CREATE UNIQUE INDEX schedule_runs_one_active_idx
    ON schedule_runs (schedule_id) WHERE status IN ('pending', 'running');
CREATE INDEX schedules_due_idx ON schedules (next_run_at)
    WHERE deleted_at IS NULL;
CREATE INDEX schedule_runs_schedule_idx ON schedule_runs (schedule_id, id DESC);
CREATE INDEX memory_pages_updated_idx ON memory_pages (deleted_at, updated_at DESC, page_id);

-- A schedule without a host thread configuration could never fire before the
-- migration. Drop it instead of introducing a third "needs setup" state.
-- The former app target becomes ordinary visible message text; no runtime
-- context prefix is synthesized when the schedule fires.
INSERT INTO schedules
    (id, name, message, cadence, interval_minutes, daily_time,
     agent_runtime, model, effort, revision, deleted_at,
     last_run_at, next_run_at, created_at, updated_at)
SELECT
    schedule.id,
    left(regexp_replace(schedule.name, E'[\r\n]+', ' ', 'g'), 100),
    left('Target Web App: ' || schedule.app_id || E'\n\n' || schedule.message, 4000),
    schedule.cadence,
    schedule.interval_minutes,
    schedule.daily_time,
    session.agent_runtime,
    session.model,
    session.effort,
    1,
    CASE
        WHEN schedule.enabled AND NOT app.archived THEN NULL
        ELSE schedule.updated_at
    END,
    schedule.last_run_at,
    schedule.next_run_at,
    schedule.created_at,
    schedule.updated_at
FROM web_app_schedules AS schedule
JOIN web_apps AS app ON app.app_id = schedule.app_id
JOIN thread_sessions AS session ON session.thread_id = schedule.app_id
ORDER BY schedule.updated_at DESC, schedule.id
LIMIT 100;

SELECT setval(
    'schedules_id_seq',
    GREATEST(
        COALESCE((SELECT MAX(id) FROM schedules), 0),
        COALESCE((SELECT MAX(id) FROM web_app_schedules), 0)
    ) + 1,
    FALSE
);

INSERT INTO schedule_revisions
    (schedule_id, revision, name, message, cadence, interval_minutes, daily_time,
     agent_runtime, model, effort, deleted, actor, created_at)
SELECT id, revision, name, message, cadence, interval_minutes, daily_time,
       agent_runtime, model, effort, deleted_at IS NOT NULL, 'migration', updated_at
FROM schedules;

-- App recovery is UI/data-only from this release onward. Old checkpoint JSON
-- may contain extra keys, but the new restore path deliberately ignores them.
DELETE FROM web_app_history WHERE kind IN ('instructions', 'memory', 'schedule');
ALTER TABLE web_app_history DROP CONSTRAINT web_app_history_kind_check;
ALTER TABLE web_app_history ADD CONSTRAINT web_app_history_kind_check
    CHECK (kind IN ('ui', 'data', 'snapshot', 'checkpoint'));

DROP TABLE web_app_memories;
DROP TABLE web_app_schedules;
ALTER TABLE web_apps DROP COLUMN instructions_updated_at;
ALTER TABLE web_apps DROP COLUMN instructions_updated_by;
ALTER TABLE web_apps DROP COLUMN instructions_md;

GRANT SELECT, INSERT, UPDATE, DELETE ON
    memory_pages,
    memory_page_revisions,
    schedules,
    schedule_revisions,
    schedule_runs
TO "kern-workspace";
GRANT USAGE, SELECT, UPDATE ON
    memory_page_revisions_id_seq,
    schedules_id_seq,
    schedule_revisions_id_seq,
    schedule_runs_id_seq
TO "kern-workspace";

-- migrate:down
SET LOCAL search_path TO public;

REVOKE ALL ON
    memory_pages,
    memory_page_revisions,
    schedules,
    schedule_revisions,
    schedule_runs
FROM "kern-workspace";
REVOKE ALL ON
    memory_page_revisions_id_seq,
    schedules_id_seq,
    schedule_revisions_id_seq,
    schedule_runs_id_seq
FROM "kern-workspace";

ALTER TABLE web_apps ADD COLUMN instructions_md TEXT NOT NULL DEFAULT '';
ALTER TABLE web_apps ADD COLUMN instructions_updated_by TEXT NOT NULL DEFAULT ''
    CHECK (instructions_updated_by IN ('', 'user', 'agent'));
ALTER TABLE web_apps ADD COLUMN instructions_updated_at TEXT NOT NULL DEFAULT '';

CREATE TABLE web_app_schedules (
    id BIGSERIAL PRIMARY KEY,
    app_id TEXT NOT NULL REFERENCES web_apps (app_id) ON DELETE CASCADE,
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

CREATE SEQUENCE web_app_memory_revision_seq;
CREATE TABLE web_app_memories (
    app_id TEXT NOT NULL REFERENCES web_apps (app_id) ON DELETE CASCADE,
    name TEXT NOT NULL CHECK (name ~ '^[a-z0-9][a-z0-9-]{0,63}$'),
    description TEXT NOT NULL CHECK (char_length(description) BETWEEN 1 AND 150),
    body_md TEXT NOT NULL,
    updated_by TEXT NOT NULL CHECK (updated_by IN ('user', 'agent')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    revision BIGINT NOT NULL DEFAULT nextval('web_app_memory_revision_seq')
        CHECK (revision >= 1),
    PRIMARY KEY (app_id, name)
);
ALTER SEQUENCE web_app_memory_revision_seq OWNED BY web_app_memories.revision;

ALTER TABLE web_app_history DROP CONSTRAINT web_app_history_kind_check;
ALTER TABLE web_app_history ADD CONSTRAINT web_app_history_kind_check CHECK (
    kind IN ('ui', 'data', 'snapshot', 'instructions', 'memory', 'schedule', 'checkpoint')
);

GRANT SELECT, INSERT, UPDATE, DELETE ON
    web_app_memories,
    web_app_schedules
TO "kern-workspace";
GRANT USAGE, SELECT, UPDATE ON
    web_app_memory_revision_seq,
    web_app_schedules_id_seq
TO "kern-workspace";

DROP TABLE schedule_runs;
DROP TABLE schedule_revisions;
DROP TABLE schedules;
DROP TABLE memory_page_revisions;
DROP TABLE memory_pages;
