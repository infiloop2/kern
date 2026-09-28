# Audit: Bundled Tools, Approvals, and Data Disclosure

Finding ID prefix: `TOOL`. See [README.md](README.md) for the sweep process,
finding format, and severity scale.

## Audit question

Can an agent compromise the tools service, access credentials or another
tool's data, bypass or alter an approval, or cause unexpected data to be sent
to a third party?

## Reviewed commits

Latest reviewed commit: `a597a8d063d735d11c30d1f2b2f6b66e7479ceac`.

| Commit | Reviewed by |
| --- | --- |
| `a597a8d063d735d11c30d1f2b2f6b66e7479ceac` | GPT-6 (Codex) |

## Findings

| Finding | Severity | Found at | Found by | Description | Resolution |
| --- | --- | --- | --- | --- | --- |
| TOOL-001 | Medium | `47daf37e08a8` | gpt-5.6-sol | Gmail and Google Calendar load a stored Google credential, perform token-refresh or identity network calls, then unconditionally save or clear the credential they originally observed. If the operator disconnects or reconnects during that round trip, a stale refresh can overwrite the new account/secret and a stale invalid-grant, missing-scope, or identity result can delete it, causing later tool calls to fail or act under the unintended stored account until another reconnect. Use the same compare-before-save/clear generation checks already used by the newer OAuth tools so the operator's concurrent change always wins. | Fixed — the shared Google credential store compares account id and secret against the loaded credential before every save/clear in access_token and refresh_identity, so a concurrent operator disconnect/reconnect always wins and a stale refresh or invalid-grant result can no longer overwrite or delete the new credential. |
| TOOL-002 | Medium | `47daf37e08a8` | Claude Opus 5 | `google_calendar.read_events` and `gmail.read_message`, plus the pre-approval preview leg of `google_calendar.event_change`, copy agent-supplied strings into third-party request URLs with no grammar check and no `api.outbound.guard_request_parameter_string` call. `_calendar_read_input` (`host/tools/google_calendar/__init__.py:232`) checks only `isinstance(value, str)` and non-empty before `_calendar_events` (`:246-258`) urlencodes the values into `timeMin`/`timeMax`; `read_message` passes `message_id` into `_format_gmail_api_path`, which percent-quotes it into the path. Each such field can carry up to the 64 KiB tool-input cap (`tools_host.PAYLOAD_MAX_BYTES`) of arbitrary agent prose off the host in a direct, no-approval action, and the host-wide 1,024-byte parameter floor described as "the floor that works when every other guard misses" never runs. `tests/test_param_guard_coverage.py:60-61` exempts every Gmail and Calendar field under blanket wildcards whose recorded reasons are `CONNECTED_ACCOUNT` ("the destination already holds the data") and `TYPED` ("enum/id/timestamp/cursor grammar is stricter than scanning") — but no grammar is enforced on these fields, and arbitrary prose is not data Google already holds. Gmail's own `search_messages` shows the intended pattern, guarded with `allow_identifiers=True` and parsing `start_time`/`end_time` through `datetime.fromisoformat`; Calendar's identical time fields are not parsed at all. Impact is bounded — values are percent-encoded, `_NoRedirectHandler` refuses 3xx, the destination stays pinned to Google, and the data lands in the operator's own connected account — so this is a missing-guard and misleading-disclosure defect rather than request injection, but `read_events`'s data policy tells the operator that "only the requested time range and fixed listing options go to Google". Parse the Calendar timestamps, constrain the Gmail id grammar, and narrow the exemption wildcards to fields that actually have one. | Fixed — Calendar time fields must parse as ISO-8601 (and are re-serialized) and every Gmail path id plus Calendar event_id must match a strict id grammar before entering a request URL, including the pre-approval preview legs; the blanket param-guard wildcard exemptions were replaced with per-field entries. |
| TOOL-004 | Medium | `a597a8d063d735d11c30d1f2b2f6b66e7479ceac` | GPT-6 (Codex) | Shared OAuth save/clear guards compare a freshly loaded account and secret, then mutate in a separate transaction. A disconnect between that comparison and save is undone by the unconditional upsert; a same-account reconnect between comparison and clear is deleted. Deterministic interleavings reproduced both. This is the residual compare-to-write race after TOOL-001, now shared by multiple OAuth providers. Compare and mutate atomically under a database row lock, returning failure when the connection is missing or changed. | Fixed — shared OAuth refresh and failure cleanup compare account id and secret under the same database row lock as the conditional save/delete; missing or changed connections cannot be recreated, overwritten or cleared by stale credential work. |
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

### `a597a8d063d735d11c30d1f2b2f6b66e7479ceac`

Reviewed by: GPT-6 (Codex)

Methodology: loaded the production discovery registry and inventoried every
manifest/action, connection mode, schema, disclosure and protection mapping.
Traced framework credentials/approvals/assets and outbound helpers, inspected
package request/approval boundaries and guard exemptions, and ran the complete
credential-free framework/package suite. This combined source and test review
does not claim exhaustive manual line-by-line inspection of every provider
response parser or live-provider behavior.

#### What was reviewed

Production discovery contains 27 packages and 135 actions:

| Packages | Actions per package |
| --- | --- |
| apify, apify_developer, brave_search, cloudwatch_logs | 2, 18, 1, 1 |
| elevenlabs, gmail, google_calendar, google_search_console | 7, 9, 2, 5 |
| h3max, ibkr, instagram, instagram_discovery | 3, 4, 6, 5 |
| linkedin, linkedin_discovery, openai_images, polymarket | 2, 1, 1, 6 |
| reddit, reddit_scrapecreators, runway, seedance | 7, 4, 7, 3 |
| twitter, twitterapi_io, upwork, vercel_analytics | 7, 1, 12, 3 |
| web_fetch, whatsapp, zoho_mail | 4, 4, 10 |

