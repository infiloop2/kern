# Audit: Public Exposure of the Admin UI and API

Finding ID prefix: `ADM`. See [README.md](README.md) for the sweep process,
finding format, and severity scale.

## Audit question

Can Kern expose the admin UI and API through public Cloudflare access while
keeping the SSH-localhost exception contained? Internet, cross-site, and
untrusted local callers must not bypass HTTPS, login, session, CSRF, route, or
firewall controls—or make the admin service unavailable.

## Reviewed commits

Latest reviewed commit: `6151eea5abb61590684c4cf667ae6f619d705231`.

| Commit | Reviewed by |
| --- | --- |
| `6151eea5abb61590684c4cf667ae6f619d705231` | gpt-5.6-sol; Claude Opus 5 |

## Findings

| Finding | Severity | Found at | Found by | Description | Resolution |
| --- | --- | --- | --- | --- | --- |
| ADM-004 | High | `309396bd8b3e` | Claude Opus 5 | `create_app_backend_admin_server()` publishes `/run/kern-admin-api/app-backend.sock` with `chmod(0o666)` inside a `RuntimeDirectoryMode=0755` directory, so every local uid — including `kern-agent`, `kern-tools`, `kern-proxy`, `cloudflared`, and app service users — can `connect()` to it (nftables governs TCP only and never sees a Unix socket). The listener is a plain `ThreadingUnixHTTPServer` with no concurrency bound, and `app_backend_api.Handler` sets no `timeout` class attribute, so the connection socket stays blocking with no deadline; `_authenticate_app_backend_id()` runs only after a full request line has been read, so a connection that sends nothing holds its handler before any authentication. Because this listener runs on a daemon thread of the kern-admin-api process itself, the binding resource is that process's file-descriptor table: `bootstrap.sh` sets no `LimitNOFILE`, so roughly a thousand silent connections exhaust the default 1024 soft limit, `socketserver` swallows the resulting `EMFILE` from `accept()`, and the operator-facing TCP admin API on `127.0.0.1:7443` — same process, same fd table — stops accepting while both accept loops busy-spin on a permanently-readable listener. The omission is isolated rather than deliberate: the TCP listener sets `timeout = REQUEST_TIMEOUT_SECONDS` and wraps itself in a 32-slot `BoundedThreadingHTTPServer` precisely so "a connection flood or slow client cannot exhaust host threads" (`docs/architecture/admin-api.md:106`), and all three sibling Unix-socket services set `timeout = REQUEST_READ_TIMEOUT_SECONDS`. Set `timeout` on the handler, apply the bounded-worker pattern, set an explicit `LimitNOFILE`, and tighten the socket to a group containing only app service accounts instead of mode 0666. | Fixed — `app-backend.sock` is restricted to the provisioned app group, peer credentials bind requests to an exact app, and the shared server has bounded workers, an idle timeout, and a raised fd limit. |
| ADM-001 | Medium | `f28b50e87b61` | Claude Opus 4.8; GPT-5.5 | The long-lived admin credential cookie lacked `Secure` on the public HTTPS path, so a later plain-HTTP request to the same hostname could disclose it before redirect. Bind the public session to a `Secure`, host-only cookie while retaining a separate loopback-only SSH transport session where needed. | Fixed — public sessions use a Secure host-only cookie while loopback SSH transport uses a separate local session. |
| ADM-005 | Medium | `20b5713338c8` | Claude Opus 5 | `Handler._handle()` dispatches `POST /v1/login` before `_authenticate()`, and `_handle_login()` performs no same-origin check: no `X-Kern-Csrf` header requirement, no `Origin`/`Referer` inspection, no `Content-Type` check. `admin_auth.begin_password_login()` calls `register_attempt(client_key)` before the lazy `password_loader()` runs, so a body-less POST still consumes one of the source's ten attempts, and the public throttle bucket is keyed on the Cloudflare-supplied client IP — the *browser's* egress address — so attempts driven from a victim's browser are charged to the victim. Any page the operator visits can therefore run `fetch(url, {method: 'POST', mode: 'no-cors'})`, which needs no custom header and no non-simple content type and so triggers no CORS preflight; on the 11th request the source is blocked and even a correct password is refused with 429 (reproduced at this commit: ten empty POSTs produced a `cf4:` bucket at count 10, after which the correct password returned 429). `register_attempt` does not extend `window_start` while blocked, so each block lasts at most fifteen minutes from the first attempt in its window and a hostile tab must re-flood after each rollover — which it can. This contradicts the recovery argument the architecture doc relies on, "blocking the real operator requires flooding their exact egress IP" (`docs/architecture/admin-api.md:92`), and is asymmetric with the second factor: `complete_passkey_login()` already requires the CSRF header (`admin_auth.py:516-517`) while the password step does not. Require the same `X-Kern-Csrf` header on `POST /v1/login` and/or charge the throttle only for a syntactically valid login body; note the fix needs a paired UI change, since `admin_ui/api.js:45-52`'s `login()` does not currently send that header. | Fixed — the login throttle is charged only for a syntactically valid login body (password_loader runs first and a malformed body raises InvalidPassword before register_attempt), so a cross-site page's bodiless no-cors POSTs can no longer exhaust the operator's IP-keyed attempt budget; a valid-shaped wrong password still counts. |
| ADM-002 | Low | `f28b50e87b61` | GPT-5.5 | Provider login links opened a new tab without an explicit `rel="noopener noreferrer"`. An older or embedded browser could let a compromised external provider page navigate the authenticated admin tab to a phishing page. Set the relationship explicitly. | Fixed — both provider login-link anchors now carry rel="noopener noreferrer", matching connection_guide.js. |
| ADM-003 | Low | `f0470b7c7487` | gpt-5.6-sol | If a client closes the connection while the Admin API writes a response, normal `BrokenPipeError`/`ConnectionResetError` conditions fall into `Handler._handle`'s unexpected-exception branch. The service then invokes the structured host-error reporter, which spawns `/usr/bin/logger` with a two-second bound, and attempts a second JSON write to the closed socket. Repeated unauthenticated disconnects can therefore create misleading host-error/journal noise and avoidable subprocess/worker work on the 32-slot listener. Treat client disconnects as expected transport termination, close without reporting or replying again, and add disconnect/concurrency regression coverage. | Fixed — Handler._handle catches BrokenPipeError/ConnectionResetError as expected transport termination, closing without invoking the host-error reporter or writing to the dead socket again; a regression test drives the handler over a closed socketpair. |
| ADM-006 | Low | `20b5713338c8` | Claude Opus 5 | `_send_https_redirect()` builds `Location: f"https://{hostname}{self.path}"` from the stored Cloudflare hostname concatenated with the raw request target, and `BaseHTTPRequestHandler.parse_request` does not require that target to be origin-form. CPython collapses a leading `//` to `/`, neutralizing protocol-relative targets, but a target beginning with `@` is untouched and turns the fixed hostname into a userinfo component: verified at this commit, `GET @evil.com/ HTTP/1.1` with `Host: admin.example.com` and `X-Forwarded-Proto: http` returns `301 Location: https://admin.example.com@evil.com/`, which resolves to `evil.com`. This is a hardening gap rather than an exploitable open redirect, and the distinction matters: the payload lives in the request line, not in a URL, and no browser emits a request target that does not begin with `/` (`https://host/@evil.com` is sent as `GET /@evil.com`, yielding a same-origin `Location`), so the party receiving the 301 is always the party that crafted the malformed target. It is a deviation from the stated design — `docs/architecture/admin-api-authentication.md` says "the redirect target is built from that stored hostname, never an untrusted `Host` value", validating the authority while neither the doc nor the code says anything about the path. Refuse to redirect unless `self.path` starts with `/`, falling back to `/`; a one-line guard. | Fixed — _send_https_redirect builds Location only from an origin-form target (self.path when it starts with '/', else '/'), so a crafted 'GET @evil.com/' request line can no longer turn the stored hostname into a userinfo component. |

