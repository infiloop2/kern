-- Let one bundled OAuth tool keep several independently addressable provider
-- accounts.  The connection id is host-owned and intentionally separate from
-- the provider account id: agents select the former, while reconnects still
-- verify and display the latter.

-- migrate:up
SET LOCAL search_path TO public;

ALTER TABLE tool_credentials
    ADD COLUMN connection_id TEXT NOT NULL DEFAULT 'default'
    CHECK (connection_id ~ '^[a-z][a-z0-9_-]{0,63}$');
ALTER TABLE tool_credentials DROP CONSTRAINT tool_credentials_pkey;
ALTER TABLE tool_credentials
    ADD PRIMARY KEY (tool_id, connection_id),
    ADD CONSTRAINT tool_credentials_account_unique UNIQUE (tool_id, account_id);

-- An approval is permanently bound to the connection and provider identity
-- that proposed it. Execution resolves credentials only by connection_id;
-- the copied account fields preserve identity for approvals created after
-- this migration.
ALTER TABLE tool_approvals
    ADD COLUMN connection_id TEXT NOT NULL DEFAULT '',
    ADD COLUMN account_id TEXT NOT NULL DEFAULT '',
    ADD COLUMN account_label TEXT NOT NULL DEFAULT '';

-- Before this migration an OAuth tool can have only one credential, so bind
-- retained approvals to that migrated row for execution. Their historical
-- provider identity is unknowable: the currently connected account may have
-- changed since an approval was created, so leave its account fields empty.
-- Tools without a credential are enable-only and keep the empty scope.
UPDATE tool_approvals AS approval
SET connection_id = credential.connection_id
FROM tool_credentials AS credential
WHERE credential.tool_id = approval.tool_id;

-- Audit rows keep the same non-secret identity context.  Empty values mean an
-- enable-only tool or an event created before connection profiles existed.
ALTER TABLE tool_events
    ADD COLUMN connection_id TEXT NOT NULL DEFAULT '',
    ADD COLUMN account_id TEXT NOT NULL DEFAULT '',
    ADD COLUMN account_label TEXT NOT NULL DEFAULT '';

-- migrate:down
SET LOCAL search_path TO public;

ALTER TABLE tool_events
    DROP COLUMN account_label,
    DROP COLUMN account_id,
    DROP COLUMN connection_id;

ALTER TABLE tool_approvals
    DROP COLUMN account_label,
    DROP COLUMN account_id,
    DROP COLUMN connection_id;

-- A pre-profile schema can represent only its historical default row.
DELETE FROM tool_credentials WHERE connection_id <> 'default';
ALTER TABLE tool_credentials
    DROP CONSTRAINT tool_credentials_account_unique,
    DROP CONSTRAINT tool_credentials_pkey;
ALTER TABLE tool_credentials DROP COLUMN connection_id;
ALTER TABLE tool_credentials ADD PRIMARY KEY (tool_id);
