# Audit: Settings Clarity and Least Surprise

Finding ID prefix: `UX`. See [README.md](README.md) for the sweep process,
finding format, and severity scale.

## Audit question

Do Kern's UI, settings, confirmations, and configuration docs describe what
the system actually does, especially for security, data loss, and lifecycle
actions? A user should not be surprised by what an action does.

## Reviewed commits

Latest reviewed commit: `6151eea5abb61590684c4cf667ae6f619d705231`.

| Commit | Reviewed by |
| --- | --- |
| `6151eea5abb61590684c4cf667ae6f619d705231` | gpt-5.6-sol; Claude Opus 5 |

## Findings

| Finding | Severity | Found at | Found by | Description | Resolution |
| --- | --- | --- | --- | --- | --- |
| UX-001 | Medium | `f28b50e87b61` | Claude Opus 4.8 | Replacing network policy to disable a managed provider terminated that runtime's active work, but the confirmation mentioned only replacing policy. An operator tightening access could unexpectedly lose in-flight work. Name affected runtimes and lifecycle impact before confirmation. | Fixed — the disable-integration confirm now names the affected runtime and discloses that disabling it immediately fails in-flight work using that provider, mirroring the reset-linked-account disclosure. |
| UX-005 | Medium | `f28b50e87b61` | GPT-5.5 | The agent composer did not say at the point of use that work ran without per-command permission prompts. Tell the operator that the agent can run autonomously within the active network policy. | Fixed — the agent-composer hint now states that the agent can run autonomously within the active network policy, at the point of use. |
| UX-006 | Medium | `f28b50e87b61` | GPT-5.5 | The reboot confirmation did not disclose that active work would be failed while queued work and durable data survived. State those consequences before reboot. | Fixed — the reboot confirm now states that rebooting fails agent work in progress while queued work and durable data (threads, files, credentials, policy) survive and resume after boot. |
| UX-008 | Medium | `a634dddb3384` | gpt-5.6-sol | Agentic Web App leaves Archive enabled while an agent turn is running and performs it immediately with no confirmation or lifecycle explanation. The archived workspace disappears from the active view and becomes read-only, but the backend deliberately allows the already-started turn to finish and apply revision-checked changes, so an operator can hide an app while it continues consuming provider work and modifying state. Disable Archive while running, or confirm explicitly that it does not stop the turn and offer “Stop and archive.” | Wontfix — the Agentic Web App was rebuilt around full-screen workspaces and no longer has archiving at all, so the archive-while-running state this finding describes cannot occur. |
| UX-009 | Medium | `f196436c530e` | gpt-5.6-sol | Public passkey login can lock an operator out after all enrolled passkeys are lost, but the login/setup/status UI and the primary README never explain the supported recovery path even though `reconfigure --reset-admin-passkeys` exists. The flag appears only in the lower-level CLI API document/help, so an operator following the main guide has no discoverable recovery procedure at the point of failure. Add lost-passkey guidance to the login/status UI and document the exact reconfigure recovery flow and its preserved/replaced credentials in the README. | Fixed — the README now documents the reconfigure --reset-admin-passkeys recovery flow: it removes all enrolled admin passkeys so the admin password can sign in again, preserves the data volumes, and reinstalls the password digest. |
| UX-010 | Medium | `f196436c530e` | Claude Opus 5 | Separately from the undocumented recovery path in UX-009, passkey enrollment is one-way from the admin UI and does not say so at the point of use. The Home banner offers "Protect public login with a passkey — Add a phishing-resistant second factor. Your admin password remains unchanged" behind a single button (`admin_ui.html:160-172`), and the top-bar shield launches the same ceremony in one click. Nothing there states that once one credential is stored, `admin_auth.begin_password_login()` makes a correct admin password insufficient on the public HTTPS origin permanently. `refreshPasskeySetup()` then hides the enrollment banner as soon as `configured` is true (`admin_ui/passkeys.js:100-110`), so a backup or second device cannot be enrolled from the UI even though the backend supports multiple credentials and already builds `excludeCredentials` from the existing list; and there is no removal or list-devices route at all — `service.py` serves only `GET /v1/admin-passkeys` and the two registration routes. The operator therefore cannot see how many credentials exist, add a second one, or remove one, and learns the enrollment was irreversible only after losing the device. State before enrolling that the password alone will no longer log in on the public hostname and that the UI cannot remove or replace the credential, and offer enrolling a second device. | Wontfix — CLI reset via reconfigure --reset-admin-passkeys is the supported recovery and is documented in the README (see UX-009); passkey management UI (list/add/remove, second-device enrollment) is future work. The enrollment banner is deliberately left short rather than carrying the irreversibility caveat, to keep the setup panel uncluttered. |
| UX-011 | Medium | `83fac7566891` | Claude Opus 5 | The Network audit log tells the operator it shows "Every request the agent makes to the internet, and whether the proxy allowed it" (`admin_ui.html:378`), but only the network proxy appends rows to that table. Bundled tools do not use the proxy: nftables grants the `kern-tools` uid its own direct DNS and TCP/443 egress (`bootstrap.sh:968-970`), and the shared tool HTTP client is built with an explicitly empty proxy handler (`host/tools/shared/web.py:74`). `docs/architecture/network-controls.md:31-34` confirms this is the design and `docs/architecture/tools/outbound-request-filtering.md:269-273` records that a tools egress proxy is deliberately deferred. Every Gmail read/send, Brave search, X/LinkedIn/Instagram post, IBKR, Polymarket, and Runway request — the traffic carrying the most sensitive third-party data — is therefore absent from the log that claims completeness, and the separate Tool audit log records tool/action/outcome but no hosts, paths, or allow/deny decisions. An operator auditing "did anything leave this host" from this tab reaches a false conclusion. Scope the subtitle to proxied agent traffic and say plainly that bundled-tool egress is recorded in the Tool audit log instead. | Fixed — the audit-log subtitle is rescoped to proxied agent requests and states that bundled tools reach the internet outside this proxy, with their egress recorded in the Tool audit log. |
| UX-012 | Medium | `9dc3d4980ac6` | Claude Opus 5 | `docs/api/DeployResult.md:39` documents an `admin_password` result field for `deploy` and `reconfigure` — "Cleartext admin password, read from `--admin-password-env` when supplied or generated otherwise" — and `:77-80` adds "Only deploy and reconfigure result files contain `admin_password`; keep them private ... Lifecycle result files are created mode `0600`." None of that is true at this commit. The lifecycle result dict (`host/cli/lifecycle.py:339-357`) contains no password; the CLI accepts only `--admin-password-sha256` (`:154-163`), and `--admin-password-env` is defined by no lifecycle parser (it exists solely on the stage test harness, which injects `admin_password` into the result itself precisely because the CLI does not); and the CLI writes no result file and applies no `0600` mode, printing JSON on stdout. The module docstring states the opposite of the doc — "The CLI never handles the admin password ... neither the CLI process, its result files, nor anything on the instance ever contains the cleartext" — as does `README.md:151-160`. An operator following DeployResult.md would look for a secret that does not exist, and may wrongly believe the deploy pipeline handles cleartext credentials that need protecting. Delete the field and the `0600`/result-file paragraph, and describe the SHA-256-only flow the code implements. | Fixed — DeployResult.md no longer lists an admin_password field or 0600 result files; it now describes the SHA-256-digest-only flow (deploy/reconfigure take only --admin-password-sha256, the host stores only the hash, results print to stdout). |
| UX-013 | Medium | `4e458e0f2c41` | Claude Opus 5 | The Python and npm integration cards state their parameter-guard Protections without qualification (`admin_ui/integration_catalog.js:306,363`, with matching unqualified Technical notes at `:350,408`), while GitHub's card discloses its exemption at `:284`. The code exempts the highest-volume path in each: `python_packages/guard.py:32-35` returns `None` for `files.pythonhosted.org`, and `npm_packages/guard.py:31-34` returns `None` for any path containing `/-/` — which is every npm tarball download — with `registry.npmjs.org` carrying no path guards at all. Executing both `request_denied` functions with a secret-shaped token confirms the split: `npm plain: request_param_secret_denied`, `npm dashy: None`, `pypi plain: request_param_secret_denied`, `files: None`. An operator reading these cards concludes that enabling package installation cannot carry secrets outbound, when the download hosts that carry the bulk of the traffic are unguarded. Qualify both cards the way GitHub's already is. | Fixed — the Python and npm integration cards now disclose that the bulk download hosts are not scanned by the parameter guard (files.pythonhosted.org for PyPI; registry.npmjs.org and every /-/ tarball path for npm), mirroring GitHub's qualified wording. |
| UX-014 | Medium | `999a6d00089f` | Claude Opus 5 | Beyond the Agentic Web App archive behaviour in UX-008, archiving hides the only Stop control while the turn keeps running, and the same defect exists in Agent Chat. `setSelectedThreadArchived()` (`host/apps/agent_chat/ui/agent_chat.js:1037-1042`) archives with no confirmation and no running-state check, and the backend flips a row without asking the host to stop the thread. The UI then hides the whole composer for an archived thread (`agent_chat.js:276,292`), and the running indicator and Stop button live inside that composer, so the one control that stops the agent disappears while it continues running commands, writing files, and consuming provider quota. The backend still permits stopping an archived thread (`backend.py:141-150`, `_require_app_thread(..., include_archived=True)`), so this is a UI-only loss of a supported capability; Personal Web App Builder repeats it (`personal_web_app_builder.js:1398`, stop handler returns early when archived at `:977`). Agent Chat also shows no archive explanation at all — `$("composer-hint").hidden = readOnly` hides even the hint — so an archived thread simply loses its footer with zero copy. The running dot does remain visible in both views, so the operator can see the turn but cannot stop it without unarchiving. Block or confirm archiving a running thread/app, or keep Stop visible on an archived-but-running one. | Fixed — Agent Chat refuses to archive a thread while its turn is running, telling the operator to stop the agent first; archiving therefore can no longer hide a thread that keeps running with Stop out of reach. |
| UX-002 | Low | `f28b50e87b61` | Claude Opus 4.8 | A wildcard such as `*.example.com` excludes the apex `example.com`, while the domain control did not explain that distinction. State the apex behavior beside the field or in its help. | Fixed — a note beside the domain field states that a wildcard like *.example.com matches sub-domains only and does not include the apex example.com, which must be added as its own rule. |
| UX-003 | Low | `f28b50e87b61` | Claude Opus 4.8 | Invalid edits in the proposed-policy JSON were silently ignored until Replace, leaving other controls displaying the last valid proposal. Show an explicit parse state while the editor is invalid. | Wontfix — the described free-form proposed-policy JSON editor no longer exists at this commit; the network policy is edited entirely through structured controls (integration toggles, domain-rule inputs, checkboxes), so there is no invalid-JSON editor state to surface. |
| UX-004 | Low | `f28b50e87b61` | Claude Opus 4.8 | A fresh empty network policy denied all traffic, but the UI displayed only `{}`, which an operator could read as unrestricted. Explain the deny-all default next to the active policy. | Wontfix — the UI no longer renders the policy as a raw `{}` object; network access is shown through structured per-integration and per-domain controls, so the empty-object display this finding describes no longer exists. |
| UX-007 | Low | `f28b50e87b61` | GPT-5.5 | The “Internet Access and Tools” tab and “Add GitHub”-style controls configured network rules only, not credentials or tools, so their labels overpromised. Rename them or state that they grant network access only. | Fixed — the network panel intro states that this tab configures network access rules only, and that credentials and tool enablement are set in each integration's own controls. |
| UX-015 | Low | `1d60f6621745` | Claude Opus 5 | `docs/api/CLI.md:15` ends the `--bootstrap-from-github` row with "Pins older than `0.35.0` are rejected." That is a stale remnant of a deleted `_MIN_GITHUB_DELIVERY_VERSION` gate, and it hides the rule the code actually enforces: exact equality with the local checkout's `VERSION` (`host/cli/lifecycle.py:440-446`, re-checked host-side by `host/bootstrap/self_provision.py:35-41`). The repository is at VERSION 1.5.3, so every pin between 0.35.0 and 1.5.2 is accepted by the documented rule and rejected by the code. `README.md:403` describes the same flag and also never states the equality requirement, saying only that the pinned commit's VERSION "is the operation's target", so neither document tells an operator why their pin will fail. Replace the stale sentence with the equality rule and repeat it in the README row. | Fixed — CLI.md and the README --bootstrap-from-github row now state that the pinned commit's VERSION must exactly equal the local checkout's VERSION, replacing the stale 0.35.0-floor sentence. |

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
   create/select/rename, chat, attachments, generated code execution, data
   persistence/revisions, preview state, empty apps, and worker failure.
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

