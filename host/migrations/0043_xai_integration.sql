-- The xAI managed network integration: an enablement row, an account record
-- for the operator-approved xAI identity, and the proxy pin its guard reads.
--
-- The anchor marker follows the Codex pattern rather than Claude's: the xAI
-- account id is captured from the exact ACP server whose login completed and
-- subsequent token claims must match it, so an operator-completed device login
-- is the approval evidence, recorded as
-- operator_approval = 'grok_device_login'. Rows without that marker are
-- legacy/unapproved state and never publish a proxy pin.
--
-- This lands the integration and the Grok runtime together: the connection
-- state above, and the durable session-row constraint below. They were two
-- migrations while the branch was in review and were never deployed apart, so
-- they are one here -- an upgrade either gains a usable Grok runtime or gains
-- nothing, and there is no ordering between halves to reason about.

-- migrate:up

-- The xAI integration now owns the x.ai and grok.com apexes, and a custom rule
-- naming a managed apex is rejected at config parse time. An upgraded host
-- that already had one would therefore fail to parse its whole stored policy,
-- and the proxy answers a policy it cannot load with network_policy_unavailable
-- -- denying every agent request, not only xAI traffic. Remove the legacy
-- rules here so the reservation cannot take egress down on upgrade. Mirrors
-- 0003_reserve_github_actions_blob_domains. Foreign-key cascades remove the
-- method and path rows belonging to each deleted custom domain.
DELETE FROM allowed_domains
WHERE
    domain IN ('x.ai', 'grok.com')
    OR domain LIKE '%.x.ai'
    OR domain LIKE '%.grok.com'
    OR (
        domain LIKE '*.%'
        AND (
            'x.ai' LIKE '%.' || substring(domain FROM 3)
            OR 'grok.com' LIKE '%.' || substring(domain FROM 3)
        )
    );

ALTER TABLE provider_accounts DROP CONSTRAINT provider_accounts_provider_check;
ALTER TABLE provider_accounts
    ADD CONSTRAINT provider_accounts_provider_check
    CHECK (provider IN ('openai', 'claude', 'bedrock', 'xai'));

ALTER TABLE proxy_provider_pins DROP CONSTRAINT proxy_provider_pins_provider_check;
ALTER TABLE proxy_provider_pins
    ADD CONSTRAINT proxy_provider_pins_provider_check
    CHECK (provider IN ('openai', 'claude', 'xai'));

ALTER TABLE managed_integrations DROP CONSTRAINT managed_integrations_integration_check;
ALTER TABLE managed_integrations
    ADD CONSTRAINT managed_integrations_integration_check
    CHECK (integration IN (
        'openai', 'claude', 'xai', 'bedrock', 'github', 'python_packages', 'npm_packages'
    ));

ALTER TABLE oauth_logins DROP CONSTRAINT oauth_logins_runtime_check;
ALTER TABLE oauth_logins
    ADD CONSTRAINT oauth_logins_runtime_check
    CHECK (runtime IN ('codex', 'claude', 'grok'));

ALTER TABLE oauth_logins DROP CONSTRAINT oauth_logins_check;
ALTER TABLE oauth_logins
    ADD CONSTRAINT oauth_logins_check
    CHECK (
        (runtime IN ('codex', 'grok') AND device_code IS NOT NULL AND login_id IS NOT NULL)
        OR (runtime = 'claude' AND device_code IS NULL AND login_id IS NULL)
    );

-- No xai_settings table, deliberately. Claude's equivalent exists to hold a
-- web-search toggle; the xAI integration has no options at all, because Grok's
-- server-side web search is not offered on this host (see the integration
-- doc). managed_integrations is presence-based and carries enablement, which
-- is the whole of this integration's configuration.

-- Status probes must be able to re-check an approved login while the
-- data-plane pin is clear, without reopening inference. Derive that narrow
-- capability from the immutable operator-approved anchor rather than keeping
-- a second mutable copy in sync. The guard consults this view only for status
-- routes; responses and chat/completions require proxy_provider_pins.
CREATE VIEW xai_status_probe_pin AS
SELECT accounts.account_id
FROM provider_accounts AS accounts
JOIN managed_integrations AS integrations
    ON integrations.integration = 'xai'
WHERE accounts.provider = 'xai'
  AND accounts.account_id IS NOT NULL
  AND accounts.metadata->>'operator_approval' = 'grok_device_login';
GRANT SELECT ON xai_status_probe_pin TO "kern-proxy";

-- Extend the anchored-account guard to xAI. Same rule as the other providers:
-- once anchored, the account id cannot be silently swapped or the row deleted;
-- only a linked-account reset (clearing account_id) releases it.
CREATE OR REPLACE FUNCTION provider_account_anchor_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    old_anchored boolean;
    new_anchored boolean;