## Threat model

- **Adversaries:** (a) arbitrary unauthenticated internet clients, including
  brute-force, credential-stuffing, connection-flood, slow-client, malformed
  HTTP, and route-discovery attackers; (b) any third-party website open in the
  operator's browser attempting CSRF, CORS abuse, login CSRF, clickjacking, or
  credential-origin confusion; (c) untrusted local users and compromised
  agent, app, tool, proxy, or other service processes trying the loopback
  listener or forging Cloudflare headers; and (d) a network observer on any
  cleartext path.
- **Assets:** the admin password and sessions; every admin API read/mutation;
  provider, tool, operator-access, and app credentials; agent files and
  history; approval and policy integrity; source-aware login throttling; and
  availability of the admin login/API.
- **Out of scope:** attacker-controlled agent output becoming browser code or
  a link after an authenticated response (axis 03 owns that); execution inside
  sandboxed app frames (axis 05); Cloudflare, TLS, browser, kernel, or OpenSSH
  implementation vulnerabilities rather than Kern configuration/use of them;
  compromise of the operator machine.

## Minimal scope checklist

This checklist is not comprehensive. The audit question and threat model are
binding; report any in-scope defect even if no item names it.

1. Enumerate every entry into admin routing: `127.0.0.1:7443` through
   Cloudflare, SSH forwarding, root/admin self-calls, and the separate
   app-backend Unix socket. Verify listener binding, IPv4/IPv6 behavior,
   socket ownership, peer uid mapping, nftables order/persistence,
   established connections, reboot ordering, and absence of alternate ports.
