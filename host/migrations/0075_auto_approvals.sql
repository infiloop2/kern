-- One operator-written policy per tool action; review history follows approval retention.
-- migrate:up
SET LOCAL search_path TO public;
CREATE TABLE auto_approval_policies (
    tool_id TEXT NOT NULL,
    action_id TEXT NOT NULL,
    instructions TEXT NOT NULL CHECK (length(btrim(instructions)) BETWEEN 1 AND 8000),
    PRIMARY KEY (tool_id, action_id)
);
CREATE TABLE auto_approval_reviews (
    id BIGSERIAL PRIMARY KEY,
    approval_number BIGINT NOT NULL REFERENCES tool_approvals(number) ON DELETE CASCADE,
    checked_at BIGINT NOT NULL,
    policy TEXT NOT NULL,
    model TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('no_policy', 'left_pending', 'approved', 'failed')),
    reason TEXT NOT NULL,
    approval_error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX auto_approval_reviews_approval ON auto_approval_reviews (approval_number, id DESC);
ALTER TABLE host_inference_usage DROP CONSTRAINT host_inference_usage_model_check;
ALTER TABLE host_inference_usage ADD CHECK (model IN ('gpt-6-luna', 'gpt-6-sol', 'jev'));
-- migrate:down
SET LOCAL search_path TO public;
DELETE FROM host_inference_usage WHERE model = 'gpt-6-sol';
ALTER TABLE host_inference_usage DROP CONSTRAINT host_inference_usage_model_check;
ALTER TABLE host_inference_usage ADD CHECK (model IN ('gpt-6-luna', 'jev'));
DROP TABLE auto_approval_reviews;
DROP TABLE auto_approval_policies;
