# Audit: Network Proxy Policy Enforcement

Finding ID prefix: `NET`. See [README.md](README.md) for the sweep process,
finding format, and severity scale.

## Audit question

Can an agent send any internet traffic the active policy does not intend to
allow?

## Reviewed commits

Latest reviewed commit: `f28b50e87b61`.

| Commit | Reviewed by |
| --- | --- |
| `f28b50e87b61` | Claude Opus 4.8; GPT-5.5 |

## Findings

| Finding | Severity | Found at | Found by | Description | Resolution |
| --- | --- | --- | --- | --- | --- |
| NET-001 | High | `f28b50e87b61` | Claude Opus 4.8 | Managed-provider account pins were derived repeatedly from agent-writable provider credentials. A malicious agent could replace those credentials with an attacker-controlled account, wait for the pin refresh, and then exfiltrate through an otherwise allowed provider domain. Anchor pins to operator-approved account state and attest refreshed credentials against that anchor. | Fixed at `fa6dc4ab5bc` |

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

### `f28b50e87b61`

Reviewed by: Claude Opus 4.8 (claude-opus-4-8); GPT-5.5 (gpt-5.5)

Methodology: static code reading of the full proxy request lifecycle and the
policy-matching/provider-guard code; one empirical check of Python's
`ipaddress.is_global` behavior for IPv4-mapped IPv6 on the target interpreter.
No live proxy run or PoC traffic.

#### What was reviewed

- `host/runtime/network_proxy/service.py`: `do_CONNECT` + `_serve_tls_request` (TLS
  interposition), `_proxy_http` (plain HTTP/WS, removed since — the proxy is
  now HTTPS-only), `connect_public` (SSRF vet),
  `host_header_denial`/target-vs-Host consistency, `read_request_head`,
  `read_body`/`read_chunked_body` (smuggling, size caps), `send_http_request`
  (single-request pinning, header stripping), the WebSocket frame guard, and
  the `BoundedThreadingHTTPServer` connection cap.
- `host/runtime/core/network_policy.py`: `domain_matches`/`find_domain_rule`
  (wildcard precedence, apex exclusion), `decide_http_request` +
  `_normalized_path` (method/path-guard semantics), `openai_request_denied`,
  `anthropic_request_denied`, `_live_web_search_denial`, `_iter_tool_objects`,
  and the bounded gzip/zlib/zstd/brotli decoders.
- `host/config.py` `parse_network_controls`/`expand_network_controls`: method
  uppercasing, domain validation, managed-domain expansion.
- Policy and provider-pin storage, proxy database grants, network-event
  writes, the nftables output chain as the independent backstop, and related
  parser/policy/provider-guard tests.

#### Coverage details

- **SSRF / DNS rebinding / mapped-IPv6 (verified negative).** `connect_public`
  resolves once, requires *every* resolved address to be `is_global`, then
  connects to the vetted address rather than re-resolving. I specifically
  tested the IPv4-mapped-IPv6 bypass (a malicious `AAAA` of
  `::ffff:169.254.169.254` under a wildcard domain) on Python 3.10.12, the
  Ubuntu 22.04 interpreter the proxy runs under: `is_global` returns `False`
  for `::ffff:169.254.169.254`, `::ffff:127.0.0.1`, and `::ffff:10.0.0.1`, so
  the mapped-address SSRF does not apply here. (This would regress on some
  older 3.9.x/early-3.10 point releases, so it is worth re-checking if the base
  image's Python changes.)
- **Request smuggling / Host confusion.** CONNECT pins one `host` used for the
  policy check, the minted cert, `connect_public`, the upstream SNI, and the
  inner-request Host/target check; absolute-form and Host-header plain
  requests must agree via `host_header_denial`; `send_http_request` strips
  `Content-Length`/`Transfer-Encoding` and re-emits a single `Content-Length`
  with forced `Connection: close`, so no CL.TE/TE.CL desync reaches upstream.
- **Upstream TLS.** `ssl.create_default_context().wrap_socket(server_hostname=host)`
  verifies the upstream certificate chain and hostname against the CONNECT
  target, so a spoofed/look-alike upstream fails the handshake.
- **Path guards.** `_normalized_path` percent-decodes and `posixpath.normpath`
  s before `re.fullmatch`, closing `../` and `%2e%2e`/`%2f` traversal against
  a restrictive guard; the dangerous direction (guard allows but origin
  resolves elsewhere) did not materialize.
- **Provider guards.** The *matching logic* is sound — OpenAI account-id header
  required-and-matched; live web-search denied across gzip/zlib/zstd/brotli
  (bounded, fail-closed decode) and by byte-marker anti-evasion; Anthropic
  bearer-hash pin with a narrow pre-pin GET allowlist; all fail closed when the
  pin/account is unavailable. But the *pinned value itself* comes from
  agent-writable credential files, which is NET-001: the guard confines the header
  to whatever account the agent is logged into, and the agent controls that, so
  it does not confine traffic to an operator-approved account.
- **Fail-closed states.** Missing policy row, unparseable policy, and database
  outage all deny; a decision that cannot be logged fails that request.

#### Coverage and confidence

- Checklist 1–3 (parsing/matching/deny paths): covered by reading; the
  matching precedence (exact > longest wildcard) and empty-`allow_http_methods`
  handling were traced against `host_allowed`/`decide_http_request`.
- Checklist 4 (upstream): SSRF vet reproduced for the mapped-IPv6 case only;
  IPv6/dual-stack ordering and redirect handling reviewed by reading (the
  proxy does not follow redirects itself — it forwards the upstream response
  bytes, and each new agent request is policy-checked).
- Checklist 5–6 (WebSockets, provider guards): the client-frame guard (masking
  required, RSV/extension denied, fragmentation, per-message size cap, opaque
  tunnel only when inspection is not required) and both provider guards read in
  full. Tracing the *provenance* of the account pin (not just its matching
  logic) through `orchestrator.refresh_runtime_status` →
  `read-codex-account-id.sh`/`read-claude-account.sh` → `proxy_provider_pins`
  is what surfaced NET-001; an earlier draft of this report wrongly concluded the
  provider guards hold, because I checked the comparison but not the source of
  the pinned value.
- Checklist 7 (overload): no policy path fails *open* under load; the closest
  reliability concern is unbounded proxy memory (64 handlers × 128 MiB buffered
  bodies, proxy not in a memory-limited cgroup) and unbounded per-host cert
  minting under wildcards — both reported as `REL-005` and `REL-002` in
  [08-reliability.md](08-reliability.md), not as policy bypasses.
- Checklist 8 (nftables backstop): output-chain uid rules and non-root DNS drop
  confirmed in bootstrap.
- Low-confidence / not done: I did not drive live traffic or fuzz the header/
  chunk parser, and I did not exhaustively test exotic percent-encoding vs a
  real origin server's path resolution. A running-proxy fuzz of
  `read_request_head`/`read_chunked_body` and the path-guard normalizer would
  raise confidence most.
