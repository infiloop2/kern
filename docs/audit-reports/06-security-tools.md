# Audit: Bundled Tools, Approvals, and Data Disclosure

Finding ID prefix: `TOOL`. See [README.md](README.md) for the sweep process,
finding format, and severity scale.

## Audit question

Can an agent compromise the tools service, access credentials or another
tool's data, bypass or alter an approval, or cause unexpected data to be sent
to a third party?

## Reviewed commits

Latest reviewed commit: `6151eea5abb61590684c4cf667ae6f619d705231`.

| Commit | Reviewed by |
| --- | --- |
| `6151eea5abb61590684c4cf667ae6f619d705231` | gpt-5.6-sol; Claude Opus 5 |

## Findings

| Finding | Severity | Found at | Found by | Description | Resolution |
| --- | --- | --- | --- | --- | --- |
| TOOL-001 | Medium | `47daf37e08a8` | gpt-5.6-sol | Gmail and Google Calendar load a stored Google credential, perform token-refresh or identity network calls, then unconditionally save or clear the credential they originally observed. If the operator disconnects or reconnects during that round trip, a stale refresh can overwrite the new account/secret and a stale invalid-grant, missing-scope, or identity result can delete it, causing later tool calls to fail or act under the unintended stored account until another reconnect. Use the same compare-before-save/clear generation checks already used by the newer OAuth tools so the operator's concurrent change always wins. | Fixed — the shared Google credential store compares account id and secret against the loaded credential before every save/clear in access_token and refresh_identity, so a concurrent operator disconnect/reconnect always wins and a stale refresh or invalid-grant result can no longer overwrite or delete the new credential. |
| TOOL-002 | Medium | `47daf37e08a8` | Claude Opus 5 | `google_calendar.read_events` and `gmail.read_message`, plus the pre-approval preview leg of `google_calendar.event_change`, copy agent-supplied strings into third-party request URLs with no grammar check and no `api.outbound.guard_request_parameter_string` call. `_calendar_read_input` (`host/tools/google_calendar/__init__.py:232`) checks only `isinstance(value, str)` and non-empty before `_calendar_events` (`:246-258`) urlencodes the values into `timeMin`/`timeMax`; `read_message` passes `message_id` into `_format_gmail_api_path`, which percent-quotes it into the path. Each such field can carry up to the 64 KiB tool-input cap (`tools_host.PAYLOAD_MAX_BYTES`) of arbitrary agent prose off the host in a direct, no-approval action, and the host-wide 1,024-byte parameter floor described as "the floor that works when every other guard misses" never runs. `tests/test_param_guard_coverage.py:60-61` exempts every Gmail and Calendar field under blanket wildcards whose recorded reasons are `CONNECTED_ACCOUNT` ("the destination already holds the data") and `TYPED` ("enum/id/timestamp/cursor grammar is stricter than scanning") — but no grammar is enforced on these fields, and arbitrary prose is not data Google already holds. Gmail's own `search_messages` shows the intended pattern, guarded with `allow_identifiers=True` and parsing `start_time`/`end_time` through `datetime.fromisoformat`; Calendar's identical time fields are not parsed at all. Impact is bounded — values are percent-encoded, `_NoRedirectHandler` refuses 3xx, the destination stays pinned to Google, and the data lands in the operator's own connected account — so this is a missing-guard and misleading-disclosure defect rather than request injection, but `read_events`'s data policy tells the operator that "only the requested time range and fixed listing options go to Google". Parse the Calendar timestamps, constrain the Gmail id grammar, and narrow the exemption wildcards to fields that actually have one. | Fixed — Calendar time fields must parse as ISO-8601 (and are re-serialized) and every Gmail path id plus Calendar event_id must match a strict id grammar before entering a request URL, including the pre-approval preview legs; the blanket param-guard wildcard exemptions were replaced with per-field entries. |
| TOOL-003 | Low | `ca99416ac9dd` | Claude Opus 5 | `_upload_staged_asset` streams a staged agent media file to whatever `uploadUrl` Runway returned, gated only by `_is_https_runway_url` (`host/tools/runway/__init__.py:583,601`). Despite its name and its failure message, that predicate (`:688-708`) checks only length ≤ 2048, scheme `https`, hostname is not an IP literal, hostname contains a dot, no userinfo, and port 443/None — every HTTPS host on the internet qualifies. The same predicate is aliased `_is_https_output_url` (`:684-685`) and decides both which URL `runway_save_video` downloads with the tools service's egress and which URL is handed to the agent as `video_url`/`image_url`/`audio_url`. The bundled Instagram package shows the tighter pattern for the same situation: `_is_meta_upload_uri` pins `rupload.facebook.com` and an `/ig-api-upload/` path prefix before sending bytes. The trigger is narrow — TLS verification is on and redirects are refused, so it requires control of the `api.dev.runwayml.com` endpoint itself rather than a stolen API key or a provider-side open redirect — and no credential is attached to the upload, but the data at stake is the operator's workspace media whose destination the manifest states as Runway plus named model providers. Constrain the accepted upload/download hosts to Runway's documented domains, and rename the predicate so it no longer implies a host check it does not make. | Wontfix — the upload and output URLs arrive only in Runway's own authenticated HTTPS API responses, and an operator who enables the Runway tool is already trusting Runway with their workspace media, so the destination Runway names is trusted by the same decision. Pinning hosts would also risk rejecting legitimate traffic, since Runway does not document which asset/CDN hosts these URLs use. The misleading predicate name was still corrected (_is_public_https_url), so it no longer implies a host check it does not make. |

