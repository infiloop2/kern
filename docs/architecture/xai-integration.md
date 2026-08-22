# The xAI integration

Every aspect of how Kern handles xAI (Grok Build) access: what is opened, how
the account is pinned, how server-side tools are decided, where the operator's
choices live, what is stored, and how Grok turns run end to end.

This covers both the network-enforcement/provider connection and the ACP turn
adapter used by Chat, Apps, and Schedules.

Everything below about the CLI's protocol, flags, files and wire format was
verified against `@xai-official/grok@1.0.5` running on a real host with a real
subscription login, not read off documentation.

## The harness this exists for

| Fact | Value |
| --- | --- |
| Product | **Grok Build** — xAI's coding agent CLI, binary `grok`. "Grok CLI" is ambiguous; at least three unrelated projects use that name. |
| Vendor source | `github.com/xai-org/grok-build`, Rust, Apache-2.0 |
| Distribution | npm: `@xai-official/grok`, a JS trampoline plus a per-platform optional dependency carrying the binary. Same install path as Codex and Claude Code, so no new download host is needed. |
| Config and state | `~/.grok` (`GROK_HOME`): `auth.json`, `config.toml`, `sessions/` (SQLite), `logs/` |
| Auth | OAuth against `auth.x.ai`; `grok login --device-auth` for headless hosts |
| Inference | The subscription weekly pool, through `cli-chat-proxy.grok.com` |

Two properties of the harness matter to this integration specifically:

- **It honours `HTTPS_PROXY`.** Its HTTP client sets no `no_proxy`, so agent
  egress traverses the Kern proxy like every other runtime.
- **It does not use the system CA store.** The client is built against
  `rustls-tls` with bundled webpki roots, so `SSL_CERT_FILE` and
  `NODE_EXTRA_CA_CERTS` are ignored. The proxy's MITM CA must be supplied
  through `GROK_EXTRA_CA_BUNDLE`, which the launcher will set when the runtime
  lands. Without it, every request fails TLS rather than bypassing the proxy —
  the failure mode is closed.

## What is opened

Two hosts, under the reserved `x.ai` and `grok.com` apexes. This is the minimum
for a login plus inference; more should be opened against observed denials
rather than anticipated.

| Host | Role | Methods | Paths | Pinned |
| --- | --- | --- | --- | --- |
| `auth.x.ai` | OAuth issuer: discovery, device code, authorize, token exchange | GET, POST | all | No |
| `cli-chat-proxy.grok.com` | Subscription data plane | GET on the allowlist; POST on inference only | allowlist below | Yes |

`auth.x.ai` is unpinned by construction. It is the endpoint that *establishes*
which account exists, so it cannot be gated on knowing the account already, and
it carries no model traffic. This mirrors `auth.openai.com` in the OpenAI
integration.

### The chat proxy is not only an inference endpoint

Opening the host was not enough, and assuming otherwise was a real hole in an
earlier revision of this integration. The same host also serves:

- **Blob storage** — `/v1/storage/batch_upload` (multipart), `batch_upload_json`,
  `multipart/init`, `download`, `exists`. A body guard that understands JSON
  cannot inspect a multipart upload, so nothing but the route table stands
  between the agent's files and xAI here.
- **Remote session state** — `/v1/sessions`, `/v1/sessions/register`,
  `/v1/sessions/search`.
- **Workspace, skill and sandbox sync** — `/v1/rest/workspaces`,
  `/v1/rest/skills`, `/v1/sandbox/environments`.
- **Bundles, feedback and traces** — `/v1/bundle/archive`,
  `/v1/subagents/bundle`, `/v1/feedback`, `/v1/traces`.

So the route carries explicit method/path allowlists, and everything above is
denied:

```
GET  ^/v1/(?:responses|chat/completions|models|settings|user|billing)(?:\?.*)?$
POST ^/v1/(?:responses|chat/completions)(?:\?.*)?$
```

| Path | Why it is needed |
| --- | --- |
| `/v1/responses`, `/v1/chat/completions` | The two inference shapes |
| `/v1/models` | The model catalog |
| `/v1/settings` | Remote settings, read at startup |
| `/v1/user` | The subscription and entitlement check (`?include=subscription`) |
| `/v1/billing` | The subscription snapshot (`?format=credits`) |

