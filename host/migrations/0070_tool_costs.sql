-- Tool code supplies USD costs; the host owns attribution, deduplication, and
-- transactionally maintained UTC-day action totals.
-- migrate:up
SET LOCAL search_path TO public;
CREATE TABLE tool_costs (
    tool_id TEXT NOT NULL,
    connection_id TEXT NOT NULL DEFAULT '',
    charge_id TEXT NOT NULL,
    execution_id TEXT NOT NULL,
    action_id TEXT NOT NULL,
    origin_thread_id TEXT,
    approval_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    amount_nano_usd BIGINT NOT NULL CHECK (amount_nano_usd >= 0 AND amount_nano_usd < 1000000000000000000),
    PRIMARY KEY (tool_id, charge_id)
);
CREATE TABLE tool_cost_daily (
    day DATE NOT NULL,
    tool_id TEXT NOT NULL,
    action_id TEXT NOT NULL,
    amount_nano_usd NUMERIC(30, 0) NOT NULL DEFAULT 0,
    charges BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (day, tool_id, action_id)
);
GRANT SELECT, INSERT ON tool_costs TO "kern-tools";
GRANT SELECT, INSERT, UPDATE ON tool_cost_daily TO "kern-tools";
-- migrate:down
SET LOCAL search_path TO public;
DROP TABLE tool_cost_daily;
DROP TABLE tool_costs;
