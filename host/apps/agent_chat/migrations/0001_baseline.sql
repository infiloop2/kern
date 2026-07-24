-- App-owned thread index for the independent Agent Chat UI. Kern 1.0.0 is a
-- fresh start, so this is Agent Chat's single genesis migration: the final
-- schema directly. Session configuration (runtime, model, effort) belongs to
-- the host; Agent Chat keeps only its own thread index, archive state, and the
-- host-task references each thread has spawned.

-- migrate:up

CREATE TABLE IF NOT EXISTS threads (
    thread_id TEXT PRIMARY KEY,
    archived BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS thread_tasks (
    task_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL REFERENCES threads(thread_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_thread_tasks_thread_id ON thread_tasks(thread_id);

-- migrate:down

DROP TABLE IF EXISTS thread_tasks;
DROP TABLE IF EXISTS threads;
