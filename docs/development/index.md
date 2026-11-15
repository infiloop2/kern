# Development

This folder contains contributor and maintainer workflows for Kern, plus two
feature references (Browser service and English dictation). For runtime design
and trust boundaries, start with [Architecture](../architecture/index.md).

## Sections

| Doc | Contents |
| --- | --- |
| [Unit test runner](../../tests/scripts/test) | `tests/scripts/test` runs the `unittest` modules you name, for example `tests/scripts/test test_deploy`; with no arguments it discovers the whole suite. It needs Python 3.11+ with Playwright installed; set `KERN_TEST_PYTHON` to choose the interpreter. |
| [Kern Cloud CLI contract](kern-cloud-contract.md) | Interfaces consumed by managed hosting. |
| [Browser service](browser-sessions.md) | Connected accounts, bounded browser actions and approval receipts. |
| [Code layout](layout.md) | Repository layout and important runtime modules. |
| [English dictation](dictation.md) | Recording, recovery, host transcription, and validation. |
| [Fresh Lima smoke](fresh-lima-smoke.md) | Real local VM deployment, lifecycle, and shared live-host checks. |
| [Fresh AWS smoke](fresh-aws-smoke.md) | Full deploy-from-scratch live AWS validation and one-time setup. |
| [Persistent AWS stage](persistent-aws-stage.md) | Long-lived staging host, provider-login checks, and start/stop/recovery workflows. |

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
