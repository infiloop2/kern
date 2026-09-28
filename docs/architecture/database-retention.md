# PostgreSQL retention and cardinality

This inventory records the current storage policy, including tables without
automatic pruning. Most runtime history is quota-bound or retained for a
count/time window. Operator-managed OAuth connections and the tool cost ledger
have no automatic retention cap; do not infer one from the audit-log limits.
User-owned current state is never removed merely because it is old. The Admin and
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
- **ledger**: durable historical rows without automatic pruning.
- **operator state**: explicitly managed by the operator, without an automatic quota.
- **reachability**: retained while a live record or recovery checkpoint needs it.

| Table | Category | Enforced policy |
| --- | --- | --- |
| `schema_migrations` | ledger | One row per shipped host migration; never runtime-controlled. |
| `config` | fixed | Singleton. |
| `operator_connections` | fixed | At most one SSH and one tunnel row. |
| `counters` | fixed | Internal named counters, including four lifetime token totals. Never pruned with detailed usage. |
| `thread_sessions` | retention | Sessions referenced by retained events or turn usage are preserved; unreferenced sessions retain the newest 100,000 per runtime. |
| `agent_events` | retention | Newest 10,000,000 rows, with at most 499 rows of amortization slack; message text is length-bounded. |
| `conversation_message_embeddings` | retention | Derived vectors for the newest 250,000 user/agent messages, with at most roughly 800 newly indexed messages of amortization slack; activity and lifecycle events do not consume this quota. Rows cascade with source events. |
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
| `tool_credentials` | operator state | One row per `(tool_id, connection_id)`; OAuth tools can have multiple connections. Disconnect deletes the selected row; no automatic connection-count quota or age pruning. |
| `tool_secrets` | fixed | At most one encrypted 16 KiB JSON object per bundled tool; explicit clear, no automatic expiry. |
| `tool_approval_risk_assessments` | retention | At most one annotation per approval; cascades with approval deletion. |
| `tool_costs`, `tool_cost_daily` | ledger | Immutable deduplicated charges and daily action totals; no automatic pruning. |
| `host_inference_providers` | fixed | One row per supported host-owned provider. |
| `host_inference_usage` | retention | Provider/model daily counters for 400 days, pruned on the next metered write each UTC day. |
| `xai_video_storage` | fixed | Optional singleton storage configuration. |
| `tool_approvals` | retention | At most 1,000 pending approvals and the newest 10,000 decided rows; pending rows expire. |
| `tool_events` | retention | Newest 1,000,000 rows, with at most 499 rows of amortization slack. |
| `bedrock_credentials` | fixed | Singleton. |
| `turn_usage` | retention | 90 days by last measurement time; Analytics queries seven UTC calendar days. |
| `bedrock_usage` | retention | Daily counters for the latest 400 days; model ids are normalized to a finite catalog plus `other`. |
| `host_diagnostics` | retention | Newest 10,000 coalesced error and warning rows, with at most 99 rows of amortization slack. |
| `admin_passkey_config` | fixed | Singleton. |
| `admin_passkeys` | fixed | Exactly zero or one administrator passkey; reset precedes replacement. |
| `conversation_search_state` | fixed | Singleton search/embedding generations. |
| `conversation_embedding_queue` | retention | Pending work per source message; source deletion cascades, message-window pruning applies, and five failed encoding attempts discard the item. |
| `workspace_navigation_order` | fixed | Two list rows, for Apps and schedules; stored ids are reconciled with current product identities. |
| `workspace_onboarding_dismissal` | fixed | Optional singleton operator dismissal. |
| `swarm_agent_ai` | reachability | At most one current annotation per host thread; cascades with thread-session deletion. |
| `swarm_peer_deliveries` | retention | Newest 50 text-free peer-delivery records. |
| `web_app_collection_state` | quota | At most one row per quota-bounded App. |
| `web_app_collection_rows` | quota | Per App: 64 collections, 100,000 rows, 50 MiB total; each row at most 128 KiB. |
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
| `web_app_ui_versions`, `web_app_document_versions` | reachability | Immutable components shared by App checkpoints; removed when no retained checkpoint references them. Cascade with the App. |
| `web_app_collection_versions` | reachability | Current row intervals plus closed intervals needed by at least one of the App's retained checkpoints; cleanup uses the App lock. Cascade with the App. |

When adding or renaming a table, update this inventory in the same change.

Provider-file cleanup is separate from PostgreSQL history retention. The admin
maintenance loop sweeps archived Chats' idle Codex sessions at startup and daily,
detaching only their provider mapping before supported Codex deletion. Chat
records, messages, self-memory, and the clear-context boundary remain, so the
next message after restoration uses the normal history handoff. See
[Codex session rotation](agent-provider-lifecycle.md#automatic-codex-session-rotation).

`turn_usage` retains 90 days by last measurement time, pruned by the ordinary
admin retention loop. The Analytics page queries only seven UTC calendar days.
Usage rows have no thread foreign key so deleting a conversation does not
remove its recent usage from the report.
