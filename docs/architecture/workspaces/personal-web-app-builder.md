# Web Apps workspace

Web Apps is Kern's built-in workspace for agent-generated browser interfaces.
Each app has an immutable id (`app-1`, `app-2`, ...), an independently editable
display name, and one host conversation thread with the same immutable id. Its
UI bundle, durable JSON data, and sparse whole-App revision history live in the
Workspace-owned tables in `public`.

The admin sidebar lists active apps, creates one with **New app**, shows
running state, and provides an archived view. Archiving is allowed only while
the agent is idle. It terminates generated code,
makes the app read-only, and preserves every stored revision. Restore returns
it to the active list.

The trusted product chrome runs inside the authenticated admin page and calls
`/v1/workspace/web-apps/...`. The App view presents one full-width command bar
above the generated canvas and only the newest agent response or error; routine
agent activity and the full conversation transcript are intentionally absent.
Selection, composer drafts, event cursors, and asynchronous responses are keyed
by app id so late results from one app cannot update another.

Generated markup is not trusted product code. HTML and CSS pass strict
sanitizers and render inside a nested ShadowRoot. Generated JavaScript runs in
a Worker inside an opaque sandbox iframe whose CSP denies network access and
dynamic evaluation. The broker exposes only bounded rendering, JSON mutation,
notification, and ask-agent capabilities. Worker calls remain pinned to the
app and revisions that created the Worker.

Browser and agent writes share per-app locks and one optimistic `revision`.
Every data operation, atomic data batch, or UI publish compares and increments
that counter once. `publish_ui` may include up to 32 targeted data operations,
allowing a data-contract change and its compatible interface to land in one
transaction without replacing the full data document.

Each retained revision is a complete UI-and-data snapshot. Recovery always
restores both together as a new forward revision; App name and archive state
remain outside revision history. Retention keeps the newest 20 revisions
exactly, then the newest revision in each one-hour UTC bucket for the first day
and each three-hour UTC bucket through day seven. Global Memory and Schedules
are separate Workspace resources and are never included in App recovery.

Polling never swaps a newer App underneath an interactive canvas. A successful
mutation from the displayed generated App advances it immediately. Any newer
revision discovered from elsewhere freezes only the canvas under an update
veil; the command bar remains usable and one **Update app** action loads the
latest coalesced revision. The first build of an empty App loads automatically.

The host-global `workspace_api` instructions document explicit
`/agent/apps/{app_id}/...` routes. Any agent thread may target any existing app
by immutable id. Archived apps remain readable to agents for inspection but
reject writes. Editable names never select authority.

The backend sends the human's message to the host thread exactly as submitted;
it does not prepend App instructions, a memory index, or a hidden App-context
block. Agents can list immutable App ids through `workspace_api`, and can read
their current host thread through `/agent/identity` when that is useful.
