-- Add one fixed second Grok runtime. It has its own OAuth row, approved
-- account anchor, proxy pin, local Grok home, and durable thread sessions;
-- both Grok runtimes continue to share the one xAI network integration.

-- migrate:up
SET LOCAL search_path TO public;

ALTER TABLE provider_accounts DROP CONSTRAINT provider_accounts_provider_check;
ALTER TABLE provider_accounts
    ADD CONSTRAINT provider_accounts_provider_check
    CHECK (provider IN ('openai', 'openai-2', 'claude', 'bedrock', 'xai', 'xai-2'));

ALTER TABLE proxy_provider_pins DROP CONSTRAINT proxy_provider_pins_provider_check;
ALTER TABLE proxy_provider_pins
    ADD CONSTRAINT proxy_provider_pins_provider_check
    CHECK (provider IN ('openai', 'openai-2', 'claude', 'xai', 'xai-2'));

ALTER TABLE oauth_logins DROP CONSTRAINT oauth_logins_runtime_check;
ALTER TABLE oauth_logins
    ADD CONSTRAINT oauth_logins_runtime_check
    CHECK (runtime IN ('codex', 'codex-2', 'claude', 'grok', 'grok-2'));

ALTER TABLE oauth_logins DROP CONSTRAINT oauth_logins_check;
ALTER TABLE oauth_logins
    ADD CONSTRAINT oauth_logins_check
    CHECK (
        (runtime IN ('codex', 'codex-2', 'grok', 'grok-2') AND device_code IS NOT NULL AND login_id IS NOT NULL)
        OR (runtime = 'claude' AND device_code IS NULL AND login_id IS NULL)
    );

CREATE OR REPLACE VIEW xai_status_probe_pin AS
SELECT accounts.account_id
FROM provider_accounts AS accounts
JOIN managed_integrations AS integrations
    ON integrations.integration = 'xai'
WHERE accounts.provider IN ('xai', 'xai-2')
  AND accounts.account_id IS NOT NULL
  AND accounts.metadata->>'operator_approval' = 'grok_device_login';

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

-- migrate:down
SET LOCAL search_path TO public;

DELETE FROM thread_sessions WHERE agent_runtime = 'grok-2';
DELETE FROM proxy_provider_pins WHERE provider = 'xai-2';
UPDATE provider_accounts SET account_id = NULL WHERE provider = 'xai-2';
DELETE FROM provider_accounts WHERE provider = 'xai-2';
DELETE FROM oauth_logins WHERE runtime = 'grok-2';

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
        agent_runtime = 'grok'
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
        OR (OLD.provider = 'xai' AND COALESCE(OLD.metadata->>'operator_approval', '') = 'grok_device_login')
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
        OR (NEW.provider = 'xai' AND COALESCE(NEW.metadata->>'operator_approval', '') = 'grok_device_login');
    IF NEW.account_id <> OLD.account_id OR NOT new_anchored THEN
        RAISE EXCEPTION 'provider account anchor for % is immutable; reset the linked account before anchoring another account', OLD.provider;
    END IF;
    RETURN NEW;
END $$;

CREATE OR REPLACE VIEW xai_status_probe_pin AS
SELECT accounts.account_id
FROM provider_accounts AS accounts
JOIN managed_integrations AS integrations
    ON integrations.integration = 'xai'
WHERE accounts.provider = 'xai'
  AND accounts.account_id IS NOT NULL
  AND accounts.metadata->>'operator_approval' = 'grok_device_login';

ALTER TABLE oauth_logins DROP CONSTRAINT oauth_logins_check;
ALTER TABLE oauth_logins
    ADD CONSTRAINT oauth_logins_check
    CHECK (
        (runtime IN ('codex', 'codex-2', 'grok') AND device_code IS NOT NULL AND login_id IS NOT NULL)
        OR (runtime = 'claude' AND device_code IS NULL AND login_id IS NULL)
    );

ALTER TABLE oauth_logins DROP CONSTRAINT oauth_logins_runtime_check;
ALTER TABLE oauth_logins
    ADD CONSTRAINT oauth_logins_runtime_check
    CHECK (runtime IN ('codex', 'codex-2', 'claude', 'grok'));

ALTER TABLE proxy_provider_pins DROP CONSTRAINT proxy_provider_pins_provider_check;
ALTER TABLE proxy_provider_pins
    ADD CONSTRAINT proxy_provider_pins_provider_check
    CHECK (provider IN ('openai', 'openai-2', 'claude', 'xai'));

ALTER TABLE provider_accounts DROP CONSTRAINT provider_accounts_provider_check;
ALTER TABLE provider_accounts
    ADD CONSTRAINT provider_accounts_provider_check
    CHECK (provider IN ('openai', 'openai-2', 'claude', 'bedrock', 'xai'));
