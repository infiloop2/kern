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
| Static admin/app assets | Yes | Served | Served after exact public boundary classification |
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

There are three ways code can reach an Admin API handler. Only the first is a
general operator API:

```text
operator browser
  -> loopback TCP listener
  -> classify SSH_FORWARD or PUBLIC_HTTPS
  -> enforce HTTPS-only route availability and app-bridge scope
  -> static/pre-auth handler, or session + CSRF authentication
  -> Admin API route

sandboxed app iframe
  -> postMessage to the authenticated admin shell
  -> same loopback TCP pipeline with X-Kern-App-Bridge: <app_id>
  -> only /v1/apps/<app_id>/api/... is permitted
  -> that installed app's loopback backend

installed app backend
  -> dedicated Unix socket
  -> SO_PEERCRED Linux uid + matching X-Kern-App-Backend claim
  -> fixed host-route allowlist + app thread-id namespace
  -> selected shared Admin API handler
```

### Authentication precedes shared dispatch

`service.route()` is an internal Python dispatcher, not another HTTP
entrypoint and not an authentication function. Its route implementations
assume that the caller has already established an authorized principal. There
are exactly two production call sites:

| Caller | Principal established before dispatch | Call into shared code |
| --- | --- | --- |
| `service.Handler._handle()` on the operator TCP listener | `RequestAuthContext`, then the matching session cookie and CSRF header; `_authenticate()` returns `OperatorPrincipal` | `service.route(..., principal=operator)` |
| `app_backend_api.Handler` on the app-backend Unix socket | Kernel `SO_PEERCRED` uid resolved to an installed app and matched to its claimed app id, then the fixed method/path allowlist creates `AppBackendPrincipal(app_id)` | `service.route(..., principal=app)` after app thread ids are prefixed |

The TCP handler processes static assets and the explicit pre-session
login/status operations before its session gate; those operations do not call
`service.route()`. Logout, passkey management, and streaming file operations
also have specialized handler methods, but only after the same operator
session authentication where required. Every ordinary operator route reaches
the shared dispatcher only after authentication.

The Unix-socket handler does not call `service.Handler` and does not manufacture
an operator session. It independently authenticates the app service process,
checks `APP_BACKEND_ALLOWED_ADMIN_ROUTES`, and only then reuses
`service.route()` for the selected business logic. The required `principal`
argument has no default: an omitted value can never silently mean operator.
`service.route()` derives the app namespace only from
`AppBackendPrincipal.app_id`; the read-only tool catalog and network-policy
routes do not need a resource namespace. A request header sent to the TCP
listener cannot substitute for Unix peer credentials.

Consequently, adding a third production caller of `service.route()` is a
security-boundary change: that caller must establish its principal and
authorization before dispatch and needs corresponding boundary tests. Merely
knowing a `/v1/...` path never grants access to the shared function.

The iframe bridge does not grant an app access to the Admin API. App UI assets
run under a sandboxed CSP with no direct network connection. The trusted admin
shell turns an iframe `postMessage` into one cookie-authenticated request and
adds `X-Kern-App-Bridge`. `service.py` checks that marker before static,
authentication, or application dispatch; a marked request is rejected unless
its literal path begins with that same app's
`/v1/apps/<app_id>/api/` prefix. `app_api_proxy.py` then forwards only the
suffix to the installed app's declared loopback port with an
`X-Kern-App-Proxy` identity.

The app backend's Unix-socket API is a different, non-browser boundary and does
not use `RequestAuthContext`, cookies, or the operator password. The peer's
kernel-reported uid must resolve to an installed app service account and must
match its claimed app id. `app_backend_api.py` then permits only:

| Method and path | App-visible capability |
| --- | --- |
| `GET /v1/tools` | Read the host tool catalog and enablement metadata |
| `GET /v1/network/policy` | Read the effective network policy |
| `GET /v1/threads` | List only this app's threads |
| `GET /v1/threads/:thread_id` | Read one thread in this app's namespace |
| `POST /v1/threads/:thread_id/messages` | Send or steer work in this app's namespace |
| `POST /v1/threads/:thread_id/stop` | Stop work in this app's namespace |
| `GET /v1/threads/:thread_id/events` | Read events in this app's namespace |

