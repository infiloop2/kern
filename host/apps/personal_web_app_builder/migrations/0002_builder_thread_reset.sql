-- Reset starts the builder over: it clears the generated bundle and moves the
-- app to a new host thread, because a thread's session configuration is fixed
-- for its lifetime. The sequence names that thread; sequence 1 keeps the
-- original `builder` id so an existing conversation survives this migration.

-- migrate:up

ALTER TABLE app_state ADD COLUMN thread_seq BIGINT NOT NULL DEFAULT 1 CHECK (thread_seq >= 1);

-- migrate:down

ALTER TABLE app_state DROP COLUMN thread_seq;
