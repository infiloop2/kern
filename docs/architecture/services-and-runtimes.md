# Services and Runtimes

| systemd unit | User | Purpose |
| --- | --- | --- |
| `kern-network-proxy.service` | `kern-proxy` | Policy proxy on `127.0.0.1:7445`. |
| `kern-postgres.service` | `postgres` | Admin-state PostgreSQL, Unix socket only (no TCP listener). |
| `kern-admin-api.service` | `kern-admin` | Admin API on `127.0.0.1:7443`. Owns admin state; holds no internet egress. |
| `kern-tools.service` | `kern-tools` | Runs the bundled tool packages and owns the agent-facing tools socket `/run/kern-tools/tools.sock` (peer-credential authenticated). The only Kern application service besides the proxy with DNS+HTTPS egress; its Postgres role is scoped to the five tool tables plus read access to the encryption key needed for tool secrets. |
| `kern-agent-network.service` | `kern-agent-network` | Serves read-only network integration and denial introspection on `/run/kern-agent-network/agent-network.sock`. No egress; its Postgres role has SELECT-only policy and network-event grants. |
| `kern-host-errors.service` | `kern-admin` | Follows structured error and warning records from journald and copies them best-effort into the bounded Postgres host-diagnostics log. |
| `kern-workspace.service` | `kern-workspace` | One Chat, Web Apps, global Memory, and Schedules backend on `127.0.0.1:7450` (reachable only from the admin API), plus the peer-authenticated agent socket `/run/kern-workspace/agent.sock`. Its Postgres role has explicit DML-only access to the Workspace tables in `public` and no egress. |
| `kern-cloudflared.service` | `cloudflared` | Optional Cloudflare Tunnel connector for Cloudflare Tunnel operator endpoints. Installed only when `operator_connections` contains `cloudflare_tunnel`. |
| `kern_agent.slice` | — | Top-level cgroup slice holding every agent runtime scope (underscore, not dash: dashes in slice names encode nesting, and the weight must compare against `system.slice` directly). `CPUWeight=50` guarantees the host services CPU time under contention while leaving idle cores to the agent; aggregate `MemoryHigh=75%`/`MemoryMax=80%`/`MemorySwapMax=5G` protect the host, while lower per-scope limits contain one busy thread before it stalls its peers; `TasksMax=4096` stops a slice-wide fork bomb from exhausting kernel PIDs. |
| `kern_workspace.slice` | — | Cgroup slice for the fixed Workspace service. `CPUWeight=50` keeps host control services responsive under contention. |

## Process Inventory

