# Users and Privilege Boundaries

| User | Purpose | Privileges |
| --- | --- | --- |
| `kern-operator` | Human SSH login. | Full passwordless sudo, and therefore intentionally equivalent to root once logged in. |
| `kern-admin` | Runs the admin API; owns admin state (the `kern_admin` database role, full access). | sudo for exactly fifteen root helpers (below). No internet egress at all. |
| `kern-tools` | Runs the bundled tool packages in the dedicated tools service; owns the agent-facing tools socket. | No sudo. Postgres role scoped to the five tool tables plus read access to `secret_keys`. DNS and outbound HTTPS (443) for tool third-party APIs; explicitly blocked from the loopback admin listener. |
| `kern-agent-network` | Runs the network-introspection service and owns its agent-facing socket. | No sudo, secrets, or egress. Postgres role has SELECT-only access to network policy and decision-log tables. |
| `kern-proxy` | Runs the policy proxy; owns proxy TLS and Git quarantine files. | No sudo. A narrow Postgres role reads enforcement inputs and the working token/key, inserts network and pending-push records, and prunes network events. Only nftables-approved DNS and TCP 80/443 egress; explicitly blocked from the loopback admin listener. |
| `kern-agent` | Runs Codex, Claude Code, Grok, and Hermes runtime processes. | None. No sudo, no database role, no direct network off the host. Its only loopback egress is the policy-proxy port and its own preview range `8000-8015` (its own HTTP servers). That range is default-deny — only the agent and the operator's SSH forward are allowed, both directions dropped for everyone else — so no other principal reaches a preview server or answers a connection the agent opened. See [agent-preview-ports.md](agent-preview-ports.md). |
| `kern-workspace` | Runs Chat, Web Apps, global Memory/Schedules, and the agent-facing Workspace API socket. | No sudo, secrets, or egress. Its database role has DML only on eight Workspace tables and five sequences in `public`, with no DDL or access to other admin state. It may answer the admin-only loopback port and call allowlisted host thread routes over the peer-authenticated admin socket. |
| `kern-embedding` | Runs the socket-activated local ONNX text encoder. | No sudo, database role, secrets, home, or network namespace. It reads only the root-owned model/runtime and accepts bounded requests from `kern-admin` and `kern-workspace` through a mode-`0660` systemd socket. |
| `cloudflared` | Runs the optional Cloudflare Tunnel connector. | No sudo, no database role. Only nftables-approved DNS, TCP 443, and TCP/UDP 7844 egress; one of the four trusted uids allowed to connect to the loopback admin listener. |
| `postgres` | Runs the admin-state Postgres. | Database superuser over the local socket; no sudo, no network egress. |

The service accounts use fixed numeric IDs: `kern-admin` is
`47741`, `kern-proxy` is `47742`, `kern-agent` is `47743`,
`cloudflared` is `47744`, `postgres` is `47745` (created by bootstrap
before the PostgreSQL packages would assign a dynamic id),
`kern-tools` is `47746`, `kern-agent-network` is `47748`, the Workspace
admin-socket group is `47749`, `kern-workspace` is `47750`, and
`kern-embedding` is `47751`. Stable IDs keep durable root-volume and
Postgres ownership valid when `/etc/passwd` is replaced. Bootstrap deletes the
retired dynamic app accounts; their UID range had no durable filesystem
ownership and is not reserved.

## Root-Owned Helper Pattern

Whenever one service user needs a narrow operation that crosses a Unix user
boundary, the host uses a root-owned helper instead of broad filesystem or sudo
access. The helper file is owned by root and not writable by service users;
`sudoers` grants `kern-admin` permission to execute exactly that helper
path. The helper then does one bounded action, usually by immediately demoting
with `runuser -u <target-user>`.

This is why the admin service can start agent runtimes and read provider account
pins without being able to generally read or write `agent-home`. The launch
helpers run the CLI processes as `kern-agent`; the account helpers read as
`kern-agent` and print only the account id or token hash needed by network
guards. If a helper or sudoers entry were writable by a service user, that user
could turn the sudo rule into arbitrary root execution, so these files stay on
the root volume as root-owned code.

`kern-admin`'s sudoers entry allows only fifteen fixed helpers in
`/usr/local/lib/kern-host/`:

