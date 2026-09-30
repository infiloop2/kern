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
