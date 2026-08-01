# Audit: Network Proxy Policy Enforcement

Finding ID prefix: `NET`. See [README.md](README.md) for the sweep process,
finding format, and severity scale.

## Audit question

Can an agent send any internet traffic the active policy does not intend to
allow?

## Reviewed commits

Latest reviewed commit: `6151eea5abb61590684c4cf667ae6f619d705231`.

| Commit | Reviewed by |
| --- | --- |
| `6151eea5abb61590684c4cf667ae6f619d705231` | gpt-5.6-sol; Claude Opus 5 |

## Findings

| Finding | Severity | Found at | Found by | Description | Resolution |
| --- | --- | --- | --- | --- | --- |
| NET-004 | Critical | `b88eee7224e7` | Claude Opus 5 | The proxy decides a connection is a WebSocket from the client's own request headers — `is_websocket = any(key.lower() == "upgrade" and value.lower() == "websocket" ...)` (`host/runtime/network_proxy/service.py:230`) — and never parses the upstream response to confirm a `101 Switching Protocols`. On that branch it drops the `Connection: close` single-request pin that every other request gets (`:459,464-465`), pushes whatever the client pipelined behind the request head straight to the upstream (`:280-286`, `initial_client_bytes=reader.drain()`), and enters `tunnel_websocket`, which relays bytes in both directions unmodified whenever `integrations.ws_message_guard` returns `None` (`:599-620`). That guard is `None` for every integration except OpenAI's two hosted domains, so one policy-passing request carrying `Upgrade: websocket` plus `Connection: keep-alive` — which an ordinary HTTP/1.1 server ignores while keeping the connection open — converts the tunnel to any host allowed by the custom, Claude, Bedrock, GitHub, PyPI, or npm integration into a plain TCP pipe. Every subsequent request on that connection escapes the method allowlist, path guards, body guards, the outbound parameter guard, and the `network_events` log. Parse the upstream response after forwarding a handshake and fall back to the ordinary single-request path unless it is `101`, and never forward `reader.drain()` bytes without the message guard. | Fixed — WebSockets now require explicit integration opt-in, pass normal request guards, verify a final upstream `101`, and always use the frame/message guard. |
| NET-001 | High | `f28b50e87b61` | Claude Opus 4.8 | Managed-provider account pins were derived repeatedly from agent-writable provider credentials. A malicious agent could replace those credentials with an attacker-controlled account, wait for the pin refresh, and then exfiltrate through an otherwise allowed provider domain. Anchor pins to operator-approved account state and attest refreshed credentials against that anchor. | Fixed — provider pins derive from operator-approved account state and refreshed credentials are attested against it. |
| NET-002 | High | `98f423bbfef4` | gpt-5.6-sol | The OpenAI and Claude body guards collapse duplicate request headers with last-value-wins while the proxy forwards every original header instance. A compressed request can therefore send `Content-Encoding: gzip` followed by `Content-Encoding: identity`: the guard inspects the still-compressed bytes as non-JSON and allows them, while an upstream that combines duplicate fields according to HTTP semantics can decode the gzip body and execute the denied hosted web/code/MCP declaration. Direct guard calls reproduced `None` for this duplicate form while each canonical gzip request was denied. Reject duplicate semantic headers and normalize one unambiguous content encoding/type before both inspection and forwarding. | Fixed — duplicate single-valued semantic headers are rejected before inspection and forwarding. |
| NET-003 | High | `dcaa9c162717` | gpt-5.6-sol | Enabling GitHub makes every subdomain of `blob.core.windows.net` eligible for GET/HEAD when the query contains one syntactically Base64-shaped 44-character `sig`; the guard cannot establish that the Azure account or SAS URL came from GitHub. An agent with a SAS for an attacker-controlled Azure account can therefore use `attackercontrolledacct.blob.core.windows.net` as arbitrary third-party ingress/egress under a policy described as limited to GitHub Actions downloads. Bind Azure downloads to a short-lived capability learned from a validated GitHub response, or require an explicit operator-owned domain rule instead of accepting signature shape alone. | Fixed — Azure Blob access is limited to GitHub's documented `productionresultssa0` through `productionresultssa19` storage accounts. |
| NET-005 | Medium | `b88eee7224e7` | Claude Opus 5 | `_serve_tls_request` derives its policy inputs with `urllib.parse.urlsplit(target)` and keeps only `parsed.path` and `parsed.query` (`host/runtime/network_proxy/service.py:222-227`), but forwards the original unmodified `target` string upstream (`:280`). `urlsplit` moves everything after a `#` into `fragment` and everything between a leading `//` and the next `/` into `netloc`, so bytes an agent places in either position are invisible to the path guards, invisible to the shared outbound parameter guard (`host/network_integrations/base.py:192-220`, reconstructed solely from `path` and `query`), and absent from the `network_events` row (`:251`) — yet they are transmitted verbatim in the request line. An exact guard such as `^/health$` therefore stops constraining what the origin resolves: the guard sees `/health` while the wire carries `/health#/../../admin`, and any origin or CDN that strips the fragment before collapsing dot segments (nginx and Apache both normalize dot segments) resolves a path the operator's rule forbade. This is a guard-fidelity and audit-record defect rather than a new exfiltration channel — arbitrary request headers on the same allowed request are already forwarded verbatim and equally unlogged. Validate the request-target as strict origin-form, rejecting any target containing `#` or beginning with `//`, or forward the reconstructed `path?query` instead of the raw target. | Fixed — the proxy rejects any TLS request-target that is not strict origin-form (no '//' prefix, no '#'), so the path guards, the outbound parameter guard, and the network_events row see exactly the bytes forwarded upstream. |
| NET-006 | Medium | `98f423bbfef4` | Claude Opus 5 | For `api.openai.com` and `chatgpt.com` the guard requires a `chatgpt-account-id` header matching the pinned account (`host/network_integrations/openai/guard.py:64-77`), but nothing in the proxy examines, strips, or replaces `Authorization` on those hosts: only GitHub and Bedrock register `rewrite_request_headers` (`host/network_integrations/runtime.py:50-85`), and `send_http_request` strips only `Proxy-Authorization`/`Proxy-Connection` (`host/runtime/network_proxy/service.py:450-451`). The proxy therefore establishes no local binding between the pinned account and the credential the request actually authenticates with: an agent holding any other OpenAI credential can present it in `Authorization` while echoing the operator's pinned account id. The integration's own denial guidance states "Only the configured OpenAI account may be used" (`openai/manifest.py:44-47`), which the code does not enforce; the Claude integration avoids the asymmetry by hashing and pinning the bearer credential itself (`claude/guard.py:68-83`). Whether a foreign credential is accepted alongside a mismatched account id is upstream behaviour that cannot be established from this repository — plausible for a platform `sk-` key, for which the header is not an authentication input, and unlikely for a ChatGPT OAuth token — so the defect is recorded as the missing local binding. Pin the credential rather than the routing header, or strip and re-inject it proxy-side as the GitHub guard does. | Fixed — the OpenAI account pin now binds the Authorization bearer to the pinned account by reading the token's own chatgpt_account_id JWT claim locally (no signature check needed); a foreign, absent, duplicated, non-Bearer, or non-JWT credential fails closed with openai_token_account_mismatch. |