## Threat model

- **Adversaries:** (a) a malicious or prompt-injected agent choosing arbitrary
  tool/action inputs and call timing; (b) malicious data returned by a
  connected account or third-party API and later reused in another request;
  (c) a compromised third-party endpoint, redirect, OAuth page, or webhook;
  (d) a buggy bundled tool package; and (e) an untrusted local process trying
  to reach the tools service or guess an approval id.
- **Assets:** OAuth/API credentials and tool config; connected-account private
  data; agent, app, and operator data; approval payload integrity; staged
  files; brokerage/social/email/calendar authority; accurate operator-facing
  action, destination, retention, and approval descriptions.
- **Out of scope:** a third party's behavior after the operator knowingly
  approves an accurately described disclosure; provider compromise that
  reveals data Kern intentionally sent according to that policy; the agent's
  separate network-proxy path except where the same outbound guard is shared.

## Minimal scope checklist

This checklist is not comprehensive. The audit question and threat model are
binding; report any in-scope defect even if no item names it.

1. Enumerate every discovered package under `host/tools/` and compare its
   directory/id, manifest, actions, input/output schemas, direct/operator
   approval mode, `data_policy`, data-summary cards, protections, setup guide,
   config keys, credential flow, and code paths. Prove discovery rejects
   malformed, duplicate, undeclared, or partially registered packages.
2. Audit tools-service confinement: Unix user and direct egress, systemd
   environment, socket path/mode and peer allowlists, PostgreSQL role/grants,
   secret-key access, encrypted credential/config storage, and inability to
   read admin/app/proxy state or accept traffic through another local path.
3. Trace all harnesses through the MCP shim to tools, network-introspection,
   and app sockets. Verify exact listing/call routing, enabled checks,
   synthetic tool behavior, tools-socket outage fallback, peer credentials,
   JSON framing, request/result/stream bounds, concurrency/read timeouts,
   disconnects, and that discovery or an approval id grants no extra authority.
4. Verify the host enforces the declared JSON-schema subset before execution
   and validates direct results afterward: required/unknown fields, nested
   objects/arrays, types versus booleans, finite numbers, enums, patterns,
   Unicode, byte limits, extra provider fields, and malformed package result
   variants must fail without side effects or raw data leakage.
5. For every direct action, list every byte derived from agent, filesystem,
   connected account, or provider data that enters a destination host, path,
   query, fragment, header, body, uploaded file, prompt, or redirect. Confirm
   direct execution is intentional and every required guard runs before any
   DNS, connection, upload, or third-party side effect.
6. Audit query and path parameters especially: percent/double encoding,
   Unicode and controls, delimiters, nested URLs, userinfo, credential-named
   keys, opaque cursors, ids, free text, provider-echoed links, fragments,
   redirects, and error URLs. Validate the effective decoded request at every
   helper/call site, not only the original tool input.
7. Audit `guard_request_parameter_string` and every identifier exemption
   against credentials, tokens, sessions, one-time codes, email/phone/payment/
   government identifiers, seed phrases, private content, encoded blobs, and
   boundary false positives. A denial must stop the action, not redact or
   silently send modified data.
