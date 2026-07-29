-- Thread-only admin model: tasks and queuing are removed. A message to an
-- idle thread starts a turn immediately (or is rejected when the runtime is
-- at capacity); a message to a busy thread steers the running turn. The event
-- log becomes the single durable record of a thread's history, so events gain
-- a direct thread_id and the task tables are dropped.
--
-- History notes: events of tasks already pruned from history keep a NULL
-- thread_id (they were unreachable through the thread view before this
-- migration too, and remain visible in the global event page). Tasks still
-- queued at migration time never produced events and are dropped with the
-- table; pending steers of a running task die with the restart that this
-- deploy performs anyway.

-- migrate:up

ALTER TABLE agent_events
    ADD COLUMN thread_id TEXT;

UPDATE agent_events SET thread_id = tasks.thread_id
    FROM tasks
    WHERE agent_events.task_id = 'task_' || tasks.number;

UPDATE agent_events SET event_type = 'turn.' || substring(event_type FROM 6)
    WHERE event_type LIKE 'task.%';

ALTER TABLE agent_events
    DROP COLUMN task_id;

CREATE INDEX agent_events_thread_id_idx ON agent_events (thread_id, seq);

DROP TABLE task_steers;
DROP TABLE tasks;
DELETE FROM counters WHERE name = 'next_task_number';

-- migrate:down

-- The task rows themselves are unrecoverable; recreate the empty structures
-- so older code finds its schema.
ALTER TABLE agent_events
    ADD COLUMN task_id TEXT;

UPDATE agent_events SET event_type = 'task.' || substring(event_type FROM 6)
    WHERE event_type LIKE 'turn.%';

DROP INDEX agent_events_thread_id_idx;
ALTER TABLE agent_events
    DROP COLUMN thread_id;
CREATE INDEX agent_events_task_id_idx ON agent_events (task_id, seq);

CREATE TABLE tasks (
    number BIGINT PRIMARY KEY,
    status TEXT NOT NULL,
    thread_id TEXT NOT NULL REFERENCES thread_sessions (thread_id),
    input_message TEXT,
    output_message TEXT,
    error_message TEXT,
    created_at TEXT,
    updated_at TEXT
);
CREATE INDEX tasks_status_idx ON tasks (status);
CREATE INDEX tasks_thread_id_idx ON tasks (thread_id);
CREATE INDEX tasks_status_updated_idx ON tasks (status, updated_at, number);

CREATE TABLE task_steers (
    id BIGSERIAL PRIMARY KEY,
    task_number BIGINT NOT NULL REFERENCES tasks (number) ON DELETE CASCADE,
    message TEXT NOT NULL
);
CREATE INDEX task_steers_task_number_idx ON task_steers (task_number, id);
