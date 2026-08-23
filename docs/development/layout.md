# Code Layout

The host control-plane Python runtime uses only the standard library.
PostgreSQL is reached through the in-repo wire client rather than a third-party
driver, and a unit test rejects non-standard-library control-plane imports. The
isolated `kern-embedding` service is the deliberate exception: its dedicated
venv contains pinned FastEmbed/ONNX dependencies and cannot access the network
or database.

```text
host/
  bootstrap/
    agent-home/             # immutable runtime instructions and harness settings
    helpers/                # root-owned fixed sudo helpers installed on the host
    user_data.sh            # minimal first-boot operator/deploy-key setup
    bootstrap.sh            # full host bootstrap run over SSH as root
    verify_deploy.py        # root end-of-deploy verification of the provisioned state
  cli/                      # operator-side lifecycle and power commands
  migrations/               # one immutable host + workspace migration stream
  runtime/                  # service entrypoints plus process-owned domains;
                            # each socket has one serving package (see runtime/__init__.py)
    admin_api/              # kern-admin: operator TCP API, admin UI assets,
                            # workspace socket, routes, GitHub credential/audit flows
    agent_runtime/          # kern-admin: turn orchestration, provider trust,
                            # agent harness adapters and process supervision
    network_proxy/          # kern-proxy: policy-enforcing HTTP(S)/WS(S) proxy
    tools/                  # kern-tools: tools socket, tool execution, assets
    agent_network/          # kern-agent-network: read-only introspection socket
    workspace/             # kern-workspace: Chat, Web Apps, global resources,
                            # agent API socket
    embeddings/            # kern-embedding service + bounded stdlib clients
    agent_shim/             # kern-agent: stdio MCP shim, client-side only
    core/                   # shared socketless libraries: db, pgclient, state,
                            # secretbox, network_policy
    deploy/                 # bootstrap-run CLIs: migrate and write_config
    root_helpers/           # standalone CLIs invoked as root via sudo helpers
  network_integrations/     # network integrations: per-integration manifest,
                            # guard, and registry (see architecture/network-controls.md)
  tools/                    # host-neutral tool contract and bundled tool packages
  config.py                 # lifecycle input and network-policy validation
  constants.py              # shared ports, socket paths, and pinned service account ids
tests/
  smoke/                    # fresh live AWS and local Lima host checks
  stage/                    # persistent, credentialed live AWS checks
  smoke-ui/                 # deterministic local admin UI mock and browser smoke
docs/                       # API, architecture, development, and commit-scoped audits
.github/                    # no-network CI plus main/admin-gated live host smoke workflows
```

Important source areas and the context that runs them:

