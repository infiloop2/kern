-- migrate:up
-- Every Kern transcript notice has a finite kind and compact display summary.
-- source=user is retained only on input actually delivered to the provider.
ALTER TABLE agent_events ADD COLUMN notice JSONB;

-- Old context events were already host-authored. Keep their bounded details
-- and event ids, changing only the presentation envelope.
UPDATE agent_events SET event_type = 'thread.notice', notice = jsonb_build_object(
    'kind', CASE
        WHEN historical_context IS NOT NULL OR message = 'Historical context transferred.' THEN 'history_transfer'
        WHEN message = 'Additional memories suggested.' THEN 'memory_suggestion'
        ELSE 'memory_injection' END,
    'summary', COALESCE(message, 'Context added.')
) WHERE event_type = 'thread.context_added';

-- Legacy user messages have no trustworthy origin metadata. Leave them intact;
-- no runtime prefix guessing supplies origin metadata that was never recorded.
-- New delivered notices participate in every existing message index.
DROP INDEX agent_events_message_search_idx;
CREATE INDEX agent_events_message_search_idx ON agent_events
USING GIN (to_tsvector('simple', COALESCE(message, '')))
WHERE event_type = 'thread.message' OR (event_type = 'thread.notice' AND source = 'user');
DROP INDEX agent_events_message_time_idx;
CREATE INDEX agent_events_message_time_idx ON agent_events (created_at DESC, seq DESC)
WHERE event_type = 'thread.message' OR (event_type = 'thread.notice' AND source = 'user');
DROP INDEX agent_events_thread_message_seq_idx;
CREATE INDEX agent_events_thread_message_seq_idx ON agent_events (thread_id, seq DESC)
WHERE event_type = 'thread.message' OR (event_type = 'thread.notice' AND source = 'user');
DROP INDEX agent_events_message_seq_idx;
CREATE INDEX agent_events_message_seq_idx ON agent_events (seq DESC)
WHERE (event_type = 'thread.message' OR (event_type = 'thread.notice' AND source = 'user'))
    AND message IS NOT NULL;
