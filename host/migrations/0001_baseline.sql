-- Consolidated baseline schema for the Kern host admin state.
--
-- Kern 1.0.0 is a fresh start: there are no deployed hosts to migrate, so the
-- prior incremental history collapses into this single genesis migration that
-- provisions the final schema and seed state directly. Roles are the kern-*
-- service accounts (peer auth maps each OS user to its same-named Postgres
-- role); the proxy, tools, and agent-network roles are created by bootstrap
-- (and the test harness) before this migration runs, so its GRANTs resolve.


-- migrate:up

-- Deploy-provided host configuration, one row by constraint (the singleton
-- key). Replaced on every deploy/reconfigure; upgrade and recover carry the
-- stored credentials forward. The checks mirror host/config.py's validation
-- as a storage-level backstop.
CREATE TABLE config (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    agent_name TEXT CHECK (agent_name ~ '^[A-Za-z0-9_-]{1,50}$'),
    admin_password_sha256 TEXT CHECK (admin_password_sha256 ~ '^[0-9a-f]{64}$')
);

-- Operator access endpoints. mode as the primary key makes duplicate modes
-- impossible by construction; the row check enforces exactly the fields each
-- mode requires.
CREATE TABLE operator_connections (
    mode TEXT PRIMARY KEY CHECK (mode IN ('ssh', 'cloudflare_tunnel')),
    ssh_public_key TEXT CHECK (ssh_public_key LIKE 'ssh-ed25519 %' OR ssh_public_key LIKE 'ssh-rsa %'),
    hostname TEXT CHECK (hostname <> ''),
    tunnel_token TEXT CHECK (tunnel_token <> '' AND tunnel_token !~ '\s'),
    CHECK (
        (mode = 'ssh' AND ssh_public_key IS NOT NULL AND hostname IS NULL AND tunnel_token IS NULL)
        OR (mode = 'cloudflare_tunnel' AND ssh_public_key IS NULL AND hostname IS NOT NULL AND tunnel_token IS NOT NULL)
    )
);

-- Monotonic counters (next_task_number). A plain row instead of a sequence so
-- the number allocation rolls back with its transaction and task numbering
-- stays dense.
CREATE TABLE counters (
    name TEXT PRIMARY KEY,
    value BIGINT NOT NULL
);

-- User thread -> provider session/thread mappings. A thread id is the single
-- session identity: tasks reference it and derive all session configuration
-- (runtime, model, effort) through this row.
CREATE TABLE thread_sessions (
    agent_runtime TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    provider_session_id TEXT,
    last_used_at TEXT,
    model TEXT NOT NULL,
    effort TEXT NOT NULL,
    PRIMARY KEY (thread_id),
    CONSTRAINT thread_sessions_options_check CHECK (
        (
            agent_runtime = 'codex'
            AND model IN ('gpt-5.6-terra', 'gpt-5.6-sol', 'gpt-5.6-luna')
            AND effort IN ('high', 'max', 'ultra')
            AND NOT (model = 'gpt-5.6-luna' AND effort = 'ultra')
        )
        OR
        (
            agent_runtime = 'claude_code'
            AND model IN ('opus', 'fable', 'sonnet')
            AND effort IN ('high', 'max', 'ultracode')
        )
        OR
        (
            agent_runtime = 'hermes'
            AND model IN ('deepseek.v3.2', 'qwen.qwen3-coder-next', 'moonshotai.kimi-k2.5')
            AND effort = 'high'
        )
    )
);
-- LRU pruning of the mapping caps.
CREATE INDEX thread_sessions_last_used_idx ON thread_sessions (agent_runtime, last_used_at);

-- The task queue and its bounded history. number (from the dense counter) is
-- the task identity; the public "task_<number>" identifier is just its label,
-- formatted by the storage accessors. Session configuration is derived through
-- the thread_sessions row this task references.
CREATE TABLE tasks (
    number BIGINT PRIMARY KEY,
    status TEXT NOT NULL,
    thread_id TEXT NOT NULL REFERENCES thread_sessions (thread_id),
    input_message TEXT,
    output_message TEXT,
    error_message TEXT,
    created_at TEXT,
    updated_at TEXT
);
-- The hot paths: active-task lookups (claiming, health, listing), per-thread
-- history, and history pruning by recency.
CREATE INDEX tasks_status_idx ON tasks (status);
CREATE INDEX tasks_thread_id_idx ON tasks (thread_id);
CREATE INDEX tasks_status_updated_idx ON tasks (status, updated_at, number);

