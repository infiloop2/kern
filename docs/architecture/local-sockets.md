# Local Sockets

Kern uses Unix-domain sockets for local, in-host communication that must
not be exposed to the network or gated on the operator password. Each socket is
authenticated by **kernel peer credentials** (`SO_PEERCRED`): the server reads
the connecting process's uid from the kernel and accepts only specific uids, the
same OS-identity model as Postgres peer authentication. Peer credentials cannot
be spoofed by a process running as another uid, and Unix sockets are invisible to
the nftables loopback rules, so adding one does not widen the network surface.

The complete inventory keeps the local trust boundaries auditable in one
place. Every socket path is defined once in `host/constants.py` and served by
exactly one package under `host/runtime/` (the runtime's boundary rule: the
package that binds a socket is the only code that parses messages arriving on
it); servers, clients, and the end-of-deploy verifier all import the same
definition.

## Inventory

| Socket | Server (uid) | Allowed client uids | Purpose |
| --- | --- | --- | --- |
| `/var/run/postgresql/.s.PGSQL.5432` | `postgres` | `kern-admin`, `kern-proxy`, `kern-tools`, `kern-agent-network`, `kern-workspace`, and `postgres`, each mapped to its matching database role | Host and workspace state. `pg_hba.conf` admits these named peer identities and then explicitly rejects everyone else; table/schema grants narrow each non-owner role. There is no TCP listener. |
| `/run/kern-tools/tools.sock` | `kern-tools` (tools service) | `kern-agent`, `kern-admin` (each path-scoped) | Agent-facing tools surface plus operator delegation, scoped strictly by path per peer. Only `kern-agent` reaches `GET /tools`, JSON `POST /call`, and raw-byte `POST /assets/video` and `POST /assets/image`; the MCP shim forwards calls and streams agent-opened media without sending its pathname. Only `kern-admin` reaches `/operator/...` for OAuth, revoke, and approved execution. Neither peer can call the other's routes. |
| `/run/kern-agent-network/agent-network.sock` | `kern-agent-network` (network-introspection service) | `kern-agent` | Agent-facing `list_network_integrations` and `recent_network_denials` tools. The service has no egress and a SELECT-only Postgres role for policy and network-event tables; the MCP shim aggregates its listing with bundled tools and `workspace_api`. |
| `/run/kern-admin-api/workspace.sock` | `kern-admin:kern-workspace-api`, mode `0660` (admin API) | `kern-workspace` | Workspace service → host admin API. The kernel peer uid authenticates the fixed service and a narrow allowlist exposes only thread list/detail/message/stop/event operations. Thread ids pass through unchanged. |
| `/run/kern-embedding.sock` | `kern-embedding:kern-workspace-api`, mode `0660` (systemd socket activation) | `kern-admin`, `kern-workspace` | Bounded local query/passage inference for conversation and memory search. The CPU-only ONNX service has no network or database access and exits after five idle minutes. |
| `/run/kern-workspace/agent.sock` | `kern-workspace` | `kern-agent` | Agent → Workspace API (`POST /call`, used by `workspace_api` and the typed conversation-history tools). Peer authentication and pre-handler connection caps precede validation of bounded Web App, global Memory/Schedules, thread-identity, and read-only conversation-history routes. See [`workspace-agent-api.md`](workspaces/workspace-agent-api.md). |

## Design notes

- **Directories are world-traversable; sockets are peer- or group-gated.** Bootstrap
  gives the admin-api unit `RuntimeDirectory=kern-admin-api`, the tools
  unit `RuntimeDirectory=kern-tools`, the network-introspection unit
  `RuntimeDirectory=kern-agent-network`, and the Workspace unit
  `RuntimeDirectory=kern-workspace`, all at mode `0755`, so admitted service
  uids can reach the socket paths. Most sockets rely on the server's peer-uid
  check. `workspace.sock` additionally uses mode `0660` and a group containing
  only its admin owner and the fixed Workspace service account.
- **Every socket server bounds pre-authentication work.** Peer credentials are
  read only once a request arrives, so each server sets a per-connection read
  timeout and caps concurrent handlers; a local uid that connects and stalls can
  cost at most one slot. `workspace.sock` drops connections past its cap
  rather than queueing them, because it shares the admin API process's fd table
  with the operator-facing TCP listener.
- **Sockets are not TCP.** They carry no port, are unreachable over SSH
  forwarding or the Cloudflare Tunnel, and are not affected by the agent's nftables
  loopback drop rules. TCP loopback listeners (the admin API on `127.0.0.1:7443`,
  the Workspace service on `127.0.0.1:7450`) are separately firewalled by uid; see
  [`network-controls.md`](network-controls.md) and
  [`services-and-runtimes.md`](services-and-runtimes.md).
- **workspaces share one backend process.** The service listens on the fixed
  loopback port `7450`; only `kern-admin` may connect.
  Browser requests use `/v1/workspace/chat/...` or `/v1/workspace/web-apps/...` and are reverse
  proxied by `workspace_proxy.py`. Calls in the other direction use
  `workspace.sock`, where peer credentials prove the fixed service account.
  Agent calls enter the same process independently through `agent.sock`.

## The tools service edge

The tools socket is served by the dedicated `kern-tools` service (see
[`tools/host-integration.md`](tools/host-integration.md)), so the agent connects
to a low-privilege tools-owned socket rather than an admin-owned one. Instead of
the tools service reaching back to admin over a fourth socket, it reads tool
state directly with a Postgres role scoped to the five tool tables plus
read-only access to the encryption key used for its encrypted config and
credentials. The **admin service** connects **into** the tools socket (peer uid `kern-admin`,
`/operator/...` routes) to delegate the operator operations that need the tools
service's egress. The data-out control is therefore split by design: the tools
service has internet egress but only tool state; the admin service has all other
state but no egress.
