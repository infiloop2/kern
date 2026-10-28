-- migrate:up
CREATE TABLE agent_reminders (
    id BIGSERIAL PRIMARY KEY,
    thread_id TEXT NOT NULL CHECK (thread_id ~ '^(thread|app|schedule)-[1-9][0-9]*$'),
    message TEXT NOT NULL CHECK (char_length(message) BETWEEN 1 AND 1000 AND btrim(message) <> ''),
    due_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'delivered', 'failed')),
    error TEXT,
    created_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX agent_reminders_due ON agent_reminders (due_at, id) WHERE status = 'pending';
CREATE INDEX agent_reminders_thread ON agent_reminders (thread_id, status, due_at, id);
CREATE INDEX agent_reminders_finished ON agent_reminders (finished_at DESC, id DESC) WHERE status <> 'pending';
GRANT SELECT, INSERT, UPDATE, DELETE ON agent_reminders TO "kern-workspace";
GRANT USAGE, SELECT ON agent_reminders_id_seq TO "kern-workspace";
