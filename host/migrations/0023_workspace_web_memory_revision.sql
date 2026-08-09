-- Memory cache markers must change for every body change, even when two
-- writes share the same second-resolution updated_at timestamp.

-- migrate:up
SET LOCAL search_path TO app_personal_web_app_builder;

CREATE SEQUENCE web_app_memory_revision_seq;

ALTER TABLE web_app_memories
    ADD COLUMN revision BIGINT NOT NULL
    DEFAULT nextval('web_app_memory_revision_seq')
    CHECK (revision >= 1);

ALTER SEQUENCE web_app_memory_revision_seq
    OWNED BY web_app_memories.revision;

-- migrate:down
SET LOCAL search_path TO app_personal_web_app_builder;

ALTER TABLE web_app_memories DROP COLUMN revision;
DROP SEQUENCE IF EXISTS web_app_memory_revision_seq;