-- Undelivered steer messages for a running task, ordered by id (delivered
-- steers are deleted; their content lives on as task.message events). Capped
-- per task at the API (20).
CREATE TABLE task_steers (
    id BIGSERIAL PRIMARY KEY,
    task_number BIGINT NOT NULL REFERENCES tasks (number) ON DELETE CASCADE,
    message TEXT NOT NULL
);
CREATE INDEX task_steers_task_number_idx ON task_steers (task_number, id);

-- Agent runtime and task events (previously events.jsonl). seq is a serial:
-- an aborted transaction burns a value, and since-based pagination only needs
-- seq to be unique and increasing. The API's event_id "event_<seq>" is
-- derived on read.
CREATE TABLE agent_events (
    seq BIGSERIAL PRIMARY KEY,
    created_at TEXT NOT NULL,
    event_type TEXT NOT NULL,
    task_id TEXT,
    -- The payload fields, typed: task.message events carry message+source,
    -- task.failed carries error_message, agent_runtime.* carries
    -- agent_runtime, and lifecycle events carry nothing.
    message TEXT,
    source TEXT CHECK (source IN ('user', 'agent')),
    error_message TEXT,
    agent_runtime TEXT
);
CREATE INDEX agent_events_task_id_idx ON agent_events (task_id, seq);

-- In-flight OAuth logins, one per provider flow: Codex uses a device-code
-- flow (device_code + a login-server handle), Claude a browser-code flow.
-- access_token_sha256 pins the token minted by the completed flow.
CREATE TABLE oauth_logins (
    runtime TEXT PRIMARY KEY CHECK (runtime IN ('codex', 'claude')),
    status TEXT NOT NULL,
    login_url TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    device_code TEXT,
    login_id TEXT,
    access_token_sha256 TEXT,
    CHECK (
        (runtime = 'codex' AND device_code IS NOT NULL AND login_id IS NOT NULL)
        OR (runtime = 'claude' AND device_code IS NULL AND login_id IS NULL)
    )
);

