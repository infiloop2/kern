# Audit: Bundled Tools, Approvals, and Data Disclosure

Finding ID prefix: `TOOL`. See [README.md](README.md) for the sweep process,
finding format, and severity scale.

## Audit question

Can an agent compromise the tools service, access credentials or another
tool's data, bypass or alter an approval, or cause unexpected data to be sent
to a third party?

## Reviewed commits

Latest reviewed commit: none.

| Commit | Reviewed by |
| --- | --- |
| _None yet_ | _No completed review_ |

## Findings

No findings recorded.

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

No completed reviews yet.
