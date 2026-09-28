# Audit: Agent Isolation From Host and Operator Data

Finding ID prefix: `ISO`. See [README.md](README.md) for the sweep process,
finding format, and severity scale.

## Audit question

Can the agent process, or anything it spawns, read or change another host
user's data, reach privileged secrets or sockets, or gain privileges through
anything available to it on the host?

## Reviewed commits

Latest reviewed commit: `a597a8d063d735d11c30d1f2b2f6b66e7479ceac`.

| Commit | Reviewed by |
| --- | --- |
| `a597a8d063d735d11c30d1f2b2f6b66e7479ceac` | GPT-6 (Codex) |

## Findings

| Finding | Severity | Found at | Found by | Description | Resolution |
| --- | --- | --- | --- | --- | --- |
| ISO-002 | Medium | `fa6dc4ab5bcd` | Claude Opus 5 | `read-claude-account --attest` is the only sudo helper that reads agent-writable state without demoting to `kern-agent`: the `--attest` branch `exec`s `python3` as root and reads `/mnt/kern-agent/agent-home/.claude/.credentials.json` through `json.loads(path.read_text())` with no `O_NOFOLLOW`, no `S_ISREG` re-check, and no size bound, and the expected-token comparison runs only after the whole file has been read. The agent owns that 0700 directory and the credential is not among the six `chattr +i` managed files, so it can replace the name with a symlink between the unprivileged read and the root read that follows seconds later in the same refresh. Pointing it at a FIFO blocks the root helper forever, and because a `kern-admin` parent cannot signal a root child, `subprocess.run`'s 20 s `ATTEST_HELPER_TIMEOUT_SECONDS` is inert and the admin thread hangs holding the Claude refresh lock; pointing it at `/dev/zero` allocates without bound outside `kern_agent.slice`; pointing it at any root-only path makes root open and drain it, which yields an existence-and-size oracle today and direct disclosure under any future change that surfaces the parsed value. Read the credential through a directory fd with `O_NOFOLLOW`/`O_NONBLOCK`, re-check `S_ISREG`, and cap the read, as the sibling `read-agent-file` helper already does — or pipe the token already read by the unprivileged pass into the attest helper on stdin. | Fixed — the root --attest branch reads .credentials.json through a directory-fd walk with O_NOFOLLOW + O_NONBLOCK, an fstat S_ISREG re-check, and a size cap (mirroring read-agent-file), so the symlink swap, FIFO hang, /dev/zero exhaustion, and root-only-path oracle no longer apply. |
| ISO-004 | Low | `a597a8d063d735d11c30d1f2b2f6b66e7479ceac` | GPT-6 (Codex) | Grok root attestation opens `.grok/auth.json` or `.grok-2/auth.json` with `O_NOFOLLOW` only on the final file. An agent can replace the account directory with a symlink between the unprivileged account read and root attestation, causing root to read a different directory's `auth.json`. A disposable-file reproduction confirmed the parent traversal; no real secret disclosure was demonstrated, and existing regular-file/size bounds remain effective. Open the home and account directories with no-follow directory descriptors before the final read. | Fixed — root Grok attestation opens the home and selected account directory with no-follow directory descriptors before opening the bounded regular auth.json file, so symlinked credential directories cannot redirect the privileged read. |
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

### `a597a8d063d735d11c30d1f2b2f6b66e7479ceac`

Reviewed by: GPT-6 (Codex)

Methodology: source review of the current bootstrap, privilege transitions,
service interfaces, and state grants, plus credential-free regression tests.
The checkout was pinned to the current `origin/main` on 2026-09-26. Root helper
experiments extracted the exact embedded Python and used disposable files as
the current unprivileged user; they did not read another user's real data.

#### What was reviewed

- `host/bootstrap/{bootstrap.sh,render.py,verify_deploy.py}`, fixed helpers in
  `host/bootstrap/helpers/`, and `host/runtime/root_helpers/`: identity
  creation, sudoers, durable-path repair, managed immutable files, systemd
  units/scopes, environment, file operations, and deployment verification.
