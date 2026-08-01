-- The temporary row that says which account is currently allowed through the
-- proxy needs only the identity already approved in provider_accounts. Claude
-- bearer hashes rotate and are verified against that identity at request time,
-- so they do not belong in durable proxy state.

-- migrate:up

ALTER TABLE proxy_provider_pins DROP COLUMN access_token_sha256;

-- migrate:down

ALTER TABLE proxy_provider_pins
    ADD COLUMN access_token_sha256 TEXT CHECK (access_token_sha256 ~ '^[0-9a-f]{64}$');
