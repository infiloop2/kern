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
sanitizers with byte, node, nesting, and CSS-rule complexity limits and render
inside a nested ShadowRoot. Sanitized CSS uses a constructed stylesheet when
the browser supports ShadowRoot adoption and a CSP-authorized blob stylesheet
otherwise; generated markup cannot create either form. Safe HTML is committed
before style installation so a browser-specific stylesheet failure cannot
leave the canvas on an obsolete Loading placeholder. Generated JavaScript runs
in a Worker inside an opaque sandbox iframe whose CSP denies network access and
dynamic evaluation. The broker exposes only bounded rendering, JSON mutation,
notification, and ask-agent capabilities. Worker calls remain pinned to the
app and revisions that created the Worker. Each turn has a five-second total
deadline from run creation, covering startup, generated execution, and network
waits. Startup, execution, and trusted-render failures are reported distinctly.
Independent reads and collection queries can run concurrently; their waits
overlap without extending the deadline. Brokered writes apply in issue order against
the revision the previous write produced. An interaction that arrives during
a running turn is queued (newest wins) and starts when the turn completes.
When a turn times out or fails, the frame records a short runtime report
naming the action, the elapsed time, and the host requests it waited on,
and appends it to the operator's next composer message so the agent sees the
actual cause.

Browser and agent writes share per-app locks and one optimistic `revision`.
Every data operation, atomic data batch, or UI publish compares and increments
that counter once. `publish_ui` may include up to 32 targeted data operations,
allowing a data-contract change and its compatible interface to land in one
transaction without replacing the full data document.

App data may occupy up to 10 MiB. Compatible generated Apps receive the full
document once at worker initialization and apply acknowledged mutations to
their local copy; mutation responses never echo the document. Large-data Apps
can register `onLoad` with `{data: "targeted"}` and use asynchronous
`app.read(path)` calls instead. In targeted mode the browser loads the UI
bundle without the data document, and `app.data()` is unavailable so a large
document cannot be pulled accidentally. In that mode `set` and `append`
resolve to the submitted value, `delete` resolves to `null`, and callers can
read the resulting stored branch when they need the authoritative container.

Each retained revision is one complete recovery checkpoint for the interface,
JSON document, and all collections. Restore replaces all three atomically as
a new forward revision. App name, archive state, Global Memory, and Schedules
remain outside App recovery. Retention still keeps the newest five revisions
exactly, then one per four-hour interval during the first day and one per day
from day two through day seven, capped at 17 visible checkpoints.

Internally, checkpoints reference immutable interface and document versions;
unchanged components are shared. Collection history stores full row values
with inclusive `valid_from` and exclusive `valid_until` revisions. Updating a
row closes its previous interval and opens a new one; deleting it closes the
interval without a replacement. Document and interface writes never scan or
copy the live collections. Collection writes version only the touched rows.
Restoring selects rows valid at the checkpoint, without replaying operations,
and records the restored rows as the next state in the same transaction.

All writers, restores, and history pruning hold the App row lock. Pruning
removes a component only after its last checkpoint reference disappears;
closed row intervals survive while any retained checkpoint falls inside them.
Open intervals represent live rows and remain available for future checkpoints.
Migration 0065 discards pre-upgrade recovery history and creates one fresh
checkpoint per App from its current live state, keeping its revision number.
Live data, collections, interface, and settings remain unchanged. Rollback
materializes the retained new checkpoints for the older runtime; discarded
pre-upgrade history does not return.

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