## Threat model

- **Adversary:** malicious agent code as `kern-agent` that can emit
  arbitrary bytes at the proxy port — malformed HTTP, protocol smuggling,
  odd encodings, WebSocket tricks — and can also drive well-formed traffic
  through the provider SDKs. Assume the agent knows the policy and the proxy
  implementation.
- **Assets:** the guarantee that every byte leaving the agent matches an
  operator-approved rule; the non-resolution of denied host names; the
  unreachability of loopback/link-local/private addresses; the OpenAI and
  Anthropic account pins.
- **Out of scope:** cryptographic attacks on the TLS protocol itself —
  everything about *how the proxy verifies* the upstream (certificates,
  hostnames, trust anchors) stays in scope; Ubuntu/kernel bugs. Whether the
  *policy an operator wrote* is wise is axis 07's problem — here the policy
  as stored is the spec.

## Minimal scope checklist

This checklist is not comprehensive: it names known-important areas, but the
audit question and threat model define the scope. Account for each item in
your coverage section, and report anything else within scope even if no item
below names it.

1. Validate the integration registry and every manifest/config parser:
   OpenAI, Claude, Bedrock, GitHub, Python packages, npm packages, and custom
   domains. Check unique ids and denial codes, disjoint owned apexes,
   managed-domain ownership under broad wildcards, disabled/omitted entries,
   extra fields, and malformed or unavailable database state.
