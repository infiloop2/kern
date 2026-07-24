-- Social Marketer app schema. Kern 1.0.0 is a fresh start, so this is the
-- app's single genesis migration: the workspace_kit base schema (byte-for-byte
-- the shared base, with the host-supported runtime constraint) plus the Social
-- Marketer domain tables.
--
-- The posts table is the single source of truth shared by the agent
-- (upsert_post / set_post_status actions) and the operator composer
-- (GET/POST /api/posts). Body length is capped by encoded bytes per platform
-- in the backend before any write; the enum CHECKs here are a second
-- structural guard. Campaign plans and performance summaries stay as free-form
-- workspace artifacts.

-- migrate:up

CREATE TABLE IF NOT EXISTS workspace (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    agent_runtime TEXT CHECK (agent_runtime IN ('codex', 'claude_code', 'hermes')),
    model TEXT,
    effort TEXT,
    thread_seq INTEGER NOT NULL DEFAULT 1,
    goal TEXT NOT NULL DEFAULT '',
    measurement TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (
        (agent_runtime IS NULL AND model IS NULL AND effort IS NULL)
        OR
        (agent_runtime IS NOT NULL AND model IS NOT NULL AND effort IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS messages (
    id BIGSERIAL PRIMARY KEY,
    role TEXT NOT NULL CHECK (role IN ('user', 'agent', 'event', 'error')),
    content TEXT NOT NULL,
    meta TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id BIGSERIAL PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('chat', 'schedule')),
    status TEXT NOT NULL CHECK (status IN ('pending', 'active', 'done')),
    task_id TEXT UNIQUE,
    host_status TEXT,
    thread_id TEXT,
    agent_runtime TEXT,
    message_id BIGINT REFERENCES messages(id),
    schedule_id TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);

CREATE TABLE IF NOT EXISTS schedules (
    schedule_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    prompt TEXT NOT NULL,
    every_minutes INTEGER,
    next_run_at TEXT,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    last_run_id BIGINT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    data TEXT NOT NULL DEFAULT 'null',
    view TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memories (
    memory_id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tools (
    tool_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    priority TEXT NOT NULL CHECK (priority IN ('must_have', 'good_to_have')),
    status TEXT NOT NULL CHECK (status IN ('enabled', 'implemented', 'not_implemented')),
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS posts (
    id TEXT PRIMARY KEY,
    platform TEXT NOT NULL CHECK (platform IN ('x', 'linkedin')),
    body TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'approved', 'posted')),
    scheduled_for TEXT,
    external_ref TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_posts_scheduled_for ON posts(scheduled_for);
CREATE INDEX IF NOT EXISTS idx_posts_status ON posts(status);

-- migrate:down

DROP TABLE IF EXISTS posts;
DROP TABLE IF EXISTS tools;
DROP TABLE IF EXISTS memories;
DROP TABLE IF EXISTS artifacts;
DROP TABLE IF EXISTS schedules;
DROP TABLE IF EXISTS runs;
DROP TABLE IF EXISTS messages;
DROP TABLE IF EXISTS workspace;