### `6151eea5abb61590684c4cf667ae6f619d705231`

Reviewed by: gpt-5.6-sol; Claude Opus 5

Methodology: repository-level walkthrough of the current operator UI, both
stable apps, lifecycle CLI help, README/API/architecture documentation, and
the backend behavior behind consequential controls. Copy and status were
compared with actual persistence, runtime, network, credential, and archive
paths. Focused UI/app tests and all six lifecycle `--help` outputs were run.
A second pass worked in the opposite direction for the documentation surfaces
— reading each API/CLI document as the source of truth and then looking for
the code that implements it, which is what surfaced two documents describing
behaviour that does not exist (UX-012, UX-015) — and executed the guards
behind operator-facing "Protections" copy rather than reading them, which
produced the npm/PyPI exemption evidence in UX-013. No real-user study, live
provider account, mobile device, or degraded-host walkthrough was performed.

#### What was reviewed

- The parent Admin UI shell and `health.js`, `network.js`, `tools.js`,
  `passkeys.js`, `threads.js`, `files.js`, `logs.js`, and
  `connection_guide.js`: first login, passkey setup/status, runtime/account
  states, provider and custom-domain controls, bundled tools and approvals,
  operational status, file/log views, reboot, and GitHub credential/push
  controls.
- `integration_catalog.js`, tool manifests, network controls and guards, and
  provider/runtime orchestration: enabled/connected/deactivated/error
  vocabulary, exact network and data disclosures, policy replacement effects,
  approval versus direct execution, and credential reset behavior.
