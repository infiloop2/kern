-- Start shared recovery from one fresh checkpoint per App. The operator has
-- chosen to discard pre-upgrade recovery history; live state and the current
-- revision number remain unchanged. No historical snapshot conversion.
-- migrate:up
SET LOCAL search_path TO public;

CREATE TABLE web_app_ui_versions (
    app_id TEXT NOT NULL REFERENCES web_apps(app_id) ON DELETE CASCADE,
    version TEXT NOT NULL,
    html TEXT NOT NULL,
    css TEXT NOT NULL,
    javascript TEXT NOT NULL,
    PRIMARY KEY (app_id, version)
);
CREATE TABLE web_app_document_versions (
    app_id TEXT NOT NULL REFERENCES web_apps(app_id) ON DELETE CASCADE,
    version TEXT NOT NULL,
    data_json TEXT NOT NULL,
    PRIMARY KEY (app_id, version)
);
CREATE TABLE web_app_collection_versions (
    app_id TEXT NOT NULL REFERENCES web_apps(app_id) ON DELETE CASCADE,
    collection TEXT NOT NULL,
    row_id TEXT NOT NULL,
    valid_from BIGINT NOT NULL CHECK (valid_from >= 0),
    valid_until BIGINT CHECK (valid_until > valid_from),
    value_json JSONB NOT NULL,
    PRIMARY KEY (app_id, collection, row_id, valid_from)
);
CREATE UNIQUE INDEX web_app_collection_versions_current_idx
    ON web_app_collection_versions(app_id, collection, row_id)
    WHERE valid_until IS NULL;
CREATE INDEX web_app_collection_versions_closed_idx
    ON web_app_collection_versions(app_id, valid_until)
    WHERE valid_until IS NOT NULL;

-- Seed immutable components and open row intervals directly from live state.
INSERT INTO web_app_ui_versions
SELECT app_id, 'baseline-' || revision, html, css, javascript FROM web_apps;
INSERT INTO web_app_document_versions
SELECT app_id, 'baseline-' || revision, data_json FROM web_apps;
INSERT INTO web_app_collection_versions
SELECT r.app_id, r.collection, r.row_id, a.revision, NULL, r.value_json
FROM web_app_collection_rows r JOIN web_apps a ON a.app_id = r.app_id;

-- Retire only recovery history. App rows, collection rows, counters, and
-- settings are untouched. Migration execution is one database transaction.
DELETE FROM web_app_revisions;
ALTER TABLE web_app_revisions ADD COLUMN ui_version TEXT NOT NULL;
ALTER TABLE web_app_revisions ADD COLUMN document_version TEXT NOT NULL;
ALTER TABLE web_app_revisions ADD FOREIGN KEY (app_id, ui_version)
    REFERENCES web_app_ui_versions(app_id, version);
ALTER TABLE web_app_revisions ADD FOREIGN KEY (app_id, document_version)
    REFERENCES web_app_document_versions(app_id, version);
ALTER TABLE web_app_revisions DROP COLUMN html, DROP COLUMN css,
    DROP COLUMN javascript, DROP COLUMN data_json, DROP COLUMN collections_json;
INSERT INTO web_app_revisions
    (app_id, revision, actor, kind, restored_from, ui_version, document_version, created_at)
SELECT app_id, revision, 'migration', 'migration', NULL,
       'baseline-' || revision, 'baseline-' || revision,
       to_char(CURRENT_TIMESTAMP AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')
FROM web_apps;

GRANT SELECT, INSERT, UPDATE, DELETE ON web_app_ui_versions,
    web_app_document_versions, web_app_collection_versions TO "kern-workspace";

-- Rollback materializes the retained new checkpoints. Discarded pre-upgrade
-- history is intentionally not recoverable.
-- migrate:down
SET LOCAL search_path TO public;
ALTER TABLE web_app_revisions ADD COLUMN html TEXT;
ALTER TABLE web_app_revisions ADD COLUMN css TEXT;
ALTER TABLE web_app_revisions ADD COLUMN javascript TEXT;
ALTER TABLE web_app_revisions ADD COLUMN data_json TEXT;
ALTER TABLE web_app_revisions ADD COLUMN collections_json TEXT;
UPDATE web_app_revisions r SET html = u.html, css = u.css, javascript = u.javascript
FROM web_app_ui_versions u WHERE u.app_id = r.app_id AND u.version = r.ui_version;
UPDATE web_app_revisions r SET data_json = d.data_json
FROM web_app_document_versions d WHERE d.app_id = r.app_id AND d.version = r.document_version;
UPDATE web_app_revisions r SET collections_json = COALESCE((
    SELECT jsonb_object_agg(c.collection, c.rows)::text FROM (
        SELECT h.collection, jsonb_object_agg(h.row_id, h.value_json) AS rows
        FROM web_app_collection_versions h WHERE h.app_id = r.app_id
          AND h.valid_from <= r.revision
          AND (h.valid_until IS NULL OR h.valid_until > r.revision)
        GROUP BY h.collection
    ) c
), '{}');
ALTER TABLE web_app_revisions ALTER COLUMN html SET NOT NULL,
    ALTER COLUMN css SET NOT NULL, ALTER COLUMN javascript SET NOT NULL,
    ALTER COLUMN data_json SET NOT NULL, ALTER COLUMN collections_json SET NOT NULL,
    ALTER COLUMN collections_json SET DEFAULT '{}';
ALTER TABLE web_app_revisions DROP COLUMN ui_version, DROP COLUMN document_version;
DROP TABLE web_app_collection_versions;
DROP TABLE web_app_document_versions;
DROP TABLE web_app_ui_versions;
