-- Durable WebAuthn credentials for the single Kern administrator. Private
-- passkey material never reaches the host; only the public verification key
-- and non-secret authenticator metadata are retained.

-- migrate:up

CREATE TABLE admin_passkey_config (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    user_handle TEXT NOT NULL CHECK (user_handle ~ '^[A-Za-z0-9_-]{22,128}$'),
    created_at TEXT NOT NULL
);

CREATE TABLE admin_passkeys (
    credential_id TEXT PRIMARY KEY CHECK (
        length(credential_id) BETWEEN 1 AND 1366
        AND credential_id ~ '^[A-Za-z0-9_-]+$'
    ),
    rp_id TEXT NOT NULL CHECK (
        rp_id ~ '^[a-z0-9-]+(\.[a-z0-9-]+)+$'
    ),
    public_key_spki TEXT NOT NULL CHECK (
        length(public_key_spki) BETWEEN 80 AND 1024
        AND public_key_spki ~ '^[A-Za-z0-9_-]+$'
    ),
    sign_count BIGINT NOT NULL DEFAULT 0 CHECK (sign_count >= 0),
    transports JSONB NOT NULL DEFAULT '[]'::jsonb,
    backed_up BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TEXT NOT NULL,
    last_used_at TEXT
);
CREATE INDEX admin_passkeys_rp_id_idx ON admin_passkeys (rp_id);

-- migrate:down

DROP TABLE admin_passkeys;
DROP TABLE admin_passkey_config;
