# Audit: Agent Isolation From Host and Operator Data

Finding ID prefix: `ISO`. See [README.md](README.md) for the sweep process,
finding format, and severity scale.

## Audit question

Can the agent process, or anything it spawns, read or change another host
user's data, reach privileged secrets or sockets, or gain privileges through
anything available to it on the host?

## Reviewed commits

Latest reviewed commit: `6151eea5abb61590684c4cf667ae6f619d705231`.

| Commit | Reviewed by |
| --- | --- |
| `6151eea5abb61590684c4cf667ae6f619d705231` | gpt-5.6-sol; Claude Opus 5 |

## Findings

| Finding | Severity | Found at | Found by | Description | Resolution |
| --- | --- | --- | --- | --- | --- |
| ISO-002 | Medium | `fa6dc4ab5bcd` | Claude Opus 5 | `read-claude-account --attest` is the only sudo helper that reads agent-writable state without demoting to `kern-agent`: the `--attest` branch `exec`s `python3` as root and reads `/mnt/kern-agent/agent-home/.claude/.credentials.json` through `json.loads(path.read_text())` with no `O_NOFOLLOW`, no `S_ISREG` re-check, and no size bound, and the expected-token comparison runs only after the whole file has been read. The agent owns that 0700 directory and the credential is not among the six `chattr +i` managed files, so it can replace the name with a symlink between the unprivileged read and the root read that follows seconds later in the same refresh. Pointing it at a FIFO blocks the root helper forever, and because a `kern-admin` parent cannot signal a root child, `subprocess.run`'s 20 s `ATTEST_HELPER_TIMEOUT_SECONDS` is inert and the admin thread hangs holding the Claude refresh lock; pointing it at `/dev/zero` allocates without bound outside `kern_agent.slice`; pointing it at any root-only path makes root open and drain it, which yields an existence-and-size oracle today and direct disclosure under any future change that surfaces the parsed value. Read the credential through a directory fd with `O_NOFOLLOW`/`O_NONBLOCK`, re-check `S_ISREG`, and cap the read, as the sibling `read-agent-file` helper already does — or pipe the token already read by the unprivileged pass into the attest helper on stdin. | Fixed — the root --attest branch reads .credentials.json through a directory-fd walk with O_NOFOLLOW + O_NONBLOCK, an fstat S_ISREG re-check, and a size cap (mirroring read-agent-file), so the symlink swap, FIFO hang, /dev/zero exhaustion, and root-only-path oracle no longer apply. |
| ISO-001 | Info | `f28b50e87b61` | GPT-5.5 | `docs/architecture/filesystem.md` described policy-update, proxy-state-read, and provider-pin-sync helpers that did not exist, overstating the privileged helper surface and misdirecting reviewers. Align the inventory with the actual fixed sudo-helper allowlist. | Fixed — the filesystem and helper inventories now match the actual fixed sudo-helper allowlist. |
| ISO-003 | Info | `fa6dc4ab5bcd` | Claude Opus 5 | `docs/architecture/privilege-boundaries.md` states the root-helper pattern as "one bounded action, usually by immediately demoting with `runuser -u <target-user>`" and lists `read-claude-account` only as an agent-file read with its outputs. Neither mentions that the helper's `--attest` branch runs its entire body as root. A reviewer working from the document would not know that a root-privileged read of agent-writable state exists at all, which is how ISO-002 stayed unexamined. Document the attest mode and its privilege level beside the demoting modes. | Fixed — privilege-boundaries.md now documents that read-claude-account --attest runs its whole body as root (the exception to the demote-immediately pattern) alongside the demoting read mode, and records the hardened-read posture. |

## Threat model

- **Adversary:** the agent itself — fully malicious code running as
  `kern-agent`, in auto-approve mode, with arbitrary local compute and
  the ability to write and execute programs. Assume the AI model is
  adversarial or prompt-injected.
- **Assets:** admin database contents (tasks, policy, account metadata),
  proxy CA private key and TLS material, admin password hash, provider OAuth
  tokens beyond the agent's own, `/etc/kern/cloudflared.token`,
  operator SSH environment, and root itself.
