-- The builder owns one agent-defined app bundle and its structured JSON data.
-- The host remains authoritative for the Web App's conversation thread.

-- migrate:up
CREATE SCHEMA IF NOT EXISTS app_personal_web_app_builder AUTHORIZATION "kern-admin";
SET LOCAL search_path TO app_personal_web_app_builder;

CREATE TABLE app_state (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    revision BIGINT NOT NULL DEFAULT 0 CHECK (revision >= 0),
    html TEXT NOT NULL DEFAULT '',
    css TEXT NOT NULL DEFAULT '',
    javascript TEXT NOT NULL DEFAULT '',
    data_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);

INSERT INTO app_state (singleton, updated_at)
VALUES (TRUE, '1970-01-01T00:00:00Z');

-- migrate:down
SET LOCAL search_path TO app_personal_web_app_builder;

DROP TABLE app_state;
DROP SCHEMA IF EXISTS app_personal_web_app_builder;
