-- Private notes are current values, separate from revisioned/searchable memory.
-- migrate:up
SET LOCAL search_path TO public;

CREATE TABLE private_notes (
    owner TEXT NOT NULL CHECK (owner ~ '^(app|thread|schedule)-[1-9][0-9]*$'),
    note_id TEXT NOT NULL CHECK (note_id ~ '^[a-z_]{1,100}$'),
    content TEXT NOT NULL CHECK (char_length(content) <= 20000),
    PRIMARY KEY (owner, note_id)
);

GRANT SELECT, INSERT, UPDATE, DELETE ON private_notes TO "kern-workspace";
