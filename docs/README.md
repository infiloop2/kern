# Repository documentation

For installation and everyday operation, start with the [root README](../README.md)
or [Kern documentation site](https://docs.kernai.cloud). This directory explains
the contracts and implementation shipped by this repository.

| Task | Start here |
| --- | --- |
| Deploy, upgrade, recover, or reconfigure a host | [Lifecycle CLI](api/CLI.md) and [result JSON](api/DeployResult.md) |
| Call the operator API or configure network policy | [API reference](api/index.md) |
| Understand service ownership, storage, or trust boundaries | [Architecture](architecture/index.md) |
| Change code and validate a pull request | [Development](development/index.md) |
| Implement or change a bundled integration | [Tool contract](architecture/tools/tool-contract.md) and [host integration](architecture/tools/host-integration.md) |
| Inspect a previous security or reliability review | [Commit-scoped audit records](audit-reports/README.md) |
| Understand the project's design goals | [Philosophy](../PHILOSOPHY.md) |

## Agent instructions shipped with the host

The bootstrap installs [host instructions](../host/bootstrap/agent-home/agents_claude.md)
and small references for [Web Apps](../host/bootstrap/agent-home/references/web-apps.md),
[generated UI](../host/bootstrap/agent-home/references/web-app-ui.md),
[memory](../host/bootstrap/agent-home/references/memory.md),
[schedules](../host/bootstrap/agent-home/references/schedules.md), and
[agent messaging](../host/bootstrap/agent-home/references/agent-messaging.md).
These describe the agent-facing contract; the operator API is a separate surface.

## Keeping documentation current

Describe implemented behavior in API and architecture references. Mark design
goals, future work, and version-specific observations explicitly. Audit findings
retain their original commit context and are not current operating instructions.

Keep each contract in one primary place and link to it from overviews. Update
service/socket inventories when adding a service, storage/retention inventories
when adding a table, and API examples when a route or schema changes. Check
commands and source paths against the same checkout. Provider setup guides live
in the integration's release-owned guide content under `host/tools/` or
`host/network_integrations/`; link to upstream provider instructions where needed
instead of copying their changing dashboards into another reference.
