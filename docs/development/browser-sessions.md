# Browser service architecture and interfaces

This is the canonical architecture and interface reference for the Browser
integration. Browser is a bundled `enable_only` tool. Its manifest declares
`host_service_dependency="kern-browser.service"`; the isolated service starts
with the host even when the integration is disabled. It launches Chromium only
for an operator login, a login check, readiness probe, or a coded action.

## Code layout

- `service.py`: socket authorization and serialized request dispatch.
- `accounts.py`: saved accounts, login state, operator leases and action counts.
- `browser.py`: Chromium lifecycle, auth snapshots, screenshots and operator input.
- `display.py`: private authenticated Xvfb lifecycle for windowed Chromium.
- `browser_network/`: in-process public HTTPS relay, private connection settings and verified Decodo TLS transport.
- `providers/__init__.py`: provider contract and explicit supported-provider registry.
- `providers/x.py`: X login URL and verified handle extraction/validation.
- `actions/x_post_tweet.py`: X connection check, inputs, daily cap and post/reply workflow.
- `host/tools/browser/__init__.py`: agent-facing actions and standard approvals.

## Ownership and boundaries

```text
Operator popup → authenticated admin API → /operator/* ┐
                                                     ├→ kern-browser → Chromium → X
Agent → Tool API → Browser tool → /actions/*           ┘
             ↳ ordinary Host API approvals
```

`kern-browser` (UID 47754) runs separately from `kern-tools` and `kern-admin`.
Its peer-authenticated Postgres role has access only to `browser_settings`,
`browser_accounts`, and the existing secretbox encryption key. The agent and
tools roles cannot read those tables. No database password or new Linux account
is introduced. The service cannot access agent-home or the admin volume directly,
and has no public TCP/CDP listener. Postgres persists Browser state on the admin
volume alongside other host state.
Chromium and Playwright are root-owned deploy artifacts. Deployment installs full
Chromium (`--no-shell`), and the service selects `channel="chromium"` in windowed
mode on a temporary Xvfb display. Xvfb is included in Playwright's installed
Linux dependencies. Each browser owns a display with TCP listening disabled and
a random MIT-MAGIC-COOKIE-1 credential in a mode-0600 Xauthority file inside a
mode-0700 temporary directory. This also protects the abstract Unix socket from
other local users. Display readiness is bounded; failed launches and browser
closure terminate the display and remove its credentials. Both processes remain
inside the Browser service's resource and filesystem boundaries. Chromium's sandbox is
enabled on the Ubuntu 22.04 images used by AWS and Lima. No custom AppArmor
profile or global user-namespace override is installed. Chromium uses a local
HTTPS relay on port 7447, running as a thread in the same Browser service and UID.
Connection settings use a constrained database row; the proxy password is encrypted
using Kern's existing secretbox mechanism. The relay accepts HTTPS CONNECT on
port 443. Direct uses the system's ordinary hostname connection; Decodo receives
the website hostname and resolves it remotely. There is no DNS precheck, IP
pinning, or custom destination-address retry loop. The Browser UID firewall is
the host's private-network and metadata boundary. It cannot enforce the remote
proxy's destination policy; Decodo is trusted with destination resolution and
routing. Website TLS remains end to end.
Chromium's implicit loopback proxy bypass is disabled, no Direct fallback is
configured, and non-proxied WebRTC UDP, QUIC and background networking are disabled.
The shared Browser UID can reach public TCP 443/7000 and DNS; its firewall blocks
private/metadata destinations and keeps other UIDs off the relay. Proxy routing
is enforced by Chromium configuration and relay code, not a separate egress UID.
A compromised Browser process could therefore bypass the configured proxy to a
public destination. Direct destinations and the Decodo gateway resolve on the
host, including through local resolvers on port 53. Proxied website hostnames
resolve at Decodo.
Browser routing also rejects non-HTTPS resources. This disables Chromium's HTTP
cache, an accepted cost of the URL policy. Agent proxy policy is unchanged.
Chromium supplies its own user agent and client hints; Kern does not override
them. Automation-specific launch signals are reduced using Chromium options. Windowed operation and native input
events improve browser compatibility; they do not make automation undetectable.
Login acceptance is not guaranteed and challenges remain operator-controlled.

