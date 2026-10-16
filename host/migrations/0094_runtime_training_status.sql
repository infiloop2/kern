-- Privacy checks outlive routine account metadata refreshes, but not account replacement.
-- migrate:up
SET LOCAL search_path TO public;

ALTER TABLE provider_accounts
    ADD COLUMN training_allowed BOOLEAN,
    ADD COLUMN training_checked_at TIMESTAMPTZ,
    ADD COLUMN training_attempted_at TIMESTAMPTZ;
