-- Give generated Web Apps a row-oriented store for datasets that need
-- filtering and pagination without loading or rewriting one JSON document.
-- Collections remain part of the App's one logical state: every collection
-- write advances web_apps.revision, and every retained recovery point stores
-- a complete copy of the collection rows alongside the UI and JSON document.

-- migrate:up
SET LOCAL search_path TO public;

CREATE TABLE web_app_collection_state (
    app_id TEXT PRIMARY KEY REFERENCES web_apps (app_id) ON DELETE CASCADE,
    row_count BIGINT NOT NULL DEFAULT 0 CHECK (row_count >= 0),
    data_bytes BIGINT NOT NULL DEFAULT 0 CHECK (data_bytes >= 0)
);

INSERT INTO web_app_collection_state (app_id)
SELECT app_id FROM web_apps;

CREATE TABLE web_app_collection_rows (
    app_id TEXT NOT NULL,
    collection TEXT NOT NULL,
    row_id TEXT NOT NULL,
    value_json JSONB NOT NULL,
    value_bytes INTEGER NOT NULL CHECK (value_bytes >= 0),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (app_id, collection, row_id),
    FOREIGN KEY (app_id) REFERENCES web_app_collection_state (app_id)
        ON DELETE CASCADE
);

CREATE INDEX web_app_collection_rows_value_idx
    ON web_app_collection_rows USING GIN (value_json);

ALTER TABLE web_app_revisions
    ADD COLUMN collections_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE web_app_revisions
    DROP CONSTRAINT web_app_revisions_kind_check;
ALTER TABLE web_app_revisions
    ADD CONSTRAINT web_app_revisions_kind_check
    CHECK (kind IN ('created', 'ui', 'data', 'collection', 'restore', 'migration'));

GRANT SELECT, INSERT, UPDATE, DELETE
    ON web_app_collection_state, web_app_collection_rows TO "kern-workspace";

-- migrate:down
SET LOCAL search_path TO public;

DELETE FROM web_app_revisions WHERE kind = 'collection';
ALTER TABLE web_app_revisions
    DROP CONSTRAINT web_app_revisions_kind_check;
ALTER TABLE web_app_revisions
    ADD CONSTRAINT web_app_revisions_kind_check
    CHECK (kind IN ('created', 'ui', 'data', 'restore', 'migration'));
ALTER TABLE web_app_revisions DROP COLUMN collections_json;

DROP TABLE web_app_collection_rows;
DROP TABLE web_app_collection_state;
