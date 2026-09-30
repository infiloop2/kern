-- The host admin API is thread-only: a message either starts a turn or
-- steers the running one, and every app route is scoped by thread id. The
-- per-task ownership ledger existed to authorize task-id actions and to
-- defend against orphaned host tasks from the create-then-record two-step;
-- neither exists anymore, so the table goes.

-- migrate:up
SET LOCAL search_path TO app_agent_chat;

DROP TABLE IF EXISTS thread_tasks;