| Process | User | Started By | Purpose |
| --- | --- | --- | --- |
| `systemd` | root | OS boot | Starts nftables, Postgres, proxy, tools, admin API, workspaces, and optional Cloudflare Tunnel services. |
| `nftables` | kernel/root configured | bootstrap/systemd | Enforces inbound and per-user outbound network policy. |
| `kern-network-proxy.service` | `kern-proxy` | systemd | Handles all agent HTTP(S)/WS(S) egress and writes network events. |
| `kern-postgres.service` | `postgres` | systemd | Stores admin state; local Unix-socket connections only. |
| `kern-admin-api.service` | `kern-admin` | systemd | Serves localhost API/UI, owns thread state, and supervises runtime work. |
| `kern-tools.service` | `kern-tools` | systemd | Executes bundled tool calls and operator-delegated OAuth/approval work; owns the peer-authenticated tools socket. |
| `kern-agent-network.service` | `kern-agent-network` | systemd | Serves the peer-authenticated network-introspection socket from SELECT-only policy and event state, without egress. |
| `kern-host-errors.service` | `kern-admin` | systemd | Validates tagged records from trusted Kern systemd units and stores/coalesces them for the read-only Host diagnostics panel. |
| `kern-workspace.service` | `kern-workspace` | systemd | Serves all browser Workspace resources on the admin-only loopback port, the agent Workspace API on a peer-authenticated Unix socket, generated Web Apps, and global scheduled runs. |
| `kern-cloudflared.service` | `cloudflared` | systemd | Optional Cloudflare Tunnel connector. Reads `/etc/kern/cloudflared.token` and exposes the admin API through the configured Cloudflare Tunnel hostname. |
| `run-codex-app-server` helper | starts as root, then `kern-agent` | admin API via sudo | Starts one Codex stdio app-server process. |
| `codex app-server` | `kern-agent` | launch helper | Executes one Codex turn, resuming its provider thread by id, then exits. |
| `run-claude-code` helper | starts as root, then `kern-agent` | admin API via sudo | Starts one Claude Code CLI process. |
| `claude` | `kern-agent` | launch helper | Executes one Claude Code turn, then exits. |
| `tools MCP shim` | `kern-agent` | Codex / Claude Code | Aggregates the tools, network-introspection, and Workspace agent sockets into one MCP server; one per agent session that uses host tools. |
| `read-codex-account-id` / `read-claude-account` | starts as root, then `kern-agent` | admin API via sudo | Reads provider auth files narrowly and prints only account guard metadata. |
| `clear-agent-auth` | starts as root, then `kern-agent` | admin API via sudo | Removes local Codex/Claude auth files during linked-account reset. |
| `read-agent-file` helper | starts as root, then `kern-agent` | admin API via sudo | Lists agent-home directories, returns a bounded text preview, or streams one bounded regular file for authenticated preview/download without giving admin general agent-home access. |
| `mint-github-app-token` helper | root | admin API via sudo | Mints installation-wide GitHub App tokens through root egress because the admin service has none; the proxy repo guard is the per-repository boundary. |
| `audit-github-repo` helper | root | admin API via sudo | Reads GitHub repository/security facts with the working token and returns facts without storing secrets. |
| `approve-github-push` helper | root | admin API via sudo | Replays or cleans up a push held by the `.github` approval gate using the proxy-state quarantine mirror and a working GitHub token piped on stdin. |
| `run-agent-script` helper | starts as root, then `kern-agent` | admin API via sudo | Runs one scheduled bash script from the agent home in the ordinary agent scope, with a scope `RuntimeMaxSec` backstop behind the 15-minute turn timeout. |
| `stop-agent-thread` helper | root | admin API via sudo | Frees a thread's transient agent scope after a stop: SIGKILLs the scope's cgroup, stops the unit, and clears any failed remnant. |
| `reboot-host` helper | root | admin API via sudo | Requests a host reboot. |

## Thread Inventory

| Thread Group | Process | Purpose |
| --- | --- | --- |
| HTTP handler threads | admin API | One per concurrent API request. Mutations use state transactions and run slow helper calls outside the state lock. |
| Tools socket handler threads | tools service | One per agent tool call (and per delegated operator operation), bounded by a concurrency cap; tool packages run their third-party requests on these threads. |
| Network-introspection socket handler threads | agent-network service | One per local request, bounded by a concurrency cap; calls perform read-only policy or denial queries. |
| Workspace agent socket handler threads | Workspace service | Peer-authenticated before allocation, with separate connection and active-call caps; calls use bounded explicit Workspace routes. |
| Maintenance thread | admin API | Periodically prunes bounded state and event history. |
| Embedding index thread | admin API | Sends bounded missing-message batches to the local encoder and commits derived vectors between inference calls. |
| Embedding request loop | embedding service | Handles one bounded query or passage batch at a time; systemd activates it on demand and it exits after five idle minutes. The unit runs one inference thread at nice level 10 with `CPUWeight=25`, `IOWeight=25`, `MemoryMax=1G`, and `TasksMax=64`, so indexing yields to the Workspace and host control plane under contention. |
| Journal follower | host-diagnostics collector | Follows new trusted-unit `KERN_HOST_DIAGNOSTIC=1` records without a replay cursor. |
| Runtime status poller | admin API/orchestrator | Rechecks provider health, including Hermes's Bedrock connection. |
| Turn threads | admin API/orchestrator | One daemon thread per admitted turn; at most ten turns run per runtime, and a message past that cap is rejected rather than queued. Each turn spawns and closes its own runtime process. |
| Proxy handler threads | network proxy | One per proxied connection, capped so buffered request bodies cannot exhaust memory. |
| Proxy certificate lock users | network proxy | Serialize per-host certificate generation so concurrent TLS CONNECTs do not race on cert files. |

