# Admin API Architecture

The admin API binds `127.0.0.1:7443` and is reached through an SSH port forward,
the optional Cloudflare Tunnel, or both. The exact path classification, route
matrix, and cookie/session invariants are specified in
[Admin API Authentication and Request Boundary](admin-api-authentication.md).
nftables admits new connections to
that listener only from root, `kern-admin`, `kern-operator`, and `cloudflared`,
then drops the port for every other local uid. This matters even on loopback:
an egress-capable compromised tool or proxy service cannot forge
`X-Forwarded-Proto`/`Cf-Connecting-Ip` and masquerade as the tunnel. The agent
can reach only the proxy port. The admin login is the authentication boundary:
the tunnel carries transport and Cloudflare's edge (DDoS) protection only, with
no Cloudflare Access gate in front, so the login is hardened to stand alone
(see [admin login sessions](#admin-login-sessions)). Static admin/app UI assets,
the side-effect-free OAuth callback shell, and the non-secret public
`GET /v1/login/status` enrollment bit are the only unauthenticated HTTP routes.

## Admin login sessions

The password is presented only once, at `POST /v1/login`, where it is compared in
constant time against the SHA-256 hash of `admin_password_sha256` from the config
table (the cleartext is never persisted). On public HTTPS, enrolled passkeys
are a strict second factor: a correct password creates only a five-minute,
single-use pre-authentication ceremony bound to the source, exact configured
hostname, origin, and random challenge. The admin session is minted only after
the browser returns a valid WebAuthn assertion with user presence and user
verification. WebAuthn is deliberately confined to this one branch; the
SSH-forward path retains password login because the SSH key is already its
other factor and a public-domain credential is unusable on `localhost`.

The passkey assertion endpoint has no separate retry counter. A caller gets
exactly one assertion attempt from each successful password proof: the server
atomically consumes the random pre-authentication token before parsing or
verifying the assertion, and a failure clears the browser cookie. WebAuthn
signatures are not guessable codes, so an additional counter would add an
attacker-triggerable lockout without protecting a brute-forceable secret.
Pending ceremonies expire after five minutes and are capped at 1,000; failed
password attempts retain the per-source throttle described below.

With no enrolled passkey, a correct password mints an opaque,
server-held session token returned as a cookie that is `HttpOnly` (JavaScript
never reads it), `SameSite=Strict`, and `Secure` whenever the request arrived
over HTTPS. Public HTTPS uses only `__Host-tc_admin_session` (`Secure`, `Path=/`,
no `Domain`); loopback HTTP uses only `tc_admin_session`. The server rejects the
other transport's cookie name and duplicate expected cookies rather than
allowing cookie ordering to influence authentication. Every other request
authenticates with that cookie alone; the password is never replayed on later
requests, and there is no bearer-token path. The admin UI page never holds the
password.

Sessions live only in admin-process memory and expire after 12 hours of
operator inactivity or 3 days absolute, whichever comes first. The UI's
central request wrapper marks requests made shortly after a real
pointer/key/touch interaction; only those valid requests refresh the idle
clock. Five-second health/task polls still authenticate but never extend an
abandoned tab. Both limits are enforced server-side; a client cannot extend the
absolute cap. `POST /v1/logout` revokes the current session, while an admin
service restart, host reboot, or upgrade clears all sessions and requires a
fresh login. Because the cookie is the credential, every cookie-authenticated
request must also carry the `X-Kern-Csrf` header, which same-origin UI code
always sends and a cross-site page cannot; combined with `SameSite=Strict` this
closes CSRF.

Passkey registration is offered after a password-only public login until the
first credential exists. Kern requests a resident ES256/P-256 credential with
user verification and no identifying attestation. One small verifier module
parses definite-length WebAuthn CBOR/COSE, validates RP ID hash, origin,
challenge, flags, credential id, and signature counter, then delegates ECDSA
public-key checks and signature verification to the system OpenSSL already
installed by bootstrap. No additional runtime package is introduced. Only
public verification material and metadata are stored in Postgres; ceremonies
remain bounded process memory. Upgrade and recover preserve credentials.
Root reconfigure deletes them only when explicitly passed
`--reset-admin-passkeys`; the service restart already clears all sessions.

**HTTPS is mandatory over the tunnel.** The edge sets `X-Forwarded-Proto`, so a
non-`https` value means the request reached the origin in the clear. The admin
API never accepts credentials or serves secrets over cleartext: it upgrades a GET
with a redirect to `https` (so a form loads securely before it is shown) and
refuses every other method, and it sends HSTS on responses. Bootstrap verifies
that `http` redirects and `https /v1/health` returns 401.

Login handling throttles per source. Each attempt is counted under a lock before
the password is compared, so a concurrent burst can never win more than the
allowed number of guesses; past ten attempts within fifteen minutes from one
source, that source is fully blocked with `429` (even a correct password is
refused) until the window self-clears. A correct login clears the source's
streak. The block is per-source only — there is deliberately no global ceiling,
which would be an attacker-triggerable lockout of every operator at once —
and a blocked operator recovers by waiting out the window, using the loopback
SSH forward (a separate, exempt bucket), or a different IP; blocking the real
operator requires flooding their exact egress IP, and a strong generated
password already defeats brute force regardless. The source key is the tunnel's
`Cf-Connecting-Ip` (or
`Cf-Connecting-IPv6` when Cloudflare Pseudo IPv4 is overwriting the address),
which the edge sets and a browser cannot spoof — required as exactly one valid
address (IPv4 bucketed per address, IPv6 per `/64`) and failed closed if missing
or malformed so a stripped header cannot collapse every visitor into one bucket;
the loopback SSH forward uses the socket peer. nftables independently ensures
that only the `cloudflared` uid can deliver tunnel-marked traffic, so another
local service cannot invent these trusted headers or rotate spoofed source
buckets. The login body is capped at 4 KiB
and validated as exactly `{"password": <string ≤256 bytes>}`. The server also
caps concurrent worker threads and sets a per-connection read timeout so a
connection flood or slow client cannot exhaust host threads.

App backends are reached only through the admin API reverse proxy. Each app
service binds a host-assigned `127.0.0.1` port, and nftables accepts new
connections to that port only from the `kern-admin` uid before dropping
the same port for every other local uid. The app receives a host proxy marker,
not the operator's session credential. Agent runtimes, app service users, and
ordinary local users cannot call app backend TCP listeners directly.

The agent, network, and tool event logs each keep the newest 1,000,000 entries.

`/v1/health` derives network status from policy validity and proxy liveness. If
the persisted policy cannot be parsed or the proxy process is not listening,
health reports `network_controls.status: error` so the operator notices and
repairs it. This failure is safe — nftables blocks the agent's direct egress
independently of the proxy, so a dead proxy leaves the agent with no network at
all, never with unfiltered access.

A background poller re-verifies an `active` agent login at most every five
minutes, so an expired login surfaces in health as `awaiting_login` without
waiting for a turn to fail. Provider usage metadata is refreshed by the same
active-runtime check and is cached in account state with a `last_checked_at`
timestamp. Claude's check performs a live authenticated usage probe for the
pinned token, or provider profile attestation for a new or rotated token,
before it publishes `active`. This lets Claude Code refresh its token and
prevents a cached-but-rejected credential from staying connected. Codex's
usage read is also live; if it fails for a pinned account, Kern asks
Codex to force one credential refresh before deciding whether the account is
still active. A locally cached provider account is therefore insufficient for
either runtime.

Each live-validation verdict is remembered in process memory, so validation
generates provider traffic at most once per scheduled recheck even though
loading or awaiting-login runtimes are polled every five seconds and Claude
turns converge a rotated credential pin as they start (the full lifecycle is in
[Agent provider lifecycle](agent-provider-lifecycle.md)). The operator refresh
endpoint bypasses this memory and performs an immediate provider check. An
authentication failure is final for automatic checks: the runtime stays
`awaiting_login` with no background provider traffic until an explicit refresh
rechecks it or an operator login or account reset replaces the credential. Any
other validation failure surfaces as `error` and is retried on the next
scheduled recheck. When a new or rotated Claude token is
validated by attestation, the same refresh reads usage once right after it
publishes the token's proxy pin, so usage appears immediately after login.

Route handlers are thin: validate the documented protocol, read or update
admin state (the local Postgres database — see
[Admin state storage](admin-state-storage.md)), and delegate to the
orchestrator, the selected runtime client, or the fixed sudo helpers. Agent
file list/read routes cross into the private agent home through
`read-agent-file`, which demotes to `kern-agent`, confines paths to
`agent-home`, rejects symlinks, caps listing scan work and responses at 1,000
entries, opens files nonblocking, and caps reads at 1 MiB.

The raw upload route uses a separate fixed `upload-agent-file` helper. The
admin service requires an exact `Content-Length`, caps it at 25 MiB, and
streams the body through stdin without buffering it in memory. The helper
demotes to `kern-agent`, rejects unsafe or oversized basenames, opens the
real `user-files/` directory without following symlinks, writes a hidden
temporary file, and publishes it with a no-overwrite hard link only after all
declared bytes are durable. This gives upload and runtime access the same
filesystem authority and leaves no partial file at the returned path.

The orchestrator has no worker pool and no queue. Sending a message runs turn
admission in the request: the message either starts a turn immediately (an
admitted turn runs on its own daemon thread), synchronously steers the
thread's running turn, or is rejected with a descriptive `409`/`429` and the
caller retries. Each live turn's delivery lock serializes synchronous provider
handoff plus its following event commit against finish and stop. The initial
turn admission remains one database mutation and publishes its live fence
only after commit.

`GET /v1/agent-processes` is a read-only diagnostic endpoint. It walks
descendant `cgroup.procs` files under `kern_agent.slice`, then reads
basic process metadata from `/proc/<pid>` without sudo or shelling out to `ps`.
The result is intentionally not turn state: turn processes exit shortly after
their turn finishes, and child processes normally inherit the runtime cgroup
and show up in the same agent slice.

- Every message names a client-chosen `thread_id`. The first message also
  names an `agent_runtime` (`codex`, `claude_code`, or `hermes`) and one
  allowed model/effort pair, which binds all four values and starts a runtime
  conversation. Later messages
  may omit the runtime, model, and effort; the host loads the thread's fixed
  configuration and resumes the recorded provider session id
  (the maps live in the `thread_sessions` table, capped at the 100,000 most
  recently used per runtime). A message for an idle thread starts a turn
  immediately; a message for a thread with a live turn is synchronously
  delivered into that turn as a steer and recorded after provider
  acknowledgement. Turns on one thread are serialized by the live-turn fence;
  turns on different threads run in parallel, up to three per runtime (each
  runtime owns an independent pool, and a message that would exceed the cap
  is rejected with `429` rather than queued).
- Each turn gets its own runtime process, spawned through the sudo
  helper and closed when the turn ends. Codex turns resume their provider
  thread by id on a fresh app-server; Claude Code and Hermes processes resume
  by session id.
- Codex receives the selected model on thread
  start/resume and the selected model and effort on every turn; it uses
  `turn/steer` for steering. Claude Code receives `--model` and `--effort` on
  every new or resumed CLI process and receives steering as additional
  stream-json user messages. Hermes receives the
  selected model through its single-shot process and rejects steering because
  that process has no mid-turn input channel (a message for a busy Hermes
  thread is a `409`). Each completed agent message becomes a
  `thread.message` event. Public history is a flat stream with no turn
  lifecycle events.
- A turn runs only while its selected runtime is `active`. Admission rejects
  a message for a non-active runtime with a `409` naming that status. Policy
  updates and provider refreshes synchronously stop live turns when they move
  a runtime out of `active`, including a turn admitted just before its worker
  starts; work never parks behind a missing login.
- If a runtime becomes non-active after policy disable, login expiry, or a
  health-check error, the orchestrator fails that runtime's running work
  (`thread.error`) and closes all live runtime processes for it.
- `POST /v1/threads/{id}/stop` ends the thread's running turn by terminating
  its runtime process — the one reliable abort for a stuck turn — and records
  a `thread.stopped` event. The database is finalized before the non-blocking
  interrupt request and response. The thread survives, so a later message
  resumes the conversation, but it stays fenced until the owning execution
  thread proves the old process scope has fully shut down (new messages get
  the retryable `the agent is finishing` `409` in that window), so a new
  turn never races a dying one for the same runtime thread/session.
  After a host
  reboot, each thread whose private stored state is `running` returns to
  `idle` and gets a `thread.error` event at startup. Messages for a thread whose session
  configuration this release no longer offers are rejected with `409`: the
  option matrix ships with the release, and running one would use a model the
  operator never chose.
- Codex login uses its device-code flow. Claude Code login starts
  `claude auth login --claudeai`, returns the browser URL, and later writes the
  browser code back to the waiting CLI process. After login, the admin service
  infers provider account metadata from the agent user's auth files and stores
  only the account id / bearer hash needed by the proxy guards.

A scheduled maintenance pass (hourly, never on the request path) bounds state
growth with indexed deletes: it caps the thread->session maps at 100,000 per
runtime (threads with retained events keep their rows) and leaves the audit
logs to their own amortized on-append pruning.
There is no steer mailbox or delivery-marker table. The message request
serializes with completion for its one live turn, hands the message directly
to the provider transport, then appends its `thread.message` event. Codex
acknowledges `turn/steer` over JSON-RPC. Its stdout reader routes the matching
response id directly to the synchronous request instead of making that request
compete with the activity-notification consumer; provider completion shares the
short steer lock and therefore cannot clear the active turn before an in-flight
acknowledgement resolves. Claude acknowledges a successful write/flush to the
live stream-json stdin (the CLI exposes no stronger per-message
acknowledgement). A database failure after that acknowledgement returns an
error with no user event; delivery is deliberately ambiguous and a caller
retry may duplicate the message. This at-least-once edge is the explicit trade
for having neither durable delivery markers nor an in-memory message queue.
The per-runtime turn cap bounds live processes.

Each admitted execution has four private, in-memory process phases:
`STARTING`, `RUNNING`, `FINISHING`, and `CLOSED`. Admission commits the initial
message and running database state before STARTING. The adapter moves to
RUNNING only after its provider transport accepts the initial message. Normal
completion, failure, and Stop finalize the database before FINISHING; the one
execution thread then owns bounded close/reap and removes the live fence at
CLOSED. Public thread status remains simply `running` while the live fence
exists and `idle` afterwards.

Provider adapters publish a non-empty resumable session id through an
orchestrator callback as soon as the id is known to identify a usable
conversation. The callback persists it immediately against the matching
private run number, preventing a late old process from overwriting a newer
mapping. Empty ids are rejected and never erase a prior mapping. Reading the
adapter's last-known id during failure is only a defensive fallback for the
narrow interval between learning the id and completing its callback.

A separate process-local poller checks for a newer public Kern version
when the admin service starts and every four hours afterward. Because the admin
user has no egress, it invokes the fixed `check-for-upgrade` root helper, which
can only read `infiloop2/kern`'s main-branch `VERSION` file over HTTPS.
The admin service validates and compares the result, then exposes it through
`/v1/health`. A failed check preserves the last successful advisory result,
does not degrade host health, and is retried from scratch on the next poll.