2. Audit the one request-classification point. Prove only `cloudflared` can
   supply tunnel transport/client identity; test missing, duplicate,
   case-varied, comma-joined, conflicting, malformed, and spoofed `Host`,
   `X-Forwarded-Proto`, `Cf-Connecting-Ip`, and `Cf-Connecting-IPv6`, including
   Pseudo IPv4 and requests from every other local uid.
3. Trace every method and route over public HTTPS, public cleartext, and
   SSH-local HTTP. Public cleartext must reject before reading credentials or
   serving secrets; HSTS, redirects/refusals, WebAuthn origin, and cookie
   security must derive only from trusted transport context. Local HTTP must
   neither mint nor accept public-session or passkey-preauthentication state.
4. Enumerate the exact pre-session surface: shell/static/favicon assets,
   password login, passkey login completion and status, and OAuth callback
   shell. Verify transport restrictions, methods, bodies, database reads,
   cache headers, response fields, errors, and that no user state, credential,
   stack trace, or side effect leaks before its required proof.
5. Audit password input and comparison: cleartext lifetime/storage/logging,
   constant-time hash comparison, JSON/UTF-8/content-type/body limits,
   duplicate/unknown fields, generated-password strength, response
   equivalence, source accounting before comparison, and concurrent attempts.
6. Audit WebAuthn enrollment and login completely: public-HTTPS-only
   availability, password as factor one, pre-auth cookie binding, challenge
   entropy/expiry/single use, source and session binding, configured RP ID and
   exact origin, credential/user ids, allowed algorithms, client-data type and
   challenge, authenticator RP hash/flags, user presence/verification,
   signature verification, sign-counter monotonicity, duplicate credentials,
   malformed encodings/keys, and cryptographic subprocess failure.
7. Audit passkey lifecycle and recovery: first and concurrent enrollment,
   listing/naming/removal, last-passkey behavior, stale ceremonies, hostname
   or operator-transport changes, SSH route concealment, logout/session
   interaction, restart persistence, and `reconfigure --reset-admin-passkeys`
   deleting passkeys without silently changing other credentials.