The pattern permits a query string because two of those reads carry one, and
matching runs against the normalized path, so `/v1/responses/../storage/...`
cannot walk out of the allowlist.

The maintenance routes are deliberately read-only. The agent can read its own
xAI bearer, so accepting POST on `/v1/settings` would otherwise let it alter
the account-backed coding-data privacy choice outside the operator workflow.

Widen this against an **observed denial**, which appears in the network event
log with a specific reason code, rather than in anticipation.

### What is deliberately closed

Everything else beneath the owned apexes is denied by the route table. Three
denials are decisions rather than omissions:

| Host | Why it stays closed |
| --- | --- |
| `api.x.ai` | The metered developer API. It bills per token against a console.x.ai credit balance instead of the operator's Grok subscription, so opening it would let a misconfigured runtime spend money without an operator decision. |
| `code.grok.com` | A second session and workspace sync surface. Note that closing this host is *not* what keeps conversation state local — the chat proxy's own session routes are, and they are denied by the path allowlist above. |
| `api.mixpanel.com` | Product analytics. Not under an owned apex, so it is denied by the default policy; the managed config will also disable telemetry at the harness. |

Because the apexes are reserved, none of these can be reopened through a
custom-domain rule — a custom rule naming a managed apex is rejected at config
parse time, enabled or not.

## Account anchoring and the proxy pin

Kern separates the **anchor** (the operator-approved account identity, in the
database) from the **pin** (what the proxy checks per request). See [Agent
provider lifecycle](agent-provider-lifecycle.md) for the general model.

xAI's request guard binds the bearer token directly to the anchored account.
Every guarded request requires exactly one `Authorization: Bearer` whose JWT
claims the pinned account. This is the credential xAI acts on. The admin side
separately attests each new token once to obtain the identity shown and pinned;
it does not add a provider round trip to every proxied request.

The claim read follows xAI's own token handling: a personal login's account id
comes from the token's `sub`, and a team login's comes from `principal_id`
(in the vendor's OIDC user-info extraction, a Team principal's user id *is* its
principal id). The signed `principal_type` decides which claim is authoritative;
the guard does not accept the other claim merely because it matches the pin.
Everything else fails closed — missing or duplicated Authorization, a
non-Bearer scheme, an opaque non-JWT key, and a token claiming a different
account.

The JWT payload is parsed **without signature verification**, which is sound for
the same reason it is in the OpenAI guard: a tampered claim breaks the signature
xAI itself verifies, so only a genuine token of the pinned account both passes
this check and authenticates upstream.

While no account is pinned, every data-plane request is denied
(`xai_account_unavailable`). That is the correct state for a host that has not
completed a Grok login, and it is the state this integration ships in.

Background connection checks use a separate approved-account pin that the
proxy accepts only for `GET` requests to `/v1/models`, `/v1/settings`,
`/v1/user`, and `/v1/billing`. This lets Kern re-check entitlement and recover
from transient provider errors while the runtime is non-active, without
temporarily reopening `/v1/responses` or `/v1/chat/completions`. Disabling the
integration or resetting the linked account clears both pins.

### The anchor marker

The account row is anchored with `operator_approval: "grok_device_login"`,
following the Codex pattern rather than Claude's attestation marker. Kern
captures the principal reported by the exact ACP server whose long-running
`authenticate` request completed; it does not derive approval from the
agent-writable auth file. Subsequent status checks require xAI's attested
identity and the token claim to match that immutable anchor. Rows without the
approval marker are legacy or unapproved state and never publish a pin.
Migration `0043` extends the `provider_accounts` anchor trigger to enforce the
same immutability xAI gets as OpenAI and Claude: once anchored, the account id
cannot be swapped or the row deleted except through an operator reset.

The completion travels over the authenticated runtime's stdio. Grok uses the
same root-to-`kern-agent` launcher and process-isolation posture as Codex and
Claude Code; this integration adds no special sysctl or alternate transport.

### What the operator sees is what the proxy enforces

The account card is the operator's recognition check, so its identity must be
the identity the proxy pins, not merely something adjacent to it.