BEGIN
    -- COALESCE keeps every branch two-valued: a missing marker key yields
    -- SQL NULL, and a NULL condition would silently skip the guard.
    old_anchored := OLD.account_id IS NOT NULL AND (
        (OLD.provider = 'claude' AND COALESCE(OLD.metadata->>'identity_attestation', '') = 'anthropic_oauth_profile')
        OR (OLD.provider = 'openai' AND COALESCE(OLD.metadata->>'operator_approval', '') = 'codex_device_login')
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
        RETURN NEW;  -- the operator reset clearing the anchor
    END IF;
    new_anchored := (NEW.provider = 'claude' AND COALESCE(NEW.metadata->>'identity_attestation', '') = 'anthropic_oauth_profile')
        OR (NEW.provider = 'openai' AND COALESCE(NEW.metadata->>'operator_approval', '') = 'codex_device_login')
        OR (NEW.provider = 'xai' AND COALESCE(NEW.metadata->>'operator_approval', '') = 'grok_device_login');
    IF NEW.account_id <> OLD.account_id OR NOT new_anchored THEN
        RAISE EXCEPTION 'provider account anchor for % is immutable; reset the linked account before anchoring another account', OLD.provider;
    END IF;
    RETURN NEW;
END $$;

-- Grok Build is now an interactive ACP runtime. Keep the durable session-row
-- constraint aligned with host/session_options.py so the API never offers a
-- model/effort pair PostgreSQL rejects at first admission.

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

-- Unwind in the reverse of the order above: the session constraint first,
-- then the connection state it was added alongside.

SET LOCAL search_path TO public;

-- The older host cannot execute Grok turns. Preserve their transcript events,
-- but remove the provider mapping so the prior constraint can be restored.
DELETE FROM thread_sessions WHERE agent_runtime = 'grok';

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

-- Legacy custom rules removed above are not restored. Operator rules cannot be
-- reconstructed safely, so rolling back the ownership leaves them absent
-- rather than inventing permissions -- the same choice 0003 made.
DROP VIEW xai_status_probe_pin;

-- Remove xAI state before narrowing the constraints back, so rolling back a
-- host that had the integration enabled does not fail on its own rows.
DELETE FROM managed_integrations WHERE integration = 'xai';
DELETE FROM proxy_provider_pins WHERE provider = 'xai';
UPDATE provider_accounts SET account_id = NULL WHERE provider = 'xai';
DELETE FROM provider_accounts WHERE provider = 'xai';
DELETE FROM oauth_logins WHERE runtime = 'grok';

CREATE OR REPLACE FUNCTION provider_account_anchor_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    old_anchored boolean;
    new_anchored boolean;
BEGIN
    old_anchored := OLD.account_id IS NOT NULL AND (
        (OLD.provider = 'claude' AND COALESCE(OLD.metadata->>'identity_attestation', '') = 'anthropic_oauth_profile')
        OR (OLD.provider = 'openai' AND COALESCE(OLD.metadata->>'operator_approval', '') = 'codex_device_login')
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
        OR (NEW.provider = 'openai' AND COALESCE(NEW.metadata->>'operator_approval', '') = 'codex_device_login');
    IF NEW.account_id <> OLD.account_id OR NOT new_anchored THEN
        RAISE EXCEPTION 'provider account anchor for % is immutable; reset the linked account before anchoring another account', OLD.provider;
    END IF;
    RETURN NEW;
END $$;

ALTER TABLE managed_integrations DROP CONSTRAINT managed_integrations_integration_check;
ALTER TABLE managed_integrations
    ADD CONSTRAINT managed_integrations_integration_check
    CHECK (integration IN (
        'openai', 'claude', 'bedrock', 'github', 'python_packages', 'npm_packages'
    ));

ALTER TABLE oauth_logins DROP CONSTRAINT oauth_logins_check;
ALTER TABLE oauth_logins
    ADD CONSTRAINT oauth_logins_check
    CHECK (
        (runtime = 'codex' AND device_code IS NOT NULL AND login_id IS NOT NULL)
        OR (runtime = 'claude' AND device_code IS NULL AND login_id IS NULL)
    );

ALTER TABLE oauth_logins DROP CONSTRAINT oauth_logins_runtime_check;
ALTER TABLE oauth_logins
    ADD CONSTRAINT oauth_logins_runtime_check
    CHECK (runtime IN ('codex', 'claude'));

ALTER TABLE proxy_provider_pins DROP CONSTRAINT proxy_provider_pins_provider_check;
ALTER TABLE proxy_provider_pins
    ADD CONSTRAINT proxy_provider_pins_provider_check
    CHECK (provider IN ('openai', 'claude'));

ALTER TABLE provider_accounts DROP CONSTRAINT provider_accounts_provider_check;
ALTER TABLE provider_accounts
    ADD CONSTRAINT provider_accounts_provider_check
    CHECK (provider IN ('openai', 'claude', 'bedrock'));
