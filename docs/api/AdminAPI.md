# Admin API

The Kern admin API is served by the localhost admin service.

Base URL after port forwarding:

```text
http://127.0.0.1:7443
```

Every API request except the narrowly scoped public-login status authenticates
with a session cookie. `POST /v1/login` with
`{"password": "<admin-password>"}` returns an `HttpOnly`, `SameSite=Strict`
session cookie immediately when no passkey is configured. Once a passkey is
enrolled, public HTTPS login returns WebAuthn request options and mints the
session only after `POST /v1/login/passkey` verifies the second factor.
Subsequent requests send the session cookie automatically and
must also include the CSRF header `X-Kern-Csrf: 1`. `POST /v1/logout`
revokes the session and clears the cookie. The password is presented only at
`/v1/login` and never replayed on later requests; there is no bearer-token path.

Requests must arrive over HTTPS through the tunnel (the origin redirects or
refuses cleartext). Repeated failed logins are throttled and return `429`; see
[admin login sessions](../architecture/admin-api.md#admin-login-sessions).

API responses are JSON. Static UI assets are the exception: `GET /`,
`GET /oauth/callback`, the admin CSS/JavaScript/favicon paths, and installed app
UI assets under `/v1/apps/{app_id}/ui/` are served without authentication.
They return only static files and perform no state change. The sole
unauthenticated JSON read is `GET /v1/login/status` on the configured public
HTTPS hostname; it returns only whether a passkey is enrolled. Every other API
route, including `GET /v1/apps` and every app backend proxy request, requires
an authenticated caller.

## Errors

Every non-2xx response returns this JSON envelope:

```json
{
  "error": {
    "message": "Human-readable error message"
  }
}
```

Error response fields:

| Field | Required | Type | Values | Meaning |
| --- | --- | --- | --- | --- |
| `error.message` | Yes | string |  | Human-readable error message for logs and operator display. |

Error status codes:

| HTTP status | Meaning |
| --- | --- |
| `400` | Request JSON, query string, or field value is invalid. |
| `401` | Missing or invalid admin password or session. |
| `403` | A cookie-authenticated request is missing the `X-Kern-Csrf` header, an authenticated app bridge attempted to target a different app, or an app backend attempted a disallowed host route. |
| `404` | Requested resource or route does not exist. |
| `409` | Request conflicts with current runtime, thread, approval, or credential state. |
| `413` | Request body exceeds the 1 MiB admin API limit. |
| `429` | Too many failed admin logins from this source (retry after the lockout window), or the selected runtime is already running its maximum concurrent threads (retry when one finishes). |
| `502` | An installed app backend or delegated tools service is unavailable or returned an invalid response. |
| `500` | Host-side error. |

## Session

```text
POST /v1/login
POST /v1/login/passkey
GET  /v1/login/status
POST /v1/logout
```

`POST /v1/login` takes `{"password": "<admin-password>"}`. With no passkey, a
correct password returns `{"ok": true}` and
a `Set-Cookie: tc_admin_session=...; HttpOnly; SameSite=Strict` header (with
`Secure` when the request arrived over HTTPS); a wrong password returns `401` and
sets no cookie. When passkeys are configured on the public HTTPS path, a correct
password instead returns `{"passkey_required": true, "publicKey": {...}}` and a
five-minute, non-session pre-authentication cookie. The browser submits its
WebAuthn assertion to `POST /v1/login/passkey`; only successful verification
mints the admin session. The assertion route therefore runs before session
authentication, but it is not an independent login path: it requires the
unguessable pre-authentication cookie issued only after a correct password.
That cookie and its server-side challenge expire after five minutes, are bound
to the same client source and public origin, and are consumed by the first
assertion attempt whether it succeeds or fails. Repeated password failures are
throttled with `429`.

`GET /v1/login/status` is available only through the configured public HTTPS
admin hostname and returns `{"passkey_configured": true|false}` with
`Cache-Control: no-store`. It requires no session so the login page and Kern
Cloud can accurately show whether public login has its second factor enabled.
It exposes no credential identifiers or metadata. The SSH-forward origin
returns `404`, preserving its database-independent recovery path.

Authenticated passkey enrollment uses:

```text
GET  /v1/admin-passkeys
POST /v1/admin-passkeys/register/options
POST /v1/admin-passkeys/register
```

The status response tells the UI whether credentials exist and whether setup is
available on the current public HTTPS hostname. Registration requires an
authenticated session, a fresh single-use challenge, an ES256 resident
credential, and authenticator user verification. Enrollment is not offered on
the SSH-forward origin: every route in this passkey-management namespace
returns `404` there because WebAuthn credentials are scoped to their public RP
domain. The host stores only the credential id, public key, signature counter,
transports, backup flag, and timestamps.

`POST /v1/logout` revokes the current session and clears the cookie, returning
`{"ok": true}`. Like every non-login route it requires an authenticated caller;
a cookie-authenticated call must include the `X-Kern-Csrf` header.

## Health

```text
GET /v1/health
```

Response:

```json
{
  "status": "ok",
  "agent_name": "kern-dev-agent",
  "agent_runtime": {
    "runtimes": [
      {
        "type": "codex",
        "status": "active",
        "active_thread_ids": []
      },
      {
        "type": "claude_code",
        "status": "deactivated",
        "active_thread_ids": []
      }
    ]
  },
  "network_controls": {
    "status": "active"
  },
  "version": {
    "status": "ok",
    "runtime": "x.y.z",
    "state": "x.y.z"
  },
  "upgrade": {
    "available": true,
    "latest": "x.y.z"
  },
  "host_runtime": {
    "cpu": {
      "usage_percent": 12.5
    },
    "memory": {
      "used_bytes": 980000000,
      "total_bytes": 2147483648
    },
    "filesystem": {
      "mounts": {
        "root": {
          "used_bytes": 6000000000,
          "total_bytes": 17179869184
        },
        "admin": {
          "used_bytes": 250000000,
          "total_bytes": 17179869184
        },
        "agent": {
          "used_bytes": 500000000,
          "total_bytes": 8589934592
        }
      }
    },
    "swap": {
      "allocated_bytes": 6442450944,
      "used_bytes": 536870912
    }
  }
}
```

Response fields:

| Field | Type | Values | Meaning |
| --- | --- | --- | --- |
| `status` | enum | `ok`, `degraded` | Overall host health. `ok` means the admin service, agent runtime supervisor, and network controls are reachable. `degraded` means the admin service is responding but at least one component is not healthy. |
| `agent_name` | string |  | Host name from the input config. |
| `agent_runtime.runtimes` | array |  | Status records for every supported runtime. |
| `agent_runtime.runtimes[].type` | enum | `codex`, `claude_code`, `hermes` | Agent runtime type. |
| `agent_runtime.runtimes[].status` | enum | `deactivated`, `loading`, `awaiting_login`, `active`, `error` | Current agent runtime supervisor state. |
| `agent_runtime.runtimes[].active_thread_ids` | string array |  | Threads with a live turn on this runtime. |
| `network_controls.status` | enum | `active`, `error` | Derived network policy enforcement state. |
| `version.status` | enum | `ok`, `mismatch`, `error` | Version health for the running root volume and preserved admin state. |
| `version.runtime` | string or null |  | Kern version from `/opt/kern-host/VERSION`. |
| `version.state` | string or null |  | Kern preserved-state version from admin disk `version.json`. |
| `upgrade.available` | boolean |  | Whether the public `infiloop2/kern` main-branch version is newer than the running version. This advisory check does not affect overall health. |
| `upgrade.latest` | string or null |  | Latest valid version returned by a successful public-repository check, or `null` until the first check succeeds after service start. A failed later check preserves the last successful value. |
| `host_runtime.cpu.usage_percent` | number | 0-100 | Current host CPU usage percentage. |
| `host_runtime.memory.used_bytes` | integer |  | Current host memory used, in bytes. |
| `host_runtime.memory.total_bytes` | integer |  | Total host memory, in bytes. |
| `host_runtime.filesystem.mounts.root.used_bytes` | integer |  | Current root filesystem used space, in bytes. |
| `host_runtime.filesystem.mounts.root.total_bytes` | integer |  | Total root filesystem capacity, in bytes. |
| `host_runtime.filesystem.mounts.admin.used_bytes` | integer | optional | Current admin data volume (`/mnt/kern-admin`) used space, in bytes. |
| `host_runtime.filesystem.mounts.admin.total_bytes` | integer | optional | Total admin data volume (`/mnt/kern-admin`) capacity, in bytes. |
| `host_runtime.filesystem.mounts.agent.used_bytes` | integer | optional | Current agent data volume (`/mnt/kern-agent`) used space, in bytes. |
| `host_runtime.filesystem.mounts.agent.total_bytes` | integer | optional | Total agent data volume (`/mnt/kern-agent`) capacity, in bytes. |
| `host_runtime.swap.allocated_bytes` | integer |  | Filesystem-backed RAM swap allocated to the host, in bytes. |
| `host_runtime.swap.used_bytes` | integer |  | Current filesystem-backed RAM swap used, in bytes. |

Runtime status is `deactivated` when that runtime's managed provider
integration is disabled, `loading` while the runtime is starting,
`awaiting_login` while an OAuth runtime needs operator login, `active` while it
can accept work, and `error` when the runtime supervisor cannot make it
healthy. Hermes has no OAuth flow: while Bedrock is enabled it is
`awaiting_login` until a validated credential is connected, then `active`.

`network_controls.status` is derived, not stored. It is `active` when the
persisted network policy is valid and the proxy process is listening. It is
`error` when the policy cannot be parsed or policy enforcement is not healthy.
The `error` state fails closed and denies all network access.

## Agent Runtime

```text
GET  /v1/agent-runtime/status
GET  /v1/agent-runtime/account
POST /v1/agent-runtime/refresh
POST /v1/agent-runtime/codex-oauth-login
GET  /v1/agent-runtime/codex-oauth-login
POST /v1/agent-runtime/claude-oauth-login
GET  /v1/agent-runtime/claude-oauth-login
POST /v1/agent-runtime/claude-oauth-login/complete
GET  /v1/agent-runtime/bedrock-credentials
POST /v1/agent-runtime/bedrock-credentials
DELETE /v1/agent-runtime/bedrock-credentials
POST /v1/agent-runtime/reset-linked-account
```

Agent runtime endpoints:

| Method | Path | Request | Response | Behavior |
| --- | --- | --- | --- | --- |
| `GET` | `/v1/agent-runtime/status` | none | Agent runtime status response | Returns current state for every runtime. |
| `GET` | `/v1/agent-runtime/account` | none | Agent account response | Returns the current account status for every runtime. |
| `POST` | `/v1/agent-runtime/refresh` | Agent runtime refresh request | Agent account response | Attempts to refresh provider account/status for one runtime or all runtimes, then returns the current account response. |
| `POST` | `/v1/agent-runtime/codex-oauth-login` | none | Codex OAuth login response | Starts a Codex OAuth login flow and returns the device code and login link. |
| `GET` | `/v1/agent-runtime/codex-oauth-login` | none | Codex OAuth login response | Returns the current Codex OAuth device code and login link. |
| `POST` | `/v1/agent-runtime/claude-oauth-login` | none | Claude OAuth login response | Starts a Claude Code OAuth login process and returns the login link. |
| `GET` | `/v1/agent-runtime/claude-oauth-login` | none | Claude OAuth login response | Returns the current Claude Code OAuth login link. |
| `POST` | `/v1/agent-runtime/claude-oauth-login/complete` | `{"code": "..."}` | status response | Submits the browser login code back to the waiting Claude Code OAuth process. |
| `GET` | `/v1/agent-runtime/bedrock-credentials` | none | `{"connected": false}` or `{"connected": true, "access_key_id": "AKIA...", "region": "us-east-1"}` | Returns whether the Bedrock connection is stored plus its non-secret access key id and region. The secret is never returned. |
| `POST` | `/v1/agent-runtime/bedrock-credentials` | `{"access_key_id": "AKIA...", "secret_access_key": "...", "region": "us-east-1"}` | `{"status": "accepted"}` | Synchronously validates the Bedrock long-term IAM access key pair with STS, then stores the credential, region, and account metadata atomically. Validation runs even while Bedrock is disabled; a rejected candidate returns `400`, is not retained, and leaves any previous validated connection unchanged. AWS checks model-specific invocation permission and model access on the first real turn, avoiding a paid setup invocation. Later AWS failures are reported by the turn that encounters them; they do not create a stored credential-health state. The request accepts exactly these three fields; the secret is never returned. |
| `DELETE` | `/v1/agent-runtime/bedrock-credentials` | none | status response | Disconnects the AWS account, clears its credential, region, and account metadata, then fails running Hermes turns. The live usage counters are retained: they record work already done. |
| `POST` | `/v1/agent-runtime/reset-linked-account` | `{"agent_runtime": "codex"\|"claude_code"}` | status response | Clears the selected OAuth runtime's linked account state. Bedrock uses the credential endpoint above because it uses an IAM credential instead of OAuth. |

The runtime-specific OAuth login endpoints work while that runtime's status is
`awaiting_login` or `error` — an errored runtime (changed account, malformed
local credentials) is recovered by simply logging in again. They return `409`
in any other state, including `deactivated`.
`POST /v1/agent-runtime/reset-linked-account` takes `{"agent_runtime": "codex"}`
or `{"agent_runtime": "claude_code"}` and deletes that runtime's linked-account
guard: the operator-approved anchor, its proxy pin, and any pending OAuth
approval. Use it to unlink the account, for example to switch a runtime to a
different provider account. It may be called in any
runtime status. It also moves the runtime out of `active`, clears local agent
auth files, closes that runtime's live processes, and fails its running turns
so no process from the old linked account keeps executing. The runtime is then
ready for a fresh operator login that links an account again.
`GET /v1/agent-runtime/account` does not accept query parameters; it always returns
one account-status entry per runtime.
`POST /v1/agent-runtime/refresh` accepts `{}` to refresh all runtimes, or
`{"agent_runtime": "codex"}`, `{"agent_runtime": "claude_code"}`, or `{"agent_runtime": "hermes"}` to refresh one.
It forces a provider check instead of reusing a remembered live-validation
verdict. It returns the same response shape as
`GET /v1/agent-runtime/account`.

Agent runtime status response:

```json
{
  "runtimes": [
    {
      "type": "codex",
      "status": "active",
      "active_thread_ids": []
    },
    {
      "type": "claude_code",
      "status": "deactivated",
      "active_thread_ids": []
    }
  ]
}
```

Agent runtime status response fields:

| Field | Type | Values | Meaning |
| --- | --- | --- | --- |
| `runtimes[].type` | enum | `codex`, `claude_code`, `hermes` | Agent runtime type. |
| `runtimes[].status` | enum | `deactivated`, `loading`, `awaiting_login`, `active`, `error` | Current runtime state. Codex uses its rate-limit request and, if that fails, one Codex-owned forced refresh. Claude Code uses a `/usage` probe for the pinned token, or provider profile attestation for a new or rotated token. Bedrock is `active` when the integration is enabled and its synchronously validated credential/account row is present. AWS checks model-specific invocation permission and current credential validity on the first real turn; later provider failures are turn failures. |
| `runtimes[].active_thread_ids` | string array |  | Threads with a live turn on that runtime, sorted by thread id. Empty when no turn is running. |
| `runtimes[].error_message` | string | optional | Present only while `status` is `error`: the underlying runtime failure message. |

Agent account response:

```json
{
  "accounts": [
    {
      "agent_runtime": "codex",
      "provider": "openai",
      "status": "active",
      "account_id": "acct_...",
      "email": "operator@example.com",
      "plan_type": "pro",
      "codex_usage": {
        "last_checked_at": "2026-06-29T23:10:00Z",
        "rate_limits": {
          "primary": {
            "used_percent": 60,
            "window_duration_mins": 300,
            "resets_at": 1782788896
          },
          "secondary": {
            "used_percent": 20,
            "window_duration_mins": 10080,
            "resets_at": 1783296254
          },
          "credits": {
            "has_credits": false,
            "unlimited": false,
            "balance": "0"
          }
        }
      }
    },
    {
      "agent_runtime": "claude_code",
      "provider": "claude",
      "status": "active",
      "account_id": "uuid...",
      "email": "operator@example.com",
      "plan_type": "pro",
      "claude_usage": {
        "current_session_used_percent": 0,
        "current_session_resets_at": 1782781800,
        "weekly_used_percent": 0,
        "weekly_resets_at": 1783094340,
        "fable_weekly_used_percent": 0,
        "fable_weekly_resets_at": 1783094340,
        "last_checked_at": "2026-06-29T23:10:00Z"
      }
    },
    {
      "provider": "bedrock",
      "agent_runtimes": ["hermes"],
      "status": "active",
      "account_id": "123456789012",
      "arn": "arn:aws:iam::123456789012:user/kern-bedrock",
      "bedrock_usage": {
        "month_to_date": 0.3102,
        "currency": "USD",
        "requests": 41,
        "metered_requests": 41,
        "input_tokens": 402118,
        "output_tokens": 31889,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0
      }
    }
  ]
}
```

Agent account response fields:

| Field | Type | Values | Meaning |
| --- | --- | --- | --- |
| `accounts[].agent_runtime` | enum | `codex`, `claude_code` | Runtime for an OAuth provider record. Absent on the Bedrock record. |
| `accounts[].agent_runtimes` | string array | `["hermes"]` | Runtime that uses the Bedrock provider. Present only on the Bedrock record. |
| `accounts[].provider` | enum | `openai`, `claude`, `bedrock` | Managed AI provider. |
| `accounts[].status` | enum | `deactivated`, `loading`, `awaiting_login`, `active`, `error` | Current provider account status. OAuth runtimes use `awaiting_login` when operator login is required. Bedrock has no OAuth flow: its status is `awaiting_login` until a synchronously validated credential is connected, then `active`. Later inference failures are reported on their turns, not persisted as provider status. |
| `accounts[].account_id` | string | optional | The linked provider account id; for `bedrock` this is the 12-digit AWS account id. Present whenever a validated account identity is available. |
| `accounts[].email` | string | optional | Present when available from the linked account metadata. |
| `accounts[].arn` | string | optional | The STS-attested IAM identity of the connected AWS credential. Present only on the Bedrock provider record while its account is linked. |
| `accounts[].plan_type` | string | optional | Common plan name for the provider account. Present only while the runtime is active. |
| `accounts[].codex_usage` | object | optional | Codex-specific usage metadata. Present only for the Codex runtime when Codex reports rate limits. |
| `accounts[].codex_usage.last_checked_at` | string | optional | UTC timestamp when Kern last refreshed the cached Codex usage snapshot. Active runtimes are rechecked every 300 seconds. |
| `accounts[].codex_usage.rate_limits` | object | optional | Codex rate-limit snapshot. |
| `accounts[].codex_usage.rate_limits.primary` | object | optional | Codex 300-minute rate-limit window. |
| `accounts[].codex_usage.rate_limits.secondary` | object | optional | Codex 10080-minute rate-limit window. |
| `accounts[].codex_usage.rate_limits.primary.used_percent`, `accounts[].codex_usage.rate_limits.secondary.used_percent` | number | optional | Percent used for this window. |
| `accounts[].codex_usage.rate_limits.primary.window_duration_mins`, `accounts[].codex_usage.rate_limits.secondary.window_duration_mins` | number | optional | Window duration in minutes. |
| `accounts[].codex_usage.rate_limits.primary.resets_at`, `accounts[].codex_usage.rate_limits.secondary.resets_at` | number | optional | Unix timestamp when this window resets. |
| `accounts[].codex_usage.rate_limits.credits` | object | optional | Codex credit snapshot. |
| `accounts[].codex_usage.rate_limits.credits.has_credits` | boolean | optional | Whether the account has credits. |
| `accounts[].codex_usage.rate_limits.credits.unlimited` | boolean | optional | Codex `unlimited`. |
| `accounts[].codex_usage.rate_limits.credits.balance` | string | optional | Codex credit balance. |
| `accounts[].claude_usage` | object | optional | Claude Code usage metadata parsed from `claude -p "/usage" --output-format json`. Windows parse independently, so any subset of the fields below can be present. |
| `accounts[].claude_usage.current_session_used_percent` | number | optional | Percent used for the current Claude Code session. |
| `accounts[].claude_usage.current_session_resets_at` | number | optional | Unix timestamp when the current Claude Code session window resets. |
| `accounts[].claude_usage.weekly_used_percent` | number | optional | Percent used for the current Claude Code weekly window across all models. |
| `accounts[].claude_usage.weekly_resets_at` | number | optional | Unix timestamp when the Claude Code weekly window resets. |
| `accounts[].claude_usage.fable_weekly_used_percent` | number | optional | Percent used for the Fable-specific weekly window. |
| `accounts[].claude_usage.fable_weekly_resets_at` | number | optional | Unix timestamp when the Fable-specific weekly window resets. |
| `accounts[].claude_usage.last_checked_at` | string | optional | UTC timestamp of the provider read that produced this Claude usage snapshot. Active runtimes are rechecked every 300 seconds; the explicit refresh endpoint forces an immediate provider read. If no usage window parses, `claude_usage` is absent rather than stale. |
| `accounts[].bedrock_usage` | object | always on the `bedrock` record | Live Hermes month-to-date usage. For each allowed Bedrock response the network proxy records the token usage AWS reports and the USD it prices that response at, per model and UTC day; this sums the current month from those stored counters, so every accounts read is current with no AWS call. Usage survives credential resets: the counters record work already done. |
| `accounts[].bedrock_usage.month_to_date` | number |  | Current-month cost: the sum of the USD the proxy priced each metered response at when it recorded it, using the host's on-demand catalog rates. Final once recorded; a later rate edit does not rewrite it. An estimate of what AWS will bill, not the bill itself. |
| `accounts[].bedrock_usage.currency` | string |  | Always `USD` (the catalog rates' currency). |
| `accounts[].bedrock_usage.requests` | number |  | Allowed Bedrock invocations forwarded this month. |
| `accounts[].bedrock_usage.metered_requests` | number |  | Invocations whose response carried a parseable usage record. A gap below `requests` means AWS errors or unparsed responses, that is, possible undercounting. A model outside the price table is still metered; its tokens count but it adds nothing to `month_to_date`. |
| `accounts[].bedrock_usage.input_tokens`, `.output_tokens`, `.cache_read_tokens`, `.cache_write_tokens` | number |  | Month-to-date token totals as AWS reported them per response. The cached-token counters mirror the Converse usage shape but stay zero: Bedrock prompt caching covers only model families outside this catalog. |

Codex OAuth login response:

```json
{
  "status": "awaiting_login",
  "device_code": "ABCD-EFGH",
  "login_url": "https://auth.openai.com/activate",
  "expires_at": "2026-06-08T00:10:00Z"
}
```

Codex OAuth login response fields:

| Field | Type | Values | Meaning |
| --- | --- | --- | --- |
| `status` | enum | `awaiting_login` | Current Codex OAuth login state. |
| `device_code` | string |  | Code the operator enters on the Codex OAuth login page. |
| `login_url` | string |  | Operator URL for Codex OAuth login. |
| `expires_at` | string | RFC 3339 timestamp | Time when the device code expires. |

Claude OAuth login response:

```json
{
  "status": "awaiting_code",
  "login_url": "https://claude.com/cai/oauth/authorize?...",
  "expires_at": "2026-06-08T00:10:00Z"
}
```

After opening the URL and completing browser login, submit the displayed code to
`POST /v1/agent-runtime/claude-oauth-login/complete`:

```json
{
  "code": "..."
}
```

Agent runtime reset-linked-account response:

```json
{
  "status": "accepted"
}
```

Agent runtime reset-linked-account response fields:

| Field | Type | Values | Meaning |
| --- | --- | --- | --- |
| `status` | enum | `accepted` | The linked account was reset. |

### Threads

```text
POST /v1/threads/{thread_id}/messages
GET  /v1/threads
GET  /v1/threads/{thread_id}
POST /v1/threads/{thread_id}/stop
GET  /v1/threads/{thread_id}/events?since=<seq>&limit=<n>&message_bytes=<b>
```

All agent work runs on threads. A thread is a durable conversation with a
client-chosen id (`thread_id`) and a session configuration (`agent_runtime`,
`model`, `effort`); it is created implicitly by its first message, never by a
separate create call. The public model has no
turn resource or turn lifecycle: a thread is simply `idle` or `running`, and
its history is one chronological stream. Work on the same thread is
serialized; work on different threads runs in parallel, up to 3 per runtime.
Codex resumes the thread's provider conversation by id on a fresh app-server;
Claude Code and Hermes resume by their recorded provider session ids. An idle
thread may replace all three configuration fields atomically on its next
message. That clears the old provider session and starts a fresh one with a
handoff containing the newest retained public thread events, bounded to
250,000 characters. Activity records include their complete stored detail and
output, as shown by an expanded activity card. The durable thread id and
visible history do not change.

**There is no queue.** A message is synchronously accepted into an idle or
running thread, or rejected with a descriptive `409` or `429`; the caller
decides whether and when to retry. Agent work runs only while its chosen
runtime is `active`: a message for a non-active runtime is rejected at
admission. Policy changes and provider-status refreshes own the transition out
of `active`; either transition closes that runtime's live processes, including
a run admitted just before the transition, and records `thread.error`. Claude
Code additionally converges its rotating credential pin before spawning each
process. Any runtime/CLI exception during execution — including a provider
rate limit — also returns the thread to idle with `thread.error` and its error
message.

Thread endpoints:

| Method | Path | Request | Response | Behavior |
| --- | --- | --- | --- | --- |
| `POST` | `/v1/threads/{thread_id}/messages` | Send message request | Send message response | Durably admits an idle thread's initial message or synchronously delivers a running thread's steer, creating the thread on its first message. Rejected with `409`/`429` when it cannot be accepted; there is no queue. |
| `GET` | `/v1/threads?before=<cursor>&limit=<n>` | query parameters optional | Thread list response | Lists one newest-first page of threads with their session configuration and live status. |
| `GET` | `/v1/threads/{thread_id}` | none | `{"thread": {...}}` | Returns one thread (the same shape as a thread list entry). `404` when no thread row exists. Accepts no query parameters. |
| `POST` | `/v1/threads/{thread_id}/stop` | none | `{"status": "accepted"}` | Stops the thread's running work: the thread returns durably to `idle`, a `thread.stopped` event is appended, and process interruption is requested. `404` for an unknown thread; `409` (`the thread has no running work`) when the thread is idle. The thread survives and a later message resumes the conversation, but sends receive a retryable `409` until the old process has fully shut down. |
| `GET` | `/v1/threads/{thread_id}/events?since=<seq>&limit=<n>&message_bytes=<b>` | query parameters optional | Event list response | One chronological page of the thread's event stream. See [Events](#events). |

Stop has no mailbox or deferred delivery. The HTTP request synchronizes
directly with steering and provider event writes, commits `thread.stopped`,
changes the private phase to FINISHING, and requests a non-blocking process
interrupt before returning `accepted`. Provider session ids are persisted by
adapter callbacks as soon as they become usable, not deferred to Stop. The
owning execution thread still closes and verifies the scope before releasing
the same-thread fence; a send during that short cleanup window receives the
documented retryable `409`.

Send message request:

```json
{
  "agent_runtime": "codex",
  "model": "gpt-5.6-terra",
  "effort": "high",
  "message": "Implement this change and report the result."
}
```

Send message request fields:

| Field | Required | Type | Meaning |
| --- | --- | --- | --- |
| `message` | Yes | string | Message for the agent runtime. Must be 1 to 50,000 characters. The host handles idle and running threads; callers use the same operation for both. |
| `agent_runtime` | New thread or configuration change | enum | Runtime for the thread: `codex`, `claude_code`, or `hermes`. Supply it together with `model` and `effort`. On an existing thread, a matching triple resumes or steers the current provider session; a different triple starts a fresh provider session only while the thread is idle. |
| `model` | New thread or configuration change | enum | Model for this session. Codex accepts `gpt-5.6-terra`, `gpt-5.6-sol`, or `gpt-5.6-luna`; Claude Code accepts `claude-opus-5`, `claude-fable-5`, or `claude-sonnet-5`; Hermes accepts the Bedrock model ids `deepseek.v3.2`, `qwen.qwen3-coder-next`, or `moonshotai.kimi-k2.5`. Must be supplied together with `agent_runtime` and `effort`. A thread created under an earlier catalog keeps its recorded model and stays readable. It can continue by switching to an offered complete triple while idle; the superseded value cannot start a new provider session. |
| `effort` | New thread or configuration change | enum | Effort for this session. Codex accepts `high`, `max`, or `ultra`, except Luna accepts only `high` or `max`. Claude Code accepts `high`, `max`, or `ultracode`; `ultracode` enables its xhigh effort plus dynamic workflow orchestration. Hermes accepts `high` (its headless CLI exposes no effort control). Must be supplied together with `agent_runtime` and `model`. |

The path's `thread_id` must be 1 to 64 characters of `A-Z`, `a-z`, `0-9`, `-`,
or `_`; other values are `404` (no such route). Ids containing the app-scoped
separator (`<app_id>__...`) are reserved for app backends and return `400`.
The first message requires all three configuration fields. Later messages may
omit all three or repeat the complete matching triple. A different complete
triple rotates an idle thread to a new provider session in the same admission
transaction; a partial triple returns `400`, and a change while running
returns `409`. The synthetic handoff is sent only to the new provider: the
visible event stream records a completed `thread.activity` describing the old
and new session configuration, followed by the operator's new message, but not
the transcript wrapper. The handoff preserves the newest events and may omit
older retained history, so provider-side context and cache reads from the
previous session are not available. Thread rows referenced by retained events
are preserved; otherwise the host retains the 100,000 most recently used
mappings per runtime. Once a thread is no longer retained, supplying a
configuration starts a fresh provider conversation.

The same bounded handoff is used without a configuration change when an
existing thread has retained events but no provider session id—for example,
after startup failed before the replacement provider published a resumable
id. A brand-new thread with no history receives only its first message.

Follow-up message request:

```json
{
  "message": "Continue with the implementation."
}
```

Send message response:

```json
{
  "status": "accepted",
  "thread": {
    "thread_id": "feature-chat-1",
    "agent_runtime": "codex",
    "model": "gpt-5.6-terra",
    "effort": "high",
    "last_used_at": "2026-06-08T00:00:00Z",
    "status": "running"
  }
}
```

Send message response fields:

| Field | Type | Values | Meaning |
| --- | --- | --- | --- |
| `status` | enum | `accepted` | The host accepted the message and its durable `thread.message` event committed. Idle and running threads intentionally have the same response. |
| `thread` | object |  | The thread, in the same shape as a thread list entry. |

For an idle thread, the host durably admits the message and starts its runtime
worker; `accepted` does not mean the provider has accepted that initial
message yet. A later startup/provider failure appears as `thread.error`. For a
running thread, `accepted` means the live provider transport acknowledged the message and its
`thread.message` event then committed. Codex acknowledgement is a successful
`turn/steer` JSON-RPC response. The Codex stdout reader routes that response
directly to the waiting request, so processing unrelated activity
notifications cannot delay acknowledgement. Claude Code exposes no per-message
response, so its acknowledgement is a successful write and flush to the live
stream-json stdin. There is no host steer mailbox and no delivery-marker table.

If provider acknowledgement succeeds but the following database write fails,
the API returns an error and no user event appears even though the CLI may act
on the message. Retrying can deliver it twice. This deliberately at-least-once
failure edge is the trade for avoiding a durable cross-system delivery
protocol; callers may safely retry when duplicate natural-language steering is
acceptable.
Hermes cannot receive another message while it is running: a message for a busy Hermes thread
returns `409`; a later message after it becomes idle resumes the stored
provider conversation.

Message rejections (there is no queue, so each names the condition and the
caller retries):

| Status | Condition | `error.message` |
| --- | --- | --- |
| `409` | The thread's runtime is not `active` (its status is `loading`, `awaiting_login`, or `error`). | `<Runtime> runtime is <status>; messages run only while it is active` |
| `409` | The thread's runtime is disabled in the network policy. | `<Runtime> runtime is deactivated; enable its provider under Internet Access and Tools` |
| `409` | The admitted process has not yet accepted its initial message. This private startup phase is normally brief; retry the same request. | `the agent is starting; retry shortly` |
| `409` | The previous work is durably final but its runtime process is still shutting down. The live fence remains so a new message never races the dying process; retry the same request. | `the agent is finishing; retry shortly` |
| `409` | Hermes has no mid-run input channel. | `Hermes cannot accept another message while running; wait for it to finish` |
| `409` | A message tries to change configuration while work is running. | `thread runtime, model, and effort can change only while the thread is idle` |
| `409` | The thread's current session configuration left the option matrix and the message does not replace it. | `this thread runs a session configuration that is no longer offered; select a currently offered model to continue` |
| `429` | The runtime is at its concurrency cap. Each runtime owns an independent pool of 3 concurrent threads, so one busy runtime cannot take capacity from its peers. | `<Runtime> runtime is already running 3 concurrent threads; retry when one finishes` |
| `502` | A provider that already declared itself running rejects a synchronous message. The host records `thread.error`, finalizes the run, and begins cleanup rather than treating this as startup. | `<Runtime> rejected the message: <provider detail>` |

Thread list response:

```json
{
  "threads": [
    {
      "thread_id": "feature-chat-1",
      "agent_runtime": "codex",
      "model": "gpt-5.6-terra",
      "effort": "high",
      "last_used_at": "2026-06-08T00:05:00Z",
      "status": "running"
    }
  ],
  "next_before": "WyIyMDI2LTA2LTA4VDAwOjA1OjAwWiIsImZlYXR1cmUtY2hhdC0xIl0"
}
```

An uncursored request returns the newest page. `limit=<n>` defaults to 100 and
must be between 1 and 100. When another page exists, the response includes an
opaque `next_before` cursor; pass it unchanged as `before=<cursor>` to load
older threads. Ordering uses `last_used_at` followed by globally unique
`thread_id`, so equal timestamps paginate without omissions. The cursor is a
URL-safe encoding of that composite position, not a thread id or a secret;
clients treat it as opaque so the sort-key representation can change without
changing their parsing logic.

Thread list response fields:

| Field | Type | Meaning |
| --- | --- | --- |
| `threads` | thread array | Recent known threads sorted by `last_used_at` descending. |
| `next_before` | string | Optional opaque cursor for the next older page. Absent when this is the last page. |
| `threads[].thread_id` | string | Client-generated conversation id. |
| `threads[].agent_runtime` | enum | Runtime for this thread: `codex`, `claude_code`, or `hermes`. |
| `threads[].model` | enum | Model for the thread's current provider session. |
| `threads[].effort` | enum | Effort for the thread's current provider session. |
| `threads[].last_used_at` | string | Latest message or successful-settlement timestamp known for this thread. |
| `threads[].status` | enum | Public current state: `running` while an admitted execution still owns the thread fence (including private startup and cleanup), `idle` otherwise. |

The host stores current run state privately on the thread row; lifecycle
markers are not public events. After a host restart or reboot, each thread
left `running` is atomically returned to `idle` and receives one
`thread.error` event (`host runtime restarted while the thread was running`).
Internally, each live execution moves through `STARTING`, `RUNNING`,
`FINISHING`, and `CLOSED`. The database commits admission before STARTING and
finalization before FINISHING, leaving process startup and teardown as
in-memory lifecycle work. A provider publishes a non-empty resumable session
id as soon as it proves one usable; Kern persists that callback immediately
for the matching run and never stores an empty replacement.

`POST /v1/threads/{thread_id}/stop` commits `thread.stopped` and the idle
database state, moves the execution to FINISHING, requests a prompt interrupt,
and returns `accepted`. The owning execution thread performs bounded process
and systemd-scope cleanup. A send during that short cleanup receives the
retryable FINISHING conflict above. If the scope cannot be proven gone, Kern
records `thread.error` and deliberately retains the fence; this indicates host
process-management failure and should be recovered with a host restart.

### Events

```text
GET /v1/threads/{thread_id}/events?since=<seq>&limit=<n>&message_bytes=<b>
GET /v1/events?before=<seq>&limit=<n>
```

Event endpoints:

| Method | Path | Request | Response | Behavior |
| --- | --- | --- | --- | --- |
| `GET` | `/v1/threads/{thread_id}/events?since=<seq>&limit=<n>&message_bytes=<b>` | query parameters optional | Event list response | One chronological page of the flat thread event stream. |
| `GET` | `/v1/events?before=<seq>&limit=<n>` | query parameters optional | Event list response | Lists newest agent events before an optional sequence cursor. |

The two endpoints serve different access patterns. Thread events follow one
conversation: an uncursored request returns the newest page (in chronological
order, so opening a long thread does not scan its full history),
`before=<seq>` loads earlier history, and `since=<seq>` keeps a loaded tail
current with events whose `seq > since` — use the highest returned `seq` as
the next `since`, and keep the same `since` when a response is empty. `since`
and `before` cannot be combined. `limit=<n>` is optional, defaults to 100,
and must be between 1 and 100. `message_bytes=<b>` is optional and must be
between 1 and 200,000: when supplied, each event's `message` and
`error_message` payload text is truncated to that many encoded bytes, and an
activity's `detail` and `output` fields share the same budget (at most 24 KiB
of it goes to `detail`), before the response crosses a proxy boundary; every
clipped value ends with `… (truncated)`.

The audit log across all threads pages newest-first like the network audit
log: the first request returns the newest events, and `before=<seq>`
continues with events whose `seq` is lower than that cursor. `limit=<n>` is
optional, defaults to 100, and must be between 1 and 100.

The host retains only the most recent 1,000,000 agent events; older events are
discarded and can no longer be listed.

Event list response:

```json
{
  "events": [
    {
      "event_id": "event_123",
      "seq": 42,
      "timestamp": "2026-06-08T00:00:00Z",
      "event_type": "thread.message",
      "thread_id": "feature-chat-1",
      "payload": {
        "message": "Update from the agent.",
        "source": "agent"
      }
    }
  ]
}
```

Event list response fields:

| Field | Type | Meaning |
| --- | --- | --- |
| `events` | event array | Events ordered by `seq`: chronological for thread events, newest first for `/v1/events`. Empty when no matching events are available. |

Event fields:

| Field | Type | Values | Meaning |
| --- | --- | --- | --- |
| `event_id` | string |  | Stable event id (`event_<seq>`). |
| `seq` | integer |  | Monotonic host-local event sequence number. |
| `timestamp` | string | RFC 3339 timestamp | Event time. |
| `event_type` | enum | See event types below. | Event type. |
| `thread_id` | string or null |  | Related thread id for thread events, or `null` for agent runtime events. |
| `payload` | object |  | Event-specific JSON payload. |

Event types:

```text
thread.message
thread.activity
thread.error
thread.stopped
agent_runtime.active
agent_runtime.login_completed
agent_runtime.linked_account_reset
agent_runtime.deactivated
```

`thread.message` payload fields:

| Field | Type | Values | Meaning |
| --- | --- | --- | --- |
| `message` | string |  | Message text: every accepted user message and every completed agent message including the final one. |
| `source` | enum | `agent`, `user` | Message source. |

`thread.activity` payload fields:

| Field | Type | Values | Meaning |
| --- | --- | --- | --- |
| `activity` | object |  | One provider-independent activity record, normalized from Claude Code stream-json content blocks, Codex app-server ThreadItems, or Hermes tool-call hook events. |

`activity` object fields:

| Field | Type | Values | Meaning |
| --- | --- | --- | --- |
| `activity.provider` | string |  | The runtime that produced the record. |
| `activity.activity_id` | string |  | Opaque host-scoped correlation key tying `started` and `completed` snapshots of one activity. The host scopes a provider id to its private execution so a fresh provider process may safely reuse ids. |
| `activity.kind` | enum | `reasoning`, `plan`, `command`, `file_change`, `tool`, `agent`, `search`, `image`, `wait`, `status` | Activity category. |
| `activity.phase` | enum | `started`, `completed` | Snapshot phase. An activity with no `completed` snapshot proves it began, not that it finished (for example when the thread was stopped mid-activity). |
| `activity.title` | string |  | Short operator-facing summary. |
| `activity.detail` | string | optional | Rich text detail (a command line, a plan, reasoning text). Retained up to 256 KiB; any clipping ends with `… (truncated)`. |
| `activity.output` | string | optional | Command or tool output. Retained up to 256 KiB with the same truncation marker. |
| `activity.status` | string | optional | Provider status text for the snapshot. |
| `activity.append_output` | boolean | optional | `true` when this snapshot's `output` appends to the previous snapshot's output for the same `activity_id` instead of replacing it. |

`thread.error` payload fields:

| Field | Type | Values | Meaning |
| --- | --- | --- | --- |
| `error_message` | string |  | Human-readable agent failure message. |

`thread.stopped` uses the top-level `thread_id` field and an empty payload
`{}`. It is emitted when `POST /v1/threads/{thread_id}/stop` ends running
work.

`agent_runtime.active` uses `thread_id: null` and payload
`{"agent_runtime": "codex"}`, `{"agent_runtime": "claude_code"}`, or `{"agent_runtime": "hermes"}`.

`agent_runtime.login_completed` uses `thread_id: null` and payload
`{"agent_runtime": "codex"}` or `{"agent_runtime": "claude_code"}`. Hermes has
no login flow.

`agent_runtime.linked_account_reset` uses `thread_id: null` and payload
`{"agent_runtime": "codex"}`, `{"agent_runtime": "claude_code"}`, or `{"agent_runtime": "hermes"}` when an
operator reset cleared that runtime's linked account (the audit record of the
reset-linked-account endpoint).

