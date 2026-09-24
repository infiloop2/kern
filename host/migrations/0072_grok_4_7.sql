-- Grok Build 1.0.40 offers Grok 4.7. Retain Grok 4.6 in storage so
-- existing threads remain readable; the current catalog controls new work.

-- migrate:up
SET LOCAL search_path TO public;

-- Schedules and Web Apps replay their saved model on later deliveries. Move
-- active settings forward so they do not request a retired model after the
-- catalog changes. Historical thread configurations remain readable as 4.6.
UPDATE web_apps SET agent_model = 'grok-4.7'
WHERE agent_runtime IN ('grok', 'grok-2') AND agent_model = 'grok-4.6';
UPDATE schedules SET model = 'grok-4.7'
WHERE agent_runtime IN ('grok', 'grok-2') AND model = 'grok-4.6';

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
        AND model IN ('grok-4.6', 'grok-4.7')
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

-- The older CLI cannot run 4.7. Return active settings to 4.6 and discard
-- provider session ids so resumed turns start against the older model.
UPDATE web_apps SET agent_model = 'grok-4.6'
WHERE agent_runtime IN ('grok', 'grok-2') AND agent_model = 'grok-4.7';
UPDATE schedules SET model = 'grok-4.6'
WHERE agent_runtime IN ('grok', 'grok-2') AND model = 'grok-4.7';
UPDATE thread_sessions SET model = 'grok-4.6', provider_session_id = NULL
WHERE agent_runtime IN ('grok', 'grok-2') AND model = 'grok-4.7';

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
