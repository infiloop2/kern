-- migrate:up
ALTER TABLE chat_threads ADD COLUMN archived_at TIMESTAMPTZ;

-- Older archives have no recorded archive time. Start their grace period at
-- upgrade rather than guessing from the last message or deleting immediately.
UPDATE chat_threads SET archived_at = CURRENT_TIMESTAMP WHERE archived = TRUE;