`agent_runtime.deactivated` uses `thread_id: null` and payload
`{"agent_runtime": "codex"}`, `{"agent_runtime": "claude_code"}`, or `{"agent_runtime": "hermes"}` when a
runtime is disabled because its managed provider integration is disabled.

## Agent Files

```text
GET /v1/agent-files?path=<path>
GET /v1/agent-files/read?path=<path>
POST /v1/agent-files/upload?filename=<name>
```

Agent file endpoints:

| Method | Path | Request | Response | Behavior |
| --- | --- | --- | --- | --- |
| `GET` | `/v1/agent-files?path=<path>` | `path` query parameter is optional; default `/` | Agent file list response | Lists one directory under the agent home, including hidden entries. |
| `GET` | `/v1/agent-files/read?path=<path>` | `path` query parameter is optional; default `/` | Agent file read response | Reads one regular file under the agent home as a UTF-8 text preview. |
| `POST` | `/v1/agent-files/upload?filename=<name>` | Raw file bytes; `Content-Length` is required | `{"file": {...}}` | Uploads one file into the agent home's `user-files/` directory. The body is capped at 25 MiB. |

The API treats `/` as `/mnt/kern-agent/agent-home`. Paths that resolve
outside that home are rejected. Symlinks are not supported: directory listings
omit symlink entries, and direct requests for symlink paths return a validation
error.

