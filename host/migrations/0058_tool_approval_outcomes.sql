-- Remember which host thread requested a tool approval.
-- migrate:up
SET LOCAL search_path TO public;
ALTER TABLE tool_approvals ADD COLUMN origin_thread_id TEXT;
ALTER TABLE pending_pushes ADD COLUMN origin_thread_id TEXT;

-- migrate:down
SET LOCAL search_path TO public;
ALTER TABLE pending_pushes DROP COLUMN origin_thread_id;
ALTER TABLE tool_approvals DROP COLUMN origin_thread_id;
