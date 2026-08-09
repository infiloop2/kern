# Agent Preview Ports

Agent preview ports are a fixed loopback TCP range (`AGENT_PREVIEW_PORT_BASE`
`8000` .. `+ AGENT_PREVIEW_PORT_COUNT - 1` = `8015`, in `host/constants.py`)
where the agent may run its own HTTP servers — a dev server, a test harness, a
UI it is building — and, uniquely, **connect back to them** to test what it
serves. An operator who wants to see such a UI forwards the port to their own
machine over SSH and opens it in their own browser.

This is deliberately the *whole* feature. An earlier design also reverse-proxied
these ports into a sandboxed iframe in the admin console; it was dropped because
rendering agent-authored (therefore untrusted, potentially prompt-injected)
HTML/JS on the admin origin inverts the trusted-UI sandbox model — Workspace
chrome is reviewed release code, while preview content would have been
adversarial code one iframe attribute away from an authenticated console. The
SSH forward gives the operator the same view from their own machine at no new
attack surface in the console — provided the preview is browsed on a **loopback
address distinct from the one used for the admin UI**, so no admin cookie is in
scope (see "Operator access" — cookies are scoped by host, not by port, so a
different port alone does not isolate them).

## What the firewall allows

Two facts about the host shape this (see
[`network-controls.md`](network-controls.md) and
[`privilege-boundaries.md`](privilege-boundaries.md)): everything shares one
network namespace, and the agent's loopback *egress* is dropped wholesale (it
may `listen()` on loopback, but may not connect out — normally not even to its
own listener).

`host/bootstrap/render.py` inserts two scoped accepts ahead of that drop:

```
oif lo tcp dport 8000-8015 meta skuid "kern-agent" accept
oif lo ct state established,related tcp sport 8000-8015 meta skuid "kern-agent" accept
```

The policy is an **allowlist followed by a two-directional default drop**, so
the set of principals that can touch the range is closed by construction — not
a fall-through to the broad `oif lo accept`. Three flows are allowed, then
everything else in the range is dropped both ways:

- **dport accept, `kern-agent`** — the agent may *originate* connections to the
  preview range: `curl 127.0.0.1:8000`, headless-browser checks, its own
  integration tests against the server it just started.
- **established sport accept, `kern-agent`** — the agent's own listeners may
  *reply*. Scoped to established, so the agent cannot use a preview *source*
  port to originate a flow (a NEW originated packet is caught by the sport drop
  below).
- **dport accept, `kern-operator`** — the operator's SSH local forward, opened
  by their `kern-operator` session, may reach a preview server so the operator
  can view it. (`kern-operator` is created by stage-1 user data on every deploy
  path, so it always resolves when the ruleset loads.)
- **dport default drop** — no other principal (any service account, root, or a
  future local user) may *dial* the range. This is what stops a compromised
  egress-capable service (`kern-tools`, `kern-proxy`, `cloudflared`) from
  connecting to a preview server and exfiltrating its content, and stops the
  admin API from becoming an SSRF confused-deputy into it.