Directory listings inspect and return at most 1,000 entries. Returned entries
are sorted. If the scan hits the cap, `truncated` is `true`.

Agent file list response:

```json
{
  "path": "/workspace",
  "truncated": false,
  "entries": [
    {
      "name": ".env",
      "path": "/workspace/.env",
      "type": "file",
      "size_bytes": 123,
      "modified_at": "2026-06-08T00:00:00Z"
    }
  ]
}
```

Agent file read response:

```json
{
  "path": "/workspace/README.md",
  "size_bytes": 123,
  "truncated": false,
  "encoding": "utf-8-replacement",
  "content": "File contents..."
}
```

File reads are capped at 1 MiB. If the file is larger, `truncated` is `true`
and `content` contains the first 1 MiB decoded with replacement characters for
invalid UTF-8 bytes.

Upload `filename` is the original basename, not a path. It must be non-empty,
at most 200 UTF-8 bytes, and contain no slash, backslash, NUL, or control
character. The host publishes a completed upload atomically under
`user-files/` and prefixes its stored name with a sortable UTC timestamp. It
never overwrites an existing file. Incomplete uploads are removed.

```json
{
  "file": {
    "path": "user-files/20260722T120000.123456Z_reference.png",
    "name": "20260722T120000.123456Z_reference.png",
    "original_name": "reference.png",
    "size_bytes": 12345,
    "uploaded_at": "2026-07-22T12:00:00.123456Z"
  }
}
```

