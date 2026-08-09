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

-- migrate:down
SET LOCAL search_path TO public;

ALTER TABLE web_apps ADD COLUMN ui_revision BIGINT;
ALTER TABLE web_apps ADD COLUMN data_version BIGINT NOT NULL DEFAULT 0
    CHECK (data_version >= 0);
UPDATE web_apps SET ui_revision = revision;
ALTER TABLE web_apps ALTER COLUMN ui_revision SET NOT NULL;
ALTER TABLE web_apps ADD CONSTRAINT web_apps_ui_revision_check CHECK (ui_revision >= 0);
ALTER TABLE web_apps DROP COLUMN revision;

CREATE TABLE web_app_history (
    id BIGSERIAL PRIMARY KEY,
    app_id TEXT NOT NULL REFERENCES web_apps (app_id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('ui', 'data', 'snapshot', 'checkpoint')),
    actor TEXT NOT NULL CHECK (actor IN ('agent', 'app', 'user')),
    ui_revision BIGINT NOT NULL CHECK (ui_revision >= 0),
    data_version BIGINT NOT NULL CHECK (data_version >= 0),
    entry_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX web_app_history_app_idx ON web_app_history (app_id, id DESC);

INSERT INTO web_app_history
    (app_id, kind, actor, ui_revision, data_version, entry_json, created_at)
SELECT app_id, 'ui', 'user', ui_revision, data_version,
       json_build_object('html', html, 'css', css, 'javascript', javascript)::text,
       updated_at
FROM web_apps;
INSERT INTO web_app_history
    (app_id, kind, actor, ui_revision, data_version, entry_json, created_at)
SELECT app_id, 'snapshot', 'user', ui_revision, data_version,
       json_build_object('data', data_json::json)::text,
       updated_at
FROM web_apps;
INSERT INTO web_app_history
    (app_id, kind, actor, ui_revision, data_version, entry_json, created_at)
SELECT app_id, 'checkpoint', 'app', ui_revision, data_version,
       json_build_object(
           'checkpoint_type', 'automatic',
           'checkpoint_date', left(updated_at, 10),
           'name', name,
           'html', html,
           'css', css,
           'javascript', javascript,
           'data', data_json::json
       )::text,
       updated_at
FROM web_apps;

GRANT SELECT, INSERT, UPDATE, DELETE ON web_app_history TO "kern-workspace";
GRANT USAGE, SELECT, UPDATE ON web_app_history_id_seq TO "kern-workspace";

DROP TABLE web_app_revisions;
