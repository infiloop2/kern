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
