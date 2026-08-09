-- The host admin API is thread-only: a message either starts a turn or
-- steers the running one, and every app route is scoped by thread id. The
-- per-task ownership ledger existed to authorize task-id actions and to
-- defend against orphaned host tasks from the create-then-record two-step;
-- neither exists anymore, so the table goes.

-- migrate:up
SET LOCAL search_path TO app_agent_chat;

DROP TABLE IF EXISTS thread_tasks;

-- migrate:down
SET LOCAL search_path TO app_agent_chat;

CREATE TABLE IF NOT EXISTS thread_tasks (
    task_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL REFERENCES threads(thread_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_thread_tasks_thread_id ON thread_tasks(thread_id);
