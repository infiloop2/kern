-- Agentic Web App now has one workspace list. Preserve every existing app as
-- active, then remove the archive state and its supporting index.

-- migrate:up

UPDATE web_apps SET archived = FALSE WHERE archived;
DROP INDEX web_apps_archive_updated_idx;
ALTER TABLE web_apps DROP COLUMN archived;
CREATE INDEX web_apps_updated_idx ON web_apps (updated_at DESC);

-- migrate:down

DROP INDEX web_apps_updated_idx;
ALTER TABLE web_apps ADD COLUMN archived BOOLEAN NOT NULL DEFAULT FALSE;
CREATE INDEX web_apps_archive_updated_idx
    ON web_apps (archived, updated_at DESC);
