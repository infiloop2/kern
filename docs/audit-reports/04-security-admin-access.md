# Audit: Public Exposure of the Admin UI and API

Finding ID prefix: `ADM`. See [README.md](README.md) for the sweep process,
finding format, and severity scale.

## Audit question

Can Kern expose the admin UI and API through public Cloudflare access while
keeping the SSH-localhost exception contained? Internet, cross-site, and
untrusted local callers must not bypass HTTPS, login, session, CSRF, route, or
firewall controls—or make the admin service unavailable.

## Reviewed commits

Latest reviewed commit: none.

| Commit | Reviewed by |
| --- | --- |
| _None yet_ | _No completed review_ |

The historical partial reviews under **Collaborative review** predate this
axis and do not cover its complete public/SSH exposure question, so they are
deliberately not listed as completed commit reviews.

## Findings

| Finding | Severity | Found at | Found by | Description | Resolution |
| --- | --- | --- | --- | --- | --- |
| ADM-001 | Medium | `f28b50e87b61` | Claude Opus 4.8; GPT-5.5 | The long-lived admin credential cookie lacked `Secure` on the public HTTPS path, so a later plain-HTTP request to the same hostname could disclose it before redirect. Bind the public session to a `Secure`, host-only cookie while retaining a separate loopback-only SSH transport session where needed. | Fixed at `fa6dc4ab5bc` |
| ADM-002 | Low | `f28b50e87b61` | GPT-5.5 | Provider login links opened a new tab without an explicit `rel="noopener noreferrer"`. An older or embedded browser could let a compromised external provider page navigate the authenticated admin tab to a phishing page. Set the relationship explicitly. | Open |

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
    credential-free URLs, cache-control, service-worker absence, and
    consistent headers on success, redirects, static content, JSON, streams,
    and errors. Axis 03 owns agent data rendered after authentication.
15. Run black-box and browser checks from public HTTPS/HTTP, SSH localhost,
    agent/app/tool/proxy uids, the app Unix socket, spoofed tunnel headers, and
    cross-origin pages. Include parallel bad logins/passkeys, slow/oversized
    bodies, mixed cookies, expired/replayed ceremonies, logout/restart, route
    enumeration, and deployment/reconfigure recovery.

## Collaborative review

These are historical partial sweeps inherited from the former combined
browser/authentication axis. They support ADM-001 but do not mark a commit as
fully reviewed against the broader public-exposure checklist above.

### `f28b50e87b61` — partial

Contributors: Claude Opus 4.8 (claude-opus-4-8); GPT-5.5 (gpt-5.5)

Methodology: static reading and grep sweeps of the served UI and API authentication/header
handling. No live public endpoint, browser-driven CSRF test, login load test,
or local-uid reachability test.

#### What was reviewed

- The then-current authentication path, credential-cookie behavior, security
  headers, cache headers, and static asset serving.
- Whether cross-site requests could supply the accepted credential under the
  then-current header-auth design.
- Cookie transport attributes, which surfaced ADM-001.
- External provider new-tab isolation, which surfaced ADM-002.

#### Coverage and confidence

The sweep checked cookie flags, the accepted authentication mechanism, lack
of CORS authorization, and framing headers at source level. It did not cover
the present standalone login/session design, Cloudflare source attribution,
brute-force/connection bounds, nftables caller restrictions, or the complete
public-versus-SSH route matrix.
