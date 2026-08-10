-- Thread identity is now a product contract: every retained thread belongs to
-- Chat, Apps, or Schedules. Remove old admin-owned history so ordinary reads
-- can operate on one uniform id space without compatibility filtering.

-- migrate:up

DELETE FROM agent_events
WHERE thread_id IS NOT NULL
  AND NOT (
      thread_id ~ '^(app|thread|schedule)-[a-z0-9-]+$'
      AND char_length(thread_id) <= 64
  );

DELETE FROM thread_sessions
WHERE NOT (
    thread_id ~ '^(app|thread|schedule)-[a-z0-9-]+$'
    AND char_length(thread_id) <= 64
);

-- migrate:down

-- Deleted thread history cannot be reconstructed.
SELECT 1;
