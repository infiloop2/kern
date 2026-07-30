# Audit: Settings Clarity and Least Surprise

Finding ID prefix: `UX`. See [README.md](README.md) for the sweep process,
finding format, and severity scale.

## Audit question

Do Kern's UI, settings, confirmations, and configuration docs describe what
the system actually does, especially for security, data loss, and lifecycle
actions? A user should not be surprised by what an action does.

## Reviewed commits

Latest reviewed commit: `f28b50e87b61`.

| Commit | Reviewed by |
| --- | --- |
| `f28b50e87b61` | Claude Opus 4.8; GPT-5.5 |

## Findings

| Finding | Severity | Found at | Found by | Description | Resolution |
| --- | --- | --- | --- | --- | --- |
| UX-001 | Medium | `f28b50e87b61` | Claude Opus 4.8 | Replacing network policy to disable a managed provider terminated that runtime's active work, but the confirmation mentioned only replacing policy. An operator tightening access could unexpectedly lose in-flight work. Name affected runtimes and lifecycle impact before confirmation. | Open |
| UX-002 | Low | `f28b50e87b61` | Claude Opus 4.8 | A wildcard such as `*.example.com` excludes the apex `example.com`, while the domain control did not explain that distinction. State the apex behavior beside the field or in its help. | Open |
| UX-003 | Low | `f28b50e87b61` | Claude Opus 4.8 | Invalid edits in the proposed-policy JSON were silently ignored until Replace, leaving other controls displaying the last valid proposal. Show an explicit parse state while the editor is invalid. | Open |
| UX-004 | Low | `f28b50e87b61` | Claude Opus 4.8 | A fresh empty network policy denied all traffic, but the UI displayed only `{}`, which an operator could read as unrestricted. Explain the deny-all default next to the active policy. | Open |
| UX-005 | Medium | `f28b50e87b61` | GPT-5.5 | The agent composer did not say at the point of use that work ran without per-command permission prompts. Tell the operator that the agent can execute commands autonomously within the active network policy. | Open |
| UX-006 | Medium | `f28b50e87b61` | GPT-5.5 | The reboot confirmation did not disclose that active work would be failed while queued work and durable data survived. State those consequences before reboot. | Open |
| UX-007 | Low | `f28b50e87b61` | GPT-5.5 | The “Internet Access and Tools” tab and “Add GitHub”-style controls configured network rules only, not credentials or tools, so their labels overpromised. Rename them or state that they grant network access only. | Open |

## Threat model

This axis has no adversary; the failure mode is honest miscommunication. The
"asset" is the user's accurate mental model: a user who approves a
setting should be able to predict its effect. Judge against a user who reads
the UI and the README but not the source code.

- **Out of scope:** visual design and polish; whether a correctly-described
  behavior is a good idea; missing features, unless their absence makes an
  existing control misleading.

Severity mapping for this axis: **High (maximum)** — an extremely clear
miscommunication can directly cause a severe security or data-loss
consequence. **Medium** — a plausible misreading causes rework or wrong
operational decisions. **Low** — confusing but self-correcting. Critical does
not apply to this axis.

## Minimal scope checklist

This checklist is not comprehensive: it names known-important areas, but the
audit question and threat model define the scope. Account for each item in
your coverage section, and report anything else within scope even if no item
below names it.

1. Inventory every operator surface: parent Admin UI modules and dialogs,
   Agent Chat, Agentic Web App, integration guides, connection guide, CLI help
   and results, README, API/architecture docs, deploy verification output, and
   errors. For every visible control/status, write the reasonable expectation,
   trace the actual code path and persistence, and record any mismatch.
2. Audit first-run and access setup: generated admin password, SSH and
   Cloudflare endpoint choices, public hostname, login errors, password plus
   passkey flow, enrollment/removal, device naming, lost-passkey recovery,
   `--reset-admin-passkeys`, and why passkeys are unavailable over SSH.
   Security consequences and recovery must be visible before lockout.
3. Audit network controls integration by integration: enabled/disabled state,
   exact domains/routes/methods, cached versus live web access, custom wildcard
   apex and precedence, parameter-data guard, Python/npm restrictions,
   OpenAI/Claude account binding, Bedrock region/models/credentials/rates, and
   GitHub read/write repositories, credential mode, repo audit, `.github`
   approval queue, and denial guidance.
4. Audit tools from discovery through completion: enable/disable, required
   config, OAuth connect/reconnect/disconnect, local setup images and callback
   URLs, action/data policy and four-card disclosure, direct versus
   operator-approved behavior, payload shown for approval, staged files,
   pending/expired/denied/executed/failed status, and what the agent sees.