`path` is relative to `/mnt/kern-agent/agent-home`, which is also the
agent runtime's working directory. Uploads are durable workspace data and are
not pruned automatically.

## Agent Processes

```text
GET /v1/agent-processes
```

Returns a read-only diagnostic snapshot of Codex, Claude Code, and processes
spawned by those runtimes. This is process state, not turn state: short-lived turn
processes may exit before the next snapshot. The response contains at most 1,000 processes; when
more matching processes exist, `truncated` is `true`.

Response:

```json
{
  "truncated": false,
  "processes": [
    {
      "pid": 1234,
      "state": "S",
      "name": "codex",
      "cmdline": "codex app-server --listen stdio://",
      "rss_bytes": 92274688,
      "elapsed_seconds": 184
    }
  ]
}
```

## Network

```text
GET    /v1/network/policy
PUT    /v1/network/policy
GET    /v1/network-tools/github-credential
PUT    /v1/network-tools/github-credential
DELETE /v1/network-tools/github-credential
POST   /v1/network-tools/github-audit
GET    /v1/network-tools/github-pending-pushes
POST   /v1/network-tools/github-pending-pushes/<id>/approve
POST   /v1/network-tools/github-pending-pushes/<id>/reject
GET    /v1/network/events?before=<seq>&decision=<allowed|denied|all>&limit=<n>
```

