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

`kern-browser` (UID 47754) owns `/mnt/kern-admin/browser-state` (0700) on the
existing private encrypted admin volume. It runs separately from `kern-tools`
and `kern-admin`; its storage is beside `tools-state`, not inside it. It has
no database credentials, agent-home access, or public TCP/CDP listener.
Chromium and Playwright are root-owned deploy artifacts. Deployment installs full
Chromium (`--no-shell`), and the service selects `channel="chromium"` for its
unified headless mode, which uses the normal browser implementation. It does not
use Playwright's separate headless shell. Chromium's sandbox is
enabled on the Ubuntu 22.04 images used by AWS and Lima. No custom AppArmor
profile or global user-namespace override is installed. Public HTTPS egress is allowed. DNS over TCP/UDP port 53 is allowed to any
destination, matching the tools service and supporting local resolvers without
deployment-time DNS configuration. This deliberately includes private addresses
on port 53. All other private, loopback, and metadata destinations are denied,
including browser background traffic. These blocks prevent access to services
on the same host (such as `127.0.0.1`), internal networks (such as `10.0.0.0/8`),
and cloud instance metadata (`169.254.169.254`).
Browser routing also rejects non-HTTPS resources. This disables Chromium's HTTP
cache, an accepted cost of the URL policy. Agent proxy policy is unchanged.
The browser uses a Linux desktop Chrome user agent matching its installed
version. It still exposes normal automation signals; login acceptance is not
guaranteed and login challenges remain operator-controlled.

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
checks; Workspace principals are rejected. Except for list, disconnect, and cancel, the Browser integration must be enabled.

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

Each account directory contains a private `auth.json` snapshot and an atomically
replaced, fsynced `state.json`. Chromium does not use this directory as its user
data directory. Each launch creates a fresh non-persistent context, restores
cookies, local storage and IndexedDB from `auth.json`, and saves updated state
before closing after operator use, login checks and actions. Disconnect closes
without saving. Readiness probes never save authentication state.

Only the browser service reads or writes snapshots. Files are mode 0600 in
mode 0700 account directories. Snapshot writes share a **1 GB total saved-state
budget** across the service directory, including space for the old snapshot
and its atomic replacement. A rejected write keeps the previous snapshot and
closes Chromium; disconnect remains available to free space. This is an
application write budget, not a filesystem quota or a bound on RAM or temporary
files. The service's private `/tmp` holds Chromium's temporary working files;
HOME and XDG cache/config paths point there too. Closing Chromium removes its
normal temporary profile. Temporary files do not live on the admin volume.

This uses Playwright 1.60's [storage-state API](https://playwright.dev/python/docs/auth#reusing-signed-in-state),
with IndexedDB explicitly included. Snapshots do not include HTTP cache,
browsing history or session storage.
IndexedDB and local storage can contain application data as well as credentials,
so a snapshot is not necessarily a tiny cookies-only file. A crash before saving
may lose refreshed login state and require another operator login.

Alongside account identity and login state, `state.json`
stores a small `usage` mapping keyed by action name. Each entry contains only
the latest UTC `day` and `count`. Account listing excludes usage. There is no
browser posting-history archive; normal approval history records execution.

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