5. Audit runtime and thread semantics for Codex, Claude Code, and Hermes:
   availability/login/error/deactivated states, model and reasoning choices,
   account/usage/reset displays, thread creation and configuration, starting/
   running/finishing/idle behavior, message steering, stop, retry, session
   handoff/history replay, activities, attachments, event paging, timestamps,
   archive/rename, busy responses, and provider failures.
6. Audit every stable app workflow. Agent Chat must explain local drafts,
   cached histories, older-message loading, runtime selection, activities,
   attachments, stop/steer, archive, and errors. Agentic Web App must explain
   create/select/rename/archive, chat, attachments, generated code execution,
   data persistence/revisions, preview state, empty apps, worker failure, and
   archived read-only behavior.
7. Audit operational views/actions: home health and filesystem metrics,
   upgrade notice, runtime usage hover/mobile behavior, host errors, agent and
   network audit logs, process list, file viewer/media behavior, operator
   connection guide, provider/tool settings, reboot, account reset, and GitHub
   push approval. Empty, loading, stale, degraded, partial, and failed states
   must say what happened and what action is safe.
8. Audit lifecycle commands and confirmations—deploy, upgrade, recover,
   reconfigure, start, stop, in-app reboot, provider/network disable, account
   reset, tool disconnect, app archive, and passkey deletion. State exactly
   what happens to the instance, root/admin/agent volumes, database/schema,
   sessions, passkeys, credentials, threads/running work, apps, approvals,
   generated files, operator endpoints, and version.
9. Compare every status/error vocabulary and transition across API, browser,
   apps, CLI, logs, and docs. Labels must distinguish pending versus failed,
   starting versus running, finishing versus idle, disabled versus
   unconfigured, stale versus current, retryable versus terminal, and
   preserved versus deleted without exposing internal machinery unnecessarily.
10. Compare CLI arguments and `host/config.py` parsing/defaults with README and
    API docs: allowed agent names/regions, operator endpoints, environment
    credential names, GitHub bootstrap pin/version confirmation, command
    preconditions, version gates, `recover --allow-upgrade`, reconfigure
    replacement semantics, and output fields. Reject unknown or obsolete
    examples instead of silently accepting them.
11. Verify the autonomy posture is visible before first use: agents run host
    commands without per-command prompts, while only tool actions explicitly
    marked for operator approval pause for approval. Repeat this distinction
    wherever tool approval could be confused with ordinary agent execution.
12. Test desktop, mobile, keyboard, slow-network, refresh/restart, and
    multi-tab behavior with realistic long names/messages/errors and every
    boundary state. Visual polish is out of scope, but hidden, unreachable,
    click-only, hover-only, or contradictory controls that change meaning are
    findings.

## Collaborative review

### `f28b50e87b61`

Reviewed by: Claude Opus 4.8 (claude-opus-4-8); GPT-5.5 (gpt-5.5)

Methodology: read each admin-UI control and compared the behavior a user would
predict from the UI copy against what the code actually does
(`admin_ui.js`/`admin_ui.html` vs `admin_api.py`, `orchestrator.py`,
`network_policy.py`, `config.py`). No usability testing with real operators.

#### What was reviewed

The network policy builder (presets, manual domain form, JSON proposal editor,
Replace flow), the runtime login/deactivation guidance, the reboot and
task steer/cancel/kill confirmations, health/status wording, and the
fresh-deploy empty-policy default. The sweep also compared README, example
config, Admin API, and architecture copy with config validation and lifecycle
behavior.

#### Coverage and confidence

- Checklist 1 (walk every control): covered for the network, home/runtime, and
  agent tabs. The four findings are the deltas I found between presented and
  actual behavior; the preset-info popovers (`PRESET_INFO`) do accurately list
  what enabling each provider/preset expands to, which is good.
- Checklist 2 (policy semantics): wildcard apex exclusion (UX-002) and the
  managed-provider expansion were checked against `domain_matches` and
  `expand_network_controls`; manual rules cannot shadow managed provider
  domains (config rejects them), which is communicated by the error path.
- Checklist 3 (lifecycle preserve/destroy): UX-001 is the significant gap. Reboot
  is confirmed and `initialize_state` fails running tasks on restart, but the
  reboot dialog likewise does not mention that running tasks will be failed —
  I folded this into UX-001's theme rather than filing it separately; worth
  stating in the reboot confirm too.
- Checklist 4 (status vocabulary): `awaiting_login`/`error`/`deactivated`
  wording plus the runtime-guidance messages read accurately.
- Checklist 5 (deploy-time config): compared `example_config.json`/README to
  `config.py` validation at a glance; nothing surprising, but I did not do a
  field-by-field README-vs-validation diff — a dedicated pass there would
  strengthen coverage.
- Not done: no real-user walkthrough; severities reflect reviewer judgment of
  how likely a user is to be surprised, not observed confusion.
