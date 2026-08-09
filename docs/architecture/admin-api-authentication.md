# Admin API Authentication and Request Boundary

The admin service has one HTTP entrypoint on `127.0.0.1:7443`, but operators
reach it through two deliberately different access paths:

| Property | SSH-forward path | Public HTTPS path |
| --- | --- | --- |
| Network proof | TCP connection admitted from `kern-operator` through the SSH forward | TCP connection admitted from `cloudflared`; exactly one `X-Forwarded-Proto` header |
| Transport | Plain HTTP on the operator's loopback | HTTPS at Cloudflare; forwarded origin scheme must be `https` |
| Host binding | Not security-sensitive; the listener is already loopback-only | Exactly the configured Cloudflare hostname, optionally with `:443` |
| Login factors | SSH key, then admin password | Admin password, then an enrolled passkey when configured |
| Session cookie | `tc_admin_session`, non-`Secure` because the browser uses local HTTP | `__Host-tc_admin_session`, `Secure`, host-only, `Path=/` |
| Passkey ceremonies | Not exposed | Exposed and bound to the configured RP ID and origin |

The two paths share application handlers only after authentication. They do not
share a transport boolean scattered through handlers.

## One classification point

`service.py` classifies every request before serving even static assets.
`admin_auth.classify_request()` returns one immutable `RequestAuthContext`:

```text
request enters loopback HTTP server
  |
  +-- no X-Forwarded-Proto --> SSH_FORWARD
  |                            (does not load database/tunnel state)
  |
  +-- header present --------> require exactly one marker
                               require exact configured Host
                               require marker value "https"
                               --> PUBLIC_HTTPS
```

An `http` public request may redirect only after its Host matches the configured
hostname; the redirect target is built from that stored hostname, never an
untrusted `Host` value. Duplicate, unknown, or contradictory transport markers
fail closed. nftables separately ensures that only `cloudflared` can deliver a
request carrying the trusted forwarding headers to this listener.

The database hostname loader is lazy: classification never calls it for
`SSH_FORWARD`. SSH key plus admin password therefore remains a recovery path
when Postgres or tunnel configuration is unavailable.

## Auth policy ownership

`host/runtime/admin_api/admin_auth.py` owns all access-path-sensitive policy:

- request classification and exact public hostname binding;
- the explicit list of HTTPS-only authentication routes;
- password verification, source throttling, and the password-versus-passkey
  login decision;
- all pre-authentication and final-session cookie issuance and clearing;
- path-specific session cookie creation, parsing, and clearing;
- CSRF enforcement, session expiration, and operator-activity refresh;
- local socket versus Cloudflare client identity for login throttling; and
- the public RP ID/origin exposed to WebAuthn handlers.

`admin_passkeys.py` owns WebAuthn ceremonies, cryptographic verification, and
credential persistence, but cannot mint an operator session. `admin_auth.py`
calls it as the second-factor verifier and remains the only module that can
turn a successful factor sequence into a session.

`service.py` owns bounded HTTP parsing, response mapping, and route
implementation. Its login handlers pass lazy body readers to `admin_auth.py`
and transmit the returned result/cookies; they do not compare passwords, choose
factors, construct cookies, or mint sessions. It must not infer an access path
from headers after classification. Authenticated application routes receive
only a successfully authenticated operator session; they do not branch on SSH
versus public transport.

This split is intentional: adding a new HTTPS-only ceremony requires a route
entry in `HTTPS_ONLY_AUTH_ROUTES`, while adding a normal admin endpoint gets
the same session and CSRF gate on both paths automatically.

The only final-session mint is private to `admin_auth.py`. A password login
reaches it immediately for SSH-forward or for HTTPS before enrollment. Once an
HTTPS passkey is enrolled, the password path can issue only the short-lived
pre-authentication cookie; successful `admin_passkeys.finish_login()` verification
is then required before the same private mint can be reached.

## Route exposure

| Route | Before session auth | SSH forward | Public HTTPS |
| --- | --- | --- | --- |
| Static admin/workspace assets | Yes | Served | Served after exact public boundary classification |
| `POST /v1/login` | Yes | Password mints a local session | Password starts passkey assertion when enrolled; otherwise mints a public session |
| `POST /v1/login/passkey` | Yes | `404` | Completes the second factor and mints the public session |
| `GET /v1/login/status` | Yes | `404` without a database read | Returns only `{"passkey_configured": bool}` with `no-store` |
| `GET /v1/admin-passkeys` | No | `404` | Reports enrollment/setup state |
| Passkey registration routes | No | `404` | Available to an authenticated session |
| Every other admin API route | No | Same cookie + CSRF session gate | Same cookie + CSRF session gate |

