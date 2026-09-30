-- Offer GPT-6 Sol and Luna. Keep retired model values valid in storage so
-- existing conversations remain readable and can switch models while idle.

-- migrate:up
SET LOCAL search_path TO public;

ALTER TABLE thread_sessions DROP CONSTRAINT thread_sessions_options_check;
ALTER TABLE thread_sessions ADD CONSTRAINT thread_sessions_options_check CHECK (
    (
        agent_runtime IN ('codex', 'codex-2', 'codex-3')
        AND model IN ('gpt-5.6-terra', 'gpt-5.6-sol', 'gpt-5.6-luna', 'gpt-6-astra', 'gpt-6-sol', 'gpt-6-luna')
        AND effort IN ('high', 'max', 'ultra')
        AND NOT (model IN ('gpt-5.6-luna', 'gpt-6-luna') AND effort = 'ultra')
    )
    OR
    (
        agent_runtime = 'claude_code'
        AND model IN (
            'claude-opus-5-5', 'claude-opus-5', 'claude-fable-5-1', 'claude-fable-5', 'claude-sonnet-5',
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
