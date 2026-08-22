-- Local semantic-search vectors are derived, replaceable state. Source
-- messages remain in agent_events and memory_pages; deleting source state
-- deletes its vector.

-- migrate:up

CREATE TABLE conversation_search_state (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    embedding_generation BIGINT NOT NULL DEFAULT 0
);

INSERT INTO conversation_search_state (singleton) VALUES (TRUE);

CREATE TABLE conversation_message_embeddings (
    event_seq BIGINT PRIMARY KEY REFERENCES agent_events(seq) ON DELETE CASCADE,
    model TEXT NOT NULL,
    embedding vector(384) NOT NULL,
    embedding_generation BIGINT NOT NULL,
    embedded_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

-- No index on model alone: one model is configured, so every row shares a
-- single value and the index would cost writes without ever narrowing a scan.
CREATE INDEX conversation_message_embeddings_cosine_idx
ON conversation_message_embeddings
USING hnsw (embedding vector_cosine_ops);

-- Outstanding work only. A caught-up indexer reads this empty table instead
-- of repeatedly anti-joining the retained agent-event history against the
-- embeddings table. Rejected passages receive a bounded retry count.
CREATE TABLE conversation_embedding_queue (
    event_seq BIGINT PRIMARY KEY REFERENCES agent_events(seq) ON DELETE CASCADE,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TIMESTAMPTZ
);

-- Supports global newest-message quota lookups by seq. Existing indexes serve
-- different orderings: (thread_id, seq) is per-thread, while
-- (created_at, seq) is chronological search/UI paging.
CREATE INDEX agent_events_message_seq_idx
ON agent_events (seq DESC)
WHERE event_type = 'thread.message' AND message IS NOT NULL;

-- Seed the newest 250,000 conversation messages once. Activity, lifecycle,
-- and runtime events do not consume this quota.
INSERT INTO conversation_embedding_queue (event_seq)
SELECT pending.seq
FROM (
    SELECT seq
    FROM agent_events
    WHERE event_type = 'thread.message' AND message IS NOT NULL
    ORDER BY seq DESC
    LIMIT 250000
) AS pending
LEFT JOIN conversation_message_embeddings AS embeddings
  ON embeddings.event_seq = pending.seq
WHERE embeddings.event_seq IS NULL
ON CONFLICT DO NOTHING;

CREATE TABLE memory_page_embeddings (
    page_id TEXT NOT NULL REFERENCES memory_pages(page_id) ON DELETE CASCADE,
    revision BIGINT NOT NULL,
    model TEXT NOT NULL,
    embedding vector(384) NOT NULL,
    embedded_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (page_id, model)
);

CREATE INDEX memory_page_embeddings_cosine_idx
ON memory_page_embeddings
USING hnsw (embedding vector_cosine_ops);

CREATE TABLE memory_page_links (
    source_page_id TEXT NOT NULL REFERENCES memory_pages(page_id) ON DELETE CASCADE,
    -- Targets deliberately have no foreign key: wiki-style links may point to
    -- a page that does not exist yet or is temporarily deleted, and become
    -- live automatically when that page id is created or restored.
    target_page_id TEXT NOT NULL,
    PRIMARY KEY (source_page_id, target_page_id)
);

CREATE INDEX memory_page_links_target_idx
ON memory_page_links (target_page_id, source_page_id);

-- Mirror _page_links(): dedupe by first occurrence and keep only the first 100
-- links in content order, so a backfilled page never exceeds the per-page quota
-- its next edit would enforce.
INSERT INTO memory_page_links (source_page_id, target_page_id)
SELECT page_id, target
FROM (
    SELECT
        page_id,
        target,
        ROW_NUMBER() OVER (PARTITION BY page_id ORDER BY first_seen) AS position
    FROM (
        SELECT
            pages.page_id AS page_id,
            matches.link[1] AS target,
            MIN(matches.ordinality) AS first_seen
        FROM memory_pages AS pages
        CROSS JOIN LATERAL regexp_matches(
            pages.content,
            '\[\[([a-z0-9][a-z0-9-]{0,63})\]\]',
            'g'
        ) WITH ORDINALITY AS matches(link, ordinality)
        WHERE pages.deleted_at IS NULL
          AND pages.page_id !~ '^(app|thread|schedule)-'
          AND matches.link[1] !~ '^(app|thread|schedule)-'
        GROUP BY pages.page_id, matches.link[1]
    ) AS distinct_links
) AS ranked
WHERE position <= 100
ON CONFLICT DO NOTHING;

GRANT SELECT, INSERT, UPDATE, DELETE ON memory_page_embeddings TO "kern-workspace";
GRANT SELECT, INSERT, UPDATE, DELETE ON memory_page_links TO "kern-workspace";

-- migrate:down

REVOKE ALL ON memory_page_embeddings FROM "kern-workspace";
REVOKE ALL ON memory_page_links FROM "kern-workspace";
DROP TABLE memory_page_links;
DROP TABLE memory_page_embeddings;
DROP TABLE conversation_embedding_queue;
DROP INDEX agent_events_message_seq_idx;
DROP TABLE conversation_message_embeddings;
DROP TABLE conversation_search_state;