8. Audit password throttling and availability: trusted source-key provenance,
   IPv4/IPv6 bucketing, limits/windows, races, cleanup/memory bounds, reset on
   success, blocked correct passwords, passkey failure interaction, slow
   clients, disconnects, read/write timeouts, handler caps, keep-alive/
   pipelining, and whether one attacker can lock out unrelated operators.
9. Audit sessions end to end: token entropy, server-side storage, cookie names
   and parser ambiguity, `HttpOnly`, `Secure`, `SameSite`, `Path`, no
   `Domain`, public/local namespace separation, idle and absolute expiry,
   which foreground actions refresh activity, background polling, logout,
   replay, restart/upgrade invalidation, concurrent cleanup, and count bounds.
10. Build a method/path/principal matrix for browser session, public
    pre-authentication, and app-backend principals. Authentication and app
    scope must precede shared dispatch; alternate encodings, `HEAD`/`OPTIONS`,
    static/app prefix confusion, forged app markers, duplicate credentials,
    reverse-proxy errors, and exceptions must not reach a stronger route.
11. Verify every session-authenticated mutation requires the exact same-origin
    CSRF signal. Cover forms, text/plain/multipart, preflight/OPTIONS, CORS,
    redirects, login/logout CSRF, streaming/WebSocket endpoints, Origin/
    Referer assumptions, and SameSite edge cases.
12. Trace Admin-to-app UI/API proxying: authenticate before installed-app
    lookup, strip cookies/password/authorization and hop-by-hop headers, add
    only the host marker, confine method/path/body/response, keep app errors
    from changing authentication behavior, and deny direct app-port access.
13. Audit every public OAuth/provider-login transition: authorization URL
    construction, state/callback binding, query and fragment handling,
    callback error reflection, opener/referrer isolation, and whether an
    external identity page can navigate or act on the authenticated admin UI.
14. Verify browser-facing containment against outside origins:
    `frame-ancestors`/`X-Frame-Options`, CSP, referrer policy, MIME sniffing,
    credential-free URLs, cache-control, service-worker scope and cache behavior, and
    consistent headers on success, redirects, static content, JSON, streams,
    and errors. Axis 03 owns agent data rendered after authentication.
15. Run black-box and browser checks from public HTTPS/HTTP, SSH localhost,
    agent/app/tool/proxy uids, the app Unix socket, spoofed tunnel headers, and
    cross-origin pages. Include parallel bad logins/passkeys, slow/oversized
    bodies, mixed cookies, expired/replayed ceremonies, logout/restart, route
    enumeration, and deployment/reconfigure recovery.

## Collaborative review

### `6151eea5abb61590684c4cf667ae6f619d705231`

Reviewed by: gpt-5.6-sol; Claude Opus 5

Methodology: repository-level transport/principal matrix and route audit of
the public Cloudflare, SSH-local, browser-session, and app-backend paths.
Bootstrap firewall/service generation, request classification, password,
passkey, session, CSRF, OAuth, app proxy, and browser containment code were
traced with their focused unit/socket tests. A second pass compared each
listener against its siblings rather than against its own documentation —
which is how the app-backend socket's missing read timeout and worker bound
surfaced (ADM-004) — and drove the pre-session surface directly to see what a
cross-site page can reach and what it costs the operator (ADM-005). Several
behaviours were reproduced locally against the real code rather than reasoned
about: the throttle lockout after ten body-less `POST /v1/login` requests
followed by a 429 on the correct password, and the `@`-prefixed request target
producing `Location: https://admin.example.com@evil.com/`. No live Cloudflare
endpoint, hardware authenticator, cross-origin browser, or deployed
uid/firewall probe was used.

#### What was reviewed

- `host/constants.py`, bootstrap/systemd/nftables generation and deployment
  verification: the `127.0.0.1:7443` listener, permitted local principals,
  Cloudflare entry, loopback ordering, and the separate app-backend socket.
- `host/runtime/admin_api/service.py`, `admin_auth.py`,
  `admin_passkeys.py`, passkey storage/reset, and the browser auth/passkey
  clients: transport classification, cleartext handling, password login and
  throttling, sessions/cookies, CSRF, WebAuthn ceremonies, recovery, routes,
  body/time/concurrency bounds, and error handling.
