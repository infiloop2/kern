# Audit: Agent Isolation From Host and Operator Data

Finding ID prefix: `ISO`. See [README.md](README.md) for the sweep process,
finding format, and severity scale.

## Audit question

Can the agent process, or anything it spawns, read or change another host
user's data, reach privileged secrets or sockets, or gain privileges through
anything available to it on the host?

## Reviewed commits

Latest reviewed commit: `f28b50e87b61`.

| Commit | Reviewed by |
| --- | --- |
| `f28b50e87b61` | Claude Opus 4.8; GPT-5.5 |

## Findings

| Finding | Severity | Found at | Found by | Description | Resolution |
| --- | --- | --- | --- | --- | --- |
| ISO-001 | Info | `f28b50e87b61` | GPT-5.5 | `docs/architecture/filesystem.md` described policy-update, proxy-state-read, and provider-pin-sync helpers that did not exist, overstating the privileged helper surface and misdirecting reviewers. Align the inventory with the actual fixed sudo-helper allowlist. | Fixed at `fa6dc4ab5bc` |

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

### `f28b50e87b61`

Reviewed by: Claude Opus 4.8 (claude-opus-4-8); GPT-5.5 (gpt-5.5)

Methodology: static code reading of the bootstrap, the six sudo helpers, the
admin API's agent-facing routes, and the nftables/pg_hba configuration.
Reasoned about filesystem modes and socket reachability from the bootstrap
source; did not run a live host or write exploit code.

#### What was reviewed

- `host/bootstrap/bootstrap.sh`: service-user creation (stable uids), durable
  volume ownership/mode fixups, the `/etc/sudoers.d/kern-host` grant,
  the nftables ruleset, Postgres `pg_hba.conf`/`postgresql.conf`, the proxy CA
  key/tunnel-token/agent-home modes, and the snapd masking.
- All six helpers in `host/bootstrap/helpers/`: `read-agent-file.sh`,
  `run-codex-app-server.sh`, `run-claude-code.sh`, `read-codex-account-id.sh`,
  `read-claude-account.sh`, `reboot-host.sh`.
- Agent-controlled content flowing into the admin service: the
  `read-agent-file` list/read routes and their argument handling
  (`host/runtime/admin_api/service.py` `_run_agent_file_helper`, `_agent_file_path`),
  and the `agent-processes` `/proc` reader.
- Runtime launch/shutdown and the environment handed to agent processes;
  database roles/grants and proxy state access; architecture claims about
  privilege, filesystem, and admin-state boundaries.

#### Coverage details

- The agent (`kern-agent`) has no sudo entry; the sudoers grant is
  scoped to `kern-admin` and six fixed absolute helper paths with no
  wildcards. Every helper either demotes to `kern-agent` via `runuser`
  before doing work (so an admin-to-agent read/launch is a downward crossing
  that hands the agent nothing new) or is a fixed command (`systemctl
  reboot`). Executed directly by the agent the helpers fail, because
  `runuser`/`systemctl reboot` need privileges the agent lacks.
- `read-agent-file.sh` opens each path component with `O_NOFOLLOW`/
  `O_DIRECTORY` under a directory fd, rejects symlinks and `..`, opens files
  `O_NONBLOCK` and re-checks `S_ISREG`, and caps listing/scan/read work — and
  it runs as the agent, so even a confinement slip could only read what the
  agent already can.
- Secret files are unreadable by the agent: proxy CA key `600 kern-proxy`
  under `proxy-state` (`700`), tunnel token `640 root:cloudflared` under
  `/etc/kern` (`0750`), admin-home/admin-state/agent-home each `700`,
  pgdata `700 postgres` under a `711 root` parent.
- nftables drops the agent's traffic to every loopback port except the proxy
  port (so the admin API on `127.0.0.1:7443` is unreachable) and drops all
  non-root DNS including to the local stub; the agent has no direct egress.

The one cross-user endpoint the agent can still reach at the socket layer is
the Postgres Unix socket (nftables filters IP, not `AF_UNIX`). Admin-state
confidentiality/integrity holds regardless, because `pg_hba.conf` grants roles
only to `kern-admin`/`kern-proxy`/`postgres` and rejects everyone
else under peer auth, so the agent cannot read or write any table. Socket
*reachability* is not an isolation break, but it is a denial-of-service vector
— see `REL-001` in [08-reliability.md](08-reliability.md).

#### Coverage and confidence

- Sudoers/helpers (checklist 1): all six helpers and the single sudoers line
  reviewed line by line; argument handling for `run-claude-code`'s `"$@"` is
  safe because the process runs as the agent regardless of arguments.
- File modes (checklist 2): traced every `chown`/`chmod`/`install` in
  bootstrap for the CA key, tunnel token, pgdata, and the three service homes;
  all deny `other`. Not independently verified against a running host's
  actual inode modes.
- Sockets (checklist 3): Postgres socket peer-auth reject confirmed in
  `pg_hba.conf`; agent→admin-API and agent→DNS drops confirmed in the nftables
  output chain; snapd masked. I did **not** enumerate every systemd/DBus
  endpoint the agent uid can reach beyond noting the container runtime is
  absent and snapd is masked — a dedicated review of the agent's reachable
  `/run` sockets would strengthen this.
- Data flows (checklist 4): agent file names/paths reach the admin service
  only through `read-agent-file` (path confined) and are rendered safely in
  the UI (see [03-security-admin-ui.md](03-security-admin-ui.md)).
- Environment (checklist 5): run-helpers set explicit `HOME`/proxy vars via
  `env`; sudo's `env_reset` plus `runuser` bound what the agent inherits, and
  no admin secret is passed as an argument or environment value to the agent
  runtimes.
- Out of scope, untested: kernel/setuid local-privilege-escalation and EC2
  metadata credential theft (the latter depends on IMDS configuration set at
  instance launch, outside this layer).
