-- Web Apps workspace can be hidden without deleting their workspace, history,
-- schedules, or agent configuration. Archived workspaces are read-only and
-- their schedules do not run until the operator restores them.

-- migrate:up
SET LOCAL search_path TO app_personal_web_app_builder;

DROP INDEX web_apps_updated_idx;
ALTER TABLE web_apps ADD COLUMN archived BOOLEAN NOT NULL DEFAULT FALSE;
CREATE INDEX web_apps_archive_updated_idx
    ON web_apps (archived, updated_at DESC);

-- migrate:down
SET LOCAL search_path TO app_personal_web_app_builder;

DROP INDEX web_apps_archive_updated_idx;
ALTER TABLE web_apps DROP COLUMN archived;
CREATE INDEX web_apps_updated_idx ON web_apps (updated_at DESC);
