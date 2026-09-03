-- Every schedule delivers into one persistent schedule-N thread. Old
-- disposable run threads and their status rows are intentionally discarded.

-- migrate:up
SET LOCAL search_path TO public;

ALTER TABLE chat_threads DROP CONSTRAINT chat_threads_id_check;
ALTER TABLE chat_threads
    ADD CONSTRAINT chat_threads_id_check
    CHECK (thread_id ~ '^(thread|schedule)-[1-9][0-9]*$');

ALTER TABLE schedules ADD COLUMN thread_id TEXT;
UPDATE schedules SET thread_id = 'schedule-' || id::text;

DELETE FROM agent_events
WHERE thread_id ~ '^schedule-[1-9][0-9]*-run-[1-9][0-9]*$';

DELETE FROM thread_sessions
WHERE thread_id ~ '^schedule-[1-9][0-9]*-run-[1-9][0-9]*$';

DROP TABLE schedule_runs;

INSERT INTO chat_threads (thread_id, name, archived)
SELECT thread_id, name, FALSE FROM schedules;

ALTER TABLE schedules
    ALTER COLUMN thread_id SET NOT NULL,
    ADD CONSTRAINT schedules_thread_id_fkey
    FOREIGN KEY (thread_id) REFERENCES chat_threads (thread_id);
CREATE UNIQUE INDEX schedules_thread_id_idx ON schedules (thread_id);

-- migrate:down
SET LOCAL search_path TO public;

DROP INDEX schedules_thread_id_idx;

DELETE FROM workspace_seen
WHERE item_kind = 'chat'
  AND item_id ~ '^schedule-[1-9][0-9]*$';

DELETE FROM agent_events AS events
USING schedules
WHERE events.thread_id = schedules.thread_id;

DELETE FROM thread_sessions AS sessions
USING schedules
WHERE sessions.thread_id = schedules.thread_id;

ALTER TABLE schedules
    DROP CONSTRAINT schedules_thread_id_fkey,
    DROP COLUMN thread_id;

DELETE FROM chat_threads WHERE thread_id ~ '^schedule-[1-9][0-9]*$';
ALTER TABLE chat_threads DROP CONSTRAINT chat_threads_id_check;
ALTER TABLE chat_threads
    ADD CONSTRAINT chat_threads_id_check
    CHECK (thread_id ~ '^thread-[1-9][0-9]*$');

CREATE TABLE schedule_runs (
    id BIGSERIAL PRIMARY KEY,
    schedule_id BIGINT NOT NULL REFERENCES schedules (id) ON DELETE CASCADE,
    thread_id TEXT NOT NULL UNIQUE
        CHECK (thread_id ~ '^schedule-[1-9][0-9]*-run-[1-9][0-9]*$'),
    message TEXT NOT NULL CHECK (char_length(message) BETWEEN 1 AND 12000),
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
CREATE INDEX schedule_runs_schedule_idx ON schedule_runs (schedule_id, id DESC);
GRANT SELECT, INSERT, UPDATE, DELETE ON schedule_runs TO "kern-workspace";
GRANT USAGE, SELECT, UPDATE ON schedule_runs_id_seq TO "kern-workspace";
