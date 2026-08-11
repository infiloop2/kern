-- Track useful strong memory matches so agents can cheaply discover common
-- context, while keeping page revisions focused on content changes.

-- migrate:up
SET LOCAL search_path TO public;

ALTER TABLE memory_pages
    ADD COLUMN strong_top_hit_count BIGINT NOT NULL DEFAULT 0
        CHECK (strong_top_hit_count >= 0),
    ADD COLUMN last_strong_top_hit_at TEXT;

CREATE INDEX memory_pages_popular_idx ON memory_pages
    (strong_top_hit_count DESC, last_strong_top_hit_at DESC, updated_at DESC, page_id)
    WHERE deleted_at IS NULL;

-- migrate:down
SET LOCAL search_path TO public;

DROP INDEX memory_pages_popular_idx;
ALTER TABLE memory_pages
    DROP COLUMN last_strong_top_hit_at,
    DROP COLUMN strong_top_hit_count;
