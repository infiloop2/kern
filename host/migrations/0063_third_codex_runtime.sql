-- Add one fixed third Codex runtime with its own account anchor, proxy pin,
-- OAuth row, local Codex home, and durable thread sessions.

-- migrate:up
SET LOCAL search_path TO public;

ALTER TABLE provider_accounts DROP CONSTRAINT provider_accounts_provider_check;
ALTER TABLE provider_accounts
    ADD CONSTRAINT provider_accounts_provider_check
    CHECK (provider IN ('openai', 'openai-2', 'openai-3', 'claude', 'bedrock', 'xai', 'xai-2'));

ALTER TABLE proxy_provider_pins DROP CONSTRAINT proxy_provider_pins_provider_check;
ALTER TABLE proxy_provider_pins
    ADD CONSTRAINT proxy_provider_pins_provider_check
    CHECK (provider IN ('openai', 'openai-2', 'openai-3', 'claude', 'xai', 'xai-2'));

ALTER TABLE oauth_logins DROP CONSTRAINT oauth_logins_runtime_check;
ALTER TABLE oauth_logins
    ADD CONSTRAINT oauth_logins_runtime_check
    CHECK (runtime IN ('codex', 'codex-2', 'codex-3', 'claude', 'grok', 'grok-2'));

ALTER TABLE oauth_logins DROP CONSTRAINT oauth_logins_check;
ALTER TABLE oauth_logins
    ADD CONSTRAINT oauth_logins_check
    CHECK (
        (runtime IN ('codex', 'codex-2', 'codex-3', 'grok', 'grok-2') AND device_code IS NOT NULL AND login_id IS NOT NULL)
        OR (runtime = 'claude' AND device_code IS NULL AND login_id IS NULL)
    );

CREATE OR REPLACE FUNCTION provider_account_anchor_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    old_anchored boolean;
    new_anchored boolean;
BEGIN
    old_anchored := OLD.account_id IS NOT NULL AND (
        (OLD.provider = 'claude' AND COALESCE(OLD.metadata->>'identity_attestation', '') = 'anthropic_oauth_profile')
        OR (OLD.provider IN ('openai', 'openai-2', 'openai-3') AND COALESCE(OLD.metadata->>'operator_approval', '') = 'codex_device_login')
        OR (OLD.provider IN ('xai', 'xai-2') AND COALESCE(OLD.metadata->>'operator_approval', '') = 'grok_device_login')
    );
    IF NOT old_anchored THEN
        IF TG_OP = 'DELETE' THEN
            RETURN OLD;
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'provider account anchor for % cannot be deleted; clear it with a linked-account reset', OLD.provider;
    END IF;
    IF NEW.account_id IS NULL THEN
        RETURN NEW;
    END IF;
    new_anchored := (NEW.provider = 'claude' AND COALESCE(NEW.metadata->>'identity_attestation', '') = 'anthropic_oauth_profile')
        OR (NEW.provider IN ('openai', 'openai-2', 'openai-3') AND COALESCE(NEW.metadata->>'operator_approval', '') = 'codex_device_login')
        OR (NEW.provider IN ('xai', 'xai-2') AND COALESCE(NEW.metadata->>'operator_approval', '') = 'grok_device_login');
    IF NEW.account_id <> OLD.account_id OR NOT new_anchored THEN
        RAISE EXCEPTION 'provider account anchor for % is immutable; reset the linked account before anchoring another account', OLD.provider;
    END IF;
    RETURN NEW;
END $$;

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

-- migrate:down
SET LOCAL search_path TO public;

-- Preserve the canonical rows that name the retired runtime so the older host
-- can still list each Codex 3 conversation and serve its transcript. Their
-- provider sessions belong to the `.codex-3` home and its own account, which
-- that host no longer launches, so clear only that provider-specific identity.
-- The model and effort values are shared across the Codex runtimes and stay as
-- selected.
UPDATE schedules SET agent_runtime = 'codex-2' WHERE agent_runtime = 'codex-3';
UPDATE web_apps SET agent_runtime = 'codex-2' WHERE agent_runtime = 'codex-3';
UPDATE thread_sessions
SET agent_runtime = 'codex-2', provider_session_id = NULL
WHERE agent_runtime = 'codex-3';

