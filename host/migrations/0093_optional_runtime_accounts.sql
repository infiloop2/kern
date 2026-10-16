-- Additional provider accounts require explicit operator enablement.
-- migrate:up
SET LOCAL search_path TO public;

-- Only additional accounts use this flag; primary accounts follow the
-- provider switch. Account refresh/reset updates preserve the flag.
ALTER TABLE provider_accounts
    ADD COLUMN extra_account_enabled BOOLEAN NOT NULL DEFAULT FALSE;

-- Keep existing operator-approved anchors enabled across the upgrade.
UPDATE provider_accounts SET extra_account_enabled = TRUE
WHERE account_id IS NOT NULL AND account_id <> ''
AND (
    (provider IN ('openai-2', 'openai-3') AND metadata->>'operator_approval' = 'codex_device_login')
    OR (provider = 'xai-2' AND metadata->>'operator_approval' = 'grok_device_login')
);

-- Unlinked slots cannot keep stale proxy authorization while admin starts.
DELETE FROM proxy_provider_pins AS pin
WHERE provider IN ('openai-2', 'openai-3', 'xai-2')
AND NOT EXISTS (
    SELECT 1 FROM provider_accounts AS account
    WHERE account.provider = pin.provider AND account.extra_account_enabled
);

-- Status-only Grok probes use anchors rather than data-plane pins. Apply the
-- same account opt-in there so disabling Grok 2 revokes both authorizations.
CREATE OR REPLACE VIEW xai_status_probe_pin AS
SELECT accounts.account_id
FROM provider_accounts AS accounts
JOIN managed_integrations AS integrations
    ON integrations.integration = 'xai'
WHERE (accounts.provider = 'xai' OR (accounts.provider = 'xai-2' AND accounts.extra_account_enabled))
  AND accounts.account_id IS NOT NULL
  AND accounts.metadata->>'operator_approval' = 'grok_device_login';
