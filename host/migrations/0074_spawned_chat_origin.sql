-- Classify delegated Chats without changing their durable thread ids.

-- migrate:up
ALTER TABLE chat_threads ADD COLUMN spawned_by_thread_id TEXT;

-- migrate:down
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM chat_threads WHERE spawned_by_thread_id IS NOT NULL) THEN
        RAISE EXCEPTION 'cannot roll back spawned Chat origin while spawned threads exist';
    END IF;
END
$$;
ALTER TABLE chat_threads DROP COLUMN spawned_by_thread_id;
