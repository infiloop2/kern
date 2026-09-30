-- Persist the operator's daily UTC sleep window independently of deploy config.
-- migrate:up
SET LOCAL search_path TO public;
CREATE TABLE auto_approval_settings (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    sleep_start_minute INTEGER NOT NULL CHECK (sleep_start_minute BETWEEN 0 AND 1439),
    sleep_end_minute INTEGER NOT NULL CHECK (sleep_end_minute BETWEEN 0 AND 1439),
    CHECK ((sleep_end_minute - sleep_start_minute + 1440) % 1440 >= 360)
);
-- migrate:down
SET LOCAL search_path TO public;
DROP TABLE auto_approval_settings;
