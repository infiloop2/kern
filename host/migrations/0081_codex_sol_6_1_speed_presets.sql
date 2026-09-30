-- Offer GPT-6.1 Sol for agents and Host AI, plus explicit Codex speed presets.

-- migrate:up
SET LOCAL search_path TO public;

-- Keep historical Sol usage in its original model bucket and at its stored cost.
ALTER TABLE host_inference_usage DROP CONSTRAINT host_inference_usage_model_check;
ALTER TABLE host_inference_usage ADD CHECK (model IN ('gpt-6-luna', 'gpt-6-sol', 'gpt-6.1-sol', 'jev'));

UPDATE web_apps SET agent_model = 'gpt-6.1-sol'
WHERE agent_runtime IN ('codex', 'codex-2', 'codex-3') AND agent_model = 'gpt-6-sol';
UPDATE schedules SET model = 'gpt-6.1-sol'
WHERE agent_runtime IN ('codex', 'codex-2', 'codex-3') AND model = 'gpt-6-sol';

ALTER TABLE thread_sessions DROP CONSTRAINT thread_sessions_options_check;
ALTER TABLE thread_sessions ADD CONSTRAINT thread_sessions_options_check CHECK (
    (
        agent_runtime IN ('codex', 'codex-2', 'codex-3')
        AND model IN ('gpt-5.6-terra', 'gpt-5.6-sol', 'gpt-5.6-luna', 'gpt-6-astra', 'gpt-6-sol', 'gpt-6.1-sol', 'gpt-6-luna')
        AND effort IN ('high', 'max', 'ultra', 'high-fast', 'high-ultrafast')
        AND NOT (model IN ('gpt-5.6-luna', 'gpt-6-luna') AND effort = 'ultra')
    )
    OR
    (
        agent_runtime = 'claude_code'
        AND model IN (
            'claude-opus-5-5', 'claude-opus-5', 'claude-fable-5-1', 'claude-fable-5', 'claude-sonnet-5-5', 'claude-sonnet-5',
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

-- The previous host cannot record the new model; retain all older usage rows.
DELETE FROM host_inference_usage WHERE model = 'gpt-6.1-sol';
ALTER TABLE host_inference_usage DROP CONSTRAINT host_inference_usage_model_check;
ALTER TABLE host_inference_usage ADD CHECK (model IN ('gpt-6-luna', 'gpt-6-sol', 'jev'));

-- Restore executable settings for the previous host. Keep transcript events
-- and schedule revisions; discard provider bindings for changed sessions.
UPDATE web_apps
SET agent_model = CASE WHEN agent_model = 'gpt-6.1-sol' THEN 'gpt-6-sol' ELSE agent_model END,
    agent_effort = CASE WHEN agent_effort IN ('high-fast', 'high-ultrafast') THEN 'high' ELSE agent_effort END
WHERE agent_runtime IN ('codex', 'codex-2', 'codex-3')
  AND (agent_model = 'gpt-6.1-sol' OR agent_effort IN ('high-fast', 'high-ultrafast'));
UPDATE schedules
SET model = CASE WHEN model = 'gpt-6.1-sol' THEN 'gpt-6-sol' ELSE model END,
    effort = CASE WHEN effort IN ('high-fast', 'high-ultrafast') THEN 'high' ELSE effort END
WHERE agent_runtime IN ('codex', 'codex-2', 'codex-3')
  AND (model = 'gpt-6.1-sol' OR effort IN ('high-fast', 'high-ultrafast'));
UPDATE thread_sessions
SET model = CASE WHEN model = 'gpt-6.1-sol' THEN 'gpt-6-sol' ELSE model END,
    effort = CASE WHEN effort IN ('high-fast', 'high-ultrafast') THEN 'high' ELSE effort END,
    provider_session_id = NULL
WHERE agent_runtime IN ('codex', 'codex-2', 'codex-3')
  AND (model = 'gpt-6.1-sol' OR effort IN ('high-fast', 'high-ultrafast'));

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
            'claude-opus-5-5', 'claude-opus-5', 'claude-fable-5-1', 'claude-fable-5', 'claude-sonnet-5-5', 'claude-sonnet-5',
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
