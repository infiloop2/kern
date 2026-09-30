-- Indexed, bounded lookup of retained host-thread messages. The simple text
-- search configuration keeps code, identifiers, and non-English words intact.

-- migrate:up

CREATE INDEX agent_events_message_search_idx
ON agent_events
USING GIN (to_tsvector('simple', COALESCE(message, '')))
WHERE event_type = 'thread.message';

CREATE INDEX agent_events_message_time_idx
ON agent_events (created_at DESC, seq DESC)
WHERE event_type = 'thread.message';