- **sport default drop** — no non-agent principal may *answer* on the range
  (the agent's own replies are already accepted above). This stops a compromised
  service that bound a preview port from completing a handshake the agent
  originated — which would let a prompt-injected agent POST workspace data
  straight to that service, bypassing the policy proxy — and it subsumes the
  workspace source-port drop.

The result is that only agent↔agent traffic and the operator's forward touch
the range; adding a new service account or user grants no access, because the
default is deny.

## Security boundary: what the carve-out does and does not unlock

The agent's egress drop exists to stop it reaching services that trust
loopback callers (Postgres, the admin API, the Workspace service, the network proxy's
unauthenticated listener). This carve-out must not weaken that, so here is the
exhaustive accounting of what the agent gains. It gains exactly two abilities:
originate TCP to `127.0.0.1:8000-8015`, and answer established connections on
those ports. Everything reachable through them is enumerable by who could sit
on the other end of the socket:

- **The agent itself** (its own dev server). No trust boundary is crossed:
  both endpoints are `kern-agent`, the same principal with the same files and
  data. The agent could already move data between its own processes through
  files, pipes, and Unix sockets — none of which the firewall governs — so
  agent↔agent TCP adds convenience (standard HTTP tooling works), not
  capability.
- **A platform service.** None exists in the range, by construction and by CI:
  every platform listener has a pinned port in the 7xxx block (admin 7443,
  proxy 7445, workspaces 7450; Postgres is Unix-socket / 5432), and
  `test_deploy` asserts the preview range is disjoint from the admin API, the
  network proxy, and the fixed Workspace port. The operational
  invariant is that no platform or root service may ever bind `8000-8015`;
  adding one is a firewall change and must be reviewed as such.
- **Any other local principal — service account or future user.** The
  default-deny closes both directions for all of them at once, so this is one
  case, not many. None can *dial* the range (the dport default drop catches
  every uid except `kern-agent` and `kern-operator`), so no service can
  reach a preview server — no unmediated read path, and the admin API cannot be
  an SSRF confused-deputy into it. None can *answer* on the range either (the
  sport default drop catches every non-agent source port), so a compromised
  service that bound a preview port cannot complete a handshake the agent
  originated — which is what would otherwise let a prompt-injected agent POST
  workspace data straight to an egress-capable service (`kern-tools` holds
  outbound HTTPS) and exfiltrate it around the policy proxy. Agent↔Web App
  communication remains only the journaled `workspace_api` path. Because the policy
  is default-deny rather than an enumeration, adding a service account or user
  later cannot silently open a hole.

And what the agent still cannot do, verbatim unchanged:

- **Reach any non-loopback destination.** Both new rules are `oif lo`;
  external egress still goes only through the network policy proxy.
- **Reach any loopback service outside the range.** The general
  `oif lo meta skuid "kern-agent" drop` still follows the accepts.
- **Originate from a preview source port.** The sport rule requires
  established/related state; the first packet of an agent-originated flow is
  NEW and is dropped, so the reply rule cannot be repurposed as an egress
  hole.

The remaining new behavior is on the *server* side: for the first time, a
local client that dials a preview port gets a working, agent-controlled HTTP
server (before, replies were dropped and the connection stalled). The only
intended client is the operator's SSH local forward. No platform component
connects to the preview range — the one that did (the admin reverse proxy from
this feature's first revision) was removed, and reintroducing any
platform-side fetch from these ports would mean serving agent-authored content
to a privileged consumer and must be treated as a new trust boundary, not a
convenience.

## Operator access: SSH local forward

With `mode: "ssh"` operator connectivity, the operator forwards a preview port
over the SSH login they already use — the `kern-operator` account, with whatever
identity and options their deployment needs (see the README, e.g.
`-i ~/.ssh/kern_operator kern-operator@<public-dns>`). **The preview must be
browsed on a different hostname than the admin UI.** The portable form (no extra
setup, works on Linux and macOS) binds `127.0.0.1` and opens a `*.localhost`
name:

```
ssh -L 8000:127.0.0.1:8000 kern-operator@<host>   # then open http://preview.localhost:8000
```

Why a different hostname, and not just `http://localhost:8000`: **cookies are
scoped by host, not by port.** The admin session cookie is set without a
`Domain`, so it is host-only — but "host" is the hostname alone, and a different
port on the *same* host shares it. When the admin UI is reached over an SSH
forward at `http://localhost:7443` (a supported setup — plain HTTP, so the
cookie is the non-`Secure` `tc_admin_session`, not the `__Host-` variant),
opening `http://localhost:8000` would send that `HttpOnly` admin session token
to the agent-controlled preview server in the very first request. `HttpOnly`
does not help here: it keeps the cookie out of page JavaScript, but the agent's
HTTP *server* reads it directly from the request headers. The agent could then
exfiltrate a live admin bearer token.

Browsers resolve `*.localhost` to loopback, and `preview.localhost` is a
distinct host from the `localhost` the admin UI uses, so no admin cookie is ever
in scope. If a browser does not resolve `*.localhost` (some Safari builds), bind
a dedicated loopback IP instead — `ssh -L 127.0.0.9:8000:127.0.0.1:8000
kern-operator@<host>` then `http://127.0.0.9:8000`; `127.0.0.0/8` is entirely
loopback on Linux, while macOS first needs `sudo ifconfig lo0 alias 127.0.0.9
up`. Either way the rule is the same: **never browse a preview on the same
hostname as the admin console.**

Two more notes:

- The content is still agent-authored: treat it like any untrusted web page.
  Don't type credentials into it.
- Nothing is ever exposed on a public interface; the loopback listener is
  reachable only through the operator's authenticated SSH session (or by the
  agent itself).

Cloudflare-tunnel-only hosts have no interactive preview path today. The agent
can still exercise its own servers and produce screenshots or recordings into
the workspace for the operator to review in the Files tab.

## Future work

If a live in-console preview is ever worth its cost, the credible design is a
**dedicated origin per port** (for example a wildcard tunnel hostname
`port-8000.<host>`, ideally on a separate registrable domain from the admin
console) — real origin isolation, the model user-content renderers converge on
— not a same-origin sandboxed iframe.