**The displayed identity is provider-attested.** Decoding a JWT does not prove
that it is genuine, and `auth.json` can be rewritten after an ACP session is
established. So Kern asks xAI who the exact current token belongs to and uses
that response for both the account id and the optional email shown on the card.
`read-grok-account --attest` runs as root — the agent uid reaches only the
proxy, whose account guard would reject the very token being attested, and the
admin uid has no egress — opens the auth file with the same hardening as the
unprivileged read, and returns the hash of the exact token sent to xAI. The
admin caller accepts the answer only when that hash matches the token it had
observed before starting attestation, so an intervening auth-file change is
discarded without passing an expected hash into the helper. The raw token never
leaves the root process; the admin caller only ever holds its sha256. Answers
are memoised per token hash, so the provider is asked once per credential
rather than once per five-second poll.

The attested account id must match both the token claim used by the proxy and
the principal reported by the authenticated ACP session. Any missing identity,
provider failure, or disagreement fails closed instead of publishing an active
status or a pin. The email is optional, but when present it comes from the same
xAI response as that enforced account id.

### An entitlement dependency the pin does not cover

Access to the Build data plane can require the account to belong to an xAI
console team. That team membership is **not** the pinned value. If the team is
deleted or the account removed from it, the pin still matches and the request
still 403s.

The runtime must therefore classify a `permission-denied` chat 403 as runtime
`error` carrying the provider message, **not** `awaiting_login`. A fresh login
cannot fix an entitlement problem, and routing the operator into one is a dead
end.

## Server-side tools

Grok declares hosted tools as entries in a request's `tools` array, alongside
the client-executed `function` tools the CLI runs locally on this host. xAI's
documented server-side set is web search, X search, and code execution.

