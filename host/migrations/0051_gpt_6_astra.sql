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
