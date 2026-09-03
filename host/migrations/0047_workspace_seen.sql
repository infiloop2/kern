-- Keep the operator's Chat and App read markers on the host so unread dots
-- follow them between browsers and devices. Existing items start at their
-- current marker; only activity arriving after this migration appears new.

-- migrate:up

-- Kern is a single-operator host, so there is one shared marker per item and
-- no account dimension to reconcile.
CREATE TABLE workspace_seen (
    item_kind TEXT NOT NULL CHECK (item_kind IN ('chat', 'apps')),
    item_id TEXT NOT NULL,
    message_seq BIGINT NOT NULL DEFAULT 0 CHECK (message_seq >= 0),
    revision BIGINT NOT NULL DEFAULT 0 CHECK (revision >= 0),
    PRIMARY KEY (item_kind, item_id)
);

INSERT INTO workspace_seen (item_kind, item_id, message_seq, revision)
SELECT
    'chat',
    chat.thread_id,
    COALESCE((
        SELECT MAX(event.seq)
        FROM agent_events AS event
        WHERE event.thread_id = chat.thread_id
          AND event.event_type = 'thread.message'
    ), 0),
    0
FROM chat_threads AS chat;

INSERT INTO workspace_seen (item_kind, item_id, message_seq, revision)
SELECT
    'apps',
    app.app_id,
    COALESCE((
        SELECT MAX(event.seq)
        FROM agent_events AS event
        WHERE event.thread_id = app.app_id
          AND event.event_type = 'thread.message'
    ), 0),
    app.revision
FROM web_apps AS app;

GRANT SELECT, INSERT, UPDATE, DELETE ON workspace_seen TO "kern-workspace";

-- migrate:down

REVOKE ALL ON workspace_seen FROM "kern-workspace";
DROP TABLE workspace_seen;
