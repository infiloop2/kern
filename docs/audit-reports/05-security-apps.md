# Audit: Installed Apps and Agent-Authored Content

Finding ID prefix: `APP`. See [README.md](README.md) for the sweep process,
finding format, and severity scale.

## Audit question

Can malicious or buggy app code exceed its assigned authority, or can
agent-controlled data handled by an app execute in the browser or trick the
operator?

## Reviewed commits

Latest reviewed commit: `6151eea5abb61590684c4cf667ae6f619d705231`.

| Commit | Reviewed by |
| --- | --- |
| `6151eea5abb61590684c4cf667ae6f619d705231` | gpt-5.6-sol; Claude Opus 5 |

## Findings

| Finding | Severity | Found at | Found by | Description | Resolution |
| --- | --- | --- | --- | --- | --- |
| APP-001 | Medium | `dcaa9c162717` | gpt-5.6-sol | An installed app frame can send `kern-app-copy-text` after any interaction it solicits, and the parent writes up to 2 MiB of app-selected text through `navigator.clipboard.writeText` or a hidden `execCommand("copy")` fallback without a host-owned preview or confirmation. A malicious app can therefore show benign text while placing a command, URL, or other attacker-selected value on the operator's clipboard; harm occurs when the operator later pastes it. Require a top-level, explicit user gesture plus host-owned confirmation that displays the exact text, or remove clipboard authority from the bridge. | Wontfix — a confirmation on every copy would add significant UX friction to a routine action, and previewing the text does not actually close the gap (invisible/bidirectional characters mean displayed text need not match copied bytes). What an operator does with copied content is treated as the operator's responsibility, consistent with an ordinary clipboard. |
| APP-003 | Medium | `309396bd8b3e` | Claude Opus 5 | The Agent Chat frame installs its `message` listener dispatching on `message.type` and `message.request_id` alone (`host/apps/agent_chat/ui/agent_chat.js:144`); it never checks `event.source` or `event.origin`. The sibling Agentic Web App frame performs exactly that check (`personal_web_app_builder.js:110`, `event.source !== parent`), so this is an asymmetry rather than a design choice, and no test covers it. Because `parent`, `parent.length`, and `parent[i]` are cross-origin-accessible Window properties even from a sandboxed opaque-origin iframe, and `postMessage` is cross-origin callable, another loaded app frame can iterate `parent.length` and call `parent[i].postMessage({type:"kern-app-api-result", request_id:"7", ok:true, body:{...}}, "*")` to have Agent Chat accept a forged host-mediated bridge reply. Request ids are `String(nextRequestId++)` — "1", "2", "3" — so nothing needs guessing. Exploitation is a race rather than a certainty: a `pending` entry exists only for one round trip, and the victim's genuine reply must traverse parent → admin API → app backend → PostgreSQL while the attacker runs in-process, which is winnable but not guaranteed per attempt, and only frames opened during the current page load are addressable. Add the same `event.source !== parent` guard to `agent_chat.js`, and state the frame-side source check as a required element of the bridge contract in `docs/architecture/apps/apps.md`, which today documents only the parent-side binding. | Fixed — the Agent Chat bridge listener returns early unless event.source === parent (mirroring the sibling frame), so another loaded frame can no longer post a forged host-mediated reply; apps.md states the frame-side source check as a required bridge-contract element. |
| APP-002 | Low | `de1c2dad0c40` | gpt-5.6-sol | Any installed app frame can send `kern-app-open-file` with an absolute agent-workspace path, causing the trusted parent shell to switch to Files and fetch/display that path immediately. The frame does not receive the bytes and path confinement still holds, but a malicious app can drive parent navigation and expose an operator-visible file without mediation. Require a host-owned confirmation/user gesture for the exact path, or replace the message with a passive request the operator must accept. | Wontfix — a confirmation prompt on every open-file request would degrade the UX of a routine navigation. The frame never receives the bytes and path confinement still holds, so the residual risk is that an app can steer the operator's own view to a file they are already authorized to see. |
| APP-004 | Info | `18543d966c0c` | Claude Opus 5 | `APP_BACKEND_ALLOWED_ADMIN_ROUTES` (`host/runtime/admin_api/app_backend_api.py:37-45`) allowlists `("GET", "/v1/network/policy")`, which returns the operator's complete stored `network_controls` object — enabled integrations, the custom allowed-domain list, the GitHub repository allowlist and approval settings — with no thread scoping and no app-scoped response filtering. The grant itself is intentional and pinned by `tests/test_admin_api.py:654-667`, and the response carries no credential, so this is documentation drift rather than a capability defect: `docs/architecture/apps/apps.md:279-282` states "The allowlist does not allow broad host routes such as network policy, files, process inventory, runtime auth, app registry, or the host-wide agent event log", and `:477-481` states "The current boundary exposes no file, process, runtime-credential, network-policy, or cross-app grant routes to app backends", while `:275-276` in the same document does mention network-policy reads. A reader auditing the app boundary from either of the first two sentences would conclude a route is absent that is in fact allowlisted and tested. Reconcile the three statements — and, since neither shipped app backend calls the route, consider dropping it and `("GET", "/v1/tools")` from the allowlist until an app needs them. | Fixed — apps.md is reconciled: all three statements now consistently state that a read-only GET /v1/network/policy read is exposed to app backends (returning stored network_controls, no credential) while policy changes remain disallowed. |

