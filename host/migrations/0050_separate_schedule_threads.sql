-- Chat and schedules own separate Workspace indexes. Their transcripts still
-- share the host-owned agent_events and thread_sessions tables.

-- migrate:up
SET LOCAL search_path TO public;

ALTER TABLE schedules DROP CONSTRAINT schedules_thread_id_fkey;
ALTER TABLE schedules
    ADD CONSTRAINT schedules_thread_id_check
    CHECK (thread_id ~ '^schedule-[1-9][0-9]*$');

DELETE FROM chat_threads WHERE thread_id ~ '^schedule-[1-9][0-9]*$';
ALTER TABLE chat_threads DROP CONSTRAINT chat_threads_id_check;
ALTER TABLE chat_threads
    ADD CONSTRAINT chat_threads_id_check
    CHECK (thread_id ~ '^thread-[1-9][0-9]*$');

-- migrate:down
SET LOCAL search_path TO public;

ALTER TABLE chat_threads DROP CONSTRAINT chat_threads_id_check;
ALTER TABLE chat_threads
    ADD CONSTRAINT chat_threads_id_check
    CHECK (thread_id ~ '^(thread|schedule)-[1-9][0-9]*$');

INSERT INTO chat_threads (thread_id, name, archived)
SELECT thread_id, name, FALSE FROM schedules;

ALTER TABLE schedules
    DROP CONSTRAINT schedules_thread_id_check,
    ADD CONSTRAINT schedules_thread_id_fkey
    FOREIGN KEY (thread_id) REFERENCES chat_threads (thread_id);