- `reboot-host` — runs `systemctl reboot`.
- `run-codex-app-server` — starts a stdio Codex app-server demoted to
  `kern-agent`, with proxy environment variables and the proxy CA set,
  in a transient scope under the resource-limited `kern_agent.slice`.
- `read-codex-account-id` — reads the agent user's Codex auth files and prints
  only the inferred ChatGPT account id.
- `run-claude-code` — runs the Claude Code CLI demoted to `kern-agent`,
  with the same proxy and CA environment, in a transient scope under the same
  slice.
- `read-claude-account` — has two modes with different privilege levels. Its
  default read mode demotes to `kern-agent` with `runuser` and prints only
  account metadata plus a SHA-256 hash of the OAuth bearer token. Its
  `--attest` mode is the exception to the demote-immediately rule above: it
  runs its whole body **as root** and makes one outbound request to
  `api.anthropic.com/api/oauth/profile` so the provider — not agent-writable
  metadata — attests which account the current token belongs to. Root is
  required because the agent uid can only reach the local proxy (which rejects
  a just-rotated token) and the admin uid has no egress, and the raw token is
  needed for the request, so it cannot be handed in by a demoted pass without
  exposing the secret to the no-egress admin uid. Because root itself opens the
  agent-owned credential file, that read is hardened the same way
  `read-agent-file` is: it walks directory fds with `O_NOFOLLOW`, opens the
  credential with `O_NOFOLLOW | O_NONBLOCK`, re-checks `S_ISREG` on the opened
  fd, and caps the read — so an agent-swapped symlink, FIFO, `/dev/zero`, or
  root-only path cannot redirect it, hang it, exhaust memory, or leak an
  existence/size oracle. The raw token never leaves the helper process; only
  the attested account uuid, optional email/organization uuid, and the token
  hash are printed.
- `run-hermes` — starts one Hermes query as `kern-agent`, passes the
  prompt over stdin, and uses the same dummy AWS and agent-slice boundary.
- `run-agent-script` — runs one scheduled bash script as `kern-agent` in the
  same agent-slice, per-thread scope, and proxy environment a model turn gets,
  so a scheduled script is exactly as confined as an agent turn and no more
  privileged. Root builds that boundary and validates the path's spelling —
  an absolute `.sh` path under `agent-home`, no relative segments — but never
  opens it: the symlink and regular-file checks run after the demotion, where
  an agent-planted symlink can reach nothing the agent could not already
  reach. The scope carries `RuntimeMaxSec` as the backstop for the admin API's
  own 15-minute turn timeout.
- `stop-agent-thread` — SIGKILLs and stops the transient
  `kern-agent-thread-<thread_id>.scope` cgroup and clears any failed
  remnant, so a stopped turn frees its thread's scope name. It validates the
  thread id against the same pattern the launch helpers enforce and touches
  only that one unit.
- `read-aws-account` — receives the Bedrock key pair from the admin
  service through its environment and makes exactly one STS identity request.
  Root egress is required because the admin uid has none; the credential is
  never written to disk or exposed to the agent.
- `clear-agent-auth` — removes Codex/Claude local auth files as
  `kern-agent` during linked-account reset.
- `read-agent-file` — demotes to `kern-agent`, confines paths to
  `agent-home`, rejects symlinks, bounds directory scan work, and lists
  directories, returns bounded text previews, or streams one bounded regular
  file to the authenticated Files viewer or download response.
- `check-for-upgrade` — fetches only the public
  `infiloop2/kern` main-branch `VERSION` file over HTTPS, with strict
  connection, transfer-time, and response-size limits. It accepts no input.
- `mint-github-app-token` — mints a short-lived, installation-wide GitHub App
  token from an App id, installation id, and private key piped on stdin. It
  runs as root because the admin service has no outbound network access; the
  key only ever moves through pipes (admin stdin in, openssl
  stdin on) and nothing is persisted.
- `audit-github-repo` — fetches the per-repository facts behind the operator
  warnings with a token piped on stdin, the same shape as the mint helper:
  root for the egress, facts on stdout, no state.
- `approve-github-push` — replays or cleans up a push held by the `.github`
  approval gate. The admin service passes the reviewed push id, ref updates,
  and working GitHub token on stdin; the helper uses root egress and the
  proxy-state quarantine mirror, then reports JSON success or failure.

