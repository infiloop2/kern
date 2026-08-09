# Architecture

Kern runs Codex, Claude Code, and Hermes runtimes on an AWS EC2 instance behind
fail-closed network controls. The architecture docs are split by responsibility
so operators and contributors can jump to the trust boundary they need.

## Sections

| Doc | Contents |
| --- | --- |
| [Architecture diagram](diagram.md) | One-page host capability map covering operator access, service users, storage, and egress boundaries. |
| [Deployment and upgrades](deployment.md) | EC2 provisioning, upgrade/recovery behavior, drive lifecycle, and secret handling. |
| [Admin state storage and migrations](admin-state-storage.md) | The local Postgres database: schema, access control, and schema migrations. |
| [Host error diagnostics](host-errors.md) | Best-effort structured unexpected-service failures, PostgreSQL retention, and the read-only operator panel. |
| [Control planes](control-planes.md) | Operator-plane and admin-plane responsibilities and authority. |
| [Privilege boundaries](privilege-boundaries.md) | Linux users, fixed sudo helpers, and root-owned helper pattern. |
| [Filesystem layout](filesystem.md) | Trusted root paths, durable volumes, and per-service ownership. |
| [Services and runtimes](services-and-runtimes.md) | systemd units, process inventory, threads, Codex, and Claude runtime model. |
| [Agent provider lifecycle](agent-provider-lifecycle.md) | Runtime status lifecycle, refresh triggers, live credential validation, account anchoring, proxy pinning, and operator recovery. |
| [Runtime harness dependencies](harness-dependencies.md) | Codex and Claude Code interfaces, auth files, request shapes, and upgrade review points. |
| [Admin API architecture](admin-api.md) | Local API security, turn orchestration, and maintenance. |
| [Chat and Web Apps workspaces](workspaces/workspaces.md) | The fixed Workspace service, UI mounting, schemas, migration, and generated-code sandbox. |
| [Chat workspace](workspaces/agent-chat.md) | Thread index, event views, composer, and archive behavior. |
| [Web Apps workspace](workspaces/personal-web-app-builder.md) | Isolated agent-generated workspaces and preview capabilities. |
| [Workspace agent API](workspaces/workspace-agent-api.md) | Peer-authenticated agent calls through the main Workspace service; Web Apps are its current agent-callable resource. |
| [Network controls](network-controls.md) | nftables, typed integration guards (AI providers, GitHub, packages, custom domains), agent introspection, and fail-closed behavior. |
| [GitHub write-path controls](github-write-path-controls.md) | The implemented `.github` push-inspection, quarantine, approval, replay, and failure model. |
| [Tools](tools/README.md) | Bundled tool framework: the host-neutral tool contract, this host's integration, approvals, and the bundled tool packages. |
| [Local sockets](local-sockets.md) | Peer-credentialed Unix-domain sockets (tools, Workspace agent/admin, network introspection, Postgres) and their trust boundaries. |
| [Agent preview ports](agent-preview-ports.md) | The loopback port range the agent may serve HTTP on and test against, and the operator's SSH-forward path to view it. |
| [IAM policy notes](iam-policy.md) | Why each deploy IAM statement exists and why its scope is constrained. |

## Overview

Kern runs Codex, Claude Code, and Hermes runtimes on an AWS EC2 instance behind
fail-closed network controls. Each thread chooses its runtime harness, such as
Codex or Claude Code. The host is long-lived in normal operation; the EC2
instance and its root EBS volume carry the
`kern-host-agent-name=<agent_name>` tag so that deploy can find,
terminate, and recreate them when the operator upgrades or recovers the host.

Kern's Python runtime uses only the Python 3 standard library. Admin,
network, workspace, and tool state live in a local Postgres database on the durable
admin volume, spoken to by an in-repo wire-protocol client
(`host/runtime/core/pgclient.py`). The proxy keeps only file-oriented TLS and Git
quarantine state in its own durable directory.
