-- Operator-owned sidebar order, shared across browsers on this host.
-- migrate:up
CREATE TABLE workspace_navigation_order (
    item_kind TEXT PRIMARY KEY CHECK (item_kind IN ('apps', 'schedules')),
    item_ids JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(item_ids) = 'array')
);
GRANT SELECT, INSERT, UPDATE ON workspace_navigation_order TO "kern-workspace";

-- migrate:down
DROP TABLE workspace_navigation_order;
