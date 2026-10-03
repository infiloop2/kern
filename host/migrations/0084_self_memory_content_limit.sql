-- Self-memory has more room; shared memory keeps its existing bound.

-- migrate:up
SET LOCAL search_path TO public;

ALTER TABLE memory_pages DROP CONSTRAINT memory_pages_content_check;
ALTER TABLE memory_pages ADD CONSTRAINT memory_pages_content_check
    CHECK (char_length(content) <= CASE
        WHEN page_id ~ '^(app|thread|schedule)-' THEN 20000 ELSE 2000 END);

ALTER TABLE memory_page_revisions
    DROP CONSTRAINT memory_page_revisions_content_check;
ALTER TABLE memory_page_revisions
    ADD CONSTRAINT memory_page_revisions_content_check
    CHECK (char_length(content) <= CASE
        WHEN page_id ~ '^(app|thread|schedule)-' THEN 20000 ELSE 2000 END);