## Threat model

- **Adversaries:** (a) deliberately malicious installed app backend/UI code;
  (b) an ordinary app bug; (c) a prompt-injected agent controlling messages,
  activity payloads, file names, file contents, generated HTML/data, paths,
  and app API inputs; (d) a compromised app backend process; and (e) a
  malicious page or frame sending crafted `postMessage` traffic.
- **Assets:** the operator's admin session and local files, admin API
  capabilities, agent runtime credentials and workspace data, other apps'
  schemas and backends, host/proxy/tool state, the integrity of what the
  operator sees, and the guarantee that agent-authored bytes remain data.
- **Out of scope:** browser or kernel zero-days; the agent's ordinary
  authority over its own workspace when no app or browser boundary is
  involved; parent Admin UI surfaces outside an installed app (axis 03 owns
  those).

## Minimal scope checklist

This checklist is not comprehensive. The audit question and threat model are
binding; report any in-scope defect even if no item names it.

1. Load every active and deprecated manifest through the real validator.
   Check ids, stable host slots, generated users/roles/schemas/services/ports,
   referenced paths, symlinks/traversal, exact fields, release-stage rules,
   agent/API and capability-worker declarations, collisions, and deterministic
   behavior across deploy/upgrade.
2. Trace provisioning for every app identity: Unix ownership and home,
   database role/schema/search path and grants, migration runner/versioning,
   loopback port, nftables callers, systemd environment/working directory,
   `kern_app.slice`, filesystem access, restart behavior, and cleanup. Prove
   one app cannot read/call another app or a host service directly.
3. Audit browser/Admin-to-app traffic end to end: installed-app lookup,
   UI-asset path confinement and MIME/cache/CSP headers, iframe creation,
   backend method/path/body/response/time limits, header stripping, host-marker
   provenance, disconnect/errors, and denial of direct app-port access.
4. Audit the app-backend-to-admin Unix socket as a distinct principal:
   `SO_PEERCRED` uid-to-app mapping, claimed app-id match, fixed route
   allowlist, reserved thread-prefix insertion, request/response filtering,
   app ownership checks before shared dispatch, body/time limits, malformed
   HTTP, and inability to reach login, passkeys, host admin, another app, or
   removed compatibility routes.
5. Audit the agent-to-app path independently: tools-shim `app_api`, agent-app
   socket peer checks, pid/cgroup-to-live-thread attribution, app-prefixed
   thread parsing, manifest opt-in, app ownership, proxy-header provenance,
   backend route grammar, body/response/time/concurrency bounds, process-exit
   races, and attempts from an unscoped or different app thread.
