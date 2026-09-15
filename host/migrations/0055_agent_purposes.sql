-- migrate:up
SET LOCAL search_path TO public;

ALTER TABLE web_apps ADD COLUMN purpose TEXT NOT NULL DEFAULT '' CHECK (char_length(purpose) <= 100);
ALTER TABLE schedules ADD COLUMN purpose TEXT NOT NULL DEFAULT '' CHECK (char_length(purpose) <= 100);
ALTER TABLE schedule_revisions ADD COLUMN purpose TEXT NOT NULL DEFAULT '' CHECK (char_length(purpose) <= 100);

-- migrate:down
SET LOCAL search_path TO public;

ALTER TABLE schedule_revisions DROP COLUMN purpose;
ALTER TABLE schedules DROP COLUMN purpose;
ALTER TABLE web_apps DROP COLUMN purpose;