-- Admin-side provider account records. account_id is promoted for direct
-- queries; metadata holds the provider CLI's own evolving shape (usage
-- blocks, plan fields, organization data) cached verbatim — deliberately not
-- typed here, because its schema belongs to the provider and changes with
-- CLI versions; the runtime treats it as opaque display metadata.
CREATE TABLE provider_accounts (
    provider TEXT PRIMARY KEY CHECK (provider IN ('openai', 'claude', 'bedrock')),
    account_id TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- The anchored-account guard: once a provider account is anchored by its
-- attestation marker, it cannot be silently swapped or deleted; only an
-- explicit linked-account reset (clearing account_id) releases it.
CREATE FUNCTION provider_account_anchor_guard() RETURNS trigger
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
        OR (NEW.provider = 'openai' AND COALESCE(NEW.metadata->>'operator_approval', '') = 'codex_device_login');
    IF NEW.account_id <> OLD.account_id OR NOT new_anchored THEN
        RAISE EXCEPTION 'provider account anchor for % is immutable; reset the linked account before anchoring another account', OLD.provider;
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER provider_accounts_anchor_guard
BEFORE UPDATE OR DELETE ON provider_accounts
FOR EACH ROW EXECUTE FUNCTION provider_account_anchor_guard();

-- Network allow/deny decisions (previously proxy-owned network_events.jsonl).
-- The proxy service writes them under its own role; that role gets exactly
-- this table and nothing else — the enforcement inputs (network policy,
-- account pins, CA material) stay proxy-owned files, and the rest of admin
-- state stays out of the proxy's reach. The agent-network role reads the log
-- for its introspection view.
CREATE TABLE network_events (
    seq BIGSERIAL PRIMARY KEY,
    created_at TEXT NOT NULL,
    protocol TEXT NOT NULL,
    method TEXT NOT NULL,
    host TEXT NOT NULL,
    port BIGINT NOT NULL,
    path TEXT NOT NULL,
    query TEXT NOT NULL,
    decision TEXT NOT NULL,
    reason_code TEXT
);
CREATE INDEX network_events_decision_seq_idx ON network_events (decision, seq);
GRANT SELECT, INSERT, DELETE ON network_events TO "kern-proxy";
GRANT USAGE, SELECT ON SEQUENCE network_events_seq_seq TO "kern-proxy";

-- The active network policy, as rows: the shape is defined by
-- host/config.py, so its parts are typed. The admin service (schema owner)
-- replaces all of it in one transaction after validation; the proxy only
-- reads. A missing network_policy row means the fail-closed default (empty
-- policy) — nothing seeds these. Managed integration access is presence-based
-- (a row means enabled); method and guard lists keep their operator-given
-- order via position.
CREATE TABLE network_policy (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    updated_at TEXT NOT NULL
);
GRANT SELECT ON network_policy TO "kern-proxy";

CREATE TABLE allowed_domains (
    domain TEXT PRIMARY KEY CHECK (domain <> '')
);
GRANT SELECT ON allowed_domains TO "kern-proxy";

CREATE TABLE domain_methods (
    domain TEXT NOT NULL REFERENCES allowed_domains (domain) ON DELETE CASCADE,
    position BIGINT NOT NULL,
    method TEXT NOT NULL CHECK (method IN ('GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE')),
    PRIMARY KEY (domain, position)
);
GRANT SELECT ON domain_methods TO "kern-proxy";

CREATE TABLE domain_path_guards (
    domain TEXT NOT NULL REFERENCES allowed_domains (domain) ON DELETE CASCADE,
    position BIGINT NOT NULL,
    pattern TEXT NOT NULL CHECK (pattern <> ''),
    PRIMARY KEY (domain, position)
);
GRANT SELECT ON domain_path_guards TO "kern-proxy";

-- The provider account pins the proxy guards check: exactly the two values
-- the guards compare; the proxy never receives the rest of the account
-- metadata. A missing row means no pin — fail closed.
CREATE TABLE proxy_provider_pins (
    provider TEXT PRIMARY KEY CHECK (provider IN ('openai', 'claude')),
    account_id TEXT,
    access_token_sha256 TEXT CHECK (access_token_sha256 ~ '^[0-9a-f]{64}$')
);
GRANT SELECT ON proxy_provider_pins TO "kern-proxy";

-- Enabled managed integrations, presence-based (a row means enabled).
CREATE TABLE managed_integrations (
    integration TEXT PRIMARY KEY
        CHECK (integration IN ('openai', 'claude', 'bedrock', 'github', 'python_packages', 'npm_packages'))
);
GRANT SELECT ON managed_integrations TO "kern-proxy";

-- The repositories the agent may write to (push and mutate through the API),
-- in operator-given order. Reads are universal when GitHub is enabled, so this
-- list only names write targets; owner/repo are stored normalized (lowercase)
-- as host/config.py validates them.
CREATE TABLE github_repositories (
    position BIGINT PRIMARY KEY,
    owner TEXT NOT NULL CHECK (owner ~ '^[a-z0-9](?:[a-z0-9-]{0,37}[a-z0-9])?$'),
    repo TEXT NOT NULL CHECK (repo ~ '^[a-z0-9._-]{1,100}$' AND repo !~ '\.git$'),
    UNIQUE (owner, repo)
);
GRANT SELECT ON github_repositories TO "kern-proxy";

-- Key for at-rest encryption of stored secrets (host.runtime.secretbox).
-- Kept in the database so all admin state lives in one place, and seeded right
-- here so the key exists from the moment the schema does — no lazy
-- first-encrypt path in code. gen_random_uuid() draws from Postgres's CSPRNG
-- (pg_strong_random); two UUIDs with dashes stripped give the 64 hex chars.
-- This is an accidental-exposure control: a stray SELECT * on a
-- secret-bearing table no longer reveals credential material. A full dump of
-- the database necessarily includes this key. No proxy grant on write.
CREATE TABLE secret_keys (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    key_hex TEXT NOT NULL CHECK (key_hex ~ '^[0-9a-f]{64}$')
);
INSERT INTO secret_keys (singleton, key_hex)
    VALUES (TRUE, translate(gen_random_uuid()::text || gen_random_uuid()::text, '-', ''));
GRANT SELECT ON secret_keys TO "kern-proxy";

-- One fixed GitHub credential (admin-owned; no proxy grant). pat mode stores
-- the pasted token; app mode stores the GitHub App identity and signing key.
-- The working token the App mints lives only in proxy_github_token below.
-- Secret columns (token, private_key_pem) hold host.runtime.secretbox
-- ciphertext, so database contents alone never expose credential material;
-- the checks are therefore shape-light.
CREATE TABLE github_credential (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    mode TEXT NOT NULL CHECK (mode IN ('pat', 'app')),
    token TEXT CHECK (token IS NULL OR (token <> '' AND token !~ '\s')),
    app_id TEXT CHECK (app_id IS NULL OR app_id ~ '^[0-9]{1,20}$'),
    installation_id TEXT CHECK (installation_id IS NULL OR installation_id ~ '^[0-9]{1,20}$'),
    private_key_pem TEXT CHECK (private_key_pem IS NULL OR private_key_pem <> ''),
    updated_at TEXT NOT NULL,
    validation JSONB NOT NULL,
    CHECK (
        (mode = 'pat' AND token IS NOT NULL
            AND app_id IS NULL AND installation_id IS NULL AND private_key_pem IS NULL)
        OR
        (mode = 'app' AND token IS NULL
            AND app_id IS NOT NULL AND installation_id IS NOT NULL AND private_key_pem IS NOT NULL)
    )
);

CREATE TABLE proxy_github_token (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    token TEXT NOT NULL CHECK (token <> '' AND token !~ '\s'),
    -- App-mode expiry of the minted token (NULL for a PAT): the admin
    -- service re-mints before it passes. This row is the only copy of the
    -- working token — there is no separate mint cache.
    expires_at TEXT,
    updated_at TEXT NOT NULL
);
GRANT SELECT ON proxy_github_token TO "kern-proxy";

CREATE TABLE github_repo_audit (
    owner TEXT NOT NULL CHECK (owner ~ '^[a-z0-9](?:[a-z0-9-]{0,37}[a-z0-9])?$'),
    repo TEXT NOT NULL CHECK (repo ~ '^[a-z0-9._-]{1,100}$' AND repo !~ '\.git$'),
    fetched_at TEXT NOT NULL,
    facts JSONB NOT NULL,
    error TEXT CHECK (error IS NULL OR error <> ''),
    PRIMARY KEY (owner, repo)
);

CREATE TABLE github_settings (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    require_dot_github_approval BOOLEAN NOT NULL DEFAULT FALSE
);
GRANT SELECT ON github_settings TO "kern-proxy";

CREATE TABLE pending_pushes (
    id TEXT PRIMARY KEY CHECK (id ~ '^[a-f0-9]{6,64}$'),
    owner TEXT NOT NULL CHECK (owner ~ '^[a-z0-9](?:[a-z0-9-]{0,37}[a-z0-9])?$'),
    repo TEXT NOT NULL CHECK (repo ~ '^[a-z0-9._-]{1,100}$' AND repo !~ '\.git$'),
    ref_updates JSONB NOT NULL,
    changed_paths JSONB NOT NULL,
    requested_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'resolving', 'approved', 'rejected', 'failed')),
    claimed_at TEXT,
    resolved_at TEXT,
    detail TEXT CHECK (detail IS NULL OR detail <> '')
);
-- The proxy enqueues; the admin service lists and resolves.
GRANT INSERT ON pending_pushes TO "kern-proxy";

