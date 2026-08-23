-- Protect the default branch from direct agent pushes. This is independent of
-- the .github approval queue: the proxy rejects the receive-pack transaction
-- immediately and stores no objects or pending row. Existing GitHub settings
-- rows inherit the safe default, while enabled integrations with no settings
-- row are interpreted as protected by the policy layer.

-- migrate:up

ALTER TABLE github_settings
    ADD COLUMN block_direct_main_pushes BOOLEAN NOT NULL DEFAULT TRUE;

-- migrate:down

ALTER TABLE github_settings
    DROP COLUMN block_direct_main_pushes;
