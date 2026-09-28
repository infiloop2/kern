# Audit: Network Proxy Policy Enforcement

Finding ID prefix: `NET`. See [README.md](README.md) for the sweep process,
finding format, and severity scale.

## Audit question

Can an agent send any internet traffic the active policy does not intend to
allow?

## Reviewed commits

Latest reviewed commit: `a597a8d063d735d11c30d1f2b2f6b66e7479ceac`.

| Commit | Reviewed by |
| --- | --- |
| `a597a8d063d735d11c30d1f2b2f6b66e7479ceac` | GPT-6 (Codex) |

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

### `a597a8d063d735d11c30d1f2b2f6b66e7479ceac`

Reviewed by: GPT-6 (Codex)

Methodology: traced the production integration registry, proxy parser,
policy decision, credential rewrite, and forwarding order at the pinned current
main commit. Ran the complete credential-free Python suite, including the
proxy's isolated HTTP/TLS/WebSocket tests and integration-specific negative
cases. No real provider request or policy-bypass traffic was sent.

#### What was reviewed

- `host/network_integrations/{registry,runtime,base}.py`, config/manifest
  parsing, and all eight guards: OpenAI, Claude, xAI, Bedrock, GitHub,
  Python packages, npm packages, and custom domains.
- `host/runtime/network_proxy/service.py` and
  `host/runtime/core/network_policy.py`: CONNECT ownership, strict inner
  request targets, semantic headers, body framing and decompression, policy
  matching, public-address resolution, verified upstream TLS, response
  framing, WebSocket upgrade/message handling, logging, and capacity bounds.
- OpenAI bearer/account binding and cached-search controls; Claude provider
  attestation, refresh cache, bootstrap reads and hosted-tool controls; xAI
  principal binding and hosted-tool restrictions; Bedrock region/model
  selection and dummy-to-real SigV4 rewriting.
- GitHub repository rules, REST administrative exclusions, GraphQL/LFS
  restrictions, bounded Actions storage hosts, credential injection, and
  `.github` push quarantine/approval with exact old-tip leases. Also checked
  package name/download rules, the narrow npm exception, and custom domains.

#### Outcome and coverage

No materially new policy bypass was confirmed. Existing NET findings were
left unchanged. The source still contains the relevant remediations: strict
origin-form targets, duplicate semantic-header rejection, explicit WebSocket
opt-in with verified `101` and frame inspection, and credential/account pins.

- Checklist 1: reviewed registry uniqueness, owned domains, disabled entries,
  typed configuration and failure on invalid/unavailable policy. Broad custom
  wildcards do not take ownership of managed domains; xAI is included even
  though the older checklist does not name it.
- Checklists 2–4: traced raw authority/target/header/body parsing through
  decision and forwarding, and inspected malformed/pipelined/duplicate-header
  negative tests. Denied hosts are rejected before DNS; request-level
  decisions precede upstream connection and credential injection. TLS
  inspection necessarily follows CONNECT host admission. Provider identity
  attestation is a deliberate bounded guard operation, not payload forwarding.
- Checklist 5: all DNS answers are checked for public addresses, then the
  selected address is connected directly while preserving certificate
  hostname/SNI verification. Redirect responses return to the caller; the
  proxy does not follow them. Local fixtures cover address and TLS failures;
  no public DNS-rebinding or external-ingress trial was run.
- Checklists 6–7: exercised integration guard tests for pins, hosted tools,
  remote MCP, fixed route/region identity, repository scope, and decoded
  parameters. Gzip/zlib are bounded; unsupported or invalid encodings fail
  body inspection. Source handling of JSON, nested declarations and replayed
  history was inspected. Unknown future provider parser/tool semantics and
  duplicate-JSON-key interpretation were not verified against live providers.
- Checklist 8: reviewed handshake response verification, masked frames,
  control/fragment rules, RSV rejection, pipelined initial data, message caps,
  and explicit integration opt-in. Existing WebSocket regressions ran in the
  full suite; arbitrary real-world WebSocket servers were not exercised.
- Checklist 9: reviewed operator-owned account anchors, provider attestation,
  credential stripping/injection, Bedrock replacement rechecks, quarantine
  ownership, and secret-free denial records. No credential was printed or
  tested against a third-party account.
- Checklist 10: source and test coverage includes admission/body budgets,
  read deadlines, failures while loading/logging policy, malformed bodies,
  TLS errors, and push-gate failures. Sustained floods, real database/proxy
  restarts, certificate-cache disk exhaustion, and concurrent live policy
  replacement were not run.
- Checklist 11: inspected generated nftables and deployment assertions for
  agent DNS/egress denial, proxy access, preview isolation, and established
  flows. Actual per-uid and post-reboot probes require a disposable deployed
  host and remain unperformed in this sweep.

Custom-domain header/body disclosure follows the operator's configured rule;
this audit does not claim those bytes are content-scanned. Registry/CDN and
provider-returned URL trust also remain explicit integration contracts. The
parameter guard is a heuristic, not a proof against every possible encoding.
The baseline suite passed 2,978 tests (608 database-dependent skips). Confidence
is high in the exercised local decisions and lower in live deployment and
provider behavior outside the fixtures.
