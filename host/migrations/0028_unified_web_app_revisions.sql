-- Give each generated Web App one coherent UI/data revision and replace the
-- internal delta/checkpoint history with bounded full-state revisions.

-- migrate:up
SET LOCAL search_path TO public;

CREATE TABLE web_app_revisions (
    app_id TEXT NOT NULL REFERENCES web_apps (app_id) ON DELETE CASCADE,
    revision BIGINT NOT NULL CHECK (revision >= 0),
    actor TEXT NOT NULL CHECK (actor IN ('agent', 'app', 'user', 'migration')),
    kind TEXT NOT NULL CHECK (kind IN ('created', 'ui', 'data', 'restore', 'migration')),
    restored_from BIGINT CHECK (restored_from IS NULL OR restored_from >= 0),
    html TEXT NOT NULL,
    css TEXT NOT NULL,
    javascript TEXT NOT NULL,
    data_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (app_id, revision)
);

ALTER TABLE web_apps ADD COLUMN revision BIGINT;

-- Old split history is intentionally retired. Preserve the current state and
-- carry its monotonic counters into one revision; gaps have no semantics.
UPDATE web_apps SET revision = ui_revision + data_version;

ALTER TABLE web_apps ALTER COLUMN revision SET NOT NULL;

-- The current App state becomes the first full snapshot in the new history.
INSERT INTO web_app_revisions
    (app_id, revision, actor, kind, restored_from,
     html, css, javascript, data_json, created_at)
SELECT app_id, revision, 'migration', 'migration', NULL,
       html, css, javascript, data_json, updated_at
FROM web_apps;

ALTER TABLE web_apps DROP COLUMN ui_revision;
ALTER TABLE web_apps DROP COLUMN data_version;
ALTER TABLE web_apps ADD CONSTRAINT web_apps_revision_check CHECK (revision >= 0);

DROP TABLE web_app_history;

GRANT SELECT, INSERT, UPDATE, DELETE ON web_app_revisions TO "kern-workspace";
