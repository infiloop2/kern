-- The script agent runtime (host/session_options.py) runs a static bash script
-- from the agent home instead of a model turn, so a schedule can automate work
-- that needs no reasoning. It has one fixed configuration, bash/fixed, which
-- keeps thread rows the same shape as every other runtime's.

-- migrate:up
SET LOCAL search_path TO public;

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
    OR
    (
        agent_runtime = 'script'
        AND model = 'bash'
        AND effort = 'fixed'
    )
);

-- migrate:down
SET LOCAL search_path TO public;

-- A script thread has no equivalent under the older matrix, so its session row
-- cannot be folded onto another runtime the way 0004 folded the Claude
-- aliases. Drop the rows; their events stay in agent_events and remain
-- readable in the global event page, and their schedule definitions survive —
-- rolled back, a script schedule simply produces a visible failed run, the
-- documented outcome for a configuration the host no longer offers.
DELETE FROM thread_sessions WHERE agent_runtime = 'script';

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