6. Enumerate every iframe sandbox token, app CSP directive and asset source,
   origin property, script/style/image/font/worker/connect allowance, MIME and
   cache rule, direct asset URL, popup/form/download/navigation capability,
   and parent-page API. Prove an app cannot acquire the Admin origin, cookies,
   credentials, DOM, or unrestricted browser/network authority.
7. Enumerate the complete parent bridge protocol: exact source window and app
   binding, request-id lifecycle, duplicate/stale/replayed replies,
   concurrency/timeouts, field and size validation, API path confinement,
   response filtering, copy behavior, and host-owned file picker/upload
   mediation. No bridge message may become arbitrary Admin API, file read,
   URL open, navigation, HTML render, or cross-app authority.
8. Grep every app UI and shared rich-text dependency for markup, attribute,
   CSS, URL, worker, object-URL, navigation, popup, download, clipboard,
   `postMessage`, and fetch sinks. Trace messages, activity, attachments,
   backend/provider errors, filenames/bytes, generated state, and restored
   history. Agent-authored data must not execute, auto-fetch/open a link,
   spoof trusted controls, or escape the frame.
9. Audit every app path into host file viewing/open/upload: picker ownership,
   path normalization, symlink/TOCTOU and regular-file checks, type/size/count
   bounds, media sniffing and `nosniff`, streaming/partial cleanup, blob URL
   lifecycle, filename rendering, atomic publish, and whether app or agent
   bytes can select/disclose operator-local or another-thread files.
10. Verify database and thread isolation under malformed ids, reserved
    prefixes, archive/rename/create races, pagination cursors, transactions,
    optimistic revisions, concurrent browser/agent writes, migration rollback,
    and oversized stored values. Every app response must expose only its
    unprefixed ids and own rows.
11. Audit Agent Chat specifically: runtime/session selection, create/list/
    archive/rename, per-thread draft/history cache, newest/older event paging,
    event ordering and byte caps, rich text, attachments, starting/finishing
    retries, steering, stop, failure/reload, and thread mapping across all
    harnesses.
12. Audit Agentic Web App specifically: app create/rename, chat and
    attachments, agent action grammar, bundle/state/revision bounds,
    capability-worker bootstrap and global removal, HTML/CSS sanitizer,
    Shadow DOM/event bridge, generated JavaScript execution, data mutations,
    worker termination/races, persistence, schedules, and recovery behavior.
13. Verify deprecated apps reserve ids/slots and run only required migrations:
    no backend, UI, agent discovery, route, service, port, or stale table
    remains; reusing a reserved identity must require an explicit future
    migration and review.
14. Run unit, malicious-browser, smoke, stage, and deployed-host probes for
    cross-app/database/socket/port access, forged markers and `postMessage`,
    hostile markup/URLs/files, traversal/symlink swaps, oversized or partial
    traffic, backend crash/restart, stale frames, and agent calls before,
    during, and after a live thread.

## Collaborative review

### `6151eea5abb61590684c4cf667ae6f619d705231`

Reviewed by: gpt-5.6-sol; Claude Opus 5

Methodology: static, repository-level trust-boundary review of every installed
and deprecated app package, the generated host identities and services, all
three app communication paths, the iframe/CSP/parent bridge, and both stable
app implementations. The real manifest loader was executed against the tree,
targeted unit/contract tests were run, and browser sinks were traced by source
and grep. No live-host cross-uid probe, browser exploit, or load test was run.

#### What was reviewed

- `host/runtime/core/app_platform.py` and every `host/apps/*/manifest.json`:
  package/path validation, stable slots and derived users/roles/schemas/ports,
  active versus deprecated packages, agent API and worker declarations, and
  static UI asset confinement. The validator loaded Agent Chat at slot 0,
  Agentic Web App at slot 6, and five migration-only deprecated packages at
  their reserved slots.
