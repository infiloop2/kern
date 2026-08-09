-- migrate:up
SET LOCAL search_path TO public;

-- Versions 0015-0025 are the former Chat and Web App histories. Bootstrap
-- adopts their existing ledger rows into schema_migrations before this
-- unified stream runs, so reaching this point means both legacy schemas are
-- current. Move their durable tables into the admin-owned public schema. This
-- migration gives kern-workspace explicit DML grants after moving the
-- tables; it never receives blanket access to public.
ALTER TABLE app_agent_chat.threads SET SCHEMA public;
ALTER TABLE public.threads RENAME TO chat_threads;

ALTER TABLE app_personal_web_app_builder.web_apps SET SCHEMA public;
ALTER TABLE app_personal_web_app_builder.web_app_history SET SCHEMA public;
ALTER TABLE app_personal_web_app_builder.web_app_memories SET SCHEMA public;
ALTER TABLE app_personal_web_app_builder.web_app_schedules SET SCHEMA public;

-- A down/up cycle may have granted the preceding UX-surface role access to
-- these objects. Remove that access again when returning to Workspace.
DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'kern-ux-surface') THEN
        REVOKE ALL ON
            chat_threads,
            web_apps,
            web_app_history,
            web_app_memories,
            web_app_schedules
        FROM "kern-ux-surface";
        REVOKE ALL ON
            web_app_history_id_seq,
            web_app_memory_revision_seq,
            web_app_schedules_id_seq
        FROM "kern-ux-surface";
        REVOKE USAGE ON SCHEMA
            app_agent_chat,
            app_personal_web_app_builder
        FROM "kern-ux-surface";
    END IF;
END
$$;

ALTER TABLE chat_threads
    ADD CONSTRAINT chat_threads_id_check
    CHECK (thread_id ~ '^thread-[1-9][0-9]*$');
ALTER TABLE web_apps
    ADD CONSTRAINT web_apps_id_check
    CHECK (app_id ~ '^app-[1-9][0-9]*$');

DROP SCHEMA app_agent_chat;
DROP SCHEMA app_personal_web_app_builder;
DROP TABLE workspace_migrations;
DROP TABLE workspace_thread_id_migrations;

GRANT USAGE ON SCHEMA public TO "kern-workspace";
GRANT SELECT, INSERT, UPDATE, DELETE ON
    chat_threads,
    web_apps,
    web_app_history,
    web_app_memories,
    web_app_schedules
TO "kern-workspace";
GRANT USAGE, SELECT, UPDATE ON
    web_app_history_id_seq,
    web_app_memory_revision_seq,
    web_app_schedules_id_seq
TO "kern-workspace";

-- migrate:down
SET LOCAL search_path TO public;

REVOKE ALL ON
    chat_threads,
    web_apps,
    web_app_history,
    web_app_memories,
    web_app_schedules
FROM "kern-workspace";

ALTER TABLE chat_threads DROP CONSTRAINT chat_threads_id_check;
ALTER TABLE web_apps DROP CONSTRAINT web_apps_id_check;
REVOKE USAGE ON SCHEMA public FROM "kern-workspace";
REVOKE ALL ON
    web_app_history_id_seq,
    web_app_memory_revision_seq,
    web_app_schedules_id_seq
FROM "kern-workspace";

-- Recreate the exact id map expected by migration 0014's rollback before
-- restoring the legacy schemas and tables.
CREATE TABLE workspace_thread_id_migrations (
    direct_id TEXT PRIMARY KEY,
    legacy_id TEXT NOT NULL UNIQUE
);
INSERT INTO workspace_thread_id_migrations (direct_id, legacy_id)
SELECT thread_id, 'agent_chat__' || thread_id FROM chat_threads
UNION ALL
SELECT app_id, 'personal_web_app_builder__' || app_id FROM web_apps;

CREATE SCHEMA app_agent_chat AUTHORIZATION "kern-admin";
CREATE SCHEMA app_personal_web_app_builder AUTHORIZATION "kern-admin";

ALTER TABLE chat_threads RENAME TO threads;
ALTER TABLE threads SET SCHEMA app_agent_chat;

ALTER TABLE web_apps SET SCHEMA app_personal_web_app_builder;
ALTER TABLE web_app_history SET SCHEMA app_personal_web_app_builder;
ALTER TABLE web_app_memories SET SCHEMA app_personal_web_app_builder;
ALTER TABLE web_app_schedules SET SCHEMA app_personal_web_app_builder;

-- Hosts rolling back to the immediately preceding UX-surface release still
-- have this role. Restore its exact runtime access; CREATE SCHEMA IF NOT
-- EXISTS in that release cannot repair privileges on schemas restored here.
DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'kern-ux-surface') THEN
        GRANT USAGE ON SCHEMA
            app_agent_chat,
            app_personal_web_app_builder
        TO "kern-ux-surface";
        GRANT SELECT, INSERT, UPDATE, DELETE ON
            app_agent_chat.threads,
            app_personal_web_app_builder.web_apps,
            app_personal_web_app_builder.web_app_history,
            app_personal_web_app_builder.web_app_memories,
            app_personal_web_app_builder.web_app_schedules
        TO "kern-ux-surface";
        GRANT USAGE, SELECT, UPDATE ON
            app_personal_web_app_builder.web_app_history_id_seq,
            app_personal_web_app_builder.web_app_memory_revision_seq,
            app_personal_web_app_builder.web_app_schedules_id_seq
        TO "kern-ux-surface";
    END IF;
END
$$;

CREATE TABLE workspace_migrations (
    workspace_kind TEXT NOT NULL CHECK (workspace_kind IN ('chat', 'web_apps')),
    version BIGINT NOT NULL,
    name TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_kind, version)
);

INSERT INTO workspace_migrations (workspace_kind, version, name) VALUES
    ('chat', 1, 'baseline'),
    ('chat', 2, 'thread_names'),
    ('chat', 3, 'drop_thread_tasks'),
    ('web_apps', 1, 'app_state'),
    ('web_apps', 2, 'builder_thread_reset'),
    ('web_apps', 3, 'multiple_web_apps'),
    ('web_apps', 4, 'workspace_platform'),
    ('web_apps', 5, 'remove_archiving'),
    ('web_apps', 6, 'memory_revision'),
    ('web_apps', 7, 'restore_archiving');
