-- Clearing a thread's working memory drops its provider session so the next
-- run opens a new provider conversation. Dropping the session alone is not
-- enough: a thread whose provider session is missing is otherwise handed a
-- replay of its recent events, which would restore the context the operator
-- just cleared. The cleared point is recorded as an event-sequence floor so
-- the handoff starts after it.

-- migrate:up

ALTER TABLE thread_sessions
    ADD COLUMN context_cleared_seq BIGINT NOT NULL DEFAULT 0,
    ADD CONSTRAINT thread_sessions_context_cleared_seq_check
        CHECK (context_cleared_seq >= 0);

-- migrate:down

ALTER TABLE thread_sessions
    DROP CONSTRAINT thread_sessions_context_cleared_seq_check,
    DROP COLUMN context_cleared_seq;
