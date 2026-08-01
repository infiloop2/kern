# Audit: Reliability, Resource Isolation, and Recovery

Finding ID prefix: `REL`. See [README.md](README.md) for the sweep process,
finding format, and severity scale.

## Audit question

Can a runaway agent or ordinary failure make the host uncontrollable or
require manual repair?

## Reviewed commits

Latest reviewed commit: `6151eea5abb61590684c4cf667ae6f619d705231`.

| Commit | Reviewed by |
| --- | --- |
| `6151eea5abb61590684c4cf667ae6f619d705231` | gpt-5.6-sol; Claude Opus 5 |

## Findings

| Finding | Severity | Found at | Found by | Description | Resolution |
| --- | --- | --- | --- | --- | --- |
| REL-002 | High | `f28b50e87b61` | Claude Opus 4.8; GPT-5.5 | Wildcard-allowed hosts caused an unbounded durable cache of generated TLS key/certificate files on the admin volume. An agent could request unlimited unique subdomains until the volume shared with Postgres filled. The files survive reboot, so reboot does not resolve the condition. Cap/evict the cache or move it to bounded ephemeral storage. | Fixed — generated certificate families are capped and evicted oldest-first under the mint/load lock, with partial mint files cleaned up. |
| REL-006 | High | `d28713d3ad19` | gpt-5.6-sol | With GitHub write access and `.github` approval enabled, an agent can repeatedly push unique packs that touch `.github` and leave them pending. Each request indexes up to the proxy's 128 MiB body into the durable quarantine mirror, pins tips under `refs/pending/<id>/...`, and inserts a `pending_pushes` row; there is no pending-count/byte quota, TTL, pruning, or bounded listing. The proxy state and PostgreSQL data share the admin volume, so this survives reboot and can exhaust disk/inodes until manual cleanup. Enforce per-repository/global pending count and byte budgets before indexing, expire and delete refs/rows, run bounded GC/eviction, and reserve volume headroom. | Fixed — the existing 128 MiB request limit bounds each push, at most ten pushes may be pending, and manual approve/reject removes its refs and immediately prunes unreachable objects under the quarantine lock. |
| REL-001 | Medium | `f28b50e87b61` | Claude Opus 4.8 | The agent uid could exhaust Postgres backend slots by opening Unix-socket connections and stalling before the startup packet, temporarily making the admin API and proxy unable to connect. Reboot clears the stalled agent processes, so this is capped at Medium under the reliability rule. Restrict the socket to service users or sharply bound authentication startup time. | Wontfix — reboot is the documented recovery for agent-induced resource exhaustion on this single-operator host; the reliability rule already caps it at Medium on that basis. |
| REL-003 | Medium | `f28b50e87b61` | Claude Opus 4.8 | Postgres socket reads and statements had no timeout, so a stalled database could hold the process-wide mutation lock indefinitely and freeze operator mutations. Reboot clears the in-memory lock when underlying durable state is healthy; add socket and statement deadlines so recovery does not require reboot. | Wontfix — reboot is the documented recovery for agent-induced resource exhaustion on this single-operator host; the reliability rule already caps it at Medium on that basis. |
| REL-004 | Medium | `f28b50e87b61` | Claude Opus 4.8 | Agent event retention bounded row count but not message size, allowing unusually large streamed messages to grow durable Postgres storage far beyond the apparent cap. Bound each stored message as well as the number of rows. | Fixed — append_agent_event now truncates each stored message/error_message to MAX_EVENT_MESSAGE_CHARS (128 KiB) with a length-recording marker, bounding per-message durable growth as well as the existing row-count cap; the bound sits below THREAD_HANDOFF_CHARACTER_LIMIT so history reconstruction is unaffected. |
| REL-005 | Medium | `f28b50e87b61` | Claude Opus 4.8; GPT-5.5 | The proxy could buffer up to 128 MiB in each of 64 handlers while running outside the agent memory cgroup, allowing roughly 8 GiB of pressure and an operator-visible proxy/OOM outage. Reboot clears the memory condition, so severity is capped at Medium. Add an aggregate memory bound, lower the body/concurrency product, or reserve service memory with cgroups. | Wontfix — reboot is the documented recovery for agent-induced resource exhaustion on this single-operator host; the reliability rule already caps it at Medium on that basis. |
| REL-007 | Medium | `47daf37e08a8` | gpt-5.6-sol | The world-connectable tools, network-introspection, and agent-app Unix sockets use `ThreadingHTTPServer`, which creates a daemon thread for every accepted connection before peer checks, header parsing, and the eight-call semaphore. A runaway agent can rapidly open or slow-roll connections for the 30-second read window, accumulating service threads, file descriptors, memory, and PIDs outside `kern_agent.slice`; the units define no explicit task/memory caps. This can starve or restart those control services but reboot clears it. Gate/reject before thread creation as the bounded Admin/proxy servers do, and add explicit service task/memory budgets. | Wontfix — reboot is the documented recovery for agent-induced resource exhaustion on this single-operator host; the reliability rule already caps it at Medium on that basis. |
| REL-008 | Medium | `e7b5494267aa` | Claude Opus 5 | `_close_turn` (`host/runtime/admin_api/orchestrator.py:1502-1527`) is the only place a turn is removed from the in-memory `_LIVE` registry, and when `server.close()` raises it records a `thread.error` event and returns early, deliberately leaving the entry in phase `FINISHING` so a new process cannot race a surviving scope. Nothing ever retries: there is no reaper, and `_stop_runtime_processes` skips turns that are not `STARTING`/`RUNNING` and never deletes entries. `close()` raises whenever `thread_scope.stop_thread_scope` does — when the `stop-agent-thread` helper exits non-zero because the scope is still active after its 5 s verification loop, or when the whole `sudo` invocation exceeds `CLOSE_TIMEOUT_SECONDS = 7`. The leaked entry then answers that thread with a permanent 409 ("the agent is finishing; retry shortly") while the durable `run_status` already reads `idle`, and it permanently consumes one of the runtime's three concurrency slots, because the capacity count at `:1128` counts every `_LIVE` entry regardless of phase. An agent that resists teardown can repeat this until the runtime admits no turns at all. A restart of the admin service clears it, so severity is capped at Medium. Retry scope teardown from the maintenance loop or on the next admission for that thread, and release the fence once the scope is confirmed gone rather than holding it for the process lifetime. | Wontfix — an admin-service restart (or reboot) is the documented recovery for this agent-induced wedge on this single-operator host; the reliability rule already caps it at Medium on that basis. |
| REL-009 | Medium | `e7b5494267aa` | Claude Opus 5 | Both provider adapters write to the child CLI's stdin over an ordinary blocking pipe with no deadline, while holding the locks the operator's recovery paths need. `ClaudeCodeSession._send_user_message_locked` (`claude_code.py:412-420`) writes and flushes under `self._stdin_lock`, and both `close()` (`:154`) and `interrupt()` (`:202`) begin by acquiring that same lock; `CodexAppServer.steer` (`codex_app_server.py:347-380`) writes under `_steer_lock` + `_stdin_lock`, and on the steer path the orchestrator holds `turn.delivery_lock` across the whole call while the admin API holds one of 64 send-lock stripes. A pipe holds 64 KiB, while a steer may be up to `MESSAGE_LIMIT = 50_000` characters and a session-handoff launch message up to `THREAD_HANDOFF_CHARACTER_LIMIT = 250_000`, so writes that block until the child consumes are routine rather than exotic — and the runtime processes run as `kern-agent`, so agent code can simply stop reading stdin. Once such a write blocks, the operator's own recovery action is what deadlocks: `stop_thread_turn` blocks on `turn.delivery_lock` and Claude's `interrupt()`/`close()` block on `_stdin_lock`. (Codex's `close()`/`interrupt()` take `_lifecycle_lock` rather than `_stdin_lock`, and block inside `proc.stdin.close()` on the CPython buffered-writer lock instead; the outcome is the same. The 250,000-character handoff write is taken under `_stdin_lock` only, since the execution worker releases `delivery_lock` before `provider.run_turn`, so it wedges interrupt/close rather than stop.) Restarting the admin service clears the held locks, capping this at Medium. Write to stdin with a deadline or hand writes to a dedicated writer thread with a bounded queue, and never hold `delivery_lock`/`_stdin_lock` across a blocking child write. | Wontfix — an admin-service restart (or reboot) is the documented recovery for this agent-induced wedge on this single-operator host; the reliability rule already caps it at Medium on that basis. |

## Threat model

- **Adversary/hazard:** a runaway or malicious agent (fork bombs, memory
  hogs, disk fills inside its cgroup and home), plus ordinary bad luck —
  crashes mid-transaction, database outages, full disks, unclean shutdowns.
  Assume no root compromise (that is axis 01).
- **Assets:** the operator's ability to observe and control the host at all
  times, and the host's ability to run indefinitely without manual disk or
  database surgery.
- **Out of scope:** AWS infrastructure failures; performance tuning where
  nothing is starved or stuck; provider API outages except where they wedge
  the admin API.

Severity mapping for this axis: a failure fully cleared by reboot is
**Medium at most**. "Fully cleared" means reboot alone returns the host to a
controllable state without data repair, configuration changes, redeployment,
or a code change. A durable full-disk condition, corrupt state that prevents
clean startup, or any other condition that survives reboot is not covered by
this cap.

## Minimal scope checklist

This checklist is not comprehensive: it names known-important areas, but the
audit question and threat model define the scope. Account for each item in
your coverage section, and report anything else within scope even if no item
below names it. At minimum, check service starvation, stuck admin operations,
reboot recovery, and unbounded database or disk growth.

1. Build a complete resource budget for agent runtimes, app backends, proxy,
   tools, admin API, Postgres, cloudflared, host-error collector, and local
   sockets: CPU, memory/swap, PIDs/threads, file descriptors, database
   connections, TCP/socket handlers, queues, disk/inodes, I/O bandwidth,
   temporary files, request/body buffers, and external calls. Identify the
   cgroup, semaphore, timeout, quota, volume, or backpressure for each—or its
   absence.
2. Verify `kern_agent.slice` and `kern_app.slice` under real contention:
   descendant placement for every harness/command, `CPUWeight`, `MemoryHigh`,
   `MemoryMax`, `MemorySwapMax`, `TasksMax`, OOM/fork-bomb behavior, orphan
   cleanup through `BindsTo`, and whether unbounded app or host services can
   still starve operator control.
3. Map concurrency and synchronization across HTTP handlers, thread execution
   workers, provider CLI processes, state/DB transactions, runtime/account
   refresh, network-policy/GitHub reconciliation, file helpers, passkey/login
   state, app backends/bridges, tools handlers/approvals/assets, network
   introspection, agent-app proxy, proxy connections, and maintenance loops.
   For every lock/semaphore/state transition, list blocking calls, ordering,
   ownership, cancellation, timeout, and crash cleanup.
4. Exercise the full thread lifecycle for all harnesses: start, provider
   session persistence callbacks, running steer/unsupported steer, activity
   updates, stop, provider failure, process exit, finishing cleanup, late
   completion, configuration handoff, admin restart, and host reboot. Database
   `run_status`/`run_number`, live in-memory fences, process/cgroup state, and
   public status must converge without duplicate execution or permanent 409.
5. Trace every service start/restart/crash dependency and systemd policy:
   Postgres, nftables, proxy, tools, network/app sockets, app backends, admin
   API, host errors, and cloudflared. Test unavailable dependencies, restart
   storms, partial boot, stale sockets/PIDs/scopes, and whether health and host
   errors remain observable while control operations recover.
6. Trace deploy, upgrade, recover, reconfigure, start, stop, and reboot end to
   end: version gates, volume discovery/attachment, migrations and rollback,
   permission repair, service/firewall ordering, passkey reset, interrupted
   bootstrap, full/read-only/corrupt admin or agent volume, and recovery from
   an instance disappearing mid-operation.
7. Inventory every growing database relation and prove both per-row/value and
   row-count/time retention: threads/events/activity, network events, provider
   usage/accounts/logins, GitHub credentials/audits/pending pushes, tools
   config/credentials/approvals/events, host errors, passkeys, migrations, and
   each active/deprecated app schema. Check indexes, sequences, WAL,
   autovacuum, transaction retries, and pruning under concurrent inserts.
8. Inventory every growing file/journal path: agent home and attachments,
   generated proxy certificates, PostgreSQL data/WAL, tool staging and streamed
   results, Git push quarantine objects, app files, provider auth/cache,
   bootstrap artifacts, systemd journal/host errors, swap, temp files, and
   partial uploads. Check separate-volume assumptions, atomic publication,
   startup/periodic cleanup, symlinks, open-file races, disk/inode exhaustion,
   and what survives reboot.
9. Audit every bound and pagination/backpressure contract at and beyond its
   limit: HTTP headers/bodies, compressed decode, proxy connections/WebSocket
   messages, thread/event lists and opaque cursors, retained handoff history,
   activity output, file/media/upload streams, app bridge/API payloads, tool
   calls/results/assets, approval counts, passkey/login attempts, processes,
   logs, errors, and database pools. Rejection must be bounded and leave the
   system reusable.
10. Inject failures at each external/blocking boundary: PostgreSQL stalls and
    restarts, DNS/TLS/provider hangs, helper timeout/EPIPE, systemd/PID1
    failure, unkillable process, proxy/tools/app/collector/cloudflared crash,
    client disconnect, corrupt provider response, journald rotation, clock
    change, and concurrent operator mutations. Verify deadlines, rollback,
    idempotency, retry ownership, redacted diagnostics, and no held global
    lock or leaked slot.
11. Check degradation and observability: health, runtime states, filesystem
    metrics, host-error coalescing, network/tool/app errors, systemd exit
    capture, upgrade notice, and operator actions must remain bounded,
    truthful, and usable when one dependency is down. Diagnostic collection
    itself must not amplify an outage or consume unbounded storage.
12. Run load, crash, reboot, migration, low-disk/inode, OOM/fork, socket/
    connection exhaustion, concurrent thread/app/tool, and long-idle tests on
    a deployed host. For each failure, document whether ordinary restart or
    host reboot fully recovers it; anything surviving reboot requires a tested
    repair path and is not eligible for the reboot-resolvable severity cap.

## Collaborative review

### `6151eea5abb61590684c4cf667ae6f619d705231`

Reviewed by: gpt-5.6-sol; Claude Opus 5

Methodology: repository-level resource-budget, blocking-boundary, durable-
growth, lifecycle, and recovery audit across every host service, agent/app
slice, local listener, database table, file path, queue/lock, and deployment
operation. Bounds and cleanup were traced in source/config/tests; focused
state, push-gate, service, orchestration, and deployment tests were used. No
deployed load, low-disk, OOM/fork, crash, migration-failure, or reboot drill
was performed.

#### What was reviewed

- Bootstrap/rendered units and deploy verification for Postgres, Admin API,
  proxy, tools, network/app sockets, app backends, cloudflared, host-error
  collector, `kern_agent.slice`, `kern_app.slice`, nftables, swap, and the
  root/agent/admin volume layout.
- Admin API/orchestrator/runtime lifecycle: handler pool, mutation/provider/
  OAuth locks, worker capacity, start/steer/stop/finish fencing, helper and
  provider subprocesses, session callbacks, restart reconciliation, health/
  filesystem/process/error observability, and reboot.
- `core/{db,pgclient,state}.py` and every growing relation: threads/events/
  activity, sessions, usage/accounts, network/tool logs, credentials,
  approvals/assets, host errors, passkeys, GitHub audit/pending pushes, and
  active/deprecated app schemas, with caps, pruning, pagination, transactions,
  and blocking calls.
- Proxy request/certificate/WebSocket/quarantine resources; tools, network-
  introspection, agent-app and app-backend sockets; app API/bridge/database
  concurrency; file view/upload, tool staging/streaming, agent attachments,
  provider caches/auth, temp files, journals, and partial cleanup.
- Deploy, upgrade, recover, reconfigure, start, stop, passkey reset, migration,
  volume attach/repair, service ordering/restart, and interruption paths.

#### Coverage and confidence

- Checklists 1–2: a service-by-service CPU, memory/swap, task/thread, FD,
  connection, handler, body, queue, disk, and timeout inventory was built.
  Agent CPU/RAM/swap/tasks and app CPU are cgrouped, but agent-reachable host
  services remain outside that containment. Existing REL-001/REL-005 and new
  REL-007 cover the concrete connection/memory/thread gaps. One correction to
  the ceiling those gaps hit: for the thread-per-connection sockets the
  binding resource is the service's file-descriptor soft limit — 1024 by
  default on the target Ubuntu 22.04/systemd 249 platform, with no
  `LimitNOFILE` set on any unit — so roughly a thousand stalled connections
  suffice, well below what a memory- or `TasksMax`-based estimate suggests.
  The app-backend admin socket shares this shape and additionally lacks the
  per-connection read timeout its three siblings all set; because it is served
  from the admin API's own process and fd table, exhausting it also stops the
  operator-facing TCP listener accepting. That is registered on axis 04 as
  ADM-004, where the admin-availability impact belongs, and was independently
  found here and on axis 05.
- Checklists 3–4: Admin, provider, OAuth, DB, worker, runtime, app, tool, proxy,
  and helper locks/semaphores were mapped with their blocking calls, ordering,
  timeouts, cancellation, and crash cleanup. Thread start/steer/stop/finish,
  late exit, session persistence, restart reconciliation, and live/database
  fences were traced for Codex, Claude Code, and Hermes. REL-003 remains the
  indefinite DB/mutation-lock case. Two further blocking boundaries were found
  by asking, for each lock, what the operator's own recovery path needs rather
  than only whether the ordering is acyclic. The child CLI's stdin is an
  ordinary blocking pipe holding 64 KiB, written under the very locks that
  stop/interrupt/close acquire, while message limits reach 50,000 and 250,000
  characters and the child runs as `kern-agent` — so an agent that stops
  reading stdin wedges the operator's stop button (REL-009). And the live-turn
  fence is released in exactly one place with no retry, so a scope teardown
  that fails or exceeds its 7 s budget strands the entry in `FINISHING`,
  yielding a permanent 409 against an `idle` durable status and a permanently
  consumed runtime slot (REL-008). Both are cleared by restarting the admin
  service, hence Medium.
- Checklists 5–6: service dependencies, restart policies, stale socket/scope
  cleanup, migration ordering, lifecycle version/volume semantics, permission
  repair, passkey reset, and interrupted replacement were reviewed. Restart
  paths are fail-closed and observable when durable state is healthy; no
  partial-boot or corrupt/full-volume run was performed.
- Checklists 7–8: all growing relations and filesystem/journal paths were
  inventoried for row/value/count/time bounds and cleanup. Existing
  REL-002/REL-004 remain open; REL-006 adds a distinct durable Git quarantine
  and pending-row path with neither quota nor expiry. An independent sweep
  reached REL-006's two halves separately — the unbounded quarantine mirror on
  the admin volume, and `pending_pushes` rows that are never pruned while the
  operator route returns the whole table — which is worth recording as
  corroboration rather than as new rows, since REL-006 already names the
  missing quota, TTL, pruning, and bounded listing. Worst-case WAL,
  autovacuum, total DB cap, journal rotation, and admin-volume sizing were not
  measured on a deployed host.
- Checklist 9: API/header/body/page/event/activity/file/app/tool/approval/
  passkey/process/log limits and their beyond-limit paths were checked.
  Rejections generally leave reusable slots/state; the socket admission point
  behind REL-007 allows work to accumulate before the intended call cap.
- Checklist 10: PostgreSQL, DNS/TLS/provider, helper, systemd, service crash,
  client disconnect, malformed response, and concurrent mutation boundaries
  were traced for deadlines, rollback, idempotency, and diagnostics.
  Repository tests cover deterministic failures, but no unkillable process,
  clock shift, sustained dependency stall, or restart storm was injected.
- Checklist 11: health, runtime, filesystem, host-error coalescing, network/
  tool/app errors, service-exit capture, upgrade notice, and safe operator
  actions were checked under represented error states. Availability after
  full disk or global task pressure remains lower confidence.
- Checklist 12: no live contention, load, crash, reboot, low-disk/inode,
  OOM/fork, socket exhaustion, concurrent app/tool, migration, or long-idle
  campaign was run. Findings are code/config-backed and classify reboot-
  persistent versus reboot-cleared impact explicitly; confidence is high for
  missing bounds and medium for the exact load needed to exhaust a real host.