`POST /v1/login/passkey` runs before session authentication because it is the
operation that finishes authentication and mints that session. It still
requires the random Secure, HttpOnly pre-authentication cookie issued only
after a correct password. Its server-side challenge is source- and
origin-bound, expires after five minutes, and is consumed by the first
assertion attempt.

The public status bit is intentionally non-secret so Kern Cloud and the login
page can report whether the second factor is enabled. It exposes no credential
identifier, public key, user handle, count, or authenticator metadata.

## Complete request paths

There are two callers of the shared Admin API dispatcher.

```text
operator browser
  -> loopback TCP listener
  -> transport classification
  -> session + CSRF authentication
  -> operator principal
  -> shared route

kern-workspace backend
  -> /run/kern-admin-api/workspace.sock
  -> socket mode/group + SO_PEERCRED fixed uid
  -> fixed thread-route allowlist
  -> workspace principal
  -> shared route
```

### Authentication precedes shared dispatch

`service.route()` is an internal Python dispatcher, not another HTTP
entrypoint and not an authentication function. Its route implementations
assume that the caller has already established an authorized principal. There
are exactly two production call sites:

| Caller | Principal established before dispatch | Call into shared code |
| --- | --- | --- |
| `service.Handler._handle()` on the operator TCP listener | `RequestAuthContext`, then the matching session cookie and CSRF header; `_authenticate()` returns `OperatorPrincipal` | `service.route(..., principal=operator)` |
| `workspace_api.Handler` on the fixed Unix socket | Kernel `SO_PEERCRED` uid matched to `kern-workspace` and the fixed method/path allowlist create `WorkspacePrincipal()` | `service.route(..., principal=workspace)` with unchanged thread ids |

The TCP handler processes static assets and the explicit pre-session
login/status operations before its session gate; those operations do not call
`service.route()`. Logout, passkey management, and streaming file operations
also have specialized handler methods, but only after the same operator
session authentication where required. Every ordinary operator route reaches
the shared dispatcher only after authentication.

The Unix-socket handler does not call `service.Handler` and does not
manufacture an operator session. It independently authenticates the fixed
Workspace process, checks its route allowlist, and only then reuses
`service.route()` for selected thread business logic. The required `principal`
argument has no default: an omitted value can never silently mean operator. A
request header sent to the TCP listener cannot substitute for Unix peer
credentials.

Consequently, adding a third production caller of `service.route()` is a
security-boundary change: that caller must establish its principal and
authorization before dispatch and needs corresponding boundary tests. Merely
knowing a `/v1/...` path never grants access to the shared function.

The authenticated browser calls `/v1/workspace/chat/...` or
`/v1/workspace/web-apps/...` through the normal TCP pipeline. The admin API
authenticates the operator cookie and CSRF header before forwarding only the
JSON body and query to path-prefixed `/chat/...` or `/apps/...` routes on the
fixed backend port 7450. No identity header or arbitrary backend target is
accepted. Fixed Workspace files under `/workspace/` are release assets, not
an API authority. The capability-worker sandbox has a separate restrictive
CSP and no network access.

The Workspace Unix-socket API is a different, non-browser boundary and does
not use `RequestAuthContext`, cookies, or the operator password. It permits
only:

| Method and path | Workspace capability |
| --- | --- |
| `GET /v1/threads` | List host threads, optionally filtered by a product-owned id prefix |
| `GET /v1/threads/:thread_id` | Read one direct thread id |
| `POST /v1/threads/:thread_id/messages` | Send or steer one direct thread id |
| `POST /v1/threads/:thread_id/stop` | Stop one direct thread id |
| `GET /v1/threads/:thread_id/events` | Read events for one direct thread id |

Thread ids pass through unchanged. Chat and Web Apps choose disjoint direct ids
(`thread-N` and `app-N`) and join/filter them inside the Workspace backend; the admin
API treats the optional list prefix as a query optimization, not authority.
The former generic-app socket also exposed `GET /v1/tools` and
`GET /v1/network/policy`. Neither fixed Workspace backend calls those routes,
so they are deliberately absent rather than retained as unused authority; the
authenticated operator API continues to serve both resources to admin UI
features that need them.
Login, passkeys, credentials, network policy, tools, files, host controls, and
audit logs remain unreachable. Adding a normal Admin API route does not expose
it to the Workspace socket.

## Operator-facing route policy

The table below is the complete authentication policy map for the
operator-facing listener. The canonical request and response schemas remain in
the [Admin API reference](../api/AdminAPI.md).

