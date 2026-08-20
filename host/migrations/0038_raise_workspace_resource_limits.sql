-- Give memory pages and schedule prompts more room while keeping the same
-- bounded-resource model at the API and database layers.

-- migrate:up
SET LOCAL search_path TO public;

ALTER TABLE memory_pages DROP CONSTRAINT memory_pages_content_check;
ALTER TABLE memory_pages ADD CONSTRAINT memory_pages_content_check
    CHECK (char_length(content) <= 2000);

ALTER TABLE memory_page_revisions
    DROP CONSTRAINT memory_page_revisions_content_check;
ALTER TABLE memory_page_revisions
    ADD CONSTRAINT memory_page_revisions_content_check
    CHECK (char_length(content) <= 2000);

ALTER TABLE schedules DROP CONSTRAINT schedules_message_check;
ALTER TABLE schedules ADD CONSTRAINT schedules_message_check
    CHECK (char_length(message) BETWEEN 1 AND 12000);

ALTER TABLE schedule_revisions DROP CONSTRAINT schedule_revisions_message_check;
ALTER TABLE schedule_revisions ADD CONSTRAINT schedule_revisions_message_check
    CHECK (char_length(message) BETWEEN 1 AND 12000);

ALTER TABLE schedule_runs DROP CONSTRAINT schedule_runs_message_check;
ALTER TABLE schedule_runs ADD CONSTRAINT schedule_runs_message_check
    CHECK (char_length(message) BETWEEN 1 AND 12000);

-- migrate:down
SET LOCAL search_path TO public;

UPDATE memory_pages SET content = left(content, 1000)
WHERE char_length(content) > 1000;
UPDATE memory_page_revisions SET content = left(content, 1000)
WHERE char_length(content) > 1000;
UPDATE schedules SET message = left(message, 4000)
WHERE char_length(message) > 4000;
UPDATE schedule_revisions SET message = left(message, 4000)
WHERE char_length(message) > 4000;
UPDATE schedule_runs SET message = left(message, 4000)
WHERE char_length(message) > 4000;

ALTER TABLE memory_pages DROP CONSTRAINT memory_pages_content_check;
ALTER TABLE memory_pages ADD CONSTRAINT memory_pages_content_check
    CHECK (char_length(content) <= 1000);

ALTER TABLE memory_page_revisions
    DROP CONSTRAINT memory_page_revisions_content_check;
ALTER TABLE memory_page_revisions
    ADD CONSTRAINT memory_page_revisions_content_check
    CHECK (char_length(content) <= 1000);

ALTER TABLE schedules DROP CONSTRAINT schedules_message_check;
ALTER TABLE schedules ADD CONSTRAINT schedules_message_check
    CHECK (char_length(message) BETWEEN 1 AND 4000);

ALTER TABLE schedule_revisions DROP CONSTRAINT schedule_revisions_message_check;
ALTER TABLE schedule_revisions ADD CONSTRAINT schedule_revisions_message_check
    CHECK (char_length(message) BETWEEN 1 AND 4000);

ALTER TABLE schedule_runs DROP CONSTRAINT schedule_runs_message_check;
ALTER TABLE schedule_runs ADD CONSTRAINT schedule_runs_message_check
    CHECK (char_length(message) BETWEEN 1 AND 4000);