- `runtime/tools/{api,assets,service,tools_host}.py`, core state/secretbox,
  `tools/host_api.py`, schema/results contracts, agent MCP shim, operator
  routes and approval UI, bootstrap uid/sockets/grants and asset lifecycle.
- Shared HTTP/streaming, OAuth/Google, input/JSON/media and parameter guards;
  package direct-request and approval dispatch sites, credentials/refresh,
  fixed URL construction, typed identifiers/cursors, error mapping and tests.
- Newer boundaries include Upwork's fixed MCP operations/PKCE/redaction,
  Apify Developer's exact code/build/cost/target approval snapshots,
  WhatsApp's private gateway/account binding, generic Web Fetch's pinned
  public DNS/HTTPS redirect checks, and Zoho's regional mail/attachment client.

#### Outcome and remediation evidence

The fixes and regression tests discussed below resolve this finding in the
stacked follow-up. The resolution records the operator-requested remediation;
the original audit commit and finding description remain unchanged.

TOOL-004 is a new, narrower race after the historical TOOL-001 check. The
shared helper loaded and compared the current credential, then called a
separate unconditional save/delete. A deterministic baseline fixture placed
`disconnect` immediately after that load: the stale save recreated the deleted
record. Placing a same-account reconnect there let stale failure cleanup erase
the new token. This affects every caller of the shared helper, including
Google, Twitter, Instagram, LinkedIn, Upwork and Zoho flows.

The remediation adds scoped `save_if_current` / `clear_if_current` to
HostAPI. The production store compares account id and decrypted secret while
holding the row with `SELECT ... FOR UPDATE` in the same transaction as the
write. Missing or changed credentials return false; refresh reports reconnect
required and stale cleanup does nothing. It does not promise to undo an
external request already sent before a disconnect.

Regression tests cover disconnect, same-account token replacement, successful
conditional mutation, and shared-helper interleavings. A database integration
test opens a second session between comparison and mutation and requires its
conflicting delete to remain locked, for both save and clear. Both database
regressions passed in CI for this implementation; they were skipped locally
under Kern's production-host database policy. TOOL-004 is resolved by the
atomic mutation and its regression coverage.

#### Coverage and confidence

- Checklist 1: all package ids/manifests/actions were loaded by real
  discovery, with linkage, schema, direct/operator mode, disclosures, config
  and protections inventoried. Malformed/duplicate/incomplete discovery cases
  are covered by existing tests. Guides and policy descriptions were compared
  at security-relevant boundaries, not every sentence independently verified
  against third-party documentation.
- Checklists 2–3: reviewed service uid/direct egress, peer-scoped operator and
  agent routes, encrypted scoped stores/grants, MCP aggregation/outage paths,
  JSON/result/stream bounds and timeouts. A listing/approval id is not operator
  decision authority. Shared transport admission pressure is a known residual
  concern; no new authority bypass or live socket flood was demonstrated.
- Checklist 4: reviewed and ran schema/result validation cases for unknown
  and nested fields, required values, booleans versus numbers, finite JSON,
  enums/patterns, Unicode/byte limits and invalid result variants.
- Checklists 5–8: inventoried direct-action input classifications and searched
  all request/guard/approval call sites. `test_param_guard_coverage.py` checks
  every agent string field is guarded or specifically exempt; package tests
  exercise actual wire construction. Inspected decoded URL/query handling,
  typed ids, free text, opaque cursors, fixed HTTPS clients, redirect refusal,
  media bounds and generic Web Fetch's per-hop public-address pinning. The
  heuristic guard does not guarantee detection of every possible encoding.
  Provider-controlled CDN destinations retain the accepted TOOL-003 trust
  decision. Real DNS/provider transformations and every response parser branch
  were not independently exercised.
- Checklist 9: traced immutable payload/account/action snapshots, single-use
  state transitions, replay/restart/expiry handling, asset bindings and target
  revalidation. Bundled code is trusted to honor its approval declaration;
  host-side pre-approval effect isolation is not promised. Existing packages'
  preview reads and direct paid/media actions remain intentional disclosures.
  No real approved mutation, broker order, publication or message was sent.
- Checklists 10–11: traced tool/connection scoping, secretbox, redaction,
  signed expiring tool-bound OAuth state, redirect/PKCE/code exchange and
  refresh/disconnect behavior; this produced TOOL-004. Private tool `secrets`
  remain a separate whole-object API without a generic CAS contract. No real
  OAuth consent, token refresh or revocation was performed.
- Checklist 12: reviewed staged file regularity/type/size/quota/hash and
  opaque tool scope, in-flight visibility, cleanup and approval binding;
  outbound streams use bounded private atomic publication. Public video
  capabilities retain real-auth scope/expiry tests. No real large transfer
  or external provider retrieval was executed.
- Checklists 13–14: inspected hostile-response filtering, ids/account/target
  checks, bounded counts, error mapping and operator summary/data-policy
  alignment. Provider-private data intentionally returned by read actions is
  not treated as an unauthorized disclosure. Contract claims beyond local
  source/fixtures remain unverified against live services.
- Checklist 15: baseline full suite passed 2,978 tests with 608 skips; after
  the fixes it passed 2,983 with 610 skips (the two added real-database tests
  are among them). Every package has package-level coverage, alongside the
  framework, guards, OAuth and assets suites. PostgreSQL and WebKit run in CI;
  their absence locally is not recorded as a pass.

Confidence is high for the reproduced race and framework/source contracts,
medium for provider-specific mock coverage, and limited for live integrations
that this sweep deliberately did not invoke.