2. Audit the proxy protocol boundary: HTTPS/WSS through CONNECT only, port
   443 only, plain HTTP/WS denial before body or DNS work, origin-form inner
   targets, one request per tunnel, and agreement among CONNECT authority,
   `Host`, SNI, policy owner, and upstream destination. Include duplicate
   headers, IPv6 authorities, userinfo, ports, absolute-form targets, header
   folding, CL/TE ambiguity, chunking, pipelining, and malformed UTF-8/bytes.
3. Verify domain, method, normalized path, and query matching exactly reflects
   typed policy: exact versus longest wildcard, apex exclusion, case and
   trailing dots, IP literals, percent/double encoding, dot segments, Unicode,
   regex anchoring, empty method lists, and the shared outbound parameter
   guard over every effective URL value.
4. For every denial and exception path, prove no certificate generation, DNS,
   upstream socket, credential rewrite, gate side effect, or forwarded byte
   occurs earlier than intended. Policy/credential reads and decision logging
   must fail closed under invalid state, PostgreSQL outage, races, disconnects,
   and internal exceptions; denials must close rather than reuse the tunnel.
5. Audit DNS and upstream TLS: all resolved answers must be public before
   connecting to a vetted address; cover rebinding, mixed public/private
   answers, IPv4-mapped IPv6, link-local/metadata/loopback ranges, dual-stack
   ordering, resolution failures, certificate chain/hostname/SNI validation,
   and the absence of proxy-followed redirects.
6. Exercise every integration-specific guard. Include OpenAI account pinning,
   cached-search-only and HTTP/WebSocket hosted-tool/remote-MCP denial; Claude
   token anchoring, narrow pre-pin reads, web-search option, server-tool and
   remote-MCP denial; Bedrock region/model routes, dummy SigV4 identity,
   query/session credential denial, race-safe re-signing and usage metering;
   GitHub read/write repository scoping, GraphQL/admin/LFS restrictions,
   credential stripping/injection and `.github` push quarantine/approval; and
   package-registry name/download plus custom-domain rules.
7. Test body inspection across content types, gzip/zlib/zstd/brotli, invalid or
   oversized encodings, JSON ambiguity, nested tool declarations, renamed
   hosted tools, and values split across structures. A body the relevant
   upstream could interpret as privileged must not bypass a failed decoder or
   parser.
8. Audit WebSockets at handshake and per message: upgrade/header validation,
   masking, RSV/extensions, control frames, fragmentation, interleaving,
   compressed/uninspectable traffic, initial pipelined frames, message and
   buffer caps, close behavior, and which integrations may tunnel opaquely
   after request-only checks.
9. Check secrets and mutable state around enforcement: provider account
   anchors, Bedrock and GitHub credentials, token refresh/replacement races,
   agent-supplied authorization/header stripping, real credential injection,
   push-gate objects, and error/event output. The agent must neither select nor
   recover operator credentials.
10. Test overload and lifecycle failures—connection/body caps, slowloris,
    memory pressure, certificate-cache failure, push-gate/quarantine failure,
    proxy/database restart, and concurrent policy replacement—and prove none
    fail open.
11. Verify the nftables backstop and live host behavior: only `kern-agent` can
    reach the proxy, agent DNS/direct egress and other loopback ports are
    denied, preview-port exceptions cannot become egress, proxy DNS/80/443
    access is scoped to its uid, and deployment probes catch missing or
    reordered rules.

## Collaborative review

### `6151eea5abb61590684c4cf667ae6f619d705231`

Reviewed by: gpt-5.6-sol; Claude Opus 5

Methodology: repository-level, pre-DNS-to-upstream trace of the proxy protocol,
typed integration registry, policy matcher, all integration guards/hooks,
credential state, WebSocket inspection, and nftables backstop. Parser and
guard edge cases were checked with focused unit tests and direct pure-function
calls; no live internet destination, deployed firewall, or extended fuzzing
was used. A second pass read `network_proxy/service.py` end to end as a single
connection lifecycle rather than per-guard, which is what surfaced the two
defects that live in the gap between what the policy layer inspects and what
the socket layer actually transmits (NET-004, NET-005), and traced each guard's
pinned value back to what it binds rather than only how it compares (NET-006).
Guard behaviour was confirmed by calling `request_denied` directly with
constructed header sets, including the duplicate `Content-Encoding` form that
independently reproduced NET-002.

#### What was reviewed

- `host/runtime/network_proxy/service.py`: CONNECT/TLS interposition, request
  head/body/chunk parsing, host/authority agreement, body and connection
  bounds, deny ordering, public-address resolution, upstream TLS/SNI,
  credential/header rewrites, single-request forwarding, response metering,
  WebSocket frame inspection, and bounded handler admission.