- **Out of scope:** Ubuntu/kernel/setuid vulnerabilities in stock OS
  packages, EC2/hypervisor escape, and physical access. (A Kern choice
  that *widens* exposure to such a bug — e.g. leaving an unnecessary
  privileged socket reachable — is in scope.)

## Minimal scope checklist

This checklist is not comprehensive: it names known-important areas, but the
audit question and threat model define the scope. Account for each item in
your coverage section, and report anything else within scope even if no item
below names it.

1. Enumerate every Unix identity, group, sudoers grant, root-owned helper,
   PATH shim, systemd unit, transient scope, and generated app identity.
   Verify stable uid/gid allocation, root ownership, modes, and that no
   agent-controlled path can replace executable or configuration code.
2. Trace Codex, Claude Code, and Hermes launch and shutdown from the admin API
   through their helpers and all descendants. Check demotion to `kern-agent`,
   `kern_agent.slice`, `BindsTo`, cgroup naming, environment, argv, stdin/stdout,
   inherited file descriptors, cwd, `HOME`, proxy variables, and root-owned
   harness/MCP configuration.
3. Audit every sudo helper and root implementation, including provider login
   and account reads, auth clearing, thread stopping, file read/upload,
   upgrade checks, AWS/GitHub credential operations, repository audit, and
   `.github` push approval. Check exact argument/stdin grammars, path and ref
   confinement, symlink/TOCTOU handling, environment preservation, subprocess
   output parsing, timeouts, cleanup, and failure behavior.
4. Walk all durable and ephemeral paths the host creates or preserves across
   deploy, upgrade, recover, and reconfigure. Check parent-directory traversal,
   ownership/modes, managed-path symlink replacement, temp files, `/proc`,
   `/run`, journald, and secrets including admin/tool/GitHub/Bedrock/provider
   credentials, proxy CA keys, tunnel tokens, SSH state, and database files.
5. Probe every local interface from the agent and its children: Postgres,
   admin/proxy/app TCP ports, preview ports 8000–8015, tools/network/app Unix
   sockets, the app-backend admin socket, systemd/D-Bus, and future listeners.
   Verify peer credentials, pid/cgroup attribution, socket modes, nftables
   source/destination and established-flow rules, and port-range disjointness.
6. Verify PostgreSQL peer authentication, role creation, grants, ownership,
   schema/search-path selection, and migration privileges for every service
   and app. The agent must have no database role, and a compromised
   low-privilege service must not expose another principal's state to it.
7. Trace every agent-controlled value entering privileged code: thread
   messages/events/activity, file names and bytes, process metadata, proxy
   requests/events, app and tool calls, provider metadata, Git refs/objects,
   and error records. Check for shell/SQL/path/unit/log injection, confused
   deputies, unsafe deserialization, and secret-bearing error output.
8. Prove the agent and its children cannot bypass the proxy through direct
   DNS/egress, cloud metadata, another service's egress, app ports, preview
   ports, or a listening service they induce a privileged process to call.
9. Repeat least-privilege probes on a freshly deployed host and after
   upgrade/recover/reconfigure. Compare actual users, modes, sockets, grants,
   nftables, units, environments, and process trees with generated artifacts
   and deployment verification tests.

## Collaborative review

### `6151eea5abb61590684c4cf667ae6f619d705231`

Reviewed by: gpt-5.6-sol; Claude Opus 5

Methodology: repository-level least-privilege audit from generated identities
and bootstrap artifacts through all privileged helpers, runtime launchers,
local listeners, database roles, durable paths, and agent-controlled inputs.
Each boundary was traced in source and against deployment verification/tests.
Privileged surfaces were read whole-file rather than by grep excerpt:
`bootstrap.sh` in full, all seventeen files under `host/bootstrap/helpers/`,
the four root implementations under `host/runtime/root_helpers/`, and the
push-gate engine. The nftables output-chain ordering was re-derived by hand
from `bootstrap.sh:950-997` plus the rendered app and preview blocks in
`render.py`. Three library behaviours the findings depend on were confirmed
by running them locally: that `json.loads(Path(x).read_text())` on a non-JSON
file leaks no file content in its traceback, that `Path('/dev/zero').read_text()`
allocates without bound, and that `subprocess.run(..., timeout=1)` returns
only after the child exits when `Popen.kill()` raises `PermissionError`.
No kernel exploit, live-host uid probe, or post-deploy inode/firewall
inspection was performed, and no exploit was executed end to end.