No hosted tool is allowed, and there is no option that allows one. `web_search`
is denied like the rest but keeps its own reason code, because it is the tool an
operator would expect to be offered — the reasoning is in [Grok web
search](#grok-web-search).

### Every hosted tool, denied

| Tool | Reason |
| --- | --- |
| `x_search` | The hosted X-search tool. Reaches X posts, users and threads from xAI's infrastructure, on the same terms as web search. |
| `file_search` / `collections_search` | Searches xAI-hosted document collections, which this host never populates. |
| `code_execution`, `code_interpreter` | Runs code on xAI infrastructure. The agent has a local shell on this host, so nothing is lost by denying it. |
| `browser`, `computer_use` | A driven remote browser. |
| `image_generation`, `video_generation` | Media generation on xAI infrastructure. |
| Remote MCP (`type: mcp`, or a `server_url` anywhere) | Makes xAI call an external server with request data. |

xAI spells two of these twice, and both spellings are denied: the wire `type`
in a Responses API request is `code_interpreter` and `file_search`, while the
Python SDK's helpers for the same tools are `code_execution` and
`collections_search`. `browser`, `computer_use`, `image_generation` and
`video_generation` are not tools xAI documents today; they are held so a rename
lands on a name already denied.

### How the guard collects tools

Two collection rules, because they fail closed against different things:

1. **Every non-`function` entry of a `tools` array**, wherever that array
   appears. A `tools` array is a declaration site, so an unrecognised entry
   there is an undeclarable capability rather than an unknown shape. This is
   what keeps a hosted tool xAI ships later from being forwarded unreviewed —
   the denial follows from *where* the entry appears, not from someone having
   remembered to add it to a list.
2. **The named hosted families anywhere else in the body**: `type: mcp`, any
   `type` starting with `web` (covering `web_search` and any future dated or
   renamed variant), or a member of the denied set above. This catches a
   declaration nested under some other key.

3. **`search_parameters`, decided on its own.** This is xAI's *second* way of
   asking for a live search, and it is not a tool declaration at all: it is a
   sibling object with no `type` of its own, so neither rule above can see it.
   A request carrying `{"search_parameters": {"mode": "on"}}` and no `tools`
   entry would otherwise reach the live web unexamined.
   It is therefore decided first, and fails closed: only an explicit
   `mode: "off"` counts as off, so an absent, unknown, or non-string mode is
   treated as a live search and denied. `sources` is not read at all — no
   corpus is reachable, so which one a request names decides nothing.

   The same key is skipped by rule 2's walk. Its source entries are
   `{"type": "web"}`-shaped, so descending would collect them on the
   `web`-prefix match and report the request as an unrecognised hosted tool
   instead of as the denied search it is.

Replay history items are excluded from rules 1 and 2, along with their whole
subtrees. They describe an earlier hosted call rather than declaring a new one,
and appear in legitimate follow-up requests.

A body that cannot be decoded (`xai_body_undecodable`) or that declares JSON and
does not parse (`xai_body_not_json`) is denied rather than forwarded
uninspected. Nesting deep enough to exhaust the interpreter stack — in the
parser, or in the walks above, which spend more frames per level than the
parser does and so give way first — is denied under the same
`xai_body_not_json` code: the request already failed closed, but as a traceback
that dropped the connection rather than a denial the operator can see. A body
that is not JSON at all is forwarded: xAI parses requests as JSON, so one it
cannot parse cannot declare tools, whatever its content-type label says.

## Grok web search

**Grok's server-side web search is not available on this host, and there is no
setting that enables it.** Unlike the Claude and OpenAI integrations, which
offer a Web search control, the xAI integration has no options at all: it is
enabled or it is not.

That is a deliberate decision rather than unfinished work, and the rest of this
section is the reasoning, because it is the kind of decision someone will
reasonably want to revisit.

### Why there is no toggle

Kern's other two search toggles are offerable because the vendor gives them a
narrow shape. OpenAI's `web_search` has a cache-backed form Kern can require
(`external_web_access: false`), so Codex searches without anything being
fetched live for the request. Grok's has no such form: it searches *and browses
live pages* during the turn, as one indivisible capability.

Three findings, each established below, together rule out offering it:

1. **Page fetching cannot be separated from searching.** There is no request
   field, tool declaration, or CLI flag that gives search without browsing.
2. **The sub-operations are not deniable.** They are chosen by the model on
   xAI's servers and appear only in responses, so the proxy has nothing to
   match on — and denying their names would break legitimate follow-up turns.
3. **An allowed search is an unmediated egress path.** `open_page` retrieves a
   model-chosen URL, query string included, from xAI's infrastructure. No
   request to that domain crosses this host, so the network policy — the
   control the whole host is built around — does not see it.

A toggle whose "on" position means *"an agent may cause arbitrary URLs to be
fetched, with agent-chosen content in the query string, invisibly to network
policy"* is not a meaningful operator choice. Offering it as **Web search**
would understate it, and stating it accurately would make the answer obvious.
So the option is not offered, and the operator-facing copy says why.

### What is lost, and what remains

Grok answers from what it already knows, plus whatever the agent reads locally.
The agent's own tools are unaffected: it still has a shell, the repository, and
every bundled Kern tool, and its ordinary egress is decided by the network
policy as usual. What is gone is Grok reaching the live web *itself*, mid-turn,
outside that policy.

Grok Build's client-side `web_fetch` is a separate thing and is also off; see
[Grok's own client-side fetch](#groks-own-client-side-fetch-is-a-different-thing).

### If this is ever revisited

The cheapest safe form would be a domain allowlist: `filters.allowed_domains`
is request-visible, xAI documents it as governing search and browsing together,
and requiring it would bound which URLs a search can reach. That converts the
unbounded egress path into a bounded one. It is still live fetching from xAI's
infrastructure, so it needs an operator decision rather than a default, but it
is the one shape that could be offered honestly.

### The sub-actions a search performs, and why none is deniable

A search is not one operation. Grok Build's own action variants —
`WebSearchToolCallAction::Search`, `OpenPage`, `Find`, `FindInPage` — surface
in the CLI's activity as roughly `Search(query)`, `OpenPage(url)`,
`Find(pattern, url)` and `FindInPage(pattern, url)`. xAI reports its own
server-side function names for the same family, all categorised under
`SERVER_SIDE_TOOL_WEB_SEARCH`: `web_search`, `web_search_with_snippets`,
`browse_page`, `open_page`, and `open_page_with_find`. The mapping between the
two vocabularies is not published, so no pairing should be read as exact.

**None of these is deniable at the proxy, and the reason is structural rather
than an omission.** xAI's built-in tools are *agentic server-side*: the request
declares only `{"type": "web_search"}`, and the model — executing on xAI's
infrastructure — decides which sub-function to invoke, without the client
intervening in the loop. Every name above therefore appears only in a
*response*: in `tool_calls`, in `server_side_tool_usage`, or as the CLI's
rendered activity. The proxy inspects request bodies. There is no request field
carrying `open_page`, so there is nothing to match on.

Denying on the response would not help either. The proxy does not inspect
responses, and by the time one names a page fetch, xAI has already performed
it — the decision point has passed.

**Listing these names in the denied set would be worse than useless.** A
follow-up turn replays its earlier hosted calls as `web_search_call` items
carrying an `action`, and that action's type is drawn from exactly this
vocabulary. Rule 2 collects named families *anywhere* in the body, so listing
`open_page` or `find_in_page` would deny ordinary multi-turn conversations. It
would read as enforcement and behave as an outage. This is why the walks skip
replay-item subtrees outright rather than merely declining to collect the item
itself.

One near-miss follows from the same mechanism and is on the upgrade checklist:
`web_search_with_snippets` starts with `web`, so if that string ever appeared
as a nested action type, rule 2's `web`-prefix match would collect it. The
replayed action vocabulary is `search`/`open_page`/`find`/`find_in_page` today,
so it does not — but it is one vendor rename away.

### The egress path an allowed search would open

Worth stating plainly, because it is the decisive reason there is no toggle.

`open_page` retrieves a URL the model chose — with arbitrary agent-chosen data
in its parameters — and xAI performs the fetch. The proxy sees only the chat-proxy request; it never sees
the fetch, because the fetch does not originate here. An agent able to reach
hosted web search could therefore cause an arbitrary URL to be retrieved, with
agent-chosen content in its query string, without any request to that domain
crossing this host's boundary — the one boundary every other capability on this
host is decided at.

That is inherent to server-side search at every vendor. It is exactly what
OpenAI's cache-backed `external_web_access: false` mode exists to avoid, and
Grok offers no equivalent.

When auditing a live session, the source address at the destination tells you
which side fetched: a server-side `open_page` arrives from xAI's
infrastructure, while anything originating here would have appeared in the
network event log and been decided by policy.

### Grok's own client-side fetch is a different thing

Grok Build also ships a **client-side** `web_fetch` tool, which is why a live
session can show a fetch prompt the operator may decline — server-side
sub-actions are never offered for approval, so an approval prompt is itself the
tell that a tool runs locally. Client-side means it runs on this host, so its
egress is the agent's egress: the proxy decides it against the operator's
ordinary domain policy, and a destination outside that policy is denied like
any other agent request. It does not ride the xAI account pin.

Nothing extra is configured for this client-side fetch. The launcher passes
`--disable-web-search` on every invocation, which removes both the client
`web_search` and `web_fetch` tools; underneath that, a fetch is ordinary agent
egress that the network policy decides like any other request. The separate
requirements setting described below controls Grok's hosted `x_search`
injection, not this local tool.

### Request fields that are not inspected, and why that is sound

Several fields shape a search without changing its `type`:
`enable_image_understanding` (which unlocks xAI's internal `view_image`),
`enable_image_search`, `enable_video_understanding` (X search only), and
`filters.allowed_domains` / `filters.excluded_domains`. Likewise
`search_parameters.sources`, which names the corpora — `web`, `x`, `news`,
`rss`.

None is read. That is sound only because no search is reachable in the first
place: a request carrying any of them is already denied on the `web_search`
entry or on `search_parameters`, so the corpus or flag it names decides
nothing. They are catalogued here because they become live decisions the moment
anyone reopens the question — `filters.allowed_domains` in particular is the
one field that could bound the egress path above.

### What would leave, if it ever did

Worth recording, because the operator-facing copy has to be accurate about a
capability that is absent rather than merely off.

**Search traffic would go to xAI and nowhere else from this host.** The query
and surrounding context would ride the ordinary chat-proxy request; xAI
performs the search and fetches pages itself, so this host would never contact
a search engine or a result page, and those sites would see xAI rather than the
operator. Grok Build's client-side search fallback is not an exception: it
posts to `/responses` on an xAI base URL, not to a search provider.

**What xAI does downstream is not disclosed.** Its developer documentation
describes web search as a server-side capability without naming the index
behind it or any search partner, so whether a third party would see the query
is *unstated* rather than ruled out. This mirrors the same gap in the Claude
integration, where Anthropic likewise does not name its search providers.

### Where the denial is enforced

Two layers, and only the first is load-bearing:

1. **The network proxy**, on every request to the chat proxy, whatever process
   sent it. A `web_search` tool entry is denied `xai_web_search_denied`, and so
   is any `search_parameters` that is not an explicit `mode: "off"`.
2. **Grok's client posture.** The launcher passes `--disable-web-search`
   unconditionally, removing the client web-search and fetch tools. The
   root-owned requirements also set `grok-4.6`'s
   `supports_backend_search = false`, because Grok 1.0.5 otherwise injects the
   separate hosted `x_search` tool on follow-up requests. Defence in depth
   only — the agent has a shell and could run `grok` itself without either.
   See [Why the launcher is not the enforcement](#why-the-launcher-is-not-the-enforcement).

## Denial reasons

Every denial is one stable snake_case code, catalogued in the manifest with
agent-facing guidance and joinable by the agent introspection tools.

| Code | Meaning |
| --- | --- |
| `xai_account_unavailable` | No pinned account yet; the Grok login has not completed. |
| `xai_token_account_mismatch` | The bearer was missing, duplicated, not a Bearer JWT, or claimed another account. |
| `xai_body_undecodable` | Content-Encoding could not be decoded for inspection. |
| `xai_body_not_json` | The body declared JSON and did not parse, or was nested too deeply to inspect. |
| `xai_web_search_denied` | Server-side web search is not available on this host, and no setting enables it. Covers both a `web_search` tool entry and a `search_parameters` object that is not an explicit `mode: "off"`. |
| `xai_server_tool_denied` | An always-denied hosted tool, or an unrecognised entry in a `tools` array. |
| `xai_remote_mcp_denied` | A remote MCP server declaration. |

## Stored state

| Where | What |
| --- | --- |
| `provider_accounts` (`provider = 'xai'`) | The operator-approved account identity and its display metadata, anchored by `operator_approval = 'grok_device_login'`. |
| `proxy_provider_pins` (`provider = 'xai'`) | The account id the proxy compares per request. Published only by a refresh that commits `active`; cleared by anything else. |
| `xai_status_probe_pin` (view) | The approved xAI anchor while the integration is enabled. Used only for guarded status routes before or while the data-plane pin is clear; it stores no second copy. |
| `managed_integrations` (`integration = 'xai'`) | Presence means enabled. |

There is deliberately **no `xai_settings` table**. Claude's equivalent exists to
hold a web-search toggle; this integration has no options, so `presence in
managed_integrations` is the whole of its configuration and there is no second
row to keep in step.

`web_search` is not among the accepted keys, which is the ordinary
`reject_extra` behaviour every integration already has rather than anything
added for xAI. Nothing has ever stored that key -- this integration has not
shipped -- so there is no old policy to stay compatible with. It matters only
for whoever adds an option later: give it a row, because a key with nowhere to
live survives parsing and is then dropped by the policy round-trip the proxy
reads, which reads to the operator as an opt-in that silently does nothing.

## Admin UI

The integration has a catalog entry, so an operator can enable it and read what
leaves the host. There is no web-search card, unlike Claude's; the catalog says
why rather than staying silent about a capability Grok has elsewhere. Its
data-disclosure copy is
specific to xAI rather than adapted from Claude's, covering both controls that
apply:

- **In Grok Build**: the coding-data, retention, and training setting that
  `/privacy` opens. On team accounts only a team admin can change it, and a team
  admin can enable Zero Data Retention, which locks the setting entirely.
  The choice is stored in xAI account/auth metadata, not `config.toml`. Kern
  reads `codingDataRetentionOptOut` from authenticated Grok status and exposes
  it as `coding_data_retention_opt_out` in the account response. Its root-owned
  Grok requirements independently pin `[features] telemetry = false` and
  `[telemetry] trace_upload = false`; those local upload controls do not
  substitute for the account-level `/privacy` choice.
- **In the Grok app and on X**: xAI's consumer terms allow conversations to be
  used for training by default, paid tiers included, and the opt-outs live in
  two separate places. xAI does not publish a specific retention period for
  Grok conversation data, and the doc says so rather than implying one.

The shared web-search card — one card and one publish function parameterised by
provider — renders for Claude only. xAI is deliberately not one of its
providers, so there is no xAI entry in its disclosure table and no branch that
renders it for this integration.

### The account card

The integration has an account card like Codex's: it shows the linked account,
its status, the live coding-data opt-out and ZDR values when available, and the
device-login button. Grok is independently selectable in each task surface;
the current pinned matrix is `grok-4.6` with
`xhigh`/`high` reasoning effort.

### Subscription usage, and why the top bar usually shows none

`_x.ai/billing` returns the billing period, the on-demand cap and used amount,
the prepaid balance, and `isUnifiedBillingUser` — but on a unified-billing
subscription account it carries **no usage percentage at all**. Grok's own
client falls back to `0.0` in that case, which would paint a permanently green
"0% used" bar.

There is no other source. `_x.ai/session/usage` reports per-session token
counts, which say nothing about how much of the subscription pool is left;
`/v1/usage` and `/v1/rate_limits` do not exist; and the absence survives a
token refresh, so it is not a stale-credential artefact. What xAI does publish
is the billing period, the on-demand cap and spend, and the prepaid balance —
none of which is a consumption figure for an account whose pay-as-you-go is
off.

So Kern omits `grok_usage` entirely rather than substituting zero, and the top
bar renders a muted note reading "usage monitoring is not available for Grok"
instead of an empty ring. The distinction matters: an empty ring says *this
poll found no number*, which implies one is coming. The note says *this
provider does not publish one*. It is styled in the dim text colour rather than
a warning colour, because nothing is wrong — it is a fact about xAI, not a
fault on this host. If a response ever does carry a percentage, the ordinary
ring replaces the note with no further change.

## Testing

| Area | Coverage |
| --- | --- |
| Route table | Both opened hosts, inference-only POST, read-only settings/model/user/billing routes, the explicit `api.x.ai` and `code.grok.com` denials, method rejection, case-insensitive host matching |
| Account and credential binding | Personal `sub`; team `principal_id`; unpinned; missing, foreign, and duplicated bearers; opaque key; non-Bearer scheme; a token whose only claim is unrelated |
| Server tools | Function tools; web search by tool entry and by `search_parameters`, over every source shape; X search; code execution; collections search under both spellings; unknown and untyped entries in a `tools` array; renamed `web*` variants; remote MCP by type and by `server_url`; nested declarations; replay items and their subtrees; tool names in prompt text; unparseable, over-nested, and non-JSON bodies |
| Config | That `enabled` is the only accepted key and a `web_search` option is rejected rather than ignored, apex reservation against custom domains |
| Persistence | The policy round-trip, that a `web_search` key is rejected rather than ignored, and that no `xai_settings` table exists |
| Admin UI | Catalog copy, that the shared web-search card is not wired to xAI, account/privacy status, and Grok runtime selection |
| ACP turns | New/load session framing, replay suppression, model/effort metadata, streamed text/reasoning/tool activity, steering acknowledgement, cancel, missing-session recovery, and orchestration persistence |

## The CLI as it actually behaves

The version and model must move together. `grok 1.0.3` advertises only
`grok-4.5` in its ACP `initialize` response, while this host offers
`grok-4.6`; submitting that newer model through the older client left the
prompt in flight without an ACP completion. The pinned `grok 1.0.5` advertises
`grok-4.6` and completes the same terminal-tool and resumed-session turns.

The properties below are verified against `grok 1.0.5` by running it:

| Property | Value |
| --- | --- |
| ACP extension methods | Carry a **leading underscore** on the wire: `_x.ai/auth/info`, `_x.ai/auth/get_url`, `_x.ai/auth/check_subscription`, `_x.ai/billing`. Without it the server answers `-32601`. |
| Login flow | **Device code.** `_x.ai/auth/get_url` returns `{auth_url, mode: "device"}`; the code is a `user_code` query parameter inside `auth_url`, and the browser leg happens on `accounts.x.ai`. xAI polls approval itself and resolves the long-running `authenticate` request, so there is no completion endpoint. |
| `--disable-web-search` | A **top-level** option. `grok agent ... stdio --disable-web-search` is rejected as an unexpected argument; it must precede the `agent` subcommand. |
| `supports_backend_search = false` | Required on the pinned `grok-4.6` model as well. Live testing through the managed MITM proxy showed that `--disable-web-search` removes the client `web_search`/`web_fetch` tools but Grok 1.0.5 otherwise adds the distinct hosted `x_search` tool after a local tool call. |
| `GROK_LOGIN_DEVICE_FLOW=1` | Required for ACP login on a remote host. Without it Grok 1.0.5 advertises a loopback callback URL that the operator's browser cannot reach. |
| Access token | A **JWT**, stored as the `key` field of an `auth.json` session keyed by `"<issuer>::<client_id>"`. Its claims carry `sub`, `principal_id`, `principal_type`, `team_id` and `tier`, and on a personal login `sub == principal_id == user_id`. |
| Binary location | The npm package is a Node trampoline that decompresses the real binary into `$GROK_HOME/bin` — inside the agent's own home. Bootstrap decompresses it to a root-owned `/usr/local/bin/grok` instead, so the version pin and the launcher's flags are not agent-editable. |

### Why the launcher is not the enforcement

The CLI flag and model capability setting are defence in depth and nothing
more. The agent has a shell on this host and can run `grok` itself without the
launcher or its root-owned requirements. What stops it is not the CLI's
configuration but the boundary
underneath: the host firewall permits the agent no egress except the policy
proxy, the proxy's MITM CA reaches the agent only through
`GROK_EXTRA_CA_BUNDLE` (so an unconfigured invocation fails TLS closed rather
than escaping), and the proxy inspects every request body and denies a hosted
tool declaration regardless of which process sent it.

## Turn lifecycle

Each turn runs a fresh `grok agent --no-leader stdio` process in the host
thread's systemd scope. A new Kern thread sends `session/new` with the pinned
model, reasoning effort, working directory, and non-interactive permission
mode. A later turn sends `session/load` with the stored ACP session id; replayed
provider history is discarded because Kern already owns the durable transcript.
The adapter then submits a text block through `session/prompt` and persists the
provider session id as soon as Grok confirms it.

Standard `session/update` and xAI `_x.ai/session/update` notifications are
mapped into provider-independent reasoning, plan, tool, command, and background
task activities. Assistant chunks are assembled in wire order and committed as
one chat message at prompt completion. Malformed progress is dropped without
failing the answer or thread lifecycle.

A second user message during a running turn is synchronously acknowledged via
`_x.ai/interject` before Kern records it. Stop sends the ACP `session/cancel`
notification first, then interrupts and reaps the whole thread scope. If
`session/load` reports that its local provider session disappeared, Kern clears
only that exact saved mapping and asks the operator to retry; the next turn
starts a fresh Grok session while the durable Kern transcript remains visible.

The authenticated catalog offers `grok-4.6` with
`xhigh`/`high`. `grok-4.6` is a family alias that currently
resolves to `grok-4.6-build`; xAI offers no versioned alternative, so this is a
documented divergence from Kern's normal exact-model rule.

## Upgrade review checklist

Before changing the pinned Grok CLI version:

1. Confirm the account id still appears in the access token as `sub` or
   `principal_id`.
2. Confirm no new host is required beneath `x.ai` or `grok.com`; a new one is a
   policy change, not a version bump.
3. Re-audit the hosted tool set. New server-side tools fail closed here by
   design, but a tool that becomes *necessary* needs an explicit decision.
   Check both spellings of anything added: xAI names some tools differently on
   the wire and in its SDK.
4. Check the replayed `web_search_call` action vocabulary against the
   `web`-prefix rule. Today it is `search`/`open_page`/`find`/`find_in_page`,
   none of which collide; an action renamed to something starting with `web`
   would be denied mid-conversation. See [The sub-actions a
   search performs](#the-sub-actions-a-search-performs-and-why-none-is-deniable).
5. Confirm the OAuth device flow still targets `auth.x.ai` and completes
   without a browser on the host.
6. Confirm the CLI still honours `HTTPS_PROXY` and `GROK_EXTRA_CA_BUNDLE`.
