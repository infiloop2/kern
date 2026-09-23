-- Swarm reuses existing identities and runtime state. Only Host AI results
-- and a bounded text-free peer-delivery feed need dedicated storage.
-- migrate:up
SET LOCAL search_path TO public;
-- Retire unsupported usage buckets before enforcing the current model catalog.
DELETE FROM host_inference_usage WHERE model NOT IN ('gpt-6-luna', 'jev');
ALTER TABLE host_inference_usage DROP CONSTRAINT host_inference_usage_model_check;
ALTER TABLE host_inference_usage ADD CONSTRAINT host_inference_usage_model_check
    CHECK (model IN ('gpt-6-luna', 'jev'));

CREATE TABLE swarm_agent_ai (
    thread_id TEXT PRIMARY KEY REFERENCES thread_sessions(thread_id) ON DELETE CASCADE,
    run_number BIGINT NOT NULL CHECK (run_number >= 1),
    task TEXT CHECK (char_length(task) BETWEEN 1 AND 100),
    needs_human BOOLEAN
);
CREATE TABLE swarm_peer_deliveries (
    event_seq BIGINT PRIMARY KEY,
    sender_thread_id TEXT NOT NULL,
    target_thread_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
GRANT SELECT, INSERT, UPDATE, DELETE ON swarm_agent_ai TO "kern-workspace";
GRANT SELECT, INSERT, DELETE ON swarm_peer_deliveries TO "kern-workspace";
CREATE INDEX agent_events_thread_run_seq_idx ON agent_events (thread_id, run_number, seq DESC)
    WHERE thread_id IS NOT NULL AND run_number IS NOT NULL
      AND event_type IN ('thread.message', 'thread.error');
-- migrate:down
-- Keep the current usage catalog on rollback; removed usage cannot be restored.
SET LOCAL search_path TO public;
DROP INDEX agent_events_thread_run_seq_idx;
DROP TABLE swarm_peer_deliveries;
DROP TABLE swarm_agent_ai;