8. Audit outbound helpers and per-tool clients: fixed HTTPS destinations,
   certificate verification, DNS/IP behavior, redirect refusal, method/header/
   content-type construction, timeouts, request and response/stream bounds,
   retry and idempotency behavior, multipart framing, provider-returned URLs,
   and whether any generic helper reaches an undeclared third party.
9. For every approval action, prove no external side effect or sensitive
   transfer occurs before approval. Bind the immutable stored payload and
   operator summary to tool/action/account, credential generation, config and
   staged assets; make decision/check capabilities unguessable and single-use;
   revalidate mutable targets; and fail closed on expiry, replay, restart,
   concurrent decision/execution, account replacement, or partial failure.
10. Trace secrets/config through HostAPI scoping, database grants, secretbox,
    memory, logs, exceptions, approval summaries/payloads/results, audit events,
    agent-visible output, OAuth URLs/callbacks, environment/argv, refresh, and
    outbound requests. One tool must not name or infer another tool's state,
    and raw credentials/provider bodies must never surface.
11. Audit OAuth end to end: public callback boundary, authorization URL and
    PKCE where used, state authenticity/tool binding/expiry, reserved fields,
    code exchange, exact redirect URI, scope/account validation, token refresh
    generation races, reconnect/disconnect/revocation, and query/log/referrer
    leakage.
12. Audit both asset directions. For staged agent files, check path/symlink/
    regular-file rules, private ownership, type/size/count/quota, hashing,
    opaque tool scope, in-flight visibility, expiry/startup cleanup, and
    approval binding. For streaming results, check authoritative provider URL,
    exact length/type/name, no buffering/spool escape, private atomic workspace
    publish, and partial-transfer cleanup.
13. Treat third-party responses as hostile: cap and strictly parse them,
    reject non-finite/invalid Unicode and mismatched ids/accounts/targets,
    validate returned URLs, strip unneeded fields, bound result counts, map
    errors without provider bodies, and prevent one response from selecting a
    later request destination or approved target.
14. Compare operator and agent UX with behavior: action descriptions, schemas,
    approval labels and exact payload, destination, retention, connected
    account, direct-action disclosure, guides/config state, pending/terminal
    status, retries, and reconnect guidance. Misleading policy text is a
    security finding even if the implementation is otherwise safe.
15. Require package-level tests for every action and framework tests for
    discovery, HostAPI scope, schemas, approval lifecycle, OAuth, assets,
    outbound guards, socket peers, concurrency, and redaction. Run negative
    tests without credentials and use the cheapest live provider only when
    source plus mocked wire tests cannot establish the boundary.

## Collaborative review

### `6151eea5abb61590684c4cf667ae6f619d705231`

Reviewed by: gpt-5.6-sol; Claude Opus 5

Methodology: repository-level inventory and end-to-end authority trace of the
bundled-tool framework and all 11 packages. Manifests, discovery, schemas,
direct/operator approval paths, OAuth/config state, secret storage, outbound
clients, parameter guard, assets, hostile responses, operator delegation, and
agent socket/shim routing were read with focused framework/package tests. No
real provider account, OAuth callback, approved third-party mutation, or live
streaming download was exercised.

#### What was reviewed

- Production discovery and manifests for Brave Search, Gmail, Google Calendar,
  IBKR, Instagram, Instagram Discovery, LinkedIn, LinkedIn Discovery,
  Polymarket, Runway, and Twitter: ids, connections, config, setup copy,
  action schemas/descriptions, direct versus operator approval, data policies,
  disclosure cards, protections, destinations, and implementation linkage.
- `host/runtime/tools/{api,assets,service,tools_host}.py`,
  `host/runtime/agent_shim/mcp_shim.py`, Admin tools client/routes/UI,
  bootstrap identities/sockets/nftables/PostgreSQL grants, state tables,
  `HostAPI`, secretbox, approval lifecycle, audit events, and cleanup.
- Shared HTTP, OAuth2, Google, JSON/schema, parameter-guard, RSA and media/
  streaming helpers; every package client, request builder, response parser,
  credential refresh/revoke path, and error mapper.
