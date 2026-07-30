# Admin State Storage and Migrations

Admin, network, app, and tool state live in a local PostgreSQL database,
`kern_admin`, served by `kern-postgres.service`. Kern
services use the in-repo standard-library wire-protocol client
(`host/runtime/core/pgclient.py`), which implements only Unix sockets, peer auth,
and text-format values. PostgreSQL has no TCP listener
(`listen_addresses = ''`); the socket is
`/var/run/postgresql/.s.PGSQL.5432`.

The admin service owns the database. The proxy, tools service, and each app
connect under separate peer-authenticated roles with grants limited to the
tables or schema their process needs. There is no proxy fallback cache: a
database outage denies agent network requests until the database returns.
The remaining writable admin-volume paths are the proxy CA/certificates and
Git quarantine mirror under `proxy-state`, the bounded temporary media spool
under `tools-state`, deploy-plane `version.json` (which bootstrap must read
before PostgreSQL starts), and the admin service home.

## What lives in the database

The schema lives in `host/migrations/` as ordered, numbered SQL migrations
applied by the migration runner; the current shape is the result of applying
them all, so this document describes the resulting tables, never the
migration history. Columns are
typed and constrained wherever the shape is ours; the JSON values are provider
account metadata (the provider CLI's own evolving shape, cached verbatim) and
tool-owned metadata and approval payloads (JSON by the tool contract).

| Table | Contents |
| --- | --- |
| `config` | Agent name and admin password hash, a single format-checked row replaced during each provisioning bootstrap; upgrade/recover carry the stored password forward. |
| `operator_connections` | Operator access endpoints, one row per mode (duplicates impossible by key); per-mode field requirements are row constraints, and the Cloudflare tunnel token is encrypted. |
| `admin_passkey_config`, `admin_passkeys` | The single administrator's stable WebAuthn user handle plus enrolled credential ids, RP domains, ES256 public keys, counters, transport/backup metadata, and timestamps. No private passkey material is stored. Upgrade/recover preserve these rows; only explicit root `reconfigure --reset-admin-passkeys` removes them. |
| `agent_events` | Agent runtime and turn events with a direct `thread_id` column (`NULL` for runtime events, indexed with `seq` for per-thread paging) and typed payload columns (message/source, error, runtime, provider-neutral activity JSON); pruned to the newest 1,000,000. The event log is the single durable record of a thread's turn history — turns themselves are orchestrator memory, and the former `tasks`/`task_steers` tables were dropped by migration `0005_thread_only_turns.sql`, which also renamed `task.*` event types to `turn.*` and replaced the events' `task_id` with `thread_id`. A synchronous steer is appended only after provider acknowledgement; there is no steer mailbox or delivery-marker table. |
| `thread_sessions` | One canonical row per user `thread_id`: current runtime, provider session/thread id, model, effort, recency, and durable idle/running state. An idle configuration change atomically replaces runtime/model/effort and clears the provider session before admitting the next run; the retained `agent_events` remain the thread's handoff source. Rows referenced by retained events stay; unreferenced rows beyond the 100,000 most recently used per runtime are pruned. |
| `oauth_logins` | In-flight OAuth logins, fully typed per flow (device code + login handle for Codex, browser code for Claude). Hermes has no row here: its Bedrock credential connect is a single API call with nothing in flight. |
| `provider_accounts` | Admin-side provider account records: `account_id` typed, remaining provider-owned metadata as a cached document. An anchored row (`account_id` plus its provider's approval marker in metadata) is trigger-guarded: `provider_accounts_anchor_guard` refuses writes that change or delete the approved identity. The singleton `bedrock` provider row carries only cached AWS display metadata, not an anchor. |
| `bedrock_credentials` | Hermes's singleton Bedrock connection: the synchronously validated IAM `access_key_id`, secret access key as `secretbox` ciphertext, and selected region. The admin service writes all three atomically; the trusted proxy reads this same row only for enabled Bedrock requests and re-signs requests carrying Hermes's fixed dummy access-key id. |
| `bedrock_usage` | Live Bedrock usage counters, one row per (runtime, model, UTC day). For each allowed Bedrock response the proxy records the reported token counts plus the USD it prices that response at (`cost_usd`, fixed to 6 decimals); the admin API sums these stored values, so cost is final and a later rate edit never rewrites history. Each write is a single `INSERT ... ON CONFLICT (runtime, model_id, day) DO UPDATE SET col = col + EXCLUDED.col`, so Postgres takes the row lock and applies the increments atomically — concurrent proxy writes simply sum, with no application-level read-modify-write. The model id is normalized to the catalog (unknown ids collapse into one `other` bucket), so growth is bounded by arithmetic (runtimes x (catalog models + 1) x days) with no pruning. |
| `network_policy` + `managed_integrations`, `github_repositories`, `github_settings`, `claude_settings`, `allowed_domains`, `domain_methods`, `domain_path_guards` | The active network policy as typed, ordered rows (shape defined by `host/config.py`). Bedrock policy contains only its soft enabled/disabled state; its region belongs to the connection row above. The policy is replaced atomically, and a missing `network_policy` row is the fail-closed empty default. |
| `github_credential` | The single fixed GitHub credential (PAT, or GitHub App identity/key). Admin-owned only, with no proxy grant; secret columns hold `secretbox` ciphertext. The working token lives only in `proxy_github_token`. |
| `proxy_provider_pins` | Exactly the two values the proxy guards compare: account id and token hash (format-checked). Rows exist for the OAuth runtimes (`codex`, `claude`); Hermes has no pin because the proxy reads its validated Bedrock credential row directly. |
| `proxy_github_token` | The proxy's working copy of the active GitHub token, injected into policy-approved GitHub requests. `secretbox` ciphertext like every other stored secret; the proxy role holds SELECT on this row and on `secret_keys`, and because grants are per-table that pair decrypts exactly this working set — the credential and audit tables keep no proxy grant. A proxy compromise therefore exposes just this token: short-lived in app mode, the PAT itself in pat mode (one reason to prefer app mode). |
| `github_repo_audit` | Per-repository audit facts (visibility, the token's effective permissions, default-branch protection, workflows and triggers) fetched by the `audit-github-repo` helper. Admin-owned, no proxy grant; the warning judgments live in code, so message changes never touch stored facts. |
| `pending_pushes` | Pushes held by the `.github` approval gate. Written by the proxy's role (INSERT, like `network_events`) when a gated push touches `.github/`; the admin service lists and resolves them (approve/reject). The held objects live under `refs/pending/<id>` in the proxy's quarantine mirror on disk, not in this table. |
| `network_events` | Network allow/deny decisions, written by the proxy's role, fully typed (pruned to the newest 1,000,000; URL fields are size-capped so the row cap is a real disk bound). |
| `enabled_tools` | Bundled tools the operator has enabled; presence-based and keyed by `tool_id`. |
| `tool_config` | Secret configuration values keyed by `(tool_id, key)`. Repeated key names are independent between tools; every value is `secretbox` ciphertext. |
| `tool_credentials` | One OAuth credential per tool, split into typed connected-account columns, encrypted provider token material, and tool-owned non-secret metadata. |
| `tool_approvals` | Host-owned approval records. `number` is the identity behind the public `approval_<number>.<token>` id (the token is an unguessable poll capability); conditional transitions make each approval single-use, and terminal result text is returned to both operator and agent (decided history is pruned to the newest 10,000). |
| `tool_events` | Tool call, approval, connection, enablement, and config audit events. Accepted calls store their exact bounded arguments; lifecycle events store no arguments. Pruned to the newest 1,000,000. |
| `host_errors` | Unexpected host-service exceptions and abnormal systemd exits copied from structured journald records. Brief repeats coalesce by service and fingerprint; retained to the newest 10,000 rows. List reads omit traceback/context until detail expansion. |
| `app_schema_migrations` | Host-owned record of applied per-app migration versions. App SQL runs under the app role, which cannot write this table. |
| `counters` | Monotonic counters as plain rows. Currently empty — the last counter, `next_task_number`, left with the task tables (event seqs are a database serial) — the table remains for future counters. |
| `secret_keys` | The at-rest encryption key for stored secrets (see below). The proxy and tools roles can read it, but their table grants expose only their own ciphertext-bearing rows. |
| `schema_migrations` | Applied migration versions (owned by the migration runner). |

Stored secrets include the Cloudflare tunnel token, GitHub PAT or App private
key and working token, every tool config value, and OAuth provider token
material. `host/runtime/core/secretbox.py` encrypts them with AES-256-CBC through
OpenSSL using the key in `secret_keys`, which the schema creates from
Postgres's CSPRNG. Upgrade and recovery carry ciphertext and key together on
the admin volume. This is an accidental-exposure control, not a root or
offline-database defense: a stray query or pasted row does not expose the
secret, while a complete database dump necessarily includes the key.

Ephemeral values — cached agent runtime statuses
(`orchestrator._RUNTIME_STATUSES`) — live in service memory and reset with the
process.

The data directory is `/mnt/kern-admin/postgres/<major>/main` on the
durable admin volume, so state survives root-volume replacement on
upgrade. The path is versioned by PostgreSQL major
(currently 14, pinned in bootstrap); a future base-image bump that changes the
major requires an explicit `pg_upgrade` step rather than silently opening old
data files with new binaries.

## How runtime code uses it

`host/runtime/core/state.py` exposes per-operation accessors that run real queries
against normalized tables; no request materializes the complete state. Hot
paths are indexed for thread/runtime lookup, per-thread event history,
event paging, and pruning. Reads use MVCC transactions and fetch only the rows
they need. Admin-process check-then-act writes use `state.mutation()`, pairing
one database transaction with a process-local mutation lock. The proxy and
tools processes write only through their granted operations, and cross-process
races such as approval decisions use conditional SQL transitions. Events
written inside a mutation share its transaction; serial event ids are unique
and increasing with harmless gaps after aborts.

`host/runtime/core/db.py` owns a small pool in each service process, capped at 14
active sessions per process. PostgreSQL allows 100 connections: the three core
database clients and two bundled apps can use at most 70, leaving 30 for operator,
superuser, and deployment access. Each app has its own 14-session semaphore,
so app pressure cannot consume that headroom through a backend pool; a new app
operation fails when its own slots or the database are unavailable. Nested
transactions on one thread take separate connections so a read inside a
mutation sees the last committed state.

Growth stays bounded by deliberately high caps (1M rows per ordinary audit
log — agent, network, and tool events each — 10k host-error rows, 100k session
mappings per runtime, and 10k decided tool approvals). Ordinary audit logs use
O(1)-planning primary-key range deletes below `MAX(seq) - N`; host errors find
the oldest boundary among the newest N rows because a coalesced row rotates
its sequence without adding a row. Logs prune on their own append cadence, the
rest on the hourly maintenance pass. The admin volume is sized for those caps;
time-based auto-cleanup and volume monitoring can replace them later without
touching the storage API.

## Access control

Access starts with the operating system's user model and then narrows
non-owner roles with table or schema grants:

- Connections are Unix-socket only with `peer` authentication: the client's
  OS user must match its database role, and no passwords exist anywhere.
- `kern-admin` owns the database and the schema; the admin service (and
  the bootstrap's migration/config steps) connect as it.
- `kern-proxy` reads network policy, provider pins, GitHub settings and
  the encrypted working token plus the key needed to decrypt it; it
  inserts/prunes network events and enqueues
  held pushes. It cannot read the stored GitHub credential or other admin
  state.
- `kern-tools` reads enablement/config and the shared secret key, and
  reads/writes tool credentials, approvals, and events. It cannot enable a
  tool, rewrite config, or reach non-tool state.
- `kern-agent-network` reads only network policy and `network_events` for
  the agent-facing introspection tools. It cannot mutate those tables, read
  credentials, or reach tool state.
- Every app role owns only its derived `app_<app_id>` schema. The host-owned
  `app_schema_migrations` table records what bootstrap applied.
- The `postgres` superuser is reachable only by the `postgres` OS user, i.e.
  by operators through sudo: `sudo -u postgres psql kern_admin`.
- Everyone else, most importantly `kern-agent`, has no role, and
  `pg_hba.conf` ends with an explicit reject rule, so even a process that can
  reach the socket cannot authenticate.

`pg_hba.conf` and `postgresql.conf` are managed config, rewritten by bootstrap
on every deploy (they live inside the preserved data directory, but their
content comes from the root-owned bootstrap, so a previous compromise cannot
persist config edits across an upgrade).

## Schema migrations

Migrations are plain SQL files in `host/migrations/`, named
`NNNN_description.sql`, with goose-style up and down sections:

```sql
-- migrate:up
ALTER TABLE thread_sessions ADD COLUMN priority BIGINT;

-- migrate:down
ALTER TABLE thread_sessions DROP COLUMN priority;
```

`host/runtime/deploy/migrate.py` is the runner
(`python3 -m host.runtime.deploy.migrate {up|down|status} [--to VERSION]`). Applied
versions are recorded in `schema_migrations`; `up` applies only pending files,
all in one transaction (PostgreSQL DDL is transactional), under an advisory
lock so concurrent runners serialize. It is an in-repo runner rather than
goose/alembic/dbmate so the host needs no extra toolchain and migrations ship
inside the runtime code archive; the file format deliberately matches that
family of tools.

Migrations are deploy-plane work, applied in exactly one place: bootstrap
runs `migrate up` (as `kern-admin`) after the database is up and before
services start. This is the upgrade path — redeploy replaces the root volume
and code, preserves the admin volume, and `migrate up` brings the preserved
database to the new code's schema. The admin service itself never migrates:
code and schema change together in one bootstrap or not at all, so a stray
service start can never move the schema under a live instance, and a
schema/code mismatch (unsupported) fails loudly instead of being papered
over.

`migrate down` is a manual operator action only
(`sudo -u kern-admin env PYTHONPATH=/opt/kern-host python3 -m host.runtime.deploy.migrate down`).

Rules for changing persisted state shape from now on:

- Never edit an applied migration; add a new `NNNN+1_*.sql` file.
- Every migration needs a working down section (the test suite migrates the
  whole history up and back down on every run).
- Old code is not expected to run against a newer schema: deploy replaces the
  code and migrates in one operation, and downgrades go through `migrate down`
  before installing older code.
