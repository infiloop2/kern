-- Use the current session catalog and migrate retired model selections.

-- migrate:up
SET LOCAL search_path TO public;

CREATE TEMP TABLE session_model_replacements (
    agent_runtime TEXT NOT NULL,
    old_model TEXT NOT NULL,
    new_model TEXT NOT NULL
) ON COMMIT DROP;

INSERT INTO session_model_replacements
SELECT runtime, old_model, new_model
FROM (VALUES ('codex'), ('codex-2'), ('codex-3')) AS runtimes(runtime)
CROSS JOIN (VALUES
    ('gpt-5.6-terra', 'gpt-6.2-sol'),
    ('gpt-5.6-sol', 'gpt-6.2-sol'),
    ('gpt-6-sol', 'gpt-6.2-sol'),
    ('gpt-6.1-sol', 'gpt-6.2-sol'),
    ('gpt-6-astra', 'gpt-6.2-astra'),
    ('gpt-5.6-luna', 'gpt-6-luna')
) AS models(old_model, new_model);
INSERT INTO session_model_replacements
SELECT 'claude_code', old_model, new_model
FROM (VALUES
    ('opus', 'claude-opus-5-5'),
    ('claude-opus-5', 'claude-opus-5-5'),
    ('fable', 'claude-fable-5-2'),
    ('claude-fable-5', 'claude-fable-5-2'),
    ('claude-fable-5-1', 'claude-fable-5-2'),
    ('sonnet', 'claude-sonnet-5-5'),
    ('claude-sonnet-5', 'claude-sonnet-5-5')
) AS models(old_model, new_model);
INSERT INTO session_model_replacements VALUES
    ('grok', 'grok-4.6', 'grok-4.7'),
    ('grok-2', 'grok-4.6', 'grok-4.7'),
    ('hermes', 'moonshotai.kimi-k2.5', 'us.moonshotai.kimi-k3.1'),
    ('hermes', 'zai.glm-5', 'us.zai.glm-5.3');

ALTER TABLE thread_sessions DROP CONSTRAINT thread_sessions_options_check;

-- Start no turns. Keep metadata and history; a changed model needs a fresh
-- provider session on its next turn, using the retained Kern conversation.
UPDATE thread_sessions AS session SET model = replacement.new_model, provider_session_id = NULL
FROM session_model_replacements AS replacement
WHERE session.agent_runtime = replacement.agent_runtime AND session.model = replacement.old_model;
UPDATE web_apps AS app SET agent_model = replacement.new_model
FROM session_model_replacements AS replacement
WHERE app.agent_runtime = replacement.agent_runtime AND app.agent_model = replacement.old_model;
UPDATE schedules AS schedule SET model = replacement.new_model
FROM session_model_replacements AS replacement
WHERE schedule.agent_runtime = replacement.agent_runtime AND schedule.model = replacement.old_model;

-- Historical SQL admitted Luna speed presets even though the public catalog
-- did not. Keep the High effort while removing the unsupported speed tier.
UPDATE thread_sessions SET effort = 'high', provider_session_id = NULL
WHERE agent_runtime IN ('codex', 'codex-2', 'codex-3')
    AND model = 'gpt-6-luna' AND effort IN ('high-fast', 'high-ultrafast');
UPDATE web_apps SET agent_effort = CASE WHEN agent_effort = 'ultra' THEN 'max' ELSE 'high' END
WHERE agent_runtime IN ('codex', 'codex-2', 'codex-3') AND agent_model = 'gpt-6-luna'
    AND agent_effort IN ('ultra', 'high-fast', 'high-ultrafast');
UPDATE schedules SET effort = CASE WHEN effort = 'ultra' THEN 'max' ELSE 'high' END
WHERE agent_runtime IN ('codex', 'codex-2', 'codex-3') AND model = 'gpt-6-luna'
    AND effort IN ('ultra', 'high-fast', 'high-ultrafast');

ALTER TABLE thread_sessions ADD CONSTRAINT thread_sessions_options_check CHECK (
    (
        agent_runtime IN ('codex', 'codex-2', 'codex-3')
        AND (
            (model IN ('gpt-6.2-sol', 'gpt-6.2-astra')
                AND effort IN ('high', 'max', 'ultra', 'high-fast', 'high-ultrafast'))
            OR (model = 'gpt-6-luna' AND effort IN ('high', 'max'))
        )
    )
    OR
    (
        agent_runtime = 'claude_code'
        AND model IN ('claude-opus-5-5', 'claude-fable-5-2', 'claude-sonnet-5-5')
        AND effort IN ('high', 'max', 'ultracode')
    )
    OR
    (
        agent_runtime IN ('grok', 'grok-2')
        AND model = 'grok-4.7'
        AND effort IN ('xhigh', 'high')
    )
    OR
    (
        agent_runtime = 'hermes'
        AND model IN ('deepseek.v3.2', 'qwen.qwen3-coder-next', 'us.moonshotai.kimi-k3.1', 'us.zai.glm-5.3', 'minimax.minimax-m2.5')
        AND effort = 'high'
    )
    OR (agent_runtime = 'script' AND model = 'bash' AND effort = 'fixed')
);