Thread ids are mechanically prefixed with the authenticated app id before the
shared handler runs, list results are filtered to that prefix, and the prefix
is removed from responses. An app backend cannot call login, passkey,
credential, network mutation, tool mutation, host-control, file, event-log, or
another app's thread routes through this socket. Adding a route requires an
explicit entry in `APP_BACKEND_ALLOWED_ADMIN_ROUTES`; merely adding a normal
Admin API route does not expose it to apps.

## Operator-facing route policy

The table below is the complete authentication policy map for the
operator-facing listener. The canonical request and response schemas remain in
the [Admin API reference](../api/AdminAPI.md).

| Route group | Session policy | Additional boundary |
| --- | --- | --- |
| `GET /`, `/oauth/callback`, fixed admin modules/styles/icons, and bundled guide assets | No session | Still classified first; fixed release files only |
| `GET /v1/apps/<app_id>/ui/<asset>` | No session | Installed-app lookup, fixed package files, sandboxed app CSP |
| `POST /v1/login` | Pre-session | Password throttle and verification; public HTTPS starts WebAuthn when enrolled |
| `GET /v1/login/status` | Pre-session, public HTTPS only | Exact public Host; non-secret enrollment boolean only |
| `POST /v1/login/passkey` | Pre-session, public HTTPS only | Password-issued single-use pre-auth cookie plus WebAuthn verification |
| `POST /v1/logout` | Session + CSRF | Revokes only the presented session |
| `GET /v1/admin-passkeys`, `POST /v1/admin-passkeys/register/options`, `POST /v1/admin-passkeys/register` | Session + CSRF, public HTTPS only | Exact RP ID and origin from `RequestAuthContext` |
| `GET /v1/health` | Session + CSRF | No unauthenticated health exception on the admin listener |
| `GET /v1/agent-runtime/{status,account}`, runtime refresh, OAuth login/completion, Bedrock credential, and linked-account reset routes | Session + CSRF | Normal shared handler on both operator paths |
| `GET /v1/threads`, `/v1/threads/<id>`, thread message/stop/event routes, and `GET /v1/events` | Session + CSRF | Operator sees the host-wide thread namespace |
| `GET /v1/agent-files`, file read/content/upload routes, and `GET /v1/agent-processes` | Session + CSRF | Content and upload use bounded streaming handlers after authentication |
| `GET\|PUT /v1/network/policy`, `GET /v1/network/events`, and GitHub credential/audit/pending-push routes under `/v1/network-tools/` | Session + CSRF | Method-specific mutation validation and root-helper boundaries still apply |
| Tool catalog/config/enablement/OAuth/approval routes and tool events under `/v1/tools` | Session + CSRF | Tool-specific authorization and approval state run after operator authentication |
| `GET /v1/apps` | Session + CSRF | Lists installed app manifests |
| `GET\|POST\|PUT\|DELETE /v1/apps/<app_id>/api/<path>` | Session + CSRF | If bridge-marked, marker must match `<app_id>`; proxy targets only that installed app |
| `GET /v1/host-errors[/<seq>]` and `POST /v1/host-runtime/reboot` | Session + CSRF | Diagnostic read or fixed privileged helper after authentication |
| Any unlisted method/path | Not dispatched | `404`; route prefixes do not confer access by themselves |

This grouping is deliberate. Transport-specific behavior ends at
classification, cookie selection, and the explicit HTTPS-only authentication
routes. After `Handler._authenticate()` succeeds, ordinary operator routes use
the same implementation for SSH-forward and public-HTTPS sessions.

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
- an app-bridge marker is scoped before static, pre-session, and authenticated
  dispatch, including non-normalized paths;
- the app-backend Unix socket rejects an unknown or mismatched peer identity
  and every route outside its fixed allowlist; and
- app-backend thread lists, ids, requests, and nested response ids remain
  mechanically confined to the authenticated app namespace.

Deployment smoke tests should verify the external HTTP-to-HTTPS redirect and an
unauthenticated `401` from an authenticated API route over HTTPS. They should
not add live probes to the deploy/upgrade critical path.