- `host/bootstrap/render.py`, `host/bootstrap/verify_deploy.py`, and
  `host/runtime/deploy/app_migrate.py`: Unix and PostgreSQL identities,
  migrations, systemd units/slices, loopback/nftables reachability, file
  ownership, restart behavior, and end-of-deploy assertions.
- `host/runtime/admin_api/app_api_proxy.py`,
  `host/runtime/admin_api/app_backend_api.py`,
  `host/runtime/agent_app/{api,service}.py`, and the Admin API dispatch:
  browser-to-backend proxying, app-backend Unix-socket `SO_PEERCRED` mapping,
  thread-id prefixing/filtering, and kernel-attributed agent-to-app calls.
- `host/runtime/admin_api/service.py`,
  `host/runtime/admin_api/admin_ui/app.js`, and
  `docs/architecture/apps/apps.md`: app lookup, CSP and sandbox tokens,
  same-app API confinement, body/response/time limits, host-owned upload
  selection, clipboard behavior, open-file behavior, and frame/source binding.
- Agent Chat's backend and UI, including thread pagination, archive/rename,
  attachments, activity rendering, and `rich_text.js`; and Agentic Web App's
  backend, migrations, multi-workspace state, optimistic revisions,
  HTML/CSS sanitizers, Shadow DOM event bridge, capability worker,
  data mutation grammar, and agent API.

Targeted tests completed successfully: 31 app-platform/rich-text tests, 31
Agent Chat backend tests, and 69 Admin UI static and Agentic Web App
contract/routing/conversation/mock tests.

#### Coverage and confidence

- Checklist 1–2: all manifests were loaded through the production validator;
  generated identities, DB namespaces, migrations, unit files, firewall
  callers, and deprecated slot reservations were checked against bootstrap and
  deploy verification. This was source/generated-config validation, not a
  comparison with live accounts, grants, cgroups, or nftables state.
- Checklist 3–7: all browser, backend-socket, and agent-app routes were traced
  through their caller identity, route grammar, bounds, and response
  filtering. The iframe CSP/sandbox and complete parent bridge were
  enumerated. Same-app API and upload mediation hold; unmediated clipboard and
  parent-file actions are APP-001 and APP-002. Comparing the two shipped
  frames against each other, rather than each against the documented contract,
  showed the bridge is only source-bound on one side: the parent binds the
  source window, and Agentic Web App checks `event.source !== parent`, but
  Agent Chat checks neither source nor origin (APP-003). Two further boundary
  facts from this pass: the app-backend route allowlist grants
  `GET /v1/network/policy`, which two of the three boundary statements in
  `apps.md` deny (APP-004); and the app-backend Unix socket itself has no
  per-connection read timeout and no worker bound, which is registered on
  axis 04 as ADM-004 because its impact lands on admin-API availability rather
  than on app authority — it was independently found here and on axis 08.
- Checklist 8–9: markup, URL, CSS, worker, object-URL, clipboard,
  `postMessage`, file-picker, and file-view sinks were searched and traced.
  Agent Chat's renderer escapes agent content and converts links/images to
  non-navigating copy controls. Agentic Web App confines generated code to its
  blob worker and sanitizes the rendered HTML/CSS. No malicious-browser run
  was performed, so confidence in browser-version edge behavior is lower than
  confidence in the source-level containment.
- Checklist 10–12: app-row/thread ownership, reserved internal ids,
  pagination, locking, revisions, concurrent browser/agent writes, Agent Chat,
  and Agentic Web App were checked in source and focused tests. Database tests
  used repository mocks rather than a deployed PostgreSQL/app-service stack.
- Checklist 13: deprecated packages retain only their manifest, migrations,
  and reserved identities; they produce no installed UI/backend service.
- Checklist 14: unit and contract tests were run. Deployed-host, crash/restart,
  cross-port, cross-schema, hostile-browser, and live-thread timing probes were
  deliberately omitted from this repository-level sweep.