| Source | Runs as | Responsibility |
| --- | --- | --- |
| `host/cli/` | Operator machine | Parses and dispatches lifecycle commands; isolated AWS and Lima modules own provider resources, while shared helpers render and run bootstrap. `operation_lock.py` supplies the ephemeral same-user guard for both providers. |
| `host/config.py` | Operator machine and host services | Validates lifecycle input and the stored/runtime network policy. |
| `host/bootstrap/user_data.sh` | root through EC2 user data | Creates the operator account and installs only the single-use deploy SSH key. |
| `host/bootstrap/bootstrap.sh` | root through lifecycle SSH | Runs the ordered provisioning phases: mounts volumes, installs pinned dependencies, creates fixed users, configures PostgreSQL/nftables/systemd, applies migrations, writes trusted host files, and ends by running `verify_deploy`. |
| `host/bootstrap/verify_deploy.py` | root at the end of bootstrap | Independently re-checks accounts, path permissions, sockets, listeners, services, database peer auth, and live firewall behavior in both directions; any mismatch fails the deploy. |
| `host/bootstrap/helpers/` | root through exact `kern-admin` sudo rules | Launches runtimes as the agent user, reads or clears narrow agent-auth state, reads bounded agent files, reboots, and performs GitHub operations that need root egress. |
| `host/runtime/workspace/` | `kern-workspace` | Contains the fixed Chat and Web Apps backends, UI assets, browser dispatcher, agent Unix-socket API, and scheduler. See [workspaces](../architecture/workspaces/workspaces.md). |
| `host/migrations/` | `kern-admin` during bootstrap | Holds one ordered immutable stream for every table in `public`, including Chat and Web Apps. The consolidation migration grants the runtime Workspace role DML only on its named tables and sequences. |
| `host/tools/` | `kern-tools` | Defines the host-neutral tool contract and bundled packages. Package discovery is directory-based; helper packages are explicitly excluded. |
| `host/runtime/admin_api/service.py` | `kern-admin` | Serves `127.0.0.1:7443`, authenticates operator APIs, dispatches the declarative route table, and starts background maintenance. Thread, account, history, file, and host-metric domains live in focused sibling modules. |
| `host/runtime/admin_api/admin_ui/` | Browser, served by admin API | Implements the native-ES-module operator UI and its static assets. |
| `host/runtime/admin_api/errors.py` | Admin route modules | Holds the shared `ApiError` class so the `__main__` service and imported route modules map status codes consistently. |
| `host/runtime/admin_api/workspace_proxy.py` | `kern-admin` | Proxies authenticated `/v1/workspace/chat` and `/v1/workspace/web-apps` browser requests to the fixed Workspace service without forwarding the operator session cookie. |
| `host/runtime/admin_api/workspace_api.py` | `kern-admin` | Serves the peer-authenticated Workspace-service Unix socket and exposes only allowlisted direct thread routes. |
| `host/runtime/agent_runtime/orchestrator.py`, `provider_account_trust.py` | `kern-admin` | Own turn admission/live processes and runtime-status polling; account trust, provider attestation, and credential convergence are isolated from turn lifecycle. `harness.py` and `harness_registry.py` define the adapter contract. |
| `host/runtime/agent_runtime/codex_app_server.py` | Admin adapter controlling an agent child | Implements the Codex stdio JSON-RPC protocol and runtime lifecycle. |
| `host/runtime/agent_runtime/claude_code.py` | Admin adapter controlling an agent child | Implements Claude Code stream-json turns, steering, login, and status probes. |
| `host/runtime/network_proxy/service.py` | `kern-proxy` | Serves `127.0.0.1:7445`, terminates/inspects proxied traffic, applies policy before upstream connections, and records network events. |
| `host/runtime/core/network_policy.py` | Admin and proxy processes | Loads policy and provides shared path canonicalization and route matching used by integration guards. |
| `host/runtime/agent_network/` | `kern-agent-network` | Serves read-only integration status and denial guidance over the peer-authenticated agent-network socket with no egress. |
| `host/runtime/core/db.py`, `pgclient.py` | Admin, proxy, tools, agent-network, Workspace, and bootstrap clients | Implement peer-authenticated Unix-socket PostgreSQL connections, pooling, and transactions without a driver dependency. |
| `host/runtime/core/state/` | Admin, proxy, tools, and agent-network processes under their OS roles | Implements per-operation normalized storage access, organized by config, threads, accounts, events, network, and tools behind a stable package facade. Database grants remain the cross-process authority boundary. |
| `host/runtime/deploy/migrate.py` | `kern-admin` during bootstrap | Applies ordered admin-state migrations before application services start; the running admin service never migrates. |
| `host/runtime/deploy/write_config.py` | `kern-admin` during bootstrap | Chooses replacement or carried-over operator config for the lifecycle mode, encrypts secrets, stores normalized rows, and returns the effective config to root bootstrap. |
| `host/runtime/admin_api/tools_client.py` | `kern-admin` | Implements operator-facing tool listing, config, enablement, OAuth delegation, approvals, and audit routes. |
| `host/runtime/tools/service.py`, `api.py` | `kern-tools` | Own the tools socket, execute tool packages with scoped database access and direct HTTPS egress, and recover interrupted approvals. |
| `host/runtime/tools/tools_host.py` | Admin and tools processes | Implements tool discovery, manifest validation, config/credential views, single-use approvals, and tool audit events. |
| `host/runtime/agent_shim/mcp_shim.py` | `kern-agent`, spawned by each harness | Aggregates the peer-authenticated tools, network-introspection, and Workspace sockets over one stdio MCP server. |
| `host/runtime/admin_api/github_*.py` | `kern-admin`, with fixed root helpers for egress | Converges the GitHub credential, derives repository warnings, and resolves operator decisions for queued `.github` pushes. Direct-main rejection stays entirely in the proxy and creates no queue item. |

Develop and run unit CI against Python 3.11. Production uses Ubuntu 22.04's
system Python 3.10; the runtime stays standard-library-only and fresh AWS smoke
tests exercise that exact host environment.
