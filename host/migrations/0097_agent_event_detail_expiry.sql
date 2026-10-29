-- migrate:up
-- Cleared events leave this index. Maintenance can find the next age-eligible
-- batch without repeatedly scanning up to 100 million compact history rows.
CREATE INDEX agent_events_detail_expiry_idx ON agent_events (created_at, seq)
WHERE activity ? 'detail' OR activity ? 'output' OR notice ? 'details'
    OR memory_recall_details IS NOT NULL OR historical_context IS NOT NULL;