- The integration registry/manifests and every guard for OpenAI, Claude,
  Bedrock, GitHub, Python packages, npm packages, and custom domains,
  including GitHub credential injection/push quarantine and the Azure Actions
  download exception.
- `host/config.py`, `host/runtime/core/network_policy.py`, the shared outbound
  parameter guard, provider/GitHub credential and account anchors, policy/
  decision persistence, database grants, and Admin UI typed controls.
- Bootstrap nftables rules and deploy verification, plus parser, policy,
  provider, GitHub, package, parameter-guard, proxy, and migration tests.
  Direct calls reproduced both duplicate-encoding bypasses and an allowed
  attacker-controlled Azure Blob account without making network traffic.

#### Coverage and confidence

- Checklist 1: manifest ids, denial codes, apex ownership/disjointness,
  strict config parsing, disabled integration behavior, and custom-versus-
  managed precedence were enumerated. NET-003 is the one over-broad owned
  surface: signature syntax does not establish GitHub provenance.
- Checklists 2–3: CONNECT/Host/SNI/destination agreement, HTTPS/443-only
  routing, absolute/origin forms, header/body grammar, CL/TE handling, path
  normalization, wildcard/apex precedence, methods, queries, encodings, and
  parameter guards were traced. CL/TE is normalized before forwarding, but
  duplicate content semantics are not, yielding NET-002. The request-target
  itself is likewise not normalized before forwarding: policy, the parameter
  guard, and the event log all read the `urlsplit`-parsed path and query while
  the raw target goes on the wire, so fragment and `//authority` bytes are
  inspected by nothing (NET-005). Worth recording as a bounding fact for that
  finding: arbitrary agent-chosen request headers on the same allowed request
  are also forwarded verbatim and absent from `network_events`
  (`send_http_request` strips only six hop-by-hop names), so NET-005 is a
  guard-fidelity defect, not a new egress capability.
- Checklists 4–5: policy/credential/database/logging failures deny before
  certificate/DNS/upstream work; all resolved addresses must be public and
  the chosen address is pinned through verified TLS. The proxy does not
  follow redirects. Mixed-address, mapped/private ranges, resolution and
  certificate failures were covered in code/tests, not against live DNS.
- Checklists 6–7: every provider/package/GitHub guard and supported body
  decoder was reviewed, including account anchors, server tools, Bedrock
  signing/metering, repo-scoped writes, `.github` approval, and package-name/
  download restrictions. Canonical gzip and malformed/oversized bodies fail
  closed; duplicate content headers are NET-002.
- Checklist 8: WebSocket handshake headers, extension removal, masking, RSV,
  fragmentation, control frames, message caps, close behavior, and which
  integrations require message inspection were traced. Opaque tunneling
  begins where no message-dependent guard applies — and a second pass
  established that entering that mode is decided by the *client's* request
  headers alone, with the upstream's `101` never checked, which is NET-004 and
  the most serious defect on this axis. `ws_message_guard` returns `None` for
  every integration except OpenAI's two hosted domains, so the opaque path is
  reachable on custom, Claude, Bedrock, GitHub, PyPI, and npm hosts. The
  frame-level guard itself (masking required, RSV/extensions denied,
  fragmentation and per-message caps) is sound where it runs; the defect is
  which connections reach it.
- Checklist 9: provider, Bedrock, and GitHub secrets are stripped/injected or
  re-signed after policy approval and are not returned to the agent.
  Operator-anchored provider pins now keep NET-001 fixed; mutable credential/
  push-gate state was included in race and failure review. Tracing what each
  pinned value binds — rather than only that it is compared correctly —
  distinguishes the two provider guards: Claude hashes and pins the bearer
  credential itself, while OpenAI pins only the advisory `chatgpt-account-id`
  routing header and leaves `Authorization` untouched on those hosts
  (NET-006). GitHub and Bedrock are the only integrations registering
  `rewrite_request_headers`.
- Checklists 10–11: connection/body caps, slow reads, cert/quarantine errors,
  policy replacement, restart fail-closed behavior, uid-scoped DNS/egress,
  direct-loopback denial, and preview-port rules were reviewed. Durable and
  aggregate resource failures are recorded under axis 08. No live proxy fuzz,
  load test, DNS-rebinding service, or deployed nftables probe was run, so
  confidence is high for deterministic guard logic and medium for unusual
  upstream/parser interpretations beyond the reproduced duplicate-header case.
