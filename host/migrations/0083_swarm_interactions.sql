-- migrate:up
SET LOCAL search_path TO public;
CREATE TABLE swarm_interaction_days (
    day TEXT NOT NULL,
    sender_thread_id TEXT NOT NULL,
    target_thread_id TEXT NOT NULL,
    interactions BIGINT NOT NULL CHECK (interactions > 0),
    PRIMARY KEY (day, sender_thread_id, target_thread_id)
);
GRANT SELECT, INSERT, UPDATE, DELETE ON swarm_interaction_days TO "kern-workspace";
-- Start counting at upgrade: the old 50-event animation feed is not a weekly history.
DROP TABLE swarm_peer_deliveries;
DELETE FROM swarm_agent_ai WHERE thread_id NOT IN (
    SELECT thread_id FROM chat_threads WHERE spawned_by_thread_id IS NULL
);
