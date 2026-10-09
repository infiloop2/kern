-- LinkedIn shares the private Browser store and five-account capacity.
-- Existing X connections and usage retain their meaning.
-- migrate:up
ALTER TABLE browser_accounts DROP CONSTRAINT browser_accounts_provider_check;
ALTER TABLE browser_accounts DROP CONSTRAINT browser_accounts_provider_identifier_check;
ALTER TABLE browser_accounts ADD CONSTRAINT browser_accounts_provider_check
    CHECK (provider IN ('x', 'linkedin'));
ALTER TABLE browser_accounts ADD CONSTRAINT browser_accounts_provider_identifier_check
    CHECK ((provider = 'x' AND provider_identifier ~ '^[a-z0-9_]{1,15}$')
        OR (provider = 'linkedin' AND provider_identifier ~ '^https://www[.]linkedin[.]com/in/[a-z0-9_-]{1,200}/$'));