Network endpoints:

| Method | Path | Request | Response | Behavior |
| --- | --- | --- | --- | --- |
| `GET` | `/v1/network/policy` | none | Network policy response | Returns active network policy. |
| `PUT` | `/v1/network/policy` | Network policy request | Network policy response | Replaces network policy atomically. Disabling a managed provider integration deactivates its runtime, clears its account pin, closes its live runtime processes, and fails its running turns. |
| `GET` | `/v1/network-tools/github-credential` | none | GitHub credential metadata | Returns credential metadata only; never the token. |
| `PUT` | `/v1/network-tools/github-credential` | GitHub credential request | GitHub credential metadata | Stores or replaces the single fixed GitHub token. The `token` field is write-only. |
| `DELETE` | `/v1/network-tools/github-credential` | none | GitHub credential metadata | Removes the stored credential and withdraws the proxy-injected working token. |
| `POST` | `/v1/network-tools/github-audit` | none | GitHub credential metadata | Force-refreshes the per-repository audits and returns the updated metadata (including `repository_audits`). |
| `GET` | `/v1/network-tools/github-pending-pushes` | none | `{pending_pushes: [...]}` | Lists pushes held by the `.github` approval gate: `id`, `owner`, `repo`, `ref_updates`, `changed_paths`, `requested_at`, `status`. |
| `POST` | `/v1/network-tools/github-pending-pushes/<id>/approve` | none | `{pending_push: {...}}` | Replays the held push to GitHub with the working token through the `approve-github-push` root helper and marks it approved. `404` if unknown, `409` if already resolved, another resolution is in progress, no working token is available (the row stays pending), or the replay fails. Replay failures mark the row `failed` after one best-effort cleanup. |
| `POST` | `/v1/network-tools/github-pending-pushes/<id>/reject` | none | `{pending_push: {...}}` | Cleans up pending quarantine refs (best-effort) and marks the held push rejected. `404`/`409` as above. |
| `GET` | `/v1/network/events?before=<seq>&decision=<allowed\|denied\|all>&limit=<n>` | query parameters optional | Network event response | Lists newest network decision events before an optional sequence cursor. |

