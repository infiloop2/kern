-- Classify delegated Chats without changing their durable thread ids.

-- migrate:up
ALTER TABLE chat_threads ADD COLUMN spawned_by_thread_id TEXT;