- Agent Chat and Agentic Web App end to end: first-run copy, runtime/model/
  effort selection, drafts and history paging, activities, attachments,
  steer/stop, archive/rename, generated code/data/worker behavior, and
  failure/empty states.
- `README.md`, `docs/api/CLI.md`, the Admin/API/app architecture documents,
  `host/cli/lifecycle.py`, `host/config.py`, lifecycle checks, and generated
  help for deploy, upgrade, recover, reconfigure, start, and stop.

#### Coverage and confidence

- Checklist 1–2: the static operator surfaces and access setup were inventoried
  and traced. Password/passkey factor behavior is accurately described where
  shown, but lost-passkey recovery is not discoverable in the primary guide or
  login/status UI (UX-009). Auditing the enrollment flow as a lifecycle rather
  than as copy adds a second gap: enrollment is irreversible from the UI and
  says so nowhere before the click, the banner disappears once configured so no
  second device can be added, and no removal or list-devices route exists at
  all (UX-010). Together these are the checklist's "security consequences and
  recovery must be visible before lockout" requirement, and neither half is met.
- Checklist 3: every managed integration and custom-domain control was
  compared with the typed policy. UX-001 and UX-002 still apply. The old raw
  JSON proposal/active-policy surfaces behind UX-003 and UX-004 no longer
  exist, and the current tab now genuinely includes credentials and bundled
  tools; finding resolutions remain owner-controlled and were not changed by
  this audit. Executing the guards rather than reading their descriptions
  showed the Python/npm cards' unqualified "Protections" copy does not match
  the code, which exempts each ecosystem's highest-volume download host
  (UX-013) — GitHub's card already discloses its own exemption, so the fix
  shape exists in the same file.