Network policy, provider account pins, and network events need no helpers:
they are database tables the admin service (schema owner) writes after
validation. The proxy's narrow database role reads enforcement inputs,
inserts and prunes its own events, and inserts held-push records. The GitHub
credential is also an admin-owned database table
with no proxy grant; the admin service publishes only the short-lived working
token to the proxy-readable `proxy_github_token` row, and the proxy injects
it into policy-approved GitHub requests. The agent never holds the credential
— there is no agent-readable token file — so app-token minting, repository
audit, and approved push replay cross privilege boundaries through the helpers
above.

The tools socket (`/run/kern-tools/tools.sock`) is the deliberate crossing
from the agent to the tools service: the harnesses spawn the MCP shim as
`kern-agent`, and the `kern-tools`-owned socket service accepts only
the `kern-agent` and `kern-admin` uids by kernel peer credentials
(`SO_PEERCRED`). The agent can enumerate the whole bundled catalog and read any
action's input schema, enabled or not — manifest data carries no credentials,
config keys, or OAuth scopes — but it can *execute* only the enabled tools'
actions, which the tools service enforces per call rather than by hiding
declarations. The admin
service uses the same socket (admin uid, `/operator/...` routes) to delegate the
operator operations that need the tools service's egress. Tool secrets and
approval decisions stay in Postgres, reachable by the scoped `kern-tools`
role and the admin role but not the agent, and approval-gated actions still
require the operator's decision in the admin UI.

For local-video handoff, the MCP shim opens a regular file under the agent uid
and streams only its bytes and bounded metadata through the same socket. The
tools service never receives or opens the agent pathname. Its private runtime
spool is mode 0700 with mode-0600 assets; packages see only tool-scoped ids and
already-open streams. Instagram bytes remain local until approval.

The Workspace agent socket (`/run/kern-workspace/agent.sock`) is the second
deliberate agent-side crossing. The main `kern-workspace` service authenticates
the `kern-agent` peer uid before allocating a bounded handler, validates one
bounded `/agent/` call, and requires an explicit immutable Web App id. There is
no cgroup-derived app authority: any agent may access any existing app, while
archived apps reject writes. It also offers swarm memory and schedule CRUD,
identity-derived self-memory for App and Chat threads, and an informational
current thread id from the peer cgroup. The service
holds no secrets and no egress; see
[`workspace-agent-api.md`](workspaces/workspace-agent-api.md).

The network-introspection socket
(`/run/kern-agent-network/agent-network.sock`) is a separate read-only
crossing from the agent to `kern-agent-network`. Peer credentials admit
only the agent uid. The service has no egress and can SELECT only policy and
network-event tables, so the egress-capable tools service receives no network
policy database access.

Admin state adds one more boundary with the same shape: the database accepts
Unix-socket connections only, authenticated by OS identity (`peer`), with a role
for `kern-admin` (full admin state), narrowly scoped roles for
`kern-proxy` (enforcement inputs, working events/pushes, and its token),
`kern-tools` (the five tool tables plus the shared encryption key),
`kern-agent-network` (SELECT-only policy and network-event state),
`kern-workspace` (DML-only on eight named tables), `postgres` for operators, and an
explicit reject for everyone else, so
the agent user cannot read or write admin state, and a compromised tools, proxy,
or Workspace service reaches only its granted tables, even though the socket
path is technically reachable. Operators inspect it with
`sudo -u postgres psql kern_admin`.

`kern-agent` has no sudo, no login shell, and is in no privileged group,
and it cannot write any root-owned code, config, policy, or CA file. To shrink
the set of root daemons it could reach for privilege escalation, the host runs
no container runtime and masks the unused, world-accessible `snapd` socket.
Beyond that, escalation reduces to a generic OS bug (a setuid/kernel local
exploit) — outside this layer's control and bounded by the security group even
if it succeeds. The proxy parses agent traffic, so it has its own service uid
instead of root. It is still intentionally small, dependency-free Python and
shells out only with argument lists (no shell) to absolute paths.

When a Cloudflare Tunnel operator endpoint is configured, `cloudflared` is a
separate unprivileged service user with no sudo and no access to admin, proxy,
or agent durable state. Its only Kern secret is the root-volume tunnel
token file `/etc/kern/cloudflared.token`, owned `root:cloudflared` and
mode `0640`; this lets the connector read the token without exposing it
directly to the admin API, proxy, or agent. The SSH operator can read it only
by deliberately using its unrestricted sudo authority.
