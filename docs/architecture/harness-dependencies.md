# Runtime Harness Dependencies

Kern treats Codex, Claude Code, Grok, and Hermes as external runtime
harnesses. The host owns process supervision, thread and turn state, network policy, and privilege
boundaries, but it depends on specific CLI protocols, auth files, and network
request shapes from those harnesses. This document lists the expectations that
can break when a harness package is upgraded.

## Current harnesses

| Harness | Package | Pinned version | Runtime id | Adapter |
| --- | --- | --- | --- | --- |
| Codex | `@openai/codex` | `0.153.3` | `codex` | `host/runtime/agent_runtime/codex_app_server.py` |
| Claude Code | `@anthropic-ai/claude-code` | `2.1.258` | `claude_code` | `host/runtime/agent_runtime/claude_code.py` |
| Grok Build | `@xai-official/grok` | `1.0.5` | `grok` | `host/runtime/agent_runtime/grok_agent.py` |
| Hermes | `hermes-agent[bedrock,mcp]` | `0.18.2` | `hermes` | `host/runtime/agent_runtime/hermes_agent.py` |

The `script` runtime (`host/runtime/agent_runtime/script_runner.py`) is on that
same adapter contract but is not a harness: it runs a bash script from the
agent home, so it depends on nothing external, pins no version, and appears in
no row above. Nothing in this document applies to it beyond the shared
launcher and scope boundary — see `docs/architecture/services-and-runtimes.md`.