- `app_backend_api.py`, `app_api_proxy.py`, tools operator/OAuth delegation,
  provider login transitions, browser security headers, and the route/
  principal matrix for pre-session, public/local sessions, app backends,
  installed-app proxying, unsupported methods, and malformed headers.
- Focused tests completed successfully: 31 admin-auth tests, 7 passkey tests,
  and 195 admin-API tests. The API suite also exercised response disconnects
  and emitted the double-`BrokenPipeError` traces recorded as ADM-003.

#### Coverage and confidence

- Checklists 1–4: the Admin API is IPv4 loopback-only; nftables admits the
  expected root/admin/operator/cloudflared uids before rejecting other local
  callers. One request classifier makes absent forwarded-proto local and
  requires an exact configured host and exact HTTP/HTTPS semantics whenever
  the header is present. Duplicate/malformed transport and client-IP headers
  fail closed. Public cleartext rejects or redirects before credentials/data;
  the fixed shell/assets, password login, HTTPS-only passkey endpoints, and
  OAuth callback shell are the complete pre-session surface.
- Checklists 5–8: password shape/size/hash comparison, generated-secret
  strength, trusted source throttling, 30-second reads, and the 32-handler cap
  were checked. WebAuthn uses bounded single-use, source/session-bound
  ceremonies, exact RP/origin/type/challenge validation, UV/UP and signature/
  counter checks, and fail-closed crypto subprocesses. Full reset preserves
  password and SSH. Expected client disconnects are mishandled as ADM-003.
  Two qualifications on the availability side of checklist 8. The 30-second
  read timeout and 32-slot cap protect the TCP listener only; the app-backend
  Unix socket in the same process has neither, and its practical limit is the
  shared file-descriptor table rather than threads, so exhausting it also stops
  the operator listener accepting (ADM-004). And the throttle's own
  source-attribution property is what makes it abusable: because the public
  bucket keys on the browser's egress IP and an attempt is registered before
  the password is even loaded, a third-party page can lock out the operator
  without guessing anything (ADM-005), which is the concrete answer to that
  checklist's "whether one attacker can lock out unrelated operators".
- Checklists 9–11: opaque hashed sessions have public/local cookie namespace
  separation, secure host-only public attributes, idle/absolute limits,
  bounded count, duplicate-cookie rejection, logout/restart revocation, and
  interaction-only idle refresh. Authentication/app scope precede dispatch,
  and every session request carries the same nonempty custom CSRF header; no
  CORS/OPTIONS or form route weakens that requirement. ADM-001 remains
  verified fixed. The requirement holds for session-authenticated mutations
  but stops at the session boundary: `POST /v1/login` is dispatched before
  `_authenticate()` and demands no CSRF header, `Origin`, `Referer`, or
  `Content-Type`, so it is reachable by a simple cross-site `fetch` with no
  preflight (ADM-005). `complete_passkey_login()` does require the header, so
  the two login factors are inconsistent with each other.
- Checklists 12–14: app proxy requests are authenticated, confined to an
  installed fixed loopback target, bounded, and stripped to trusted marker/
  content headers. OAuth state completion remains authenticated and
  CSRF-protected and callback queries are scrubbed. CSP, framing, referrer,
  no-store, and `nosniff` coverage was checked; ADM-002 remains open for two
  provider links without explicit opener isolation.
- Checklist 15: focused source/unit/socket coverage includes spoofed and
  duplicate headers, transport/cookie matrices, throttle races, malformed and
  oversized bodies, passkey replay/origin/counter/reset, app-peer/bridge
  scoping, and file security. No live Cloudflare, real nftables uid matrix,
  cross-origin browser, hardware passkey, restart/reconfigure, or sustained
  slow/flood exercise was run. Confidence is high for source and unit-level
  route/auth controls and medium for deployed edge behavior.
