-- Keep thread-summary message-tip lookups bounded when a running agent emits
-- a long activity stream after its latest conversation message.

-- migrate:up

CREATE INDEX agent_events_thread_message_seq_idx
ON agent_events (thread_id, seq DESC)
WHERE event_type = 'thread.message';

-- migrate:down

DROP INDEX agent_events_thread_message_seq_idx;