-- Applied per-app migration versions, host-owned (host/runtime/deploy/app_migrate).
CREATE TABLE app_schema_migrations (
    app_id TEXT NOT NULL CHECK (app_id ~ '^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$'),
    version BIGINT NOT NULL,
    name TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (app_id, version)
);

-- The proxy reads the Claude web-search toggle before allowing the tool.
CREATE TABLE claude_settings (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    web_search BOOLEAN NOT NULL DEFAULT FALSE
);
GRANT SELECT ON claude_settings TO "kern-proxy";

-- The agent-network introspection role reads the policy inputs and the event
-- log to render the agent-facing network view.
GRANT SELECT ON network_policy, managed_integrations, github_repositories,
    github_settings, claude_settings, allowed_domains, domain_methods,
    domain_path_guards, network_events TO "kern-agent-network";

-- Bundled tools the operator has enabled. Presence-based (a row means
-- enabled), like managed_integrations.
CREATE TABLE enabled_tools (
    tool_id TEXT PRIMARY KEY CHECK (tool_id ~ '^[a-z][a-z0-9_]{0,63}$')
);

-- Deployment-level configuration values declared by tool manifests (OAuth
-- client ids/secrets, API keys). Keyed by (tool_id, manifest config key):
-- config is scoped per tool, so two tools that declare the same key (for
-- example GOOGLE_OAUTH_CLIENT_ID) hold their own independent value. All config
-- values are secrets, stored as secretbox ciphertext at rest (see
-- host/runtime/secretbox.py), the same accidental-exposure control as the
-- GitHub credential and tunnel token columns.
CREATE TABLE tool_config (
    tool_id TEXT NOT NULL CHECK (tool_id ~ '^[a-z][a-z0-9_]{0,63}$'),
    key TEXT NOT NULL CHECK (key ~ '^[A-Z][A-Z0-9_]{0,127}$'),
    value TEXT NOT NULL CHECK (value <> ''),
    PRIMARY KEY (tool_id, key)
);