- Checklist 4: tool discovery, configuration, OAuth, four-card disclosure,
  direct/approval modes, exact approval payload, and terminal statuses were
  checked in the UI and manifests. No new mismatch was found.
- Checklist 5–6: runtime/thread vocabulary and both stable app workflows were
  traced through their APIs. Archive behavior for an already-running Agentic
  Web App is the new least-surprise gap in UX-008. The same pattern is present
  in Agent Chat, and in both apps archiving hides the composer that contains
  the only Stop control while the backend still permits stopping — UX-014
  covers that additional surface and impact.
- Checklist 7–9: health, filesystem, errors/logs, process/file views, account
  reset, push approval, reboot, and lifecycle/status vocabulary were checked.
  UX-006 still applies to reboot; loading and error states otherwise identify
  the safe next action consistently in the reviewed paths. Checking the audit
  views against what actually writes to them found one completeness claim that
  does not hold: the Network audit log promises every internet request the
  agent makes, but only proxied traffic is recorded and all bundled-tool egress
  bypasses the proxy by design (UX-011).
- Checklist 10: all lifecycle help was generated and compared with the README,
  `docs/api/CLI.md`, parser, and config validation. Accepted arguments and
  defaults match, apart from the README's omitted passkey-reset recovery
  procedure captured in UX-009. Reading the documents as the specification and
  then looking for the implementation found two further mismatches that a
  help-versus-parser comparison does not surface, because the drift is in
  documents the parser never contradicts: `docs/api/DeployResult.md` describes
  an `admin_password` result field, an `--admin-password-env` flag, and
  `0600` result files, none of which exist (UX-012); and `docs/api/CLI.md`
  states a `--bootstrap-from-github` floor of `0.35.0` where the code enforces
  exact `VERSION` equality (UX-015). The README repeats neither rule, so the
  equality gate is documented nowhere.
- Checklist 11: the README explains autonomous execution, while Agent Chat and
  Agentic Web App still do not repeat that posture before first use, so UX-005
  remains applicable.
- Checklist 12: responsive/keyboard/multi-tab code paths and existing smoke
  assertions were read, but this sweep did not run an interactive desktop/
  mobile, slow-network, refresh, or multi-tab usability session. Confidence is
  therefore high for source/document mismatches and lower for discoverability
  judgments that require observed users.
