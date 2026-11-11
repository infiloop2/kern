-- GET /agent/usage runs in kern-workspace, which had no access to turn_usage.
-- migrate:up
SET LOCAL search_path TO public;

GRANT SELECT ON turn_usage TO "kern-workspace";