-- Tool OAuth credentials, the store behind HostAPI.credentials. One row per
-- tool holds that tool's single StoredCredential, stored as its contract
-- fields (host/tools/host_api.py) rather than one opaque blob: the non-secret
-- connected-account metadata (stable provider account id, display label,
-- granted scopes), the provider token material, and the tool's non-secret
-- bookkeeping. Only ``secret`` is secret: it is the serialized token JSON
-- object stored as secretbox ciphertext, so OAuth access and refresh tokens
-- are encrypted at rest like every other secret column; the runtime decrypts
-- and re-parses it on read.
CREATE TABLE tool_credentials (
    tool_id TEXT PRIMARY KEY CHECK (tool_id ~ '^[a-z][a-z0-9_]{0,63}$'),
    account_id TEXT NOT NULL CHECK (account_id <> ''),
    account_label TEXT NOT NULL,
    account_scopes JSONB NOT NULL,
    secret TEXT NOT NULL CHECK (secret <> ''),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- Host-owned approval workflow records for proposed tool actions. number is
-- the record identity; the public "approval_<number>" id is formatted by the
-- storage accessors. Status transitions are atomic conditional updates from
-- the expected prior status, so an approval is single-use by construction.
-- An approved action's outcome is a single user-visible text per the tool
-- contract (ApprovalExecuted.message, or the failure error), so result is
-- text; the status column says which of the two it is.
CREATE TABLE tool_approvals (
    number BIGSERIAL PRIMARY KEY,
    tool_id TEXT NOT NULL,
    action_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'denied', 'expired', 'executed', 'failed')),
    summary TEXT NOT NULL,
    payload JSONB NOT NULL,
    check_token TEXT NOT NULL CHECK (check_token ~ '^[A-Za-z0-9_-]{32,255}$'),
    result TEXT NOT NULL DEFAULT '',
    created_at BIGINT NOT NULL CHECK (created_at >= 0),
    decided_at BIGINT NOT NULL DEFAULT 0
);
-- The hot paths: pending lists in the admin UI and the expiry sweep.
CREATE INDEX tool_approvals_status_idx ON tool_approvals (status, number);

-- Tool audit log: one row per tool event (agent call, approval decision,
-- connect/disconnect), the tool-side peer of the agent and network event
-- logs. Newest-first pages read the seq primary-key index; retention keeps
-- the most recent rows, pruned amortized on insert. arguments carries the
-- typed call arguments for agent-call rows.
CREATE TABLE tool_events (
    seq BIGSERIAL PRIMARY KEY,
    created_at TEXT NOT NULL,
    tool_id TEXT NOT NULL,
    action_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    arguments JSONB
);

-- The dedicated tools service reads tool state with its own scoped Postgres
-- role, the same pattern as the kern-proxy grants (the role and its pg_hba
-- line are provisioned by bootstrap before migrations run). Enablement and
-- config are operator actions written only by the admin API, so the tools role
-- is read-only on enabled_tools and tool_config and cannot enable a tool or
-- rewrite config; it writes only the credentials/approvals/events it mutates.
-- SELECT on secret_keys decrypts the secretbox-encrypted tool config and OAuth
-- credentials (read-only, exactly as the proxy role holds it for its own
-- secrets).
GRANT SELECT ON enabled_tools, tool_config TO "kern-tools";
GRANT SELECT, INSERT, UPDATE, DELETE ON tool_credentials, tool_approvals, tool_events TO "kern-tools";
GRANT USAGE ON SEQUENCE tool_approvals_number_seq, tool_events_seq_seq TO "kern-tools";
GRANT SELECT ON secret_keys TO "kern-tools";

