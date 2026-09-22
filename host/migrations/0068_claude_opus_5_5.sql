-- Claude Code 2.1.280 exposes the exact claude-opus-5-5 model id. Keep
-- claude-opus-5 and the older aliases valid in storage so their transcripts
-- and recorded configuration remain readable, while host/session_options.py
-- controls which exact ids may start new work.

-- migrate:up
SET LOCAL search_path TO public;

ALTER TABLE thread_sessions DROP CONSTRAINT thread_sessions_options_check;
ALTER TABLE thread_sessions ADD CONSTRAINT thread_sessions_options_check CHECK (
    (
        agent_runtime IN ('codex', 'codex-2', 'codex-3')
        AND model IN ('gpt-5.6-terra', 'gpt-5.6-sol', 'gpt-5.6-luna', 'gpt-6-astra')
        AND effort IN ('high', 'max', 'ultra')
        AND NOT (model = 'gpt-5.6-luna' AND effort = 'ultra')
    )
    OR
    (
        agent_runtime = 'claude_code'
        AND model IN (
            'claude-opus-5-5', 'claude-opus-5', 'claude-fable-5-1', 'claude-fable-5',
            'claude-sonnet-5', 'opus', 'fable', 'sonnet'
        )
        AND effort IN ('high', 'max', 'ultracode')
    )
    OR
    (
        agent_runtime IN ('grok', 'grok-2')
        AND model = 'grok-4.6'
        AND effort IN ('xhigh', 'high')
    )
    OR
    (
        agent_runtime = 'hermes'
        AND model IN ('deepseek.v3.2', 'qwen.qwen3-coder-next', 'moonshotai.kimi-k2.5', 'zai.glm-5')
        AND effort = 'high'
    )
    OR
    (
        agent_runtime = 'script'
        AND model = 'bash'
        AND effort = 'fixed'
    )
);

-- migrate:down
SET LOCAL search_path TO public;

-- Rollback explicitly returns active Claude settings to Opus 5, which the
-- older host and older migration constraints support. Preserve canonical
-- conversations and their metadata, but clear native Opus 5.5 session IDs so
-- the older model starts fresh with retained Kern history. Update App and
-- schedule settings consistently; historical revisions/events stay unchanged.
UPDATE web_apps SET agent_model = 'claude-opus-5'
WHERE agent_runtime = 'claude_code' AND agent_model = 'claude-opus-5-5';
UPDATE schedules SET model = 'claude-opus-5'
WHERE agent_runtime = 'claude_code' AND model = 'claude-opus-5-5';
UPDATE thread_sessions SET model = 'claude-opus-5', provider_session_id = NULL
WHERE agent_runtime = 'claude_code' AND model = 'claude-opus-5-5';

ALTER TABLE thread_sessions DROP CONSTRAINT thread_sessions_options_check;
ALTER TABLE thread_sessions ADD CONSTRAINT thread_sessions_options_check CHECK (
    (
        agent_runtime IN ('codex', 'codex-2', 'codex-3')
        AND model IN ('gpt-5.6-terra', 'gpt-5.6-sol', 'gpt-5.6-luna', 'gpt-6-astra')
        AND effort IN ('high', 'max', 'ultra')
        AND NOT (model = 'gpt-5.6-luna' AND effort = 'ultra')
    )
    OR
    (
        agent_runtime = 'claude_code'
        AND model IN (
            'claude-opus-5', 'claude-fable-5-1', 'claude-fable-5', 'claude-sonnet-5',
            'opus', 'fable', 'sonnet'
        )
        AND effort IN ('high', 'max', 'ultracode')
    )
    OR
    (
        agent_runtime IN ('grok', 'grok-2')
        AND model = 'grok-4.6'
        AND effort IN ('xhigh', 'high')
    )
    OR
    (
        agent_runtime = 'hermes'
        AND model IN ('deepseek.v3.2', 'qwen.qwen3-coder-next', 'moonshotai.kimi-k2.5', 'zai.glm-5')
        AND effort = 'high'
    )
    OR
    (
        agent_runtime = 'script'
        AND model = 'bash'
        AND effort = 'fixed'
    )
);
