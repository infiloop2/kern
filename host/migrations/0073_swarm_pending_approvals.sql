-- Swarm derives human attention from native pending approvals.
-- migrate:up
SET LOCAL search_path TO public;
ALTER TABLE swarm_agent_ai DROP COLUMN needs_human;
DROP INDEX agent_events_thread_run_seq_idx;
CREATE INDEX tool_approvals_pending_origin_idx ON tool_approvals (origin_thread_id)
    WHERE status = 'pending' AND origin_thread_id IS NOT NULL;
CREATE INDEX pending_pushes_pending_origin_idx ON pending_pushes (origin_thread_id)
    WHERE status = 'pending' AND origin_thread_id IS NOT NULL;

-- migrate:down
SET LOCAL search_path TO public;
DROP INDEX pending_pushes_pending_origin_idx;
DROP INDEX tool_approvals_pending_origin_idx;
ALTER TABLE swarm_agent_ai ADD COLUMN needs_human BOOLEAN;
CREATE INDEX agent_events_thread_run_seq_idx ON agent_events (thread_id, run_number, seq DESC)
    WHERE thread_id IS NOT NULL AND run_number IS NOT NULL
      AND event_type IN ('thread.message', 'thread.error');