-- The validated operator-connected Bedrock credential. The access key id is
-- public SigV4 metadata; the secret access key is secretbox ciphertext. The
-- proxy reads this same row and decrypts the secret only for an enabled
-- Bedrock request.
CREATE TABLE bedrock_credentials (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    access_key_id TEXT NOT NULL,
    secret_access_key_encrypted TEXT NOT NULL,
    region TEXT NOT NULL CHECK (region IN ('us-east-1', 'us-east-2', 'us-west-2'))
);
GRANT SELECT ON bedrock_credentials TO "kern-proxy";

-- Per-day Bedrock usage metering, keyed by model and day.
CREATE TABLE bedrock_usage (
    -- The invoked model id, normalized to the price catalog before recording:
    -- a catalog id is kept as-is, anything else collapses into 'other'. That
    -- bounds the row count and stops a buggy or adversarial agent from
    -- creating unbounded rows by looping over random model ids.
    model_id TEXT NOT NULL CHECK (model_id <> '' AND length(model_id) <= 256),
    day DATE NOT NULL,
    -- requests counts every allowed, forwarded invocation; metered_requests
    -- only those whose response carried a parseable usage record. The gap is
    -- the fail-visible signal for AWS errors and unparsed responses.
    requests BIGINT NOT NULL DEFAULT 0 CHECK (requests >= 0),
    metered_requests BIGINT NOT NULL DEFAULT 0 CHECK (metered_requests >= 0),
    input_tokens BIGINT NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
    output_tokens BIGINT NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
    cache_read_tokens BIGINT NOT NULL DEFAULT 0 CHECK (cache_read_tokens >= 0),
    cache_write_tokens BIGINT NOT NULL DEFAULT 0 CHECK (cache_write_tokens >= 0),
    -- USD cost accumulated at record time from the manifest price table, fixed
    -- to 6 decimal places. Final once written: read paths sum this column
    -- instead of re-pricing tokens, so historical cost is stable across rate
    -- edits. A model outside the catalog contributes 0 (its tokens still
    -- count) rather than an estimate at a guessed rate.
    cost_usd NUMERIC(18, 6) NOT NULL DEFAULT 0 CHECK (cost_usd >= 0),
    PRIMARY KEY (model_id, day)
);
-- The proxy increments with INSERT ... ON CONFLICT DO UPDATE, which reads the
-- conflicting row, so it needs SELECT alongside INSERT and UPDATE.
GRANT SELECT, INSERT, UPDATE ON bedrock_usage TO "kern-proxy";

-- migrate:down

DROP TRIGGER provider_accounts_anchor_guard ON provider_accounts;
DROP FUNCTION provider_account_anchor_guard();

DROP TABLE bedrock_usage;
DROP TABLE bedrock_credentials;
DROP TABLE tool_events;
DROP TABLE tool_approvals;
DROP TABLE tool_credentials;
DROP TABLE tool_config;
DROP TABLE enabled_tools;
DROP TABLE claude_settings;
DROP TABLE app_schema_migrations;
DROP TABLE pending_pushes;
DROP TABLE github_settings;
DROP TABLE github_repo_audit;
DROP TABLE proxy_github_token;
DROP TABLE github_credential;
DROP TABLE secret_keys;
DROP TABLE github_repositories;
DROP TABLE managed_integrations;
DROP TABLE proxy_provider_pins;
DROP TABLE domain_path_guards;
DROP TABLE domain_methods;
DROP TABLE allowed_domains;
DROP TABLE network_policy;
DROP TABLE network_events;
DROP TABLE provider_accounts;
DROP TABLE oauth_logins;
DROP TABLE agent_events;
DROP TABLE task_steers;
DROP TABLE tasks;
DROP TABLE thread_sessions;
DROP TABLE counters;
DROP TABLE operator_connections;
DROP TABLE config;
