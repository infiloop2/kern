# Audit: Reliability, Resource Isolation, and Recovery

Finding ID prefix: `REL`. See [README.md](README.md) for the sweep process,
finding format, and severity scale.

## Audit question

Can a runaway agent or ordinary failure make the host uncontrollable or
require manual repair?

## Reviewed commits

Latest reviewed commit: `f28b50e87b61`.

| Commit | Reviewed by |
| --- | --- |
| `f28b50e87b61` | Claude Opus 4.8; GPT-5.5 |

## Findings

| Finding | Severity | Found at | Found by | Description | Resolution |
| --- | --- | --- | --- | --- | --- |
| REL-001 | Medium | `f28b50e87b61` | Claude Opus 4.8 | The agent uid could exhaust Postgres backend slots by opening Unix-socket connections and stalling before the startup packet, temporarily making the admin API and proxy unable to connect. Reboot clears the stalled agent processes, so this is capped at Medium under the reliability rule. Restrict the socket to service users or sharply bound authentication startup time. | Open |
| REL-002 | High | `f28b50e87b61` | Claude Opus 4.8; GPT-5.5 | Wildcard-allowed hosts caused an unbounded durable cache of generated TLS key/certificate files on the admin volume. An agent could request unlimited unique subdomains until the volume shared with Postgres filled. The files survive reboot, so reboot does not resolve the condition. Cap/evict the cache or move it to bounded ephemeral storage. | Open |
| REL-003 | Medium | `f28b50e87b61` | Claude Opus 4.8 | Postgres socket reads and statements had no timeout, so a stalled database could hold the process-wide mutation lock indefinitely and freeze operator mutations. Reboot clears the in-memory lock when underlying durable state is healthy; add socket and statement deadlines so recovery does not require reboot. | Open |
| REL-004 | Medium | `f28b50e87b61` | Claude Opus 4.8 | Agent event retention bounded row count but not message size, allowing unusually large streamed messages to grow durable Postgres storage far beyond the apparent cap. Bound each stored message as well as the number of rows. | Open |
| REL-005 | Medium | `f28b50e87b61` | Claude Opus 4.8; GPT-5.5 | The proxy could buffer up to 128 MiB in each of 64 handlers while running outside the agent memory cgroup, allowing roughly 8 GiB of pressure and an operator-visible proxy/OOM outage. Reboot clears the memory condition, so severity is capped at Medium. Add an aggregate memory bound, lower the body/concurrency product, or reserve service memory with cgroups. | Open |

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

### `f28b50e87b61`

Reviewed by: Claude Opus 4.8 (claude-opus-4-8); GPT-5.5 (gpt-5.5)

Methodology: static reading of the cgroup/slice unit, the admin-API threading
and lock discipline, the DB connection layer and wire client, the orchestrator
worker pool, and the state pruning/caps. The five findings below are reasoned
from the code and standard PostgreSQL/systemd defaults; none were reproduced on
a live host.

#### What was reviewed

- `host/bootstrap/bootstrap.sh`: `kern_agent.slice`
  (`CPUWeight`/`MemoryHigh`/`MemoryMax`/`MemorySwapMax`/`TasksMax`), the
  network-proxy/admin-api/postgres units and restart policies, the volume
  layout, `postgresql.conf` (`max_connections=50`), and `pg_hba.conf`.
- `host/runtime/admin_api/service.py` locks (mutation lock usage, `NETWORK_POLICY_LOCK`,
  `OAUTH_LOGIN_LOCK`), helper timeouts, the maintenance loop, and the
  queue/steer caps.
- `host/runtime/core/state.py` (`mutation()` RLock, event caps + amortized prune,
  task/thread-session pruning), `host/runtime/core/db.py` (pool, `MAX_ACTIVE_CONNECTIONS`,
  checkout timeout), `host/runtime/core/pgclient.py` (socket handling),
  `host/runtime/admin_api/orchestrator.py` (worker pool, claim caps, lock ordering).
- `host/runtime/network_proxy/service.py` for proxy-side resource bounds.

#### Coverage and confidence

- **REL-A (starvation):** CPU/memory/PIDs are bounded for agent runtimes by
  `kern_agent.slice` (`CPUWeight=50`, `MemoryHigh/Max`, `MemorySwapMax`,
  `TasksMax=4096`) and agent-home is on a separate volume, so those are sound.
  The gaps are the resources the slice does not cover and the services that
  are *not* in it: the Postgres connection budget (REL-001), the admin-volume
  disk shared with Postgres (REL-002/REL-004), and the un-capped proxy service
  memory (REL-005).
- **REL-B (admin API stuck):** lock discipline is otherwise good — mutation
  lock, `NETWORK_POLICY_LOCK`/`OAUTH_LOGIN_LOCK` (both 5s-timeout → 409), and
  the orchestrator's documented mutation→`_POOL_LOCK` ordering are acyclic, and
  slow work (runtime spawn/turn/close, helper subprocesses) runs outside all
  locks. The single systemic weakness is the missing DB timeout (REL-003), which
  turns any Postgres stall into an indefinite mutation-lock hold. Helper
  subprocess calls are timeout-guarded (10s).
- **REL-C (reboot recovery):** reviewed by reading only. `initialize_state`
  fails orphaned running tasks on restart; every service is `Restart=always`
  with `StartLimitIntervalSec=0`; nftables is enabled so egress stays
  fail-closed before the proxy comes up; connections are ping-verified on
  checkout so a Postgres restart costs a reconnect. I did **not** test a real
  reboot, a reboot with a full admin volume, or the `reboot-host` path under a
  degraded box — worth a live drill.
- **REL-D (bounded growth):** every admin-state table has a cap with an
  amortized range-delete prune (agent_events/network_events 1M rows,
  finished tasks 100k, thread sessions 100k/runtime, idempotency 10k). Growth
  is bounded in *row count*; the exceptions are per-row size (REL-004) and the
  on-disk cert cache (REL-002), which are the real disk risks. I did not compute
  the worst-case total DB size against the admin volume's provisioned size —
  the caps permit tens of GB, so volume sizing deserves its own check.
- Not done: no live load test, fork-bomb/OOM drill, socket-exhaustion
  reproduction, or reboot drill. The findings are reasoned from the code and
  documented PostgreSQL/systemd defaults; REL-001/REL-002 in particular should be
  confirmed on a running host.
