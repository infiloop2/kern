-- Host-owned remote inference credentials. The admin service manages the
-- configuration; the dedicated host-inference service may only read enabled
-- providers and decrypt their keys for peer-authenticated host requests.
-- migrate:up
SET LOCAL search_path TO public;

CREATE TABLE host_inference_providers (
    provider TEXT PRIMARY KEY CHECK (provider IN ('openai', 'typesafe')),
    enabled BOOLEAN NOT NULL DEFAULT FALSE,
    features JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(features) = 'object'),
    api_key_encrypted TEXT CHECK (
        api_key_encrypted IS NULL
        OR (api_key_encrypted LIKE 'enc:v1:%' AND octet_length(api_key_encrypted) <= 4096)
    ),
    updated_at TEXT NOT NULL
);

INSERT INTO host_inference_providers
    (provider, enabled, features, api_key_encrypted, updated_at)
VALUES
    ('openai', FALSE, '{}'::jsonb, NULL, '1970-01-01T00:00:00Z'),
    ('typesafe', FALSE, '{}'::jsonb, NULL, '1970-01-01T00:00:00Z');

GRANT SELECT ON host_inference_providers TO "kern-host-inference";
GRANT SELECT ON secret_keys TO "kern-host-inference";

-- migrate:down
SET LOCAL search_path TO public;
DROP TABLE host_inference_providers;
