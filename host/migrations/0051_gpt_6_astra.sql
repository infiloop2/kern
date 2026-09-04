-- Codex 0.153.3 adds GPT-6 Astra to the supported model catalog. Keep the
-- database constraint aligned with host/session_options.py so a new Astra
-- thread can persist the configuration selected by the operator.

-- migrate:up
SET LOCAL search_path TO public;

ALTER TABLE thread_sessions DROP CONSTRAINT thread_sessions_options_check;
ALTER TABLE thread_sessions ADD CONSTRAINT thread_sessions_options_check CHECK (
    (
        agent_runtime = 'codex'
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
        agent_runtime = 'grok'
        AND model = 'grok-4.6'
        AND effort IN ('xhigh', 'high')
    )
    OR
    (
        agent_runtime = 'hermes'
        AND model IN ('deepseek.v3.2', 'qwen.qwen3-coder-next', 'moonshotai.kimi-k2.5')
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

-- Scheduled agents and Web Apps keep their selected configuration outside
-- thread_sessions. Move active Astra selections to the older host's explicit
-- Codex default so they keep working after rollback. Revision history and
-- transcript events remain readable with their original model recorded.
UPDATE schedules
SET model = 'gpt-5.6-sol', effort = 'high'
WHERE agent_runtime = 'codex' AND model = 'gpt-6-astra';

UPDATE web_apps
SET agent_model = 'gpt-5.6-sol', agent_effort = 'high'
WHERE agent_runtime = 'codex' AND agent_model = 'gpt-6-astra';

-- Preserve the canonical thread rows so the older host can still list each
-- conversation and serve its transcript. Its provider session belongs to an
-- Astra turn and cannot be resumed with the fallback model, so clear only
-- that provider-specific identity.
UPDATE thread_sessions
SET model = 'gpt-5.6-sol', effort = 'high', provider_session_id = NULL
WHERE agent_runtime = 'codex' AND model = 'gpt-6-astra';

ALTER TABLE thread_sessions DROP CONSTRAINT thread_sessions_options_check;
ALTER TABLE thread_sessions ADD CONSTRAINT thread_sessions_options_check CHECK (
    (
        agent_runtime = 'codex'
        AND model IN ('gpt-5.6-terra', 'gpt-5.6-sol', 'gpt-5.6-luna')
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
        agent_runtime = 'grok'
        AND model = 'grok-4.6'
        AND effort IN ('xhigh', 'high')
    )
    OR
    (
        agent_runtime = 'hermes'
        AND model IN ('deepseek.v3.2', 'qwen.qwen3-coder-next', 'moonshotai.kimi-k2.5')
        AND effort = 'high'
    )
    OR
    (
        agent_runtime = 'script'
        AND model = 'bash'
        AND effort = 'fixed'
    )
);
