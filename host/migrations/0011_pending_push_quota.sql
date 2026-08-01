-- migrate:up
-- The proxy refuses to index another held push when ten rows are pending, so
-- it needs to count the queue it already has INSERT access to.
GRANT SELECT ON pending_pushes TO "kern-proxy";

-- migrate:down
REVOKE SELECT ON pending_pushes FROM "kern-proxy";
