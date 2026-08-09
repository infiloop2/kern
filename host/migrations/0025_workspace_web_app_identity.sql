-- A Web App has its own immutable app id (`app-1`, `app-2`, ...). It also
-- happens to use that id for its host conversation thread, but app identity is
-- no longer modelled as thread ownership throughout Workspace storage.

-- migrate:up
SET LOCAL search_path TO app_personal_web_app_builder;

ALTER TABLE web_apps RENAME COLUMN thread_id TO app_id;
ALTER TABLE web_app_history RENAME COLUMN thread_id TO app_id;
ALTER TABLE web_app_memories RENAME COLUMN thread_id TO app_id;
ALTER TABLE web_app_schedules RENAME COLUMN thread_id TO app_id;

ALTER INDEX web_app_history_thread_idx RENAME TO web_app_history_app_idx;

-- migrate:down
SET LOCAL search_path TO app_personal_web_app_builder;

ALTER INDEX web_app_history_app_idx RENAME TO web_app_history_thread_idx;

ALTER TABLE web_app_schedules RENAME COLUMN app_id TO thread_id;
ALTER TABLE web_app_memories RENAME COLUMN app_id TO thread_id;
ALTER TABLE web_app_history RENAME COLUMN app_id TO thread_id;
ALTER TABLE web_apps RENAME COLUMN app_id TO thread_id;
