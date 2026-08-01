-- migrate:up
-- WebSockets are never inferred from GET access. An operator must grant the
-- long-lived opaque channel explicitly on the exact custom-domain rule.
ALTER TABLE allowed_domains
    ADD COLUMN allow_websocket BOOLEAN NOT NULL DEFAULT FALSE;

-- migrate:down
ALTER TABLE allowed_domains DROP COLUMN allow_websocket;
