-- One-shot TypeSafe Jev annotations for pending tool approvals. The tools
-- service owns both the approval and its one best-effort assessment; the
-- annotation never participates in the approval state machine.
-- migrate:up
SET LOCAL search_path TO public;

CREATE TABLE tool_approval_risk_assessments (
    approval_number BIGINT PRIMARY KEY REFERENCES tool_approvals(number) ON DELETE CASCADE,
    model TEXT NOT NULL CHECK (model <> '' AND octet_length(model) <= 128),
    assessed_at BIGINT NOT NULL CHECK (assessed_at >= 0),
    scores JSONB NOT NULL CHECK (jsonb_typeof(scores) = 'object')
);

GRANT SELECT, INSERT ON tool_approval_risk_assessments TO "kern-tools";
GRANT SELECT (provider, enabled) ON host_inference_providers TO "kern-tools";

-- migrate:down
SET LOCAL search_path TO public;
REVOKE SELECT (provider, enabled) ON host_inference_providers FROM "kern-tools";
DROP TABLE tool_approval_risk_assessments;
