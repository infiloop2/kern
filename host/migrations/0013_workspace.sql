-- migrate:up

-- The built-in Chat and Web App workspaces are no longer generic installed
-- apps. This migration adopts their independent migration ledger; migration
-- 0014 then moves their host threads to the product-owned immutable ids.

ALTER TABLE app_schema_migrations RENAME TO workspace_migrations;
ALTER TABLE workspace_migrations RENAME COLUMN app_id TO workspace_kind;
ALTER TABLE workspace_migrations DROP CONSTRAINT app_schema_migrations_app_id_check;

DELETE FROM workspace_migrations
WHERE workspace_kind NOT IN ('agent_chat', 'personal_web_app_builder');
UPDATE workspace_migrations SET workspace_kind = 'chat' WHERE workspace_kind = 'agent_chat';
UPDATE workspace_migrations
SET workspace_kind = 'web_apps'
WHERE workspace_kind = 'personal_web_app_builder';
ALTER TABLE workspace_migrations
    ADD CONSTRAINT workspace_migrations_workspace_kind_check
    CHECK (workspace_kind IN ('chat', 'web_apps'));

-- Provider-session pointers deliberately remain unchanged. Consolidating the
-- host's Workspace bookkeeping does not invalidate an agent provider's opaque
-- session, and agents can discover the current Workspace tool contract during
-- subsequent work without forcing a cross-provider-style history handoff.

-- migrate:down

ALTER TABLE workspace_migrations
    DROP CONSTRAINT workspace_migrations_workspace_kind_check;
UPDATE workspace_migrations SET workspace_kind = 'agent_chat' WHERE workspace_kind = 'chat';
UPDATE workspace_migrations
SET workspace_kind = 'personal_web_app_builder'
WHERE workspace_kind = 'web_apps';
ALTER TABLE workspace_migrations
    ADD CONSTRAINT app_schema_migrations_app_id_check
    CHECK (workspace_kind ~ '^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$');
ALTER TABLE workspace_migrations RENAME COLUMN workspace_kind TO app_id;
ALTER TABLE workspace_migrations RENAME TO app_schema_migrations;