DELETE FROM proxy_provider_pins WHERE provider = 'openai-3';
UPDATE provider_accounts SET account_id = NULL WHERE provider = 'openai-3';
DELETE FROM provider_accounts WHERE provider = 'openai-3';
DELETE FROM oauth_logins WHERE runtime = 'codex-3';

ALTER TABLE thread_sessions DROP CONSTRAINT thread_sessions_options_check;
ALTER TABLE thread_sessions ADD CONSTRAINT thread_sessions_options_check CHECK (
    (
        agent_runtime IN ('codex', 'codex-2')
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

CREATE OR REPLACE FUNCTION provider_account_anchor_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    old_anchored boolean;
    new_anchored boolean;
BEGIN
    old_anchored := OLD.account_id IS NOT NULL AND (
        (OLD.provider = 'claude' AND COALESCE(OLD.metadata->>'identity_attestation', '') = 'anthropic_oauth_profile')
        OR (OLD.provider IN ('openai', 'openai-2') AND COALESCE(OLD.metadata->>'operator_approval', '') = 'codex_device_login')
        OR (OLD.provider IN ('xai', 'xai-2') AND COALESCE(OLD.metadata->>'operator_approval', '') = 'grok_device_login')
    );
    IF NOT old_anchored THEN
        IF TG_OP = 'DELETE' THEN
            RETURN OLD;
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'provider account anchor for % cannot be deleted; clear it with a linked-account reset', OLD.provider;
    END IF;
    IF NEW.account_id IS NULL THEN
        RETURN NEW;
    END IF;
    new_anchored := (NEW.provider = 'claude' AND COALESCE(NEW.metadata->>'identity_attestation', '') = 'anthropic_oauth_profile')
        OR (NEW.provider IN ('openai', 'openai-2') AND COALESCE(NEW.metadata->>'operator_approval', '') = 'codex_device_login')
        OR (NEW.provider IN ('xai', 'xai-2') AND COALESCE(NEW.metadata->>'operator_approval', '') = 'grok_device_login');
    IF NEW.account_id <> OLD.account_id OR NOT new_anchored THEN
        RAISE EXCEPTION 'provider account anchor for % is immutable; reset the linked account before anchoring another account', OLD.provider;
    END IF;
    RETURN NEW;
END $$;

ALTER TABLE oauth_logins DROP CONSTRAINT oauth_logins_check;
ALTER TABLE oauth_logins
    ADD CONSTRAINT oauth_logins_check
    CHECK (
        (runtime IN ('codex', 'codex-2', 'grok', 'grok-2') AND device_code IS NOT NULL AND login_id IS NOT NULL)
        OR (runtime = 'claude' AND device_code IS NULL AND login_id IS NULL)
    );

ALTER TABLE oauth_logins DROP CONSTRAINT oauth_logins_runtime_check;
ALTER TABLE oauth_logins
    ADD CONSTRAINT oauth_logins_runtime_check
    CHECK (runtime IN ('codex', 'codex-2', 'claude', 'grok', 'grok-2'));

ALTER TABLE proxy_provider_pins DROP CONSTRAINT proxy_provider_pins_provider_check;
ALTER TABLE proxy_provider_pins
    ADD CONSTRAINT proxy_provider_pins_provider_check
    CHECK (provider IN ('openai', 'openai-2', 'claude', 'xai', 'xai-2'));

ALTER TABLE provider_accounts DROP CONSTRAINT provider_accounts_provider_check;
ALTER TABLE provider_accounts
    ADD CONSTRAINT provider_accounts_provider_check
    CHECK (provider IN ('openai', 'openai-2', 'claude', 'bedrock', 'xai', 'xai-2'));
