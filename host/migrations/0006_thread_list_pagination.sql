-- Serve newest-first thread pages by the same complete key used by the
-- opaque cursor, without sorting every retained session row per request.

-- migrate:up

CREATE INDEX thread_sessions_recency_page_idx
    ON thread_sessions (
        COALESCE(last_used_at, '') DESC,
        thread_id DESC
    );

-- migrate:down

DROP INDEX thread_sessions_recency_page_idx;