The `github-credential` routes work whether or not
`network_integrations.github` is enabled, so the credential can be
staged before the integration is turned on; the proxy-injected working token
is only ever published while GitHub is enabled.

Network policy request:

```json
{
  "network_integrations": {
    "openai": {"enabled": true},
    "github": {
      "enabled": true,
      "write_repositories": [
        {"owner": "infiloop2", "repo": "kern"}
      ]
    },
    "custom": {
      "domains": {
        "example.com": {
          "allow_http_methods": ["GET", "HEAD"],
          "path_guards": ["^/$", "^/docs(?:/.*)?$"]
        }
      }
    }
  }
}
```

The request body is the replacement runtime network controls object using the
schema from [`NetworkControls.md`](NetworkControls.md).

When `PUT /v1/network/policy` is accepted, the replacement policy has been
validated and atomically written. Concurrent replacements are last-writer-wins:
the stored policy is always exactly one submitted body, never a blend.

Network policy response:

The API response uses the operator-facing network controls shape. Managed
integration domains are not listed under the custom integration; the proxy maps
each public field directly to its typed integration config, and credential
secrets are never included.

```json
{
  "network_controls": {
    "network_integrations": {
      "openai": {"enabled": true},
      "github": {
        "enabled": true,
        "write_repositories": [
          {"owner": "infiloop2", "repo": "kern"}
        ]
      },
      "custom": {
        "domains": {
          "example.com": {
            "allow_http_methods": ["GET", "HEAD"],
            "path_guards": ["^/$", "^/docs(?:/.*)?$"]
          }
        }
      }
    }
  },
  "updated_at": "2026-06-08T00:00:00Z"
}
```

Network policy response fields:

| Field | Type | Meaning |
| --- | --- | --- |
| `network_controls` | object | Runtime network controls using the schema from [`NetworkControls.md`](NetworkControls.md). |
| `updated_at` | string | RFC 3339 timestamp for the last policy update. Present in responses only. |

GitHub credential request — fine-grained PAT mode:

```json
{
  "mode": "pat",
  "token": "github_pat_..."
}
```

