-- Daily, provider-local usage counters for host-owned inference. The
-- dedicated inference service records and prunes provider response usage; the
-- admin service reads current-month totals for the operator usage bar.
-- migrate:up
SET LOCAL search_path TO public;

CREATE TABLE host_inference_usage (
    provider TEXT NOT NULL CHECK (provider IN ('openai', 'typesafe')),
    -- Model names are normalized to the small price catalog before storage;
    -- unknown models share one bucket so model churn cannot grow this table.
    model TEXT NOT NULL CHECK (model IN ('gpt-5.6-luna', 'jev', 'other')),
    day DATE NOT NULL,
    requests BIGINT NOT NULL DEFAULT 0 CHECK (requests >= 0),
    measured_requests BIGINT NOT NULL DEFAULT 0 CHECK (measured_requests >= 0),
    priced_requests BIGINT NOT NULL DEFAULT 0 CHECK (priced_requests >= 0),
    input_tokens BIGINT NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
    cached_input_tokens BIGINT NOT NULL DEFAULT 0 CHECK (cached_input_tokens >= 0),
    output_tokens BIGINT NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
    cost_usd NUMERIC(20, 9) NOT NULL DEFAULT 0 CHECK (cost_usd >= 0),
    PRIMARY KEY (provider, model, day)
);

GRANT SELECT, INSERT, UPDATE, DELETE ON host_inference_usage TO "kern-host-inference";

-- migrate:down
SET LOCAL search_path TO public;
DROP TABLE host_inference_usage;