Agent runtimes are spawned through fixed sudo helpers that demote them to
`kern-agent`, each inside a transient systemd scope under
`kern_agent.slice`. Without the scope they would inherit the admin API's
service cgroup and compete with the host services for resources. The slice's
`CPUWeight=50` versus `system.slice`'s default 100 keeps the admin API, proxy,
and Postgres responsive while an agent build or test run saturates the cores,
and costs the agent nothing when the host services are idle (weights, unlike
quotas, are work-conserving). Every transient scope has `MemoryHigh=35%`,
`MemoryMax=50%`, `MemorySwapMax=3G`, and `TasksMax=1024`; a runaway turn is
therefore reclaimed or killed before it consumes the whole shared slice and
stalls another Claude or Codex startup. The parent slice's `MemoryHigh=75%`
and `MemoryMax=80%` remain aggregate backstops, so a runaway agent process
dies instead of triggering a host-wide OOM kill; the admin API records the
failed turn and the host stays up. Parent `MemorySwapMax=5G`
keeps 1G of the 6G swapfile available to host services (systemd 249 offers no
percentage form for swap, and bootstrap owns the swapfile size). `TasksMax=4096`
bounds agent threads and processes so a fork bomb cannot exhaust kernel PIDs,
which would otherwise block the admin API from spawning helpers at all. Each
launcher points `TMPDIR` at the separate agent volume. The PostgreSQL test
harness self-detects the live host and shows a clear skip message there;
GitHub Actions remains authoritative for that suite without adding
repository-specific variables to general agent environments. Each
scope is `BindsTo=kern-admin-api.service`: leaving the admin API's
cgroup must not decouple lifecycles, so when the admin service stops,
restarts, or crashes, systemd stops the scopes too and no orphaned runtime
keeps running after its turn was recovered as failed.
Codex runs as stdio app-server child processes; status
checks and login flows use short-lived servers, and each turn runs on a
fresh app-server that resumes its provider thread by id. The host supplies the
session's selected model on Codex thread start/resume and its model and effort
on every turn. Claude Code does not expose the same app-server protocol, so the
host runs one CLI process per turn
with the selected `--model` and `--effort`, then resumes the Claude session id
recorded for the user thread. Both OAuth runtimes persist login/session state under
`agent-home`, so restarted admin services can
re-derive active status from the agent user's home directory.

The `script` runtime is the one runtime with no model behind it: a schedule
may select it to run a static bash script from the agent home instead of a
model turn, with the script's path in place of the prompt. It runs on the same
turn machinery — admission, run history, stop, the per-thread scope — through
the same kind of sudo launcher, so a scheduled script is confined exactly as an
agent turn is. Having no provider connection, it needs no login and is always
active — its status is published once at startup and the status poller, which
exists to re-derive what can change underneath the host, leaves it alone;
having no session, every run starts fresh; and having no mid-turn channel, it
cannot be steered. Its turn budget is a fixed fifteen minutes
rather than an operator setting, enforced by the admin API and backstopped by
the scope's `RuntimeMaxSec`. It is offered only where a schedule is
configured — Chat and the Web App builder are conversational surfaces and
reject it — and the send path enforces the same boundary on the thread id, so
only a `schedule-*` thread can run it however the request arrives.

## Reboot and restart

The admin API, proxy, tools, network-introspection, host-diagnostics collector,
Postgres, Workspace service,
nftables, and optional Cloudflare Tunnel service are `systemctl enable`d, so
they resume on every boot. Postgres starts before the proxy, tools, and
network-introspection services; those services start before the admin API; the
Workspace service and `cloudflared` start after the admin API. nftables
reloads `/etc/nftables.conf`.
Because admin state and agent home data live on the two data EBS volumes, a
reboot, including via `POST /v1/host-runtime/reboot`, preserves them: the proxy comes
back immediately enforcing the last active policy (no fail-open window),
Cloudflare Tunnel reconnects when configured, agent login and thread
histories survive, and the swapfile is re-enabled from `/etc/fstab`. Redeploys can
replace the root volume and runtime code while reattaching the tagged admin
and agent volumes for the same `agent_name`.

On start the admin API runs a recovery pass: each thread whose private durable
run state is `running` is returned to `idle` and gets one `thread.error` event
(in-flight agent work cannot survive a reboot). There is no queue to recover:
a message sent before its
chosen runtime's first status poll publishes `active` is rejected with that
non-active status, and the caller retries — turns run only against an active
runtime, never park behind one.

The tools service independently marks an approval caught in `approved`
execution as `failed` with an unknown outcome; it never repeats the
third-party side effect after a restart.
