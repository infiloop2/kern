# PostgreSQL retention and cardinality

Every durable table must have an explicit storage policy. Runtime input is
either rejected at a hard quota or pruned to a count/time window. User-owned
current state is never removed merely because it is old. The Admin and
Workspace services each maintain the tables they own, once at startup and
hourly; write-time pruning remains in place for high-volume logs and revision
tables.

Retention deletes make space reusable inside PostgreSQL. Normal autovacuum
cleans dead tuples; maintenance does not run blocking table rewrites merely to
make the database files immediately smaller.

The categories are:

- **fixed**: keys come from a finite product/configuration catalog.
- **quota**: new user-owned rows are rejected at a hard limit.
- **retention**: old history is removed by a count or time rule.
- **ledger**: one row is retained per shipped schema migration.

| Table | Category | Enforced policy |
| --- | --- | --- |
| `schema_migrations` | ledger | One row per shipped host migration; never runtime-controlled. |
| `config` | fixed | Singleton. |
| `operator_connections` | fixed | At most one SSH and one tunnel row. |
| `counters` | fixed | Internal named counters only. |
| `thread_sessions` | retention | Event-referenced sessions are bounded by retained events; unreferenced sessions retain the newest 100,000 per runtime. |
| `agent_events` | retention | Newest 10,000,000 rows, with at most 499 rows of amortization slack; message text is length-bounded. |
| `conversation_message_embeddings` | retention | Derived vectors only within the newest 250,000 `agent_events` sequence window, with at most 5,000 events of amortization slack; rows also cascade with source events. |
| `oauth_logins` | fixed | At most one row for each supported OAuth runtime. |
| `provider_accounts` | fixed | At most one row for each supported provider. |
| `network_events` | retention | Newest 1,000,000 rows, with at most 499 rows of amortization slack. |
| `network_policy` | fixed | Singleton. |
| `allowed_domains` | fixed | Replaced atomically from one bounded, validated network-policy request. |
| `domain_methods` | fixed | Child rows of the bounded network policy. |
| `domain_path_guards` | fixed | Child rows of the bounded network policy. |
| `proxy_provider_pins` | fixed | At most one row for each supported provider pin. |
| `managed_integrations` | fixed | Finite bundled integration catalog. |
| `github_repositories` | fixed | Replaced atomically from one bounded, validated network-policy request. |
| `secret_keys` | fixed | Singleton. |
| `github_credential` | fixed | Singleton. |
| `proxy_github_token` | fixed | Singleton. |
| `github_repo_audit` | fixed | At most one row per configured GitHub repository; stale repositories are pruned. |
| `github_settings` | fixed | Singleton. |
| `pending_pushes` | retention | At most 10 pending pushes and the newest 100 resolved rows. |
| `claude_settings` | fixed | Singleton. |
| `enabled_tools` | fixed | Finite bundled tool catalog. |
| `tool_config` | fixed | Finite manifest-declared keys for bundled tools. |
| `tool_credentials` | fixed | At most one credential row per bundled tool. |
| `tool_approvals` | retention | At most 1,000 pending approvals and the newest 10,000 decided rows; pending rows expire. |
| `tool_events` | retention | Newest 1,000,000 rows, with at most 499 rows of amortization slack. |
| `bedrock_credentials` | fixed | Singleton. |
| `bedrock_usage` | retention | Daily counters for the latest 400 days; model ids are normalized to a finite catalog plus `other`. |
| `host_diagnostics` | retention | Newest 10,000 coalesced error and warning rows, with at most 99 rows of amortization slack. |
| `admin_passkey_config` | fixed | Singleton. |
| `admin_passkeys` | fixed | Exactly zero or one administrator passkey; reset precedes replacement. |
| `workspace_seen` | retention | At most one read marker per retained Chat, Web App, or schedule. Schedule markers are removed when their deleted definitions leave the 90-day restore window; Chat and App markers remain bounded by their product quotas. |
| `chat_threads` | quota | At most 10,000 durable Chat thread records; no age-based deletion. |
| `web_apps` | quota | At most 100 durable Web Apps; no age-based deletion. |
| `memory_pages` | quota | At most 10,000 retained pages; deleted pages are removed after 90 days. |
| `memory_page_revisions` | retention | Newest 100 revisions per retained page; cascades with its page. |
| `memory_page_embeddings` | quota | At most one current derived vector per retained page and model; soft deletion removes it and page deletion also cascades. |
| `memory_page_links` | quota | At most 100 current outgoing links per retained swarm page; source-page deletion cascades. |
| `schedules` | quota | At most 100 active schedules. Deleted definitions remain restorable for 90 days, then are removed independently of their stable `schedule-N` host thread. |
| `schedule_revisions` | retention | Newest 100 revisions per retained schedule; cascades with its schedule. |
| `web_app_revisions` | retention | Newest 5 exact revisions, then one recovery point per four-hour interval during the first day and one per day from day two through day seven, capped at 17 revisions; cascades with its quota-bounded App. |

When adding or renaming a table, update this inventory in the same change.
