# Architecture

Kern runs Codex, Claude Code, Grok, and Hermes runtimes on an AWS EC2 instance
or local Lima VM behind fail-closed network controls. The architecture docs are
split by responsibility so operators and contributors can jump to the trust
boundary they need.

New to the codebase? Read the [diagram](diagram.md),
[control planes](control-planes.md),
[privilege boundaries](privilege-boundaries.md), and
[network controls](network-controls.md) in that order.

## Host and deployment

| Doc | Contents |
| --- | --- |
| [Architecture diagram](diagram.md) | One-page host capability map covering operator access, service users, storage, and egress boundaries. |
| [Control planes](control-planes.md) | Operator-plane and admin-plane responsibilities and authority. |
| [Deployment and upgrades](deployment.md) | EC2 provisioning, upgrade/recovery behavior, drive lifecycle, and secret handling. |
| [Host provider and local Lima design](host-provider-design.md) | The infrastructure provider boundary (AWS or Lima), lifecycle state machine, and the implemented local Lima provider that runs the same Kern guest runtime on the operator's machine ([operator setup](../../README.md#quick-start-run-kern-on-your-computer)). |
| [IAM policy notes](iam-policy.md) | Why each deploy IAM statement exists and why its scope is constrained. |

## Service and data boundaries

| Doc | Contents |
| --- | --- |
| [Privilege boundaries](privilege-boundaries.md) | Linux users, fixed sudo helpers, and root-owned helper pattern. |
| [Filesystem layout](filesystem.md) | Trusted root paths, durable volumes, and per-service ownership. |
| [Services and runtimes](services-and-runtimes.md) | systemd units, process inventory, and interactive/script runtime lifecycles. |
| [Local sockets](local-sockets.md) | Peer-credentialed Unix-domain sockets (tools, Workspace agent/admin, network introspection, Postgres) and their trust boundaries. |
| [Admin state storage and migrations](admin-state-storage.md) | The local Postgres database: schema, access control, and schema migrations. |
| [Database retention](database-retention.md) | Per-table quotas, retention, and explicitly unpruned state. |
| [Host diagnostics](host-diagnostics.md) | Best-effort structured service errors and contained warnings, PostgreSQL retention, and the read-only operator panel. |
| [Host AI inference](host-ai-inference.md) | Dedicated remote-inference service, fixed provider calls, credentials, and failure behavior. |
| [Token analytics](token-analytics.md) | Runtime usage measurements, seven-day reports, and lifetime totals. |

## Operator and agent interfaces

| Doc | Contents |
| --- | --- |
| [Admin authentication](admin-api-authentication.md) | SSH/public HTTPS classification, sessions, passkeys, and route exposure. |
| [Admin API architecture](admin-api.md) | Local API security, turn orchestration, and maintenance. |
| [Agent provider lifecycle](agent-provider-lifecycle.md) | AI account and runtime status lifecycle, refresh triggers, live credential validation, account anchoring, proxy pinning, and operator recovery. |
| [Runtime harness dependencies](harness-dependencies.md) | Codex, Claude Code, Grok, and Hermes interfaces, auth files, request shapes, and upgrade review points. |
| [Chat and Web Apps workspaces](workspaces/workspaces.md) | The fixed Workspace service, UI mounting, schemas, migration, and generated-code sandbox. |
| [Chat workspace](workspaces/agent-chat.md) | Thread index, event views, composer, and archive behavior. |
| [Kern messages and notices](agent-messages.md) | Central message catalog, provenance, and visible Kern action outcomes. |
| [Memory recall](memory-recall.md) | Bounded recent-user context, PostgreSQL English hybrid retrieval, and optional Luna/Jev ranking. |
| [Web Apps workspace](workspaces/personal-web-app-builder.md) | Isolated agent-generated workspaces and preview capabilities. |
| [Swarm map](swarm-map.md) | How the admin UI's Swarm map places and draws active agents. |
| [English dictation](../development/dictation.md) | Microphone capture, recovery, and local host transcription for Chat and App Chat. |
| [Workspace agent API](workspaces/workspace-agent-api.md) | Peer-authenticated agent calls through the main Workspace service; Apps, memory, schedules, history, identity, and peer messaging. |

## Network and integrations

| Doc | Contents |
| --- | --- |
| [Network controls](network-controls.md) | nftables, typed integration guards (AI providers, GitHub, packages, custom domains), agent introspection, and fail-closed behavior. |
| [The xAI integration](xai-integration.md) | Everything about Grok Build access: hosts opened and deliberately closed, bearer-token account pinning, the X-search/media hosted-tool allowlist, why web search is not offered, stored state, admin UI, and ACP runtime. |
| [GitHub write-path controls](github-write-path-controls.md) | The default-on direct-main push block plus `.github` inspection, quarantine, approval, replay, and failure model. |
| [Tools](tools/README.md) | Bundled tool framework: the host-neutral tool contract, this host's integration, approvals, and the bundled tool packages. |
| [Browser service](../development/browser-sessions.md) | Connected browser accounts, anonymous screenshots, coded browser actions, and their approval flow. |
| [Agent preview ports](agent-preview-ports.md) | The loopback port range the agent may serve HTTP on and test against, and the operator's SSH-forward path to view it. |

## Overview

Each thread chooses its runtime harness. The host is
long-lived in normal operation. On AWS, the instance and root EBS volume carry
the `kern-host-agent-name=<agent_name>` tag for lifecycle discovery. Upgrade and
reconfigure retain the instance and replace its root volume; recovery replaces
the instance. Lima upgrade, reconfigure and recovery replace the VM while
preserving its durable data disks.

Kern's Python control plane uses the standard library. Local embedding and
speech inference run in isolated services with pinned dependencies. Admin,
network, workspace, and tool state live in a local Postgres database on the durable
admin volume, spoken to by an in-repo wire-protocol client
(`host/runtime/core/pgclient.py`). The proxy keeps only file-oriented TLS and Git
quarantine state in its own durable directory.