| Route group | Session policy | Additional boundary |
| --- | --- | --- |
| `GET /`, `/oauth/callback`, fixed admin modules/styles/icons, bundled guide assets, and fixed `/workspace/` assets | No session | Still classified first; fixed release files only |
| `POST /v1/login` | Pre-session | Password throttle and verification; public HTTPS starts WebAuthn when enrolled |
| `GET /v1/login/status` | Pre-session, public HTTPS only | Exact public Host; non-secret enrollment boolean only |
| `POST /v1/login/passkey` | Pre-session, public HTTPS only | Password-issued single-use pre-auth cookie plus WebAuthn verification |
| `POST /v1/logout` | Session + CSRF | Revokes only the presented session |
| `GET /v1/admin-passkeys`, `POST /v1/admin-passkeys/register/options`, `POST /v1/admin-passkeys/register` | Session + CSRF, public HTTPS only | Exact RP ID and origin from `RequestAuthContext` |
| `GET /v1/health` | Session + CSRF | No unauthenticated health exception on the admin listener |
| `GET /v1/agent-runtime/{status,account}`, runtime refresh, OAuth login/completion, Bedrock credential, and linked-account reset routes | Session + CSRF | Normal shared handler on both operator paths |
| `GET /v1/threads`, `/v1/threads/<id>`, thread message/stop/event routes, and `GET /v1/events` | Session + CSRF | Operator sees the host-wide thread namespace |
| `GET\|POST\|PUT\|DELETE /v1/workspace/{chat,web-apps}/...` | Session + CSRF | Proxy targets only the fixed Workspace backend and adds a fixed route prefix without forwarding credentials |
| `GET /v1/agent-files`, file read/content/upload routes, and `GET /v1/agent-processes` | Session + CSRF | Content and upload use bounded streaming handlers after authentication |
| `GET\|PUT /v1/network/policy`, `GET /v1/network/events`, and GitHub credential/audit/pending-push routes under `/v1/network-tools/` | Session + CSRF | Method-specific mutation validation and root-helper boundaries still apply |
| Tool catalog/config/enablement/OAuth/approval routes and tool events under `/v1/tools` | Session + CSRF | Tool-specific authorization and approval state run after operator authentication |
| `GET /v1/host-errors[/<seq>]` and `POST /v1/host-runtime/reboot` | Session + CSRF | Diagnostic read or fixed privileged helper after authentication |
| Any unlisted method/path | Not dispatched | `404`; route prefixes do not confer access by themselves |

This grouping is unchanged for external operator access. Transport-specific
behavior ends at classification, cookie selection, and the explicit HTTPS-only
authentication routes. After `Handler._authenticate()` succeeds, ordinary
operator routes use the same implementation for SSH-forward and public-HTTPS
sessions.

## Session invariants

- The admin password appears only in the login request and is never persisted
  in cleartext or replayed as an API credential.
- A session token is opaque; only its SHA-256 hash remains in process memory.
- Public and local cookie names are mutually exclusive. The parser rejects a
  duplicate expected cookie rather than trusting cookie order.
- Every cookie-authenticated request requires `X-Kern-Csrf: 1`. Only requests
  marked after real operator interaction refresh the idle timestamp.
- Sessions expire after 12 hours of operator inactivity or 3 days absolute.
  Restart, reboot, and upgrade clear all sessions.
- Public login throttling requires one valid Cloudflare client IP (IPv6 is
  bucketed by `/64`). SSH-forward login uses the local socket peer.

## Regression requirements

Unit tests must cover the boundary independently from route behavior:

- local classification does not invoke the public-hostname loader;
- duplicate markers, wrong public hosts, and non-HTTPS markers fail closed;
- HTTPS-only auth routes are unavailable on the SSH-forward context;
- each context accepts only its own session cookie name;
- session authentication includes CSRF and expiry in the centralized check;
- raw session and pre-authentication cookie constructors have no production
  caller outside `admin_auth.py`;
- password success on enrolled HTTPS can issue only pre-authentication state,
  and failed passkey verification cannot mint a final session;
- public client identity is required and validated;
- integration tests exercise login and status behavior through both paths;
- the shared dispatcher requires an explicit authenticated principal with no
  default, and both production entrypoints pass the principal their boundary
  established;
- Workspace static assets remain read-only and Workspace API requests still pass
  through the authenticated browser dispatcher;
- the workspace Unix socket rejects an unknown peer and every route outside
  its fixed thread allowlist; and
- workspace product ids pass through unchanged while product filtering and writable
  state remain enforced by the Workspace backend.

Deployment smoke tests should verify the external HTTP-to-HTTPS redirect and an
unauthenticated `401` from an authenticated API route over HTTPS. They should
not add live probes to the deploy/upgrade critical path.