- Codex and its additional accounts, Claude Code, Grok/Grok 2, Hermes, and
  scheduled Bash launchers: validated thread ids, `runuser` demotion,
  explicit environments, `kern_agent.slice`, `BindsTo`, stop, and cleanup.
- The admin, proxy, tools, Workspace, agent-network, Workspace agent API,
  inference, embedding, transcription, and PostgreSQL boundaries. Reviewed
  socket modes and peer checks, database role/grant migrations, credential
  stores, `peer_identity.py`, and the nftables output-chain ordering.
- File read/upload, provider account attestation, AWS/GitHub account helpers,
  token minting, repository audit, and Git push quarantine: argv/stdin
  grammars, descriptor-relative opens, regular-file/size checks, subprocess
  boundaries, fixed provider destinations, leases, and error outputs.

#### Outcome and remediation evidence

The fixes and regression tests discussed below resolve this finding in the
stacked follow-up. The resolution records the operator-requested remediation;
the original audit commit and finding description remain unchanged.

ISO-004 is a new instance of a privileged path-confinement gap: Grok's root
attestation read protected the final file but followed its parent directory.
Replacing `.grok` with a symlink to a disposable sibling directory made the
baseline `read_auth()` return that directory's `auth.json`. This proves the
path traversal; it does not prove disclosure of an unknown real credential.
The remediation opens the agent home and selected account directory
with directory descriptors and `O_NOFOLLOW` before opening `auth.json`.
`test_grok_attestation_rejects_symlinked_credential_directories` exercises both
Grok slots and a symlinked home, rejecting them before the stubbed network call.
The focused deployment/helper suite passed 107 tests, including existing
special-file, oversized-file, and successful-attestation fixtures. ISO-004 is
resolved by the confined read and its regression coverage.

#### Coverage and confidence

- Checklist 1: identities, groups, sudo grants, shims, units, and scopes were
  inventoried. The agent receives no sudo or database role. Installed app
  identities are retired; the current architecture has one fixed Workspace
  principal, not one OS user per generated Web App.
- Checklists 2–3: launch/shutdown and helper boundaries were traced in source
  and exercised by launcher/deployment fixtures. Account reads that remain
  root were examined separately from demoting reads; this found ISO-004.
  Git approvals retain exact old-tip/ref checks and proxy-owned quarantine.
- Checklist 4: root-owned ancestors, agent-writable descendants, immutable
  managed configuration, service homes, `/run` sockets, secret files, and
  temporary-file handling were inspected. Recovery/upgrade behavior was
  checked through source and fake lifecycle tests, not a replacement host.
- Checklist 5: current listener/socket entry points and peer checks were
  traced. No live per-uid socket, D-Bus, `/proc`, or listener impersonation
  probe was performed. Tools' shared transport still authenticates after
  accepting a connection; historical admission-pressure concerns are not a
  newly demonstrated privilege bypass.
- Checklist 6: PostgreSQL uses Unix peer authentication, explicit service
  roles and grants, and no agent role. Actual cross-role queries and
  migrations are reserved for CI/deployed-host tests; local database tests
  intentionally skip on this production Kern host.
- Checklists 7–8: file/path, Git/ref, process/thread, request, and tool inputs
  were traced at privileged entry points. The generated firewall allows the
  agent only the policy proxy and its isolated preview range, with explicit
  established-flow ordering. Inference and media services do not accept the
  agent principal. No live direct-egress, DNS, metadata, or induced-listener
  experiment was attempted.
- Checklist 9: fresh deployment, upgrade, recover, and reconfigure probes were
  not run. They require the repository-admin-gated Lima/AWS workflows. Source
  and test results do not attest the running host's permissions or firewall.

Confidence is strongest for the deterministic helper regression and generated
configuration contracts, and lower for actual deployment and kernel-enforced
isolation until those live workflows run.