Bootstrap installs the npm packages globally with npm, installs Hermes with
uv into its own dedicated Python 3.12 venv (`/usr/local/lib/hermes-venv`; the
base image's Python is too old for it), and verifies the exact version
strings before completing. A version bump should be treated as an interface
review, not a package-only change.

## Shared expectations

Every harness must keep these properties:

- They can run non-interactively as `kern-agent` with `HOME` set to
  `/mnt/kern-agent/agent-home`.
- They store durable harness state under the agent home so redeploys and service
  restarts preserve login and conversation continuity. Most conversation and
  session state is opaque to Kern: the harness only needs to keep it
  compatible with its own future versions.
- They respect `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, `NO_PROXY`,
  `NODE_EXTRA_CA_CERTS`, and the local proxy CA enough for all data-plane and
  auth traffic to traverse the Kern proxy.
- They can be launched through a root-owned sudo helper that immediately
  demotes to `kern-agent`.
- A running turn can be stopped by closing stdin and, if needed, terminating the
  child process.
- Their account state can be checked without interactive prompts and without
  giving `kern-admin` direct read access to the agent home. Unlike
  conversation/session state, the account helpers do parse specific auth file
  locations and fields listed below.

If any of those properties changes, the runtime status poller, turn threads,
network guards, or privilege boundary can fail.

## Codex harness expectations

### Process interface

Kern starts Codex through:

```text
codex app-server --listen stdio://
```

The app-server is expected to speak newline-delimited JSON-RPC over stdio.
Kern sends `initialize` followed by the `initialized` notification before
any account or thread calls.

Expected methods:

| Method | Expected behavior |
| --- | --- |
| `account/read` | Accepts a `refreshToken` boolean and returns a JSON object with an `account` field. Normal status reads pass `false`; if the live usage probe fails for a pinned account, Kern retries with `true` so Codex validates or refreshes its credential before the UI reports connected. A ChatGPT account contains `email` and `planType`; any provider-specific account type is intentionally ignored. A falsey account means login is still required. |
| `account/rateLimits/read` | Returns Codex usage-limit snapshots. Kern exposes only the default `rateLimits` snapshot in admin API responses; per-limit `rateLimitsByLimitId` entries and duplicated snapshot identity fields are intentionally not returned. Rate-limit windows contain `usedPercent`, `windowDurationMins`, and `resetsAt`; the default snapshot may contain `credits`. |
| `account/login/start` | Accepts `{"type": "chatgptDeviceCode"}` and returns `type`, `loginId`, `verificationUrl`, and `userCode`. |
| `thread/start` | Accepts `cwd`, `approvalPolicy`, `sandbox`, developer instructions, and the selected `model`. Kern supplies the same short host developer instruction to every thread; the release-owned Workspace contract lives in the immutable agent-home instructions. Returns `thread.id`. |
| `thread/resume` | Accepts `threadId`, `cwd`, the selected `model`, and refreshed developer instructions. Returns a resumed `thread.id`, or fails when the thread cannot be resumed. |
| `turn/start` | Accepts `threadId`, text input, and the selected `model` and `effort`. Returns `turn.id`. It may emit notifications before the response. |
| `turn/steer` | Accepts `threadId`, `expectedTurnId`, and text input. The submitting API request waits for its JSON-RPC response; `no active turn` is returned to the caller as a retryable `409`, not retained by a host mailbox. |

The pinned Codex catalog must advertise `gpt-5.6-terra`, `gpt-5.6-sol`, and
`gpt-6-astra` with `high`, `max`, and `ultra`, plus `gpt-5.6-luna` with `high` and `max`.
Kern intentionally exposes only that small subset; the API rejects
unsupported pairs before a message is accepted.

Expected notifications:

| Notification | Expected behavior |
| --- | --- |
| `item/agentMessage/delta` | Carries partial assistant text in `params.delta`. |
| `item/started` | Starts a structured ThreadItem. Kern normalizes reasoning, plans, command execution, file changes, MCP/dynamic/collaboration tools, sub-agent activity, web search, images, waits, review mode, and context compaction into provider-independent turn activity. |
| `item/completed` | Completes the matching structured ThreadItem. Agent messages carry text as `params.item.type == "agentMessage"`; other supported item types update the matching activity card with status and bounded output. |
| `turn/completed` | Ends the turn. `params.turn.status == "completed"` is success; any other status must include enough error detail to fail the turn. |

The adapter relies on responses and notifications being interleavable: a
notification may arrive while a request is waiting for its response, and the
client must be able to keep it for the thread event stream.

### Auth and account identity

Codex device-code login must continue polling while the app-server process that
started login stays alive. Kern therefore keeps that app-server alive
until its completed login is captured as the trusted account, a new login
starts, or an operator resets the linked account.
Linked-account reset also clears local Codex auth files and closes live Codex
runtime processes, so a new login flow starts from an unlinked local auth
state.
First-account capture also requires that same parked app-server to emit a
successful `account/login/completed` notification with the matching `loginId`.
That notification (like `account/read`) carries only `loginId`, `success`, and
`error`; it does not include a ChatGPT account id. So the completion only
attests that the operator's device login for that `loginId` succeeded on the
app-server Kern started; the account id itself is read from the login
tokens through `read-codex-account-id` (the provider-signed `chatgpt_account_id`
claim) promptly after completion. An active `account/read` result by itself is
not operator approval for the stored device-code flow. The residual window
between the CLI writing `~/.codex/auth.json` and that read matches the Claude
first-capture path, and the linked account is shown to the operator once pinned.
The resulting OpenAI provider account row is tagged with
`operator_approval: "codex_device_login"`; rows without that marker are
legacy/unapproved state and never publish a proxy pin.

`account/read` is not assumed to expose the ChatGPT account id. Kern reads
the account id through `read-codex-account-id`, which parses a small part of
Codex auth state at:

```text
~/.codex/auth.json
```

Supported account-id sources, in order:

1. Top-level JSON object `tokens`, field `account_id`.
2. Top-level JSON object `tokens`, field `access_token`, parsed as a JWT. The
   decoded payload must contain object `https://api.openai.com/auth` with string
   field `chatgpt_account_id`.
3. Top-level JSON object `tokens`, field `id_token`, parsed as a JWT. The
   decoded payload must contain object `https://api.openai.com/auth` with string
   field `chatgpt_account_id`.

For steady-state status refresh, an active Codex account without one of those
account-id sources is treated as a runtime error, because the proxy cannot pin
OpenAI data-plane traffic to the logged-in account. Kern stores only the
operator-approved account id in admin and proxy state, not the tokens needed to
recreate Codex auth files.

Kern keeps the observed `account/read` identity fields (`email`, and
`planType` stored as the common `plan_type` field) in admin state; the Admin
API exposes stored account metadata as is, so sanitization happens once, at
capture. It reads Codex usage limits from
`account/rateLimits/read`, not from `account/read`, and exposes only the default
snapshot's `primary`, `secondary`, and `credits` fields under `codex_usage`.
For a pinned account, failure of that live read triggers one forced
Codex-owned credential refresh; an authentication failure becomes
`awaiting_login`, while a successful refresh may remain active without usage
metadata. An account that is not pinned yet cannot reach the guarded usage
endpoint at all, so its usage-read failure is routine: it stays a readable
account awaiting operator approval and its refresh token is never rotated.
The refresh verdict is remembered: an authentication failure stands, with no
further provider traffic, until an operator login or reset replaces the
credential, and any other failure is retried on the next scheduled recheck.
The proxy still receives only the account id needed for account pinning.

### Network request shape

The OpenAI managed provider policy depends on Codex/OpenAI traffic keeping these
request shapes:

- `auth.openai.com` is the only managed OpenAI auth domain. It is allowed for
  `GET` and `POST` and is not account-pinned.
- `api.openai.com` is a managed OpenAI data-plane domain. It is allowed only for
  `POST`, requires `ChatGPT-Account-Id`, and applies the external URL request
  guard.
- `chatgpt.com` is a managed ChatGPT/Codex data-plane domain. It is allowed for
  `GET` and `POST`, requires `ChatGPT-Account-Id`, and applies the
  external URL request guard.
- Codex must not require additional OpenAI, ChatGPT, or wildcard ChatGPT
  domains without updating the managed provider policy.
- ChatGPT/Codex data-plane requests carry `ChatGPT-Account-Id` matching the
  account id inferred from local auth files.
- Data-plane request bodies expose OpenAI tool declarations in parseable JSON
  when web search is requested.
- Cached web search uses `{"type": "web_search", "external_web_access": false}`
  with `indexed_web_access` false or absent, or on the standalone Codex search
  endpoints a body with `settings.external_web_access: false` and no
  `settings.indexed_web_access: true`. This is the only web-tool shape forwarded;
  everything else is denied (fail-closed).
- Non-cached web access — `web_search` with `external_web_access` enabled or
  omitted, `indexed_web_access: true` (Codex `indexed` mode: OpenAI fetches
  server-approved external URLs), `web_search_preview` (including dated
  variants), a bare `web`/`web_fetch`/`browser`/`computer_use`/`code_interpreter`
  tool, any tool carrying a truthy `*_web_access` flag, Chat Completions
  `web_search_options`/search models, or a standalone search request without the
  cached setting — is denied by the proxy. New or renamed web tools fail closed:
  a Codex upgrade that adds a web/browse tool type not matched here still needs a
  guard re-audit, but is denied by default rather than forwarded.
- Remote MCP tools are declared as parseable `type: mcp` tool objects (with a
  `server_url` or hosted `connector_id`), so the proxy can deny them.

Bootstrap also installs `/etc/codex/requirements.toml` to pin Codex web search
to cached (`allowed_web_search_modes = ["cached"]`, which excludes `live` and
`indexed`) and disable Codex app/plugin/browse feature surfaces (`apps`,
`plugins`, `tool_search`, `tool_suggest`, `computer_use`, `remote_plugin`,
`plugin_sharing`) so the agent does not attempt a proxy-denied tool. It also
pins `enable_request_compression = false`, keeping request JSON inspectable by
the fail-closed proxy. Bootstrap additionally installs
`/mnt/kern-agent/agent-home/.codex/config.toml` from
`host/bootstrap/agent-home/.codex/config.toml`. That file must set
`approval_policy = "never"`, `sandbox_mode = "danger-full-access"`, and trust
`/mnt/kern-agent/agent-home`. Bootstrap installs it root-owned, readable,
and immutable so the agent cannot edit or delete the active policy file. The
proxy guard is still required as the web-search enforcement layer. The root-owned
managed config layer `/etc/codex/managed_config.toml` also registers the bundled
tools MCP server (`mcp_servers.kern` spawning `host.runtime.agent_shim.mcp_shim`).
Codex must keep reading both root-owned `/etc/codex` layers and spawning
configured stdio MCP servers as the runtime user.

## Claude Code harness expectations

### Process interface

Kern starts one Claude Code process per turn through:

```text
claude -p --input-format stream-json --output-format stream-json --verbose \
  --model <model> --effort <effort> \
  --setting-sources user --strict-mcp-config \
  --mcp-config <inline JSON for the bundled tools MCP shim>
```

Kern passes the session selection on every new and resumed process.
Claude Code `2.1.258` accepts the exposed model ids `claude-opus-5`,
`claude-fable-5-1`, and `claude-sonnet-5`; it also accepts `high`, `max`, and the
session-only `ultracode` effort. `ultracode` combines xhigh effort with dynamic
workflow orchestration, so an older CLI that silently ignores that value is not
compatible. The catalog names exact model ids rather than the CLI's
unversioned aliases (`opus`, `fable`, `sonnet`): an alias re-points to a new
model generation on a CLI upgrade, which would move existing threads across
generations without the operator choosing it. Threads created while the catalog
offered aliases keep their stored alias and stay readable. They cannot resume
that superseded configuration, but an idle thread can switch to a complete
currently offered runtime/model/effort triple and hand its retained transcript
to a fresh provider session.

The stream adapter consumes the documented assistant content blocks rather
than reducing each record to text: `thinking` becomes reasoning activity,
`tool_use`/`server_tool_use` starts a tool or search card, and the matching
user `tool_result` completes it with output and error status. Text blocks are
still combined and emitted once as the assistant message. Kern intentionally
does not persist every partial token delta: semantic activity provides
interactive progress without a database transaction per generated token.

Bootstrap installs `/mnt/kern-agent/agent-home/.claude/settings.json`
from `host/bootstrap/agent-home/.claude/settings.json` (root-owned, readable,
immutable). It sets `permissions.defaultMode = "bypassPermissions"` and
`skipDangerousModePermissionPrompt = true`. Its
`env.FORCE_PROMPT_CACHING_5M = "1"` pins Claude Code to the five-minute prompt
cache TTL used by the Infiverse development box, instead of allowing the CLI
to select a longer cache policy. `env.CLAUDE_CODE_NO_MODEL_FALLBACK = "1"`
keeps each turn on its selected model: when that model is unavailable, Claude
Code fails the turn instead of substituting another model.
`--setting-sources user` keeps stale local or project settings out of the turn
harness while still allowing `CLAUDE.md` instructions to load, and makes this
file the only loaded settings source.

WebSearch availability follows the operator's
`network_integrations.claude.web_search` toggle (default off) and is
applied at launch, not written to disk. The orchestrator — the only side with a
database role — reads the toggle in `run()` and states the decision to the
launcher as its required first argument (`web-search=on`/`web-search=off`). The
launcher (`host/bootstrap/helpers/run-claude-code.sh`) is authoritative for the
enforcement: on `web-search=off` it appends
`--settings '{"permissions":{"deny":["WebSearch"]}}'` to the Claude invocation
itself, so the deny is built and verifiable in one place rather than trusted
from its caller. That CLI settings override is always loaded regardless of
`--setting-sources`, a `deny` rule applies in every mode (including
`bypassPermissions`) and wins first-match, and the agent cannot influence the
launched command — so there is no file for the agent to tamper with and no way
to re-enable the tool. Non-agent maintenance calls (auth, usage) run no model
turn, so they pass `web-search=off` and keep the deny-by-default posture.
`WebFetch`
and `Bash` stay enabled — their egress is client-side and already gated by the
domain allow-list. Agent and authentication invocations set
`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, `DISABLE_TELEMETRY=1`, and
`DISABLE_ERROR_REPORTING=1`. The umbrella flag suppresses registry, feedback,
and auto-update traffic; the dedicated flags make telemetry and error-reporting
suppression explicit across Claude Code releases. The host-owned `/usage`
probe instead sets `DISABLE_AUTOUPDATER=1`, `DISABLE_FEEDBACK_COMMAND=1`, and
`DISABLE_ERROR_REPORTING=1`: current Claude Code builds omit the Fable window
when either telemetry flag is present. None of these flags affects WebSearch or
WebFetch.

The network proxy is the ultimate layer and enforces the same toggle
independently: the Claude integration guard on `api.anthropic.com` always
denies server-side `web_fetch`/`code_execution`/remote-MCP declarations, and
reads `web_search` directly from its typed config. So even if the harness
settings layer were bypassed, web search stays off unless the operator enabled
it.

`--strict-mcp-config` plus the inline `--mcp-config` make the bundled tools
shim (`host.runtime.agent_shim.mcp_shim`, spawned as `kern-agent`) the only
MCP server; it lists the same static discovery surface whether or not any tool
is enabled (`host/agent_tool_surface.py`). The invocation
deliberately does not pass `--safe-mode`: the pinned CLI drops every non-SDK
MCP server in safe mode, which would disable the bundled tools entirely. The
agent's isolation comes from the OS boundaries (dedicated user, nftables,
policy proxy), not from harness flags, and `--strict-mcp-config` already
ignores any MCP configuration outside the host-supplied one.

Bootstrap installs the same compact
`host/bootstrap/agent-home/agents_claude.md` contents to
`/mnt/kern-agent/agent-home/AGENTS.md` and `CLAUDE.md`. That always-loaded core
orients agents to Kern, states safety and failure-prone invariants, and carries
a capability index so lazy discovery has a bootstrap. Detailed Web App,
memory, and schedule contracts remain in the root-owned release tree under
`/opt/kern-host/host/bootstrap/agent-home/references/`; the core requires an
agent to read the relevant file before conditional operations. Mandatory
memory reads and common App and schedule entrypoints remain in the core, so
routine work never needs a preliminary reference-file read merely to discover
a route. Keeping the references outside the durable agent home avoids claiming
or replacing a user-created path during an upgrade. No per-surface prompt is
appended at process launch.

Kern deliberately does not splice self-memory or swarm page bodies into a new
provider prompt. The host cannot know which shared pages are relevant before
seeing the request, and dynamic agent-written bodies would add stale or
unrelated text while defeating the stable prefix this split protects. The
forced core instead requires one identity-scoped self-memory read (outside
schedule threads) and one request-relevant swarm search at execution start;
the agent then loads only matching bodies. If this ritual later needs stronger
enforcement, enforce completion of that retrieval handshake rather than
injecting an unfiltered memory snapshot.

When resuming a thread, Kern appends:

```text
--resume <session_id>
```

Expected stdin shape:

```json
{"type":"user","message":{"role":"user","content":"..."},"parent_tool_use_id":null}
```

Expected stdout messages:

| Message | Expected behavior |
| --- | --- |
| `type == "assistant"` | Assistant text is read from `message.content[]` blocks where `type == "text"`. |
| `type == "result"` | Ends one submitted user message when `subtype == "success"` and `is_error` is not true. |
| `session_id` | May appear on assistant or result messages; the final turn must provide a session id so Kern can resume future turns. |

Steering is implemented by writing more user messages to the same stream while
the process is running. The adapter waits for one successful `result` per user
message submitted to that process, including steers.

### Auth and account identity

Kern requires Claude Code to use Claude.ai OAuth, not another auth method.
Account status is checked through:

```text
claude auth status --json
```

Expected status fields:

| Field | Expected behavior |
| --- | --- |
| `loggedIn` | `true` means the CLI believes it is logged in. Missing or false means `awaiting_login`. |
| `authMethod` | Must be `claude.ai`; any other value is an error. |
| `email`, `orgId` | Used as optional metadata when helper-read auth files do not already expose equivalent values. |
| `accountId`, `account_id`, `userId`, `userID`, `user_id` | Optional provisional account-id sources within a single status probe. The identity that gets anchored and displayed always comes from token attestation (below), never from these agent-writable fields. Legacy stored Claude rows without `identity_attestation: "anthropic_oauth_profile"` are treated as no anchor (like an unapproved OpenAI row), so a plain operator re-login re-captures them through the first-capture attestation gate; no separate reset is required. |

`loggedIn` is only Claude Code's local credential state, so it is not enough to
publish an active runtime. Once a token is pinned, every steady-state status
refresh also runs:

```text
claude -p /usage --output-format json
```

This live probe makes Claude Code authenticate and gives the CLI ownership of
refreshing an expired access token. Kern reads the credential hash again
after the probe. If refresh rotated it, the proxy provider-attests that bearer
on its first request and allows it when its account uuid matches the approved
account; existing parallel Claude processes keep working with their own cached,
attested token hashes. The orchestrator also notices the new hash, attests it
through the profile endpoint below, and updates its credential metadata. First
capture and a rotation already visible at the start of a check use that same
live profile attestation directly; the refresh then reads usage once so usage
metadata is available immediately after login. A steady-token authentication
failure becomes `awaiting_login`; another steady probe failure becomes `error`.

The probe's verdict is memoized per token hash. Active runtimes are rechecked
every five minutes, and each Claude turn enters the same refresh before spawn
to converge local credential metadata. Only a refresh whose memo has expired
runs the probe, so turn-start convergence is normally memory-only. An
`awaiting_login` verdict never expires: that token is rejected and no
background retry can fix it. An explicit refresh probes once; an operator login
(which mints a new token) or an account reset replaces the credential. An
`error` verdict expires with the memo, so infrastructure failures recover on
the next scheduled recheck. Orchestrator attestation results are memoized per
token hash the same way. Separately, the proxy keeps a bounded cache of token
hashes whose provider-attested account uuid matched the approved account; a
cache miss performs one direct profile check before the original request can
be forwarded.

Login starts with:

```text
claude auth login --claudeai
```

The login command must print a line matching:

```text
If the browser didn't open, visit: <https-url>
```

Kern returns that URL to the admin UI, then later writes the browser code
to the same process stdin.

The proxy guard pins Anthropic data-plane traffic on the OAuth bearer token
hash. `read-claude-account` parses a small part of Claude Code auth state from
one of these locations. In production, both the Claude launcher and account
helper set `CLAUDE_CONFIG_DIR=/mnt/kern-agent/agent-home/.claude`.

| Data | Expected locations |
| --- | --- |
| OAuth account metadata | `/mnt/kern-agent/agent-home/.claude/.claude.json`, `/mnt/kern-agent/agent-home/.claude.json`, or `~/.claude.json` |
| OAuth tokens | `/mnt/kern-agent/agent-home/.claude/.credentials.json` or `~/.claude/.credentials.json` |

The credentials file must contain `claudeAiOauth.accessToken`. The helper stores
only `sha256(accessToken)` plus optional `account_id`, `organization_id`, and
`email`; it never copies the bearer token into admin or proxy state.

When the operator submits the browser code, Kern reads that hash once
right after the login command finishes and records it on the completed OAuth
row. First account capture only accepts an attestation of that exact token: the
admin API passes the approved hash to `read-claude-account --attest`, and the
helper verifies the current credential hash before any profile request. Agent
credentials swapped after completion do not inherit the operator's approval;
the remaining swap window is the moment between the CLI writing the file and
this read, and the linked account is shown in the admin UI once pinned.

### Account identity attestation

The durable Anthropic account anchor is attested against the token itself
instead of being read from agent-writable files. On first operator login and
whenever the orchestrator observes a token rotation,
`read-claude-account --attest` calls:

```text
GET https://api.anthropic.com/api/oauth/profile
Authorization: Bearer <claudeAiOauth.accessToken>
```

Expected response fields:

| Field | Expected behavior |
| --- | --- |
| `account.uuid` | Required. The account the token belongs to. Must match the anchored account id; on first capture during an operator OAuth login it becomes the anchor. |
| `account.email`, `organization.uuid` | Optional identity metadata stored alongside the anchor. |

Properties this depends on:

- This is the same private endpoint Claude Code itself calls during login
  bootstrap — it is one of the pre-pin allowlisted paths in
  `host/runtime/core/network_policy.py` — so the pinned harness version already
  requires it to exist and accept the OAuth bearer.
- The orchestrator's attest call runs as root over direct host egress, not
  through the proxy:
  the agent uid can only reach the local proxy (whose account guard would
  reject a just-rotated token mid-attest), and the admin uid has no egress.
  The bearer token never leaves the helper process; only its hash and the
  attested identity are returned to admin code.
- The network proxy applies the same UUID rule to every bearer. Its first
  request for a distinct token makes a fixed-endpoint profile call directly
  from the trusted proxy; a successful `(approved account uuid, token hash)`
  result is cached in bounded memory. Raw bearers are never cached or logged.
- Cached tokens skip the proxy profile call, so steady traffic adds no extra
  network requests. Parallel Claude processes may use independently rotated
  tokens without racing one mutable hash pin.

If Anthropic changes this endpoint's auth or response shape, uncached Claude
tokens fail closed at the proxy and orchestrator token rotations degrade to a
retryable runtime `error` until this integration is updated. Already cached
tokens continue until the proxy restarts. Treat the endpoint like the other
harness interfaces in this document during upgrade reviews.

Kern also extracts the observed `subscriptionType` value from
`claude auth status --json` into the common Admin API `plan_type` field.
Claude usage is read with:

```text
claude -p "/usage" --output-format json
```

On pinned Claude Code `2.1.258`, the command returns a JSON object whose
`result` string contains lines like:

```text
Current session: 0% used · resets Jul 11, 1am (UTC)
Current week (all models): 0% used · resets Jul 3, 3:59pm (UTC)
Current week (Fable): 0% used · resets Jul 3, 3:59pm (UTC)
```

Kern parses each window line independently: `Current session` into
`claude_usage.current_session_*`, `Current week (all models)` into
`claude_usage.weekly_*`, and `Current week (Fable)` into
`claude_usage.fable_weekly_*`; other model-specific week lines are ignored.
Each window carries `used_percent` and,
when its reset time parses, `resets_at`. Reset times are Unix timestamps;
Kern converts the provider's UTC text while capturing the snapshot; a
reset in any other timezone label drops only that window's `resets_at`. A line
that does not match contributes nothing, and the snapshot keeps whatever did
parse. When no usage window parses, `claude_usage` is absent; Kern never
presents percentages from an older provider read as the current snapshot.

Current Claude Code builds stop rendering the model-scoped Fable row when
either `DISABLE_TELEMETRY` or its umbrella
`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` makes `/usage` exit successfully
with only the session and all-models rows. The launcher therefore exempts only
the host-owned `/usage` maintenance probe from those two variables while still
setting `DISABLE_AUTOUPDATER`, `DISABLE_FEEDBACK_COMMAND`, and
`DISABLE_ERROR_REPORTING`. Agent turns and authentication processes retain the
full nonessential-traffic and telemetry opt-outs.

Kern therefore cannot recreate Claude Code auth files from admin state.
Doing that would require storing refresh/access tokens or equivalent provider
secrets, tracking the harness's private auth file format, and taking
responsibility for token refresh.

### Network request shape

The Claude managed provider policy depends on Claude Code traffic keeping these
request shapes:

- `platform.claude.com` is the only managed Claude OAuth domain. It is allowed
  for `GET` and `POST`, and only for paths matching `^/v1/oauth(?:/.*)?$`.
- `api.anthropic.com` is the only managed Anthropic API domain. It is allowed
  for `GET` and `POST`; each distinct bearer token is provider-attested to the
  approved Claude account uuid and successful token hashes are cached.
- Claude Code must not require `claude.ai`, `claude.com`, wildcard Anthropic
  domains, or additional Anthropic API domains without updating the managed
  provider policy.
- Data-plane Anthropic API calls carry `Authorization: Bearer <token>` whose
  profile identity must match the approved Claude account.
- Before the account uuid is approved, only the narrow Claude Code bootstrap profile
  and settings endpoints listed in `host/runtime/core/network_policy.py` are allowed.

If Claude Code changes its auth domain, token storage, bearer-token use, or
pre-pin bootstrap endpoints, the managed Claude policy must be updated with the
harness upgrade.

## Hermes harness expectations

Hermes is the host's open-source Bedrock harness. One Bedrock integration
enables or disables this runtime. Hermes is a Python
3.11+ application, so bootstrap
provisions a dedicated interpreter and venv with ``uv`` rather than the base
image Python, and pins only ``hermes-agent[bedrock,mcp]``. The Bedrock extra
brings its native boto3 Converse transport; the mcp extra brings the MCP
client SDK for the bundled tools shim; the Anthropic provider extra is not
installed because Hermes exposes only Bedrock here.

### Process interface

Kern runs one Hermes headless chat process per prompt through the
``run-hermes`` launcher and its small stdin adapter:

```text
hermes-stdin.py --model <model> [--resume <session_id>] [--activity-nonce <nonce>]
stdin: <prompt>
```

Bootstrap installs a root-owned immutable ``~/.hermes/config.yaml`` alongside
the managed Codex and Claude configs. It pins provider ``bedrock``, disables
``tirith``, disables Hermes memory, user-profile, post-turn skill review, and
curator features, and registers the bundled tools MCP server
(``mcp_servers.kern`` spawning ``host.runtime.agent_shim.mcp_shim`` as
the runtime user, the same shim wiring as the managed Codex and Claude Code
configs). Bootstrap also owns an immutable empty ``~/.hermes/.env``
so Hermes cannot replace the launcher's turn-scoped region from agent-written
dotenv state. The launcher's required first argument is
``region=<aws-region>``; it passes that turn-specific value as ``AWS_REGION``.
The fixed toolsets ``terminal,file,kern`` limit tools to the terminal,
files, and the shim's bundled tools (no web, browser, skills, or messaging
surfaces); ``--yolo`` disables Hermes's approval prompts (the OS/proxy
boundary is the enforcement); ``-Q`` is quiet mode. The adapter calls the
pinned Hermes package's one-query API with those fixed toolsets, quiet
output, approvals disabled, and session-id reporting. Hermes starts MCP
discovery only from its interactive TUI, gateway, and ACP entrypoints, never
from the one-query path, so the adapter connects the shim itself with one
synchronous ``discover_mcp_tools()`` call before the query; a shim that fails
to serve just leaves bundled tools unregistered for that turn, the same
omit-unavailable contract the shim applies to its own tool sockets.
Prompt content travels only over stdin, never process arguments.
The launcher injects the Bedrock dummy credential as
``AWS_ACCESS_KEY_ID`` and ``AWS_SECRET_ACCESS_KEY``; it never reads the
operator credential.

The pinned Hermes package loads ``AGENTS.md`` from its working directory as a
context file. Kern runs Hermes from
``/mnt/kern-agent/agent-home`` and installs that file root-owned and
immutable there. A separate Hermes-specific instruction file is unnecessary.

Hermes's built-in memory is global to its active profile, not scoped to a
Kern turn or thread. It stores bounded entries in ``MEMORY.md`` (agent
notes) and ``USER.md`` (facts and preferences about the user), then adds a
frozen snapshot to later system prompts. With the memory tool loaded, Hermes
normally starts an in-process daemon thread every ten user turns. That thread
creates a second ``AIAgent``, replays the conversation, and may make additional
model and memory-tool calls. Skill self-improvement is a parallel trigger based
on tool iterations. It can rewrite agent-created skills through the same
background-review agent.

Kern disables both stores, omits the memory and skills toolsets, and sets
both the skill-review cadence and the separate curator scheduler off. The
curator is an interactive-CLI startup hook that periodically marks or archives
unused skills and can optionally run a separate LLM consolidation pass. It is
not a child OS process: both review mechanisms use daemon threads and extra
``AIAgent`` objects inside the current Hermes process. Kern runs the
single-query API, whose process exits after returning the answer and does not
wait for those daemon reviews. Enabling the feature therefore requires a
host-owned completion lifecycle and an explicit cross-thread memory product
contract; changing the YAML alone would make the extra calls unreliable and
invisible to Kern turn status.

Expected behavior:

| Signal | Expected behavior |
| --- | --- |
| exit code | ``0`` on success; any non-zero exit fails the turn with the process's stderr/stdout tail. |
| stderr | ``--pass-session-id`` prints a ``session_id: <id>`` line; the host mints nothing, it reads Hermes's id and resumes with ``--resume``. |
| stdout | The final answer text, interleaved with sentinel-framed live activity records (below). The adapter streams stdout line by line, routes the activity lines away, and keeps the remaining lines (minus the session line) as the answer. |

### Live activity

Hermes runs quiet, so unlike the Codex app-server and the Claude Code
stream-json transports it emits no structured event stream of its own. To give
Agent Chat the same live activity the other harnesses show, the stdin adapter
subscribes to Hermes's ``pre_tool_call`` and ``post_tool_call`` plugin hooks —
appending two observer callbacks to the process-global plugin manager without
triggering plugin discovery, so it loads nothing the single-query path would
not otherwise load, while the universal tool dispatcher still invokes them for
every tool — and prints one provider-independent activity record per event to
stdout. Each record is a single line behind an
ASCII Record-Separator sentinel (``ACTIVITY_LINE_PREFIX``) plus the per-turn
``--activity-nonce`` the host mints fresh each turn; the host only treats a
stdout line carrying that exact secret as activity, and everything else is
answer text. Keying on a per-turn secret — not just the sentinel — is what
keeps the two channels separate on a shared stdout: the model never sees the
nonce, so its (model-controlled) answer text cannot reproduce the frame to
forge a card or steal a line out of the response. (A same-user shell that reads
the process argv could still emit the frame, but that is the OS/proxy boundary's
concern, as everywhere in these harnesses, not the activity channel's.)
``terminal``/``process`` calls map to command activity, ``write_file``/``patch``
to file changes, ``search_files`` to searches, and every other tool (bundled
MCP tools, file reads) to a generic tool card; the shared ``tool_call_id`` folds
the started and completed snapshots into one card. Emission is best-effort — a
hook that raises is swallowed so agent progress can never fail a turn — and the
host re-validates and bounds every record (``agent_activity.normalize_record``)
at its trust boundary before persisting it, dropping any malformed or
out-of-contract line.

Hermes does not support steering. A message for a Hermes thread with a
running turn returns ``409`` from ``POST /v1/threads/{thread_id}/messages``,
and Agent Chat surfaces no steering for it.
A later instruction is a new message on the same ``thread_id`` once the turn
finishes; the adapter starts
one new process with ``--resume`` for the stored Hermes session. This keeps one
turn equal to one Hermes process and one model turn. A thread stop terminates
the running turn's process; the thread remains available for a later message.

### Bedrock transport

Hermes uses its boto3 Converse transport for the DeepSeek, Qwen, and
Kimi catalog. The Bedrock guard therefore admits only
``/model/<id>/converse`` and ``/converse-stream``. Hermes honors
``HTTP_PROXY``/``HTTPS_PROXY`` and reads the proxy CA through
``SSL_CERT_FILE`` and ``AWS_CA_BUNDLE`` (both set by the launcher to the
system bundle). IMDS is disabled so boto3 never waits on the
instance-metadata endpoint.

### Auth and account identity

The operator pastes a long-term IAM key and selects a region in the singleton
Bedrock provider row. The admin service stores them together in the
``bedrock_credentials`` table, and
``sts:GetCallerIdentity`` attests the account. Only long-term keys are accepted;
temporary session credentials are denied. The agent side signs with fixed
dummy values using routing key id ``AKIAKERNHERMES`` and secret
``kern-bedrock-dummy-secret``. The proxy re-signs accepted requests, so
Hermes never holds the operator's key. The dummy values carry no AWS
capability. The proxy meters each response's reported token usage by model.

## Upgrade review checklist

Before changing a harness version:

1. Confirm bootstrap installs the intended package and verifies the exact
   version string.
2. Run the unit tests for the changed adapter and network policy guards.
3. Verify login status and login start flows on a real host for every changed
   harness.
4. Verify the account helper still reads the account id or bearer-token hash
   without broadening `kern-admin` filesystem access, and that
   `read-claude-account --attest --expected-token-sha256 <hash>` rejects a
   mismatched local token before egress and still resolves the expected token to
   the expected `account.uuid` through the profile endpoint.
5. Verify a turn can start, stream messages, accept steering, complete, and be
   stopped.
6. Verify thread/session resume still works after a second turn on the same
   `thread_id`.
7. Verify managed provider network events still show the expected account guard
   behavior and no unexpected denied bootstrap traffic.
8. Update this document, adapter tests, managed provider policy, and stage smoke
   expectations in the same change if any interface changes.