Browser joins `kern_workspace.slice`, alongside transcription and embeddings,
so it has lower CPU priority than the admin control plane. Its whole process
tree has `CPUQuota=100%` (one core), `CPUWeight=25`, `IOWeight=25`,
`MemoryMax=2G` and `TasksMax=256`. These bound Browser usage and reduce its
priority during contention; they do not reserve memory or guarantee UI latency.
Shutdown sends SIGTERM to the service first (`KillMode=mixed`), allowing an
in-flight action and browser cleanup to finish, with a 180-second stop timeout.
The IPC client also uses a plain 180-second timeout, allowing for launch,
navigation, submission and snapshot work. This is not a request deadline;
a stalled worker or lost response can still leave publication unconfirmed.

The private transport is JSON over HTTP POST on
`/run/kern-browser/browser.sock`. The server checks kernel-supplied `SO_PEERCRED`
before allocating a request thread, admitting only `kern-admin` and `kern-tools`.
It then checks the caller UID against the route namespace on every request.
Neither the agent nor Chromium can call these interfaces. The tools process
cannot use operator routes, and the admin process cannot submit tool actions.
Screens, typed input, passwords, and cookies never enter agent tool results.

## Connected accounts

The operator popup forwards pointer hover, drag, button and wheel events. It
coalesces unsent movement without crossing a key, button, text or scroll event,
so a slow connection does not accumulate an unbounded hover queue. Desktop
characters use keyboard events (with Chromium text insertion for characters
outside Playwright's keyboard map). Paste and the mobile text bar remain bulk
text insertion. Approved X posts use sequential keyboard input at a fixed 50 ms
per character, then wait for an enabled submit button. This pacing is not a
human-presence claim or a substitute for a successful live-site test.

A saved account is the first-class service object:

```json
{
  "account_id": "acct_0123456789abcdef0123456789abcdef",
  "provider": "x",
  "provider_identifier": "example",
  "state": "connected",
  "checked_at": "2026-09-28T14:00:00+00:00"
}
```

`account_id` is a random Kern identifier. `provider` currently supports only `x`.
`provider_identifier` is defined and verified by the provider adapter. For X,
it is the lowercase handle without `@`, detected from the signed-in profile
link. Future providers may use another verified identifier; email availability
is not assumed. We do not currently extract a provider user ID or email. Each account
owns private saved authentication state as an implementation detail; tools never
receive cookies or storage snapshots.

Saved account states are exactly:

| State | Meaning |
| --- | --- |
| `connected` | The saved identity passed the last login check. Actions still apply their own approvals and limits. |
| `needs_attention` | Login is unverified, missing, or different. All agent actions using the account are blocked. |

There is no saved `connecting` or `disconnected` state. New logins are temporary,
identified by an operator-only `login_id`. Only successful Save creates a saved
account. Cancel, ten minutes of inactivity, or a service restart discards the
unfinished login and its browser files. Up to five saved accounts are allowed;
only one interactive browser can be open at once.

Opening an existing account marks it `needs_attention` before browser control
is granted. Successful Save or Check login restores `connected` only for the
same handle. Cancel or lease expiry releases control but leaves the account
paused. A changed handle cannot silently replace the saved identity; this
includes renames, which cannot currently be distinguished from switching
accounts. Connect a new account for a different handle.

A failed check does not delete data. Open browser lets the operator restore the
same external account under its existing Kern account ID. Disconnect deletes
that account's entire directory, including cookies and usage data, and releases
its slot. Connecting again creates a new account ID, even for the same X account.
Daily limits are scoped to each Kern connection.

## Admin interface

The admin API forwards `POST /v1/browser/{operation}` to the identically named
`POST /operator/{operation}` (except `ready`, which is a service-only host probe). It requires the ordinary admin cookie and CSRF
checks; Workspace principals are rejected. Except for list, disconnect, cancel and connection settings/test operations, the Browser integration must be enabled.

All bodies are JSON objects. Below, `account` means `{account_id: string}`;
`reference` means exactly one of `{account_id: string}` or `{login_id: string}`.
`control` adds the opaque `lease` returned by Open. Unknown fields are rejected.

| Operation | Body | Response / effect |
| --- | --- | --- |
| `ready` | `{}` | `{ready: true}` after a temporary Chromium frame probe. |
| `list` | `{}` | `{accounts: Account[]}`; no temporary logins, usage data, or secrets. |
| `create` | `{provider: "x"}` | `{login_id}`; allocates one temporary login without creating an account. |
| `open` | reference | `{site, lease, width, height}`; launches the fixed provider URL and grants operator control. |
| `frame` | control | `{image, origin}`; JPEG base64 plus current origin, with no-store admin responses. |
| `input` | control plus input fields below | `{ok: true}`; input refreshes the ten-minute lease. |
| `save` | control | Verifies identity and closes Chromium. Returns the full Account. |
| `cancel` | control | Closes Chromium; discards an unfinished login (`{ok: true}`) or returns the existing Account, still paused. |
| `check` | account | Runs the provider identity check and returns the Account. Requires the popup to be closed. |
| `disconnect` | account | `{ok: true}`; deletes the account, browser profile and usage data. |

Inputs are fixed: `kind: "pointer"` plus `phase` (`down`, `move`, `up`) and integer
`x,y` within the 1100×760 viewport; `kind: "click"` with the same coordinates; `kind: "text"` plus `text` of 1–4096 characters;
`kind: "key"` plus `key` (`Enter`, `Tab`, `Shift+Tab`, `Backspace`, `Delete`, `Escape`,
`ArrowLeft`, `ArrowRight`, `ArrowUp`, `ArrowDown`, `Home`, `End`, `Control+a`); `kind: "scroll"` plus
integer `delta` between -1000 and 1000; `kind: "home"` to return to the fixed
provider URL; or `kind: "reload"` to reload the current page. Frames alone do not extend a lease. No operation accepts caller
JavaScript, selectors, cookies, or arbitrary navigation URLs.

Reload page reloads the remote website. Chromium still opens the provider home
URL for new and saved connections. Host diagnostics records failed document,
script, stylesheet, XHR and fetch requests (hostname, resource type, HTTP status
or Chromium error code), Browser URL policy blocks, uncaught page script errors,
and screenshot capture failures. These are events from the whole browser session,
including background pages, not a verdict about the displayed page. Each session
logs at most 20 distinct failures, with duplicates suppressed until Chromium is
closed. Logs exclude URL paths, page content and raw error messages. A blank page
without one of these failures produces no warning. The popup reports image-load
and frame-request errors separately from control errors, clears them when an
image loads, and continues polling after transient capture failures. It does not
inspect image pixels.

The popup streams images from hosted Chromium. Clicks and desktop typing go to
the selected remote field. A compact phone text bar supplies native keyboard
input. Login challenges remain operator-controlled. There is no editable
address bar; verification providers may be opened by X in the same remote view.
Downloads, file uploads, service workers, and WebSockets are unsupported.

## Tools service interface and approval flow

Only `kern-tools` can call these private routes:

| Route | Body | Response |
| --- | --- | --- |
| `/actions/list` | `{}` | `{accounts: Account[]}`; reads saved metadata without browsing. |
| `/actions/post_tweet` | `{account_id, provider_identifier, text, in_reply_to_tweet_id?}` | `{status: "posted", url}`; executes the already-approved proposal. |

The agent-facing tool actions are `browser.x_connection_status` and
`browser.x_post_tweet`. Posting accepts `account_id`, exact `text`, and optional
numeric `in_reply_to_tweet_id`. The agent does not supply the provider identifier
or a request ID. Text is nonempty and at most 280 characters. Reply IDs contain 1 to 25 digits.
Exact-text approval is the content control; approved text is structurally
validated without the outbound parameter guard.

The Browser tool resolves the saved X identity and calls the normal Host API
`approvals.request`. The approval freezes the action, account ID,
`provider_identifier`, exact text and optional reply target. Approval execution
rechecks the connection, then calls the private posting route once. The host
also checks that the tool remains enabled. Denied approvals send nothing.
Approval can be given by the operator or an applicable configured auto-approval
policy. There is no per-account posting-enable flag.

Each approval authorizes one attempt. Only a confirmed X response returns
success and a post URL. All other outcomes return failure and finish the call.
If submission may have occurred, the failure tells the operator to check X
before approving another attempt. There is no automatic retry, duplicate-text
check, idempotency API, uncertain status, reconciliation endpoint or persistent
submission-error pause. A new attempt requires a new tool request and approval;
separately approved calls may intentionally publish identical content.

The service applies a common connected-state and operator-control gate to
account-scoped tool dispatch. Future actions inherit this gate. Each provider file supplies its login URL and identity verification through the
shared Provider contract. Each action file validates its expected provider and
owns its workflow and limits. Adding LinkedIn requires its own provider file,
registry entry and explicitly exposed action files. It does not give agents
general browser control.

## Action usage and limits

Account identity, status and daily posting usage use typed database columns.
Cookies, localStorage and IndexedDB are serialized and encrypted in the account's
`auth_ciphertext` column. Each launch creates a fresh context and restores the
decrypted snapshot directly from memory; no auth snapshot file is written.
Updated snapshots are saved before closing after operator use, login checks and
actions. Disconnect closes without saving and deletes the complete account row.
Unfinished logins stay only in memory and disappear on restart or cancellation.
Readiness probes never save authentication state.

Snapshots have a **64 MiB serialized limit per account** to leave encryption and
decryption headroom within the service memory limit. Accumulation is bounded
before encryption, and a conservative ciphertext-size estimate is checked before
calling OpenSSL. Snapshot writes also share a **1 GB total ciphertext budget**
across saved accounts.
A transaction locks the account table while checking capacity and saving, so a
rejected write retains the previous snapshot. This limits live stored values,
not Postgres WAL, old row versions, RAM or temporary files. Database failures
surface as failed operations; unavailable settings never silently select Direct.
Secretbox encryption reduces accidental exposure; its key is in the database,
so it does not protect against a full database dump or host compromise.

The service's private `/tmp` holds Chromium's temporary working files; HOME and
XDG cache/config paths point there too. Closing Chromium removes its normal
temporary profile. Temporary files do not live on the admin volume.
Deployment stops the old Browser service and deletes the retired
`/mnt/kern-admin/browser-state` directory without importing its files. Existing
file-based logins and proxy settings must be entered again. Browser state already
stored in Postgres is preserved across deployments.

This uses Playwright 1.60's [storage-state API](https://playwright.dev/python/docs/auth#reusing-signed-in-state),
with IndexedDB explicitly included. Snapshots do not include HTTP cache,
browsing history or session storage.
IndexedDB and local storage can contain application data as well as credentials,
so a snapshot is not necessarily a tiny cookies-only file. A crash before saving
may lose refreshed login state and require another operator login.

Alongside account identity and login state, the `browser_accounts` row
stores the latest UTC `usage_day` and `usage_count` for posting. Account listing
excludes usage. There is no browser posting-history archive; normal approval history records execution.

`x_post_tweet` enforces **50 submission attempts per connected account per UTC
day**, shared by posts and replies. This is a Kern action policy, not an X quota
or an agent-supplied parameter. After preparing the composer, the service saves
the incremented count before its single submit click. An attempted submission still
counts if X rejects it or confirmation fails. A request denied in Kern never
reaches the Browser service and does not count. Validation and preparation
failures also do not count. A new day resets that action's count on its next submission. Disconnect
deletes all usage with the account.

All service operations run on one worker, with a request-wide busy lock. The
limit check and count update happen within the serialized action. Tools do not
read a count and later claim a slot. Requests wait up to one second for brief
expiry maintenance to finish; longer contention returns busy without a retry
queue. The admin bulk-approval UI serializes Browser decisions. The
auto-approval worker already executes its whole batch sequentially through the
synchronous tools client, so it needs no Browser-specific queue. Overlapping
manual and automatic calls can still fail busy before submission.

The X adapter verifies the live handle, prepares the composer or exact reply
target, waits for the target and enabled submit button, and scopes editing
and submission to the composer dialog. A post is confirmed only when X's
CreateTweet response verifies the expected author, post ID and reply target,
including the `TweetWithVisibilityResults` wrapper. Explicit provider rejections
return bounded reasons for known error codes, never raw provider messages.
After the action, snapshot/close failures are recorded in Host diagnostics and
cannot replace its confirmed URL or original failure.
Host diagnostics tag posting failures with `prepare`, `confirm`, `rejected` or
`cleanup`, preserving a source stack and exception type. Kern-authored reasons
include HTTP status, known X rejection codes, or the failed identity/reply
check. Raw Playwright messages, response bodies, post text and credentials are
not logged. These diagnostics help debug the operator's live post/reply test;
fixture success alone does not establish live X compatibility.
Submission failure does not change the account's recorded login state; explicit
login checks and operator control continue to govern that state.

## Failures and verification

Unauthorized admitted callers receive HTTP 403; invalid operations, account
state, expired leases, and busy service return 409 with bounded errors. Unexpected
failures return 503 and emit a redacted Host diagnostic with operation and stack,
not provider DOM, typed input, cookies, or exception messages. The unit also
reports abnormal exits. Socket failures surface as Browser service errors;
after a lost response around submission, check the normal approval outcome
and X before requesting a new approval.

Unit tests exercise persistent identity binding, temporary-login cleanup,
account isolation, snapshot size limits, approvals, terminal failures, daily caps, and
socket capability separation. The focused Chromium journey covers popup input,
initial Save, reconnect, login checks, removal of posting settings, and disconnect
against fixtures. An offline real-Chromium adapter test covers selectors and
submission responses, plus cookie/local-storage/IndexedDB restoration across full
browser restarts, refreshed snapshots and account isolation. This uses dummy
fixture data and never a real X login. The adapter test uses the service's
Chromium channel without substituting a different browser executable.
Host smoke covers service ownership, private paths,
socket admission, and blocked internal-network access. Fixtures and green CI do
not establish compatibility with live X: a real login and submission require
an explicitly authorized live-account test.


## Browser connection settings

Under Home > Integrations > Browser, the operator chooses Direct or optional
Decodo Residential. A residential proxy is preferred on AWS because websites
may restrict datacenter addresses. This is not proof of the cause of a login
failure and does not guarantee website acceptance.

Decodo uses its documented `https://gate.decodo.com:7000` Residential gateway.
The operator enters a base proxy username/password and chooses one location.
The hardcoded presets in `browser_network/config.py` supply all targeting and
browser identity fields; there are no separate overrides:

| Location | Country | Decodo city | Browser language | Timezone |
| --- | --- | --- | --- | --- |
| New York | `us` | `new_york` | `en-US` | `America/New_York` |
| London | `gb` | `london` | `en-GB` | `Europe/London` |

Only the location ID is saved. Public settings include the supported choices
and derived identity fields, so the UI and browser share one source of truth.
The gateway is fixed: arbitrary, static ISP and datacenter endpoints cannot be
configured. Kern trusts Decodo's product classification rather than claiming to
certify each exit IP. API keys are not proxy credentials.

Kern generates and saves a random sticky session ID, reused across connections
and restarts. It requests 1,440 minutes (24 hours). Saving the same route keeps
that ID; changing username/location creates a new one. Peer availability can
shorten the assignment and IPs can change. Kern keeps country/city filters and
never widens them or falls back to Direct. Website geolocation may disagree.

The selected preset supplies the browser language and IANA timezone, including
daylight-saving transitions. These drive native Chromium locale/Accept-Language and date formatting rather
than JavaScript property replacements. The headed sandboxed browser keeps its
native UA/client hints, a consistent screen/viewport and scale, and saved
cookies/localStorage/IndexedDB. The automation infobar flag is omitted and
Chromium's AutomationControlled feature is disabled. No random fingerprint,
canvas/GPU/font spoofing, forged Windows/macOS UA, or CAPTCHA solver is added.
Chromium remains automated and detectable; a custom hosted browser's claims
cannot be reproduced by promising that a handful of flags makes a human.
Service workers, WebSockets and nonproxied WebRTC UDP remain restricted.

Settings are operator-only and serialized with Browser actions. Finish the
login popup before saving/testing. Changing settings closes existing tunnels;
Direct removes proxy credentials while keeping saved account logins. Blank
password retains the secret only for the same username. Public responses omit
password and the internal sticky token. No migration is provided for the
unreleased earlier draft.

The network service verifies the gateway certificate and hostname before
sending CONNECT or proxy authentication. It never downgrades to HTTP or ignores
certificate failures. CONNECT carries the destination hostname, matching ordinary
HTTPS proxy clients; website TLS runs end to end inside the encrypted proxy tunnel.
Proxy credentials never reach Chromium or the destination website.

Test saved connection runs the installed curl against `https://api.ipify.org`
through the same local relay as Chromium. Curl verifies website TLS and limits
the whole request to 30 seconds, including time waiting for a relay stuck in DNS;
the subprocess has a 35-second backstop. Curl configuration files and proxy
bypass are disabled. Proxy credentials remain in the relay and never enter curl
arguments. The test returns only the public IP, not a location,
residential-classification or X login verdict.

Host diagnostics records Direct/gateway connection, gateway TLS and upstream
CONNECT failures with the exception type, errno when available, hostname and
proxy HTTP status. System DNS errors appear as `gaierror` at the connection
stage. The test additionally records curl's exit code (28 means timeout).
There is no custom DNS worker or hard deadline on libc resolution inside a
relay thread; the caller's test deadline remains bounded independently.
Proxy failures additionally record the gateway, elapsed milliseconds, exception
message, received header byte count, header completeness, CONNECT status line
and up to 16 response headers. Exact matches for the proxy username, password,
Basic authorization value, base username and session are redacted before the
existing diagnostic limits (512 bytes per field, 4096 bytes per context) apply.
Authorization, Proxy-Authorization, Cookie and Set-Cookie values are also
redacted because the gateway can issue credentials not known to Kern.
Incomplete trailing header lines are omitted. Decodo's `x-error-message` also
appears as `proxy_error`, limited to 512 printable ASCII characters.
The local relay passes this sanitized failure to the connection test, which
shows it inline. Curl captures headers separately from the IP response; only
the first (local relay) header block can supply a test error, regardless of
its HTTP version or status. Response bodies and destination website headers
are not logged.

The relay shares `kern-browser.service` and its resource limits. Settings and
connection tests use in-process calls from the existing operator-only Browser
interface. Agents cannot configure connections or use the relay. No separate
network account, service or control socket is installed. Tunnels, request
headers, idle time and total lifetime are bounded.

Admin `POST /v1/browser/network_get` and `network_test` take `{}`.
`network_save` takes exactly one of:

```json
{"mode":"direct"}
{"mode":"decodo","username":"example","password":"...","location":"new_york"}
```

Offline tests cover encrypted authentication, certificate rejection, nested TLS,
credential lifecycle, sticky identity, location validation, hostname forwarding,
connection-test deadlines, diagnostic redaction and caller isolation. Deployment
verification probes the Browser UID firewall's private-network/metadata blocks. Real Chromium fixtures cover locale/timezone,
native UA/client hints, language headers/APIs, daylight-saving offsets, input,
auth restoration and settings UI. They do not
prove live Decodo delivery or X acceptance; those need an operator account.

Provider references: [Decodo protocols](https://help.decodo.com/docs/residential-proxy-protocols),
[Decodo targeting](https://help.decodo.com/docs/residential-proxy-user-pass-requests),
[Decodo sticky sessions](https://help.decodo.com/docs/residential-proxy-custom-sticky-sessions).
