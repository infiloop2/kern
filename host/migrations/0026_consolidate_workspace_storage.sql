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