#### What was reviewed

- `host/bootstrap/bootstrap.sh`, `render.py`, `verify_deploy.py`, both user-data
  entry points, and constants/config: every fixed Unix uid/gid, operator and
  service home, app identity, volume mode repair, sudoers entry, systemd unit/
  slice, environment, nftables rule, PostgreSQL role and `pg_hba` line.
- All fixed helpers in `host/bootstrap/helpers/` and their Python
  implementations/callers: runtime launch and stop, provider/account reads,
  auth clearing, file read/upload, upgrade check, AWS and GitHub credential/
  repository operations, `.github` push approval, and reboot.
- Codex, Claude Code, and Hermes launch, session persistence, thread scope,
  shutdown, and account/auth paths across the Admin API, orchestrator,
  root-owned harness/shim configuration, and transient
  `kern_agent.slice` scopes.
- Every local crossing reachable by an agent: proxy and preview ports,
  Postgres, tools/network/app Unix sockets, the app-backend socket and app
  ports, Admin API, DNS, systemd/cgroup attribution, plus nftables and
  `SO_PEERCRED`/peer-auth checks.
- Durable and temporary agent/admin/proxy/tool/app/provider/GitHub paths,
  secret material, `/proc` readers, event/error data, Git refs and objects,
  filenames/bytes, helper stdin/argv grammars, subprocess invocation, and
  database schema/grant boundaries.

#### Coverage and confidence

- Checklist 1: stable identities, generated app identities, ownership/modes,
  services/slices, PATH shims, and the one fixed `kern-admin` sudo allowlist
  were enumerated. The agent has no sudo grant; helper installation and config
  paths are root-owned and not sourced from its workspace.
- Checklist 2: all three runtimes demote to `kern-agent`, receive bounded
  explicit environments and root-owned harness/MCP configuration, and run in
  per-thread scopes under `kern_agent.slice`; stop/exit cleanup and cgroup
  attribution were traced. No inherited privileged descriptor or secret
  argument/environment was found.
- Checklist 3: every current sudo helper and Python implementation was
  reviewed for fixed grammar, path/ref confinement, symlink/open-fd handling,
  environment, subprocess form, timeout, and cleanup. File helpers operate as
  the agent over dirfd/`O_NOFOLLOW` paths; credential/GitHub helpers bind
  operator-owned state and fixed repositories/refs before privileged work.
  A second pass over the same surface enumerated which helpers run as root
  after demotion and which do not, and found one exception: the `--attest`
  branch of `read-claude-account` (ISO-002), whose privilege level is also
  undocumented (ISO-003). Verified negatives from that pass:
  `check-for-upgrade` ignores caller arguments entirely; `reboot-host` is a
  fixed command; `clear-agent-auth` accepts exactly `codex|claude`;
  `upload_agent_file.py` links the final name `follow_symlinks=False`;
  `mint-github-app-token` keeps the App private key on `openssl`'s stdin and
  puts only the public JWT input in an `O_EXCL` temp file; `read-aws-account`
  takes its credential from the environment and never prints it; and
  `approve-github-push` operates only inside the `kern-proxy`-owned quarantine
  mirror the agent cannot traverse.
- Checklist 1, additional verified negative: the six root-owned files inside
  the agent's own 0700 directories would otherwise be unlinkable by the agent,
  because a directory owner may remove entries regardless of file ownership and
  no sticky bit is set. `chattr +i` (`bootstrap.sh:870-876`) is what actually
  prevents that, and `verify_deploy.check_immutable_agent_files` enforces it.
  Separately, every lifecycle mode terminates the instance and boots a fresh
  one (`host/cli/lifecycle.py:253-306`), so no agent process is alive between
  `sanitize_durable_paths` and `install_agent_home_files`; the symlink-
  replacement window that layout suggests does not exist.
