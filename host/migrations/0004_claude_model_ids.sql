-- migrate:up
-- The Claude Code catalog (host/session_options.py) now offers exact model
-- ids instead of the unversioned aliases, so new threads record which model
-- generation they run. The alias values stay accepted here so that threads
-- created before this migration keep their stored model and stay readable;
-- the admin API refuses to run further tasks on them, and nothing new is
-- written with an alias.
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
            'claude-opus-5', 'claude-fable-5', 'claude-sonnet-5',
            'opus', 'fable', 'sonnet'
        )
        AND effort IN ('high', 'max', 'ultracode')
    )
    OR
    (
        agent_runtime = 'hermes'
        AND model IN ('deepseek.v3.2', 'qwen.qwen3-coder-next', 'moonshotai.kimi-k2.5')
        AND effort = 'high'
    )
);

-- migrate:down
-- Rolling back restores the older Claude Code CLI pin, where the aliases are
-- the only accepted model values. Fold the pinned ids back onto the alias of
-- the same family rather than deleting the threads that used them.
UPDATE thread_sessions
SET model = CASE model
        WHEN 'claude-opus-5' THEN 'opus'
        WHEN 'claude-fable-5' THEN 'fable'
        WHEN 'claude-sonnet-5' THEN 'sonnet'
    END
WHERE agent_runtime = 'claude_code'
    AND model IN ('claude-opus-5', 'claude-fable-5', 'claude-sonnet-5');
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
        AND model IN ('opus', 'fable', 'sonnet')
        AND effort IN ('high', 'max', 'ultracode')
    )
    OR
    (
        agent_runtime = 'hermes'
        AND model IN ('deepseek.v3.2', 'qwen.qwen3-coder-next', 'moonshotai.kimi-k2.5')
        AND effort = 'high'
    )
);
