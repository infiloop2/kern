-- The Home Stats row separates human input from agent output. Re-seed the
-- affected counters from retained events; future writes keep them monotonic
-- with the same split even after the event log prunes old rows.

-- migrate:up

UPDATE counters
SET value = (
    SELECT COUNT(*)
    FROM agent_events
    WHERE event_type = 'thread.message' AND source = 'user'
)
WHERE name = 'agent_history_messages';

UPDATE counters
SET value = (
    SELECT COUNT(*)
    FROM agent_events
    WHERE event_type = 'thread.activity'
       OR (event_type = 'thread.message' AND source = 'agent')
)
WHERE name = 'agent_history_activities';

-- migrate:down

UPDATE counters
SET value = (
    SELECT COUNT(*) FROM agent_events WHERE event_type = 'thread.message'
)
WHERE name = 'agent_history_messages';

UPDATE counters
SET value = (
    SELECT COUNT(*) FROM agent_events WHERE event_type = 'thread.activity'
)
WHERE name = 'agent_history_activities';