- Every direct action's fixed destination and outbound values; every approved
  action's immutable stored payload, operator summary, credential/config
  binding, decision token, execution path, staged asset use, and terminal
  status. Google refresh races are TOOL-001.

#### Coverage and confidence

- Checklist 1: all 11 production packages loaded through discovery; ids,
  action linkage, schema subset, connection type, declared disclosure, and
  direct/approval classification were compared with code. Unknown, duplicate,
  malformed, extra, and incomplete packages fail discovery/tests.
- Checklists 2–3: the dedicated `kern-tools` uid has only DNS/HTTPS, its
  scoped database grants and encrypted credential state, and peer-gated
  agent/admin routes. MCP listing/call aggregation, enabled checks, socket
  failure handling, body/result/stream bounds, read timeouts, and call slots
  were traced. Connection admission before handler creation is a reliability
  issue recorded as REL-007, not a tool-authority bypass. An independent pass
  reached the same conclusion by a different route and adds two details worth
  keeping with that row: `MAX_CONCURRENT_CALLS = 8` is acquired inside
  `do_POST`, so it bounds in-flight executions and not connections or threads;
  and the binding ceiling is the tools service's file-descriptor soft limit
  (1024 by default on the target Ubuntu 22.04/systemd 249 platform), so roughly
  a thousand stalled connections suffice rather than the far larger numbers a
  memory-based estimate suggests. The operator-facing consequence is the
  reason it matters here: approve/deny/disconnect calls are reverse-proxied to
  the same server, so `host-integration.md:200-206`'s claim that "a busy agent
  can never block the operator from deciding approvals or disconnecting a
  tool" does not hold once that server cannot accept.
- Checklist 4: host-side schema validation covers required/unknown/nested
  fields, booleans versus numbers, finite values, enums/patterns, arrays, and
  output variants before/after execution. Package code receives only validated
  inputs; malformed results become bounded generic failures.
- Checklists 5–8: every direct action's host, method, URL components, headers,
  body, and file/prompt data were inventoried. Fixed HTTPS clients verify TLS,
  refuse redirects, bound time/response work, and apply the shared decoded
  parameter guard before connection/side effect. Provider-returned
  destinations are either rejected or revalidated; no undeclared generic
  egress helper was found. Two qualifications from auditing checklist 7's
  identifier exemptions against the code rather than against their recorded
  reasons. First, the exemption list is not self-consistent: every Gmail and
  Calendar field is exempted as `TYPED` or `CONNECTED_ACCOUNT`, yet several of
  those fields enforce no grammar at all and accept arbitrary agent prose up
  to the 64 KiB payload cap, so the parameter floor never runs on them
  (TOOL-002). Second, "provider-returned destinations are either rejected or
  revalidated" holds in most packages but not uniformly: Runway's
  revalidation predicate accepts any HTTPS host despite its name, where the
  Instagram package pins host and path prefix (TOOL-003).
- Checklist 9: approval creation performs no external side effect; opaque
  single-use ids bind immutable action/input/summary, tool, account,
  credential/config generation, staged assets, expiry, and decision state.
  Execution revalidates mutable state and concurrent decisions fail closed.
- Checklists 10–11: credentials/config are tool-scoped, encrypted at rest,
  absent from agent-visible results/logs, and used only in fixed request
  locations. OAuth state is signed, tool-bound, expiring, and callback/
  redirect/scope/account checked. Newer OAuth packages compare the loaded
  credential before mutation; the shared Google flow does not (TOOL-001).
- Checklist 12: staged image/video files use agent-opened streams, private
  opaque tool scope, byte/type/count/expiry bounds, hashes, startup/periodic
  cleanup, and approval binding. Streaming results validate provider URL,
  length/type/name, cap bytes, publish privately/atomically to the workspace,
  and remove partial output.
- Checklists 13–14: third-party JSON and media metadata are bounded, typed,
  identifier/account checked, stripped to declared results, and mapped to
  generic errors. UI descriptions, exact approval payloads, destinations,
  retention, connection state, direct-action disclosure, and terminal
  statuses were compared with behavior.
- Checklist 15: framework and package tests were reviewed/run without live
  credentials. No live OAuth, provider mutation, large streaming transfer,
  socket flood, or deployed uid/database probe was performed. Confidence is
  high for deterministic discovery/schema/approval/scoping logic and medium
  for provider-specific behavior that mocks cannot establish.