GitHub credential request — GitHub App mode (the host mints
installation-wide tokens and refreshes them before their one-hour expiry;
the proxy's repo guard, not the token, is the per-repository boundary — see
[NetworkControls.md](NetworkControls.md#github-integration)):

```json
{
  "mode": "app",
  "app_id": "12345",
  "installation_id": "67890",
  "private_key_pem": "-----BEGIN RSA PRIVATE KEY-----\n..."
}
```

GitHub credential request fields (fields outside the chosen mode are
rejected):

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `mode` | string | Yes | `pat` or `app`. |
| `token` | string | `pat` mode only | The fine-grained personal access token. |
| `app_id` | string | `app` mode only | The numeric GitHub App id. |
| `installation_id` | string | `app` mode only | The numeric installation id. |
| `private_key_pem` | string | `app` mode only | The App's PEM private key. |

`token` and `private_key_pem` are write-only: they are encrypted at rest and
never returned by any endpoint or echoed by the UI. While GitHub is enabled
and a credential is stored, the network proxy injects the active token into
policy-approved GitHub requests — the agent never holds the credential —
and plain `git` and `gh` read any repository the token reaches and write to
the configured `write_repositories`; see
[`NetworkControls.md`](NetworkControls.md#github-credential).

GitHub credential metadata response — returned by all three
`github-credential` methods and by `POST /v1/network-tools/github-audit`:

```json
{
  "configured": true,
  "mode": "app",
  "app_id": "12345",
  "installation_id": "67890",
  "app_token_expires_at": "2026-06-08T01:00:00Z",
  "updated_at": "2026-06-08T00:00:00Z",
  "validation": {"status": "ok", "checked_at": "2026-06-08T00:00:00Z"},
  "repository_audits": [
    {
      "owner": "infiloop2",
      "repo": "kern",
      "audited_at": "2026-06-08T00:00:00Z",
      "warnings": [
        {
          "code": "unprotected_default_branch",
          "severity": "critical",
          "message": "The token can push and the default branch is unprotected: ..."
        }
      ]
    }
  ]
}
```

GitHub credential metadata response fields:

| Field | Type | Present | Meaning |
| --- | --- | --- | --- |
| `configured` | boolean | Always | Whether a credential is stored. |
| `mode` | string | When configured | `pat` or `app`. |
| `updated_at` | string | When configured | RFC 3339 time the credential was last stored. |
| `app_id` | string | `app` mode only | The stored GitHub App id. |
| `installation_id` | string | `app` mode only | The stored installation id. |
| `app_token_expires_at` | string | `app` mode, once the host has minted an installation token | Expiry of the current minted token (the host re-mints before it passes). Absent until the first successful mint. |
| `validation` | object | When configured | Credential health: `{"status": "not_checked"}` before the first check, `{"status": "ok", "checked_at": ...}` after a success, `{"status": "error", "message": ..., "checked_at": ...}` after a failure — on failure the working token is withdrawn (fail closed) and the poller retries. |
| `repository_audits` | array | When the policy lists `write_repositories` | One entry per listed write repository, in policy order. Audits warn, never gate: a failed or missing audit never blocks the credential or a policy publish. If no credential is configured, each repository reports an incomplete-audit warning. |

`repository_audits[]` entry fields:

| Field | Type | Present | Meaning |
| --- | --- | --- | --- |
| `owner` | string | Always | The write repository's owner. |
| `repo` | string | Always | The write repository's name. |
| `audited_at` | string | Once an audit attempt has been stored; absent while the first attempt is still pending | RFC 3339 time of the last audit attempt (success or failure). |
| `warnings` | array | Always | Operator warnings, each `{"code", "severity", "message"}` with `severity` `critical` or `warning` — for example a public write repository, an unprotected default branch the token can push, workflows whose triggers expose secrets to PR-influenced code, or an incomplete audit when Kern lacks enough information. Empty means a clean audit. |
| `error` | string | When the last audit attempt failed | Raw failure detail for diagnostics; the same condition also appears as a warning and the next poller pass retries it. |

Network event response:

Network event endpoints return newest-first events. Pass `before=<seq>` to
continue with events whose `seq` is lower than that cursor. `decision=allowed`
or `decision=denied` filters the listed events; `decision=all` is equivalent
to omitting the filter. `limit=<n>` is optional, defaults to 100, and must be
between 1 and 100.

Network events are only defined for HTTP, HTTPS, WebSocket, and secure WebSocket
requests. SSH and other non-HTTP traffic are not represented by this endpoint.

The host retains only the most recent 1,000,000 network events; older events are
discarded and can no longer be listed.

```json
{
  "events": [
    {
      "seq": 42,
      "timestamp": "2026-06-08T00:00:00Z",
      "protocol": "https",
      "method": "GET",
      "host": "api.github.com",
      "port": 443,
      "path": "/repos/infiversehq/kern-host",
      "query": "per_page=5",
      "decision": "allowed"
    }
  ]
}
```

A denied event additionally carries a stable snake_case `reason_code` (e.g.
`host_not_allowed`, `openai_web_tool_denied`) identifying the denial class;
allowed events omit it. The same code is the proxy's 403 response body, and
the agent-facing `recent_network_denials` tool maps it to guidance.

Network event response fields:

| Field | Type | Meaning |
| --- | --- | --- |
| `events` | network event array | Up to `limit` newest-first events. |

Network event fields:

| Field | Type | Values | Meaning |
| --- | --- | --- | --- |
| `seq` | integer |  | Monotonic host-local network event sequence number. |
| `timestamp` | string | RFC 3339 timestamp | Decision time. |
| `protocol` | enum | `http`, `https`, `ws`, `wss` | Request protocol. |
| `method` | enum | `GET`, `HEAD`, `POST`, `PUT`, `PATCH`, `DELETE`, `CONNECT` | HTTP method. For WebSocket requests, this is the handshake method. `CONNECT` appears only on denied HTTPS or secure WebSocket tunnels that were refused before an inner request was read; allowed tunnels are logged with the method of the inner request. |
| `host` | string |  | Requested host. |
| `port` | integer |  | Requested TCP port. |
| `path` | string |  | Request path without the query string. |
| `query` | string |  | Request query string without the leading `?`, or an empty string when no query was present. |
| `decision` | enum | `allowed`, `denied` | Network decision. |
| `reason_code` | string | optional | Present only on denied events: the stable snake_case code for the denial class. The agent-facing `recent_network_denials` tool joins it against per-integration guidance. |

## Apps

```text
GET                 /v1/apps
GET                 /v1/apps/{app_id}/ui/{asset_path}
GET|POST|PUT|DELETE /v1/apps/{app_id}/api/{backend_path}
```

`GET /v1/apps` lists active app packages installed with this host release and
the host-derived resources assigned to each one. Migration-only manifests with
`"deprecated": true` are intentionally absent and have no UI or API routes:

```json
{
  "apps": [
    {
      "id": "agent_chat",
      "title": "Agent Chat",
      "release_stage": "stable",
      "backend": {
        "api_route": "/v1/apps/agent_chat/api/"
      },
      "ui": {
        "iframe_src": "/v1/apps/agent_chat/ui/index.html",
        "sandbox": ["allow-scripts", "allow-forms", "allow-modals"],
        "host_fullscreen": false
      }
    }
  ]
}
```

| Field | Meaning |
| --- | --- |
| `apps[].id`, `title` | Stable manifest id and operator-facing title. |
| `apps[].release_stage` | Required manifest stage: `stable` or `beta`. The admin shell places stable apps in the always-visible Apps section and beta apps in a collapsed Apps (Beta) group; this field grants no additional authority. |
| `apps[].backend.api_route` | Authenticated admin API prefix that reverse-proxies to this app backend. |
| `apps[].ui.iframe_src` | Static entry point mounted by the admin API. |
| `apps[].ui.sandbox` | iframe permissions the admin shell applies. `allow-same-origin` is deliberately absent, so the app frame has an opaque origin. |
| `apps[].ui.host_fullscreen` | Whether the host displays the app over its full shell with an unoccludable host-owned exit control. |

App UI assets under `/v1/apps/{app_id}/ui/` are static and do not require
authentication. They carry a restrictive CSP and no-store cache headers, expose
no state by themselves, and cannot make browser network connections directly.
The admin shell loads the entry point in a sandboxed iframe.

App backend routes require the normal admin session. The admin API consumes that
session itself and forwards no operator credential onward: it sends the JSON
request and query string to the app's host-assigned loopback port with an
`X-Kern-App-Proxy` marker and accepts a JSON response of at most 1 MiB. The browser app bridge pins a request to its own `app_id`;
attempting to bridge to another app returns `403`. App backend failures are
returned through the standard error envelope. App-backend-to-host calls use a
separate peer-authenticated Unix socket and narrow thread-route allowlist,
documented in [Apps architecture](../architecture/apps/apps.md).

## Tools

```text
GET  /v1/tools
PUT  /v1/tools/{tool_id}/config
POST /v1/tools/{tool_id}/enable
POST /v1/tools/{tool_id}/disable
POST /v1/tools/{tool_id}/oauth_connect/start
POST /v1/tools/{tool_id}/oauth_connect/complete
POST /v1/tools/{tool_id}/oauth_connect/disconnect
GET  /v1/tools/{tool_id}/approvals
GET  /v1/tools/{tool_id}/approvals/{approval_id}
POST /v1/tools/{tool_id}/approvals/{approval_id}/approve
POST /v1/tools/{tool_id}/approvals/{approval_id}/deny
GET  /v1/tools/events
GET  /v1/tools/events/{seq}
```

Bundled tool packages the agent can call once the operator enables them; see
the [tool contract](../architecture/tools/tool-contract.md) and
[host integration](../architecture/tools/host-integration.md) for how calls,
state, and approvals flow through the host.

Tool endpoints:

| Method | Path | Request | Response | Behavior |
| --- | --- | --- | --- | --- |
| `GET` | `/v1/tools` | none | Tool list response | Lists every bundled tool with its manifest (actions with per-action data policy, config requirements), enablement, per-tool config status, and OAuth connection account. Responses never include config values, tokens, or client secrets. |
| `PUT` | `/v1/tools/{tool_id}/config` | `{"key", "value"}` | `{"tool_id", "key", "set"}` | Sets one config value declared by that tool's manifest. Config is scoped per tool (a repeated key name holds an independent value per tool) and every value is a secret: write-only, stored encrypted at rest (secretbox); an empty `value` clears the key. `400` when `key` is not declared by `{tool_id}`. |
| `POST` | `/v1/tools/{tool_id}/enable` | none | `{"tool_id", "enabled"}` | Enables the tool for agent calls. Not gated on config: a tool can be enabled with partial or no config set (per-key config status is reported by `GET /v1/tools`); an action that needs an unset key fails when the tool reads it. |
| `POST` | `/v1/tools/{tool_id}/disable` | none | `{"tool_id", "enabled"}` | Disables the tool. Stored connections and credentials are kept; use disconnect to remove them. |
| `POST` | `/v1/tools/{tool_id}/oauth_connect/start` | `{"redirect_uri"}` | `{"authorization_url", "state"}` | Starts the tool's OAuth connect flow (OAuth tools only, `409` otherwise or when disabled). The UI uses `<admin origin>/oauth/callback` as the redirect URI; register that URL with the OAuth provider. Reached over SSH-forwarded localhost it is a loopback URL such as `http://localhost:7443/oauth/callback` (providers accept loopback without HTTPS); reached over a Cloudflare Tunnel hostname it is that HTTPS origin's `/oauth/callback`. Building the URL needs no egress and runs in the admin service; the later code exchange (`oauth_connect/complete`) runs in the dedicated tools service. |
| `POST` | `/v1/tools/{tool_id}/oauth_connect/complete` | `{"code", "state", "redirect_uri"}` | `{"account": {...}}` | Completes the OAuth flow with the provider callback values and stores tokens in the tool credential store. Returns the connected `account` (see `ConnectionAccount` below); `400` for an invalid or expired `state`. |
| `POST` | `/v1/tools/{tool_id}/oauth_connect/disconnect` | none | `{"tool_id", "connected": false}` | Revokes third-party tokens where possible and deletes the stored credential. |
| `GET` | `/v1/tools/{tool_id}/approvals` | none | Approval list response | Lists `{tool_id}`'s action approvals as a bounded working set: pending first (so open decisions surface at the top), then newest decided ones as bounded history. Approvals are addressed under their tool so the operator UI shows each tool's approvals in its own row. Payload is omitted from the list; fetch it per approval. The paginated audit trail is `/v1/tools/events`. |
| `GET` | `/v1/tools/{tool_id}/approvals/{approval_id}` | none | `{"approval"}` | The full approval record for `{approval_id}`, including its (up to 64 KiB) payload. `404` when `{approval_id}` is not an approval of `{tool_id}`. |
| `POST` | `/v1/tools/{tool_id}/approvals/{approval_id}/approve` | none | `{"approval", "result"}` | Approves a pending approval and immediately executes the recorded payload exactly once; the response carries the terminal approval record (`executed` or `failed`) and the execution result. `404` when `{approval_id}` is not an approval of `{tool_id}`; `409` when it is not pending. |
| `POST` | `/v1/tools/{tool_id}/approvals/{approval_id}/deny` | none | `{"approval"}` | Denies a pending approval; terminal. `404` when `{approval_id}` is not an approval of `{tool_id}`; `409` when it is not pending. |
| `GET` | `/v1/tools/events` | `?before=&limit=` | `{"events": [...]}` | The tool audit log, newest first: tool calls, approval decisions, connect/disconnect, enable/disable, and config set/clear events. Pages with the same `before` (an event `seq`) and `limit` cursor model as `/v1/events` and `/v1/network/events`. |
| `GET` | `/v1/tools/events/{seq}` | none | `{"event": {...}}` | Loads one tool event with its exact action `arguments`. The paginated list returns only `has_arguments`, so live refreshes do not repeatedly transfer up to 64 KiB per event. `404` when `{seq}` does not exist. |

Tool list response:

```json
{
  "tools": [
    {
      "tool_id": "example_tool",
      "display_name": "Example Tool",
      "description": "Read and act on a connected third-party account. Sensitive actions are approval-gated.",
      "connection": "oauth",
      "enabled": true,
      "actions": [
        {
          "id": "search_items",
          "description": "Search items.",
          "data_policy": "Read-only. Sends the query to Example and returns item ids and metadata. Runs directly with no approval.",
          "approval": "direct",
          "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
          "output_schema": {"type": "object", "required": ["status"], "properties": {"status": {"type": "string"}}}
        },
        {
          "id": "send_item",
          "description": "Queue approval to send an item.",
          "data_policy": "Sends an item through the connected account. Queued for approval before any third-party state changes.",
          "approval": "operator",
          "input_schema": {"type": "object", "properties": {"item_id": {"type": "string"}}, "required": ["item_id"]},
          "output_schema": {}
        }
      ],
      "config": [
        {"key": "EXAMPLE_CLIENT_ID", "description": "Third-party client id for the hosting deployment.", "set": true},
        {"key": "EXAMPLE_CLIENT_SECRET", "description": "Third-party client secret for the hosting deployment.", "set": true}
      ],
      "protections": [
        "OAuth tokens stay in the host credential store and are never exposed to the agent.",
        "Writes wait for explicit operator approval."
      ],
      "setup_steps": [
        {
          "title": "Create an OAuth client",
          "description": "Create a Web application client and register the exact Kern callback URI.",
          "link_url": "https://provider.example/oauth-guide",
          "link_label": "View provider instructions",
          "image_path": "/guide-assets/provider-oauth-client.png",
          "image_alt": "Provider Web application client form.",
          "show_callback": true,
          "show_config": false
        }
      ],
      "data_summary": {
        "cards": [
          {
            "title": "What leaves this host",
            "description": "Only the query text, filters, and resource ids an action uses.",
            "points": [],
            "links": []
          },
          {
            "title": "Where it can go",
            "description": "Only to Provider's API service.",
            "points": [],
            "links": []
          },
          {
            "title": "What Provider can do with it",
            "description": "Provider processes request data under its privacy policy.",
            "points": [{"label": "Before connecting", "text": "Review the connected account's data settings."}],
            "links": [{"label": "Provider privacy policy", "url": "https://provider.example/privacy"}]
          },
          {
            "title": "How long Provider retains it",
            "description": "Provider retains request records for at most 90 days.",
            "points": [],
            "links": [{"label": "Provider privacy policy", "url": "https://provider.example/privacy"}]
          }
        ]
      },
      "connection_status": {"connected": true, "account": {"id": "provider-sub-1", "label": "operator@example.com", "scopes": ["..."]}}
    }
  ]
}
```

The example above is illustrative; the fields are the same for every bundled
tool. Each tool object has:

| Field | Meaning |
| --- | --- |
| `tool_id` | Stable package identifier; keys config, credentials, approvals, and audit records. |
| `display_name`, `description` | Operator-facing name and one-line summary from the manifest. |
| `connection` | `oauth` (operator third-party auth) or `enable_only` (deployment key only). |
| `enabled` | Whether the operator has enabled the tool for agent calls. |
| `actions[]` | Each action's stable `id`, `description`, per-action `data_policy`, `approval` (`direct` or `operator`), `input_schema`, and `output_schema` (empty `{}` for approval-gated actions, which return a user-visible message rather than a JSON result). |
| `config[]` | This tool's declared config keys with `description` and `set`. All config is secret and scoped per tool; values are never returned (see `PUT /v1/tools/{tool_id}/config`). |
| `protections[]` | Short operator-facing safeguards rendered in the tool's info popover and full Integration Guides entry. |
| `setup_steps[]` | Ordered provider-side and Kern setup steps. A step may include a provider documentation link and a local audited screenshot with alt text; `show_callback`/`show_config` render this host's OAuth callback URI or the tool's config keys inside that step. |
| `data_summary` | The operator-facing data story as exactly four `cards`, in order: what leaves this host, where it can go, what the third party can do with it, and how long it retains it. Each card has a `description` and/or labeled `points`, plus authoritative policy `links`. |
| `connection_status` | OAuth tools only: `{"connected": bool, "account"?: ConnectionAccount}`; never contains tokens or client secrets. |

`ConnectionAccount` is the explicit connected-account structure every OAuth tool
returns and the host stores/displays: `{"id", "label", "scopes"}` — `id` is the
stable provider account identifier (e.g. a Google `sub`) used to bind approvals
to the connected account, `label` is the human-readable account (an email), and
`scopes` are the granted OAuth scopes.

Approval record:

```json
{
  "approval_id": "approval_7.Xr9K2unguessable-token",
  "tool_id": "gmail",
  "action_id": "send_email",
  "status": "pending",
  "summary": "Send Gmail message to billing@example.com with subject \"Invoice\".",
  "payload": {"...": "the exact JSON the tool executes if approved"},
  "result": "",
  "created_at": 1782200000,
  "decided_at": 0
}
```

| Field | Type | Value |
| --- | --- | --- |
| `approval_id` | string | Host-assigned id `approval_<number>.<token>`: the sequential number plus an unguessable capability token, so the id itself is the agent's poll capability and a guessed number never resolves. |
| `tool_id` | string | The tool the approval belongs to. |
| `action_id` | string | The manifest action id (`ActionSpec.id`) the approval will execute. |
| `status` | string | One of `pending`, `approved`, `denied`, `expired`, `executed`, `failed`. Terminal states are `denied`, `expired`, `executed`, `failed`. |
| `summary` | string | Redacted, operator-displayable description of the proposed action (1-500 UTF-8 bytes). |
| `payload` | object | The exact JSON the tool executes if approved (up to 64 KiB). Omitted from the list response; returned by `GET /v1/tools/{tool_id}/approvals/{approval_id}`. |
| `result` | string | The terminal outcome text: the executed action's user-visible `ApprovalExecuted.message`, or the failure error. Empty until `executed` or `failed`. |
| `created_at` | integer | Unix seconds when the approval was created. |
| `decided_at` | integer | Unix seconds when the approval reached a terminal state; `0` while `pending`. |

Every approval is single-use, and `pending` approvals expire after 24 hours. New
approval creation is capped while too many are already pending, so a runaway agent
cannot grow admin storage without bound or hide older decisions from the operator.
Agents queue approvals by calling approval-gated actions through the tools MCP
surface; the pending call response carries the token-bearing `approval_id`, which
`check_tool_approval` verifies before returning the summary or terminal result, so
another agent process cannot enumerate old approvals by guessing sequential ids.
Only these admin endpoints decide approvals.

Tool event summary (from `/v1/tools/events`):

```json
{
  "seq": 412,
  "timestamp": "2026-07-08T01:15:00Z",
  "event_id": "tool_event_412",
  "tool_id": "example_tool",
  "action_id": "send_item",
  "outcome": "executed",
  "detail": "approval_7",
  "has_arguments": true
}
```

| Field | Type | Value |
| --- | --- | --- |
| `seq` | integer | Monotonic event id, returned newest-first. Page older events by setting `before` to the oldest `seq` you have. |
| `timestamp` | string | ISO 8601 UTC time the event was recorded. |
| `event_id` | string | `tool_event_<seq>`. |
| `tool_id` | string | The tool the event concerns. |
| `action_id` | string | The manifest action id (`ActionSpec.id`) for a call; `oauth_connect` for a connect/disconnect, `enablement` for an enable/disable, or `config` for a config change. |
| `outcome` | string | For a tool call: `executed`, `pending_approval`, or `failed`. For an approval decision: `executed`, `failed`, or `denied`. For a connection change: `connected` or `disconnected`. For an enablement change: `enabled` or `disabled`. For a config change: `set` or `cleared`. |
| `detail` | string | Short context string: an error message, the related `approval_id`, the connected account label, or the config key that changed. May be empty. |
| `has_arguments` | boolean | `true` for an accepted tool call or approval decision, including calls whose exact argument object is `{}`. `false` for config, enablement, and connection lifecycle events. |

`GET /v1/tools/events/{seq}` returns the same fields plus `arguments`, either
the exact schema-validated tool input, the exact approved/denied payload, or
`null` for a lifecycle event. Argument objects are capped at 64 KiB. They are
stored in the local Postgres `tool_events.arguments` column. The tools service
writes tool-call, approval, and OAuth events through its scoped database role;
the admin service writes config and enablement events and reads the table for
the Tool Audit Log. The UI loads arguments only after the operator expands an
event. Tool config values and OAuth callback parameters are never stored as
event arguments.

## Host Errors

```text
GET /v1/host-errors
GET /v1/host-errors/{id}
```

| Method | Path | Request | Response | Behavior |
| --- | --- | --- | --- | --- |
| `GET` | `/v1/host-errors` | `?before=&limit=&service=` | `{"events": [...]}` | Lists unexpected host-service failures newest first. `before` is the row's ordering `seq`, `limit` is 1–100, and optional `service` selects one exact systemd service name. List rows omit traceback, context, and fingerprint. |
| `GET` | `/v1/host-errors/{id}` | none | `{"error": {...}}` | Loads the full bounded diagnostic record by its stable numeric `id`, including traceback, context, and fingerprint. Returns `404` when the row does not exist. |

The API is display-only: there are no resolve, dismiss, delete, or report
routes. Expected thread, provider, tool, validation, and network-policy outcomes
do not belong in this log. See [Host error diagnostics](../architecture/host-errors.md)
for best-effort capture, safety, and retention.

Host error list rows contain stable `id` and `error_id` fields, the rotating
newest-first paging cursor `seq`, `first_seen_at`,
`last_seen_at`, `service`, `component`, `kind`, `exception_type`, `summary`,
`occurrence_count`, `host_version`, `boot_id`, `pid`, and `has_details`. Detail
reads additionally contain `traceback`, `context`, and `fingerprint`.

## Host Runtime

```text
POST /v1/host-runtime/reboot
```

Host runtime endpoints:

| Method | Path | Request | Response | Behavior |
| --- | --- | --- | --- | --- |
| `POST` | `/v1/host-runtime/reboot` | none | Host runtime mutation response | Reboots the host machine. |

Host runtime mutation response:

```json
{
  "status": "accepted"
}
```

Host runtime mutation response fields:

| Field | Type | Values | Meaning |
| --- | --- | --- | --- |
| `status` | enum | `accepted` | Host runtime operation was accepted and will be applied asynchronously. |
