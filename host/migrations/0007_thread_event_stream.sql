-- Replace public turn-lifecycle events with durable internal thread run state.
-- The public history is now a flat stream of messages, activities, errors,
-- and stops.  ``run_number`` remains storage-only: it namespaces provider
-- activity ids because providers may reuse the same id in later processes.

-- migrate:up

ALTER TABLE thread_sessions
    ADD COLUMN run_status TEXT NOT NULL DEFAULT 'idle'
        CONSTRAINT thread_sessions_run_status_check
        CHECK (run_status IN ('idle', 'running')),
    ADD COLUMN run_number BIGINT NOT NULL DEFAULT 0
        CONSTRAINT thread_sessions_run_number_check
        CHECK (run_number >= 0);

ALTER TABLE agent_events
    ADD COLUMN run_number BIGINT;

-- Give retained history a stable private scope before removing its lifecycle
-- boundaries.  The globally unique start-event seq is also a valid monotonic
-- per-thread run number.
WITH scoped AS (
    SELECT
        seq,
        MAX(CASE WHEN event_type = 'turn.started' THEN seq END)
            OVER (PARTITION BY thread_id ORDER BY seq) AS run_number
    FROM agent_events
    WHERE thread_id IS NOT NULL
)
UPDATE agent_events AS event
SET run_number = scoped.run_number
FROM scoped
WHERE event.seq = scoped.seq;

UPDATE thread_sessions AS session
SET run_number = latest.run_number
FROM (
    SELECT thread_id, COALESCE(MAX(run_number), 0) AS run_number
    FROM agent_events
    WHERE thread_id IS NOT NULL
    GROUP BY thread_id
) AS latest
WHERE session.thread_id = latest.thread_id;

-- A deploy stops the old admin process.  If its newest lifecycle marker is a
-- start, preserve that fact in the new state column so new-code startup emits
-- one thread.error and returns the thread to idle.
WITH latest_lifecycle AS (
    SELECT DISTINCT ON (thread_id)
        thread_id,
        event_type
    FROM agent_events
    WHERE thread_id IS NOT NULL
      AND event_type IN (
          'turn.started',
          'turn.completed',
          'turn.failed',
          'turn.cancelled'
      )
    ORDER BY thread_id, seq DESC
)
UPDATE thread_sessions AS session
SET run_status = 'running'
FROM latest_lifecycle AS latest
WHERE session.thread_id = latest.thread_id
  AND latest.event_type = 'turn.started';

UPDATE agent_events
SET event_type = CASE event_type
    WHEN 'turn.message' THEN 'thread.message'
    WHEN 'turn.activity' THEN 'thread.activity'
    WHEN 'turn.failed' THEN 'thread.error'
    WHEN 'turn.cancelled' THEN 'thread.stopped'
    ELSE event_type
END
WHERE event_type IN (
    'turn.message',
    'turn.activity',
    'turn.failed',
    'turn.cancelled'
);

DELETE FROM agent_events
WHERE event_type IN ('turn.started', 'turn.completed');

-- migrate:down

UPDATE agent_events
SET event_type = CASE event_type
    WHEN 'thread.message' THEN 'turn.message'
    WHEN 'thread.activity' THEN 'turn.activity'
    WHEN 'thread.error' THEN 'turn.failed'
    WHEN 'thread.stopped' THEN 'turn.cancelled'
    ELSE event_type
END
WHERE event_type IN (
    'thread.message',
    'thread.activity',
    'thread.error',
    'thread.stopped'
);

ALTER TABLE agent_events
    DROP COLUMN run_number;

ALTER TABLE thread_sessions
    DROP COLUMN run_status,
    DROP COLUMN run_number;
