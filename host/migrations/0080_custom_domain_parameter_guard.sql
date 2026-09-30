-- migrate:up
-- Existing custom APIs may require credentials or opaque request content; opt in per rule.
ALTER TABLE allowed_domains
    ADD COLUMN guard_request_content BOOLEAN NOT NULL DEFAULT FALSE;
