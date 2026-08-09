-- Monotonic Home history totals. Seed from the complete state currently
-- retained on this host; future writes increment in the same transaction as
-- the thread or event, so audit-log and session pruning never lowers them.

-- migrate:up

INSERT INTO counters (name, value)
VALUES
    ('agent_history_threads', (SELECT COUNT(*) FROM thread_sessions)),
    ('agent_history_messages', (
        SELECT COUNT(*) FROM agent_events WHERE event_type = 'thread.message'
    )),
    ('agent_history_activities', (
        SELECT COUNT(*) FROM agent_events WHERE event_type = 'thread.activity'
    ));

-- migrate:down

DELETE FROM counters
WHERE name IN (
    'agent_history_threads',
    'agent_history_messages',
    'agent_history_activities'
);
