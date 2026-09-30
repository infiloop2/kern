-- Browser-owned durable state; temporary Chromium profiles remain in private /tmp.
-- migrate:up
CREATE TABLE browser_settings (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    mode TEXT NOT NULL CHECK (mode IN ('direct', 'decodo')),
    username TEXT,
    password_ciphertext TEXT,
    location TEXT CHECK (location IN ('new_york', 'london')),
    session TEXT,
    CHECK ((mode = 'direct' AND username IS NULL AND password_ciphertext IS NULL AND location IS NULL AND session IS NULL)
        OR (mode = 'decodo' AND username IS NOT NULL AND username ~ '^[a-zA-Z0-9_]{1,128}$'
            AND password_ciphertext IS NOT NULL AND password_ciphertext LIKE 'enc:v1:%'
            AND location IS NOT NULL AND session IS NOT NULL AND session ~ '^[a-f0-9]{24}$'))
);
CREATE TABLE browser_accounts (
    account_id TEXT PRIMARY KEY CHECK (account_id ~ '^acct_[a-f0-9]{32}$'),
    provider TEXT NOT NULL CHECK (provider = 'x'),
    provider_identifier TEXT NOT NULL CHECK (provider_identifier ~ '^[a-z0-9_]{1,15}$'),
    state TEXT NOT NULL CHECK (state IN ('connected', 'needs_attention')),
    checked_at TEXT NOT NULL,
    usage_day DATE,
    usage_count INTEGER NOT NULL DEFAULT 0 CHECK (usage_count BETWEEN 0 AND 50),
    auth_ciphertext TEXT NOT NULL CHECK (auth_ciphertext LIKE 'enc:v1:%')
);
GRANT SELECT, INSERT, UPDATE, DELETE ON browser_settings, browser_accounts TO "kern-browser";
GRANT SELECT ON secret_keys TO "kern-browser";