- Checklist 2, additional verified negative: `subprocess.Popen` defaults to
  `close_fds=True`, so no admin-side database or listening-socket descriptor is
  inherited by an agent process, and `/proc/<pid>/environ` of a root sudo child
  is not readable by `kern-agent`. One lower-confidence point: Claude/Codex
  session ids returned by an agent-controlled process are re-passed as
  `--resume <id>` argv with no format check. That is not an isolation break —
  anything smuggled still executes with agent privileges — but a format check
  would be cheap.
- Checklists 4 and 6: secret and durable paths deny the agent by ownership and
  parent traversal; PostgreSQL uses Unix peer auth with explicit named roles
  and rejects the agent. Scoped service/app grants and schemas were compared
  with migrations. Actual deployed inode modes, grants, WAL paths, and
  post-upgrade repair were not sampled.
- Checklists 5 and 8: the agent reaches only the proxy and reserved preview
  ports over loopback plus three peer-authenticated service sockets.
  nftables blocks direct DNS/egress, metadata/private destinations, Admin/API/
  app ports, and other service uids from using preview listeners as egress.
  PostgreSQL's world-connectable socket still admits no agent role; its
  availability consequence remains REL-001 rather than an isolation break.
- Checklist 7: messages/events, file metadata and bytes, process/cgroup data,
  proxy/tool/app calls, Git data, and structured errors were traced into
  privileged parsers and subprocesses. No shell/SQL/unit injection, unsafe
  deserialization, stronger route, or secret-bearing error path was found.
- Checklist 9: generated artifacts and verification code cover identities,
  permissions, sockets, roles, units, and firewall rules at deploy. This
  repository-level sweep did not repeat those probes on a freshly deployed,
  upgraded, recovered, and reconfigured host. Confidence is high for generated
  policy and source boundaries, and medium for live-state drift or distro/
  systemd behavior not exercised here. This is the weakest area of the axis:
  no live or deployed Kern host existed in either review environment, so actual
  inode modes, a live `nft list ruleset`, real socket modes, and real process
  trees were never sampled. Naming the gaps in the deploy-time suite that
  substitutes for them: `verify_deploy.py` has no enforced probe for
  agent→app-backend port, agent→preview-port-from-another-uid, or a non-agent
  principal dialling the preview range, though the ruleset intends all three to
  be dropped; and no test exercises `read-claude-account --attest` against a
  non-regular credential file (`tests/test_deploy.py` asserts only the helper's
  presence in the sudoers line and the install list).
- Checklist 5, lower-confidence area: the systemd/D-Bus surface was reasoned
  about but not tested. `/run/dbus/system_bus_socket` is world-connectable on
  stock Ubuntu, and the conclusion that the agent can only read unit properties
  (polkit denying `manage-units` to a session-less daemon user) rests on stock
  policy files not read on a live host. `snapd` is masked and no container
  runtime is installed. Unprivileged user/network namespaces do not help the
  agent: a fresh netns has no route to the host's loopback, and `meta skuid`
  still resolves to the outer `kern-agent` kuid.
- Two in-scope observations that are recorded here rather than as findings,
  because neither had a reachable trigger at this commit. (a) `hermes-stdin.py`'s
  `--activity-nonce` sits in the argv of a process the agent itself runs, so
  `/proc/self/cmdline` gives a shell-capable agent the nonce; the "the model
  never sees the nonce" rationale in that file's docstring does not hold against
  this axis's adversary, but the consequence — forged activity cards — is
  axis 03's. (b) An agent turn running while an app backend's loopback port is
  momentarily free (a `RestartSec=3` crash window, or the gap between
  `start_services` and `finalize_deploy`) could bind that port and receive the
  admin API's browser-bridge requests for that app; `SO_REUSEADDR` does not let
  two live sockets share a port and every agent process is SIGKILLed with its
  thread scope at turn end, so this needs a second bug that crashes an app
  backend mid-turn.
