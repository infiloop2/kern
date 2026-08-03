# Agentic Web App

Agentic Web App is an installed app for creating multiple durable web apps
through conversation. Each app is an isolated workspace with its own generated
interface, structured data model, always-on instructions, memory store,
scheduled agent calls, whole-workspace recovery, and permanent agent thread. The
human selects or creates a workspace from the persistent desktop **All apps**
sidebar, which becomes a dismissible panel on phones;
the same sidebar remains available from the library, generated canvas, and
admin surface. The selected app fills the remaining product surface as a
full-screen canvas. **View admin** opens Chat, Schedules, Memory, and Recovery,
and **Go to app** returns to the generated interface. The host-owned **Back to
host** control leaves Agentic Web App entirely.

## Product Contract

The generated app has three authored layers:

- HTML describes the semantic interface.
- CSS supplies layout, typography, color, responsive behavior, animation, and
  other visual presentation accepted by the trusted CSS sanitizer.
- JavaScript supplies computation and event handling through a frozen `app`
  capability object.

The structured JSON document is the generated app's backend data. It may
contain any JSON object shape within the encoded size limit.

Two independent counters guard concurrent writers:

- `ui_revision` bumps only when the interface bundle is replaced. It keys
  rendering, worker lifecycle, and the bundle's compiled-script cache, so a
  data-only write never tears down the rendered app or the user's focus.
- `data_version` bumps on every data write and is the optimistic concurrency
  token for `set`, `delete`, and `append`. Concurrent agent, user, or worker
  changes fail with a conflict instead of overwriting one another.

There is deliberately no whole-document data replace and no atomic
bundle-plus-data action. The agent replaces the interface wholesale
(`replace_ui`) and edits data surgically through typed paths. Every data
operation is small, individually recorded, and individually reversible; a
restructure is a handful of visible per-key writes instead of one opaque blob
swap.

### Instructions and memory

Each workspace stores bounded always-on instructions (markdown, human- and
agent-editable) and a set of named memories (slug, one-line description,
markdown body). The backend prepends a trusted `[Workspace context]` block —
instructions plus the memory index — directly after the provenance line of
every outgoing message. Memory bodies stay out of the block; the agent fetches
them on demand and can search them. The browser strips the block from
displayed user bubbles. Because the injected context is exactly what the
Memory panel shows, a prompt-injected instruction that tries to persist itself
is visible to the human in one place, with a last-editor attribution.

### Schedules

A schedule is a stored message that fires on a cadence — every N minutes
(minimum five) or daily at a UTC time. Firing sends an ordinary
provenance-prefixed message (`Requested by schedule:`) on the workspace's
fixed thread with its current session; schedules never choose or change a
session, and a workspace whose thread has no session yet skips the occurrence.
A running turn defers the fire rather than steering it. Both the human and the
agent can create, pause, edit, and delete schedules; agent-created schedules
are labelled in the panel. The backend fires due schedules from a poll loop in
its own process; no host or systemd surface is added.

### Autosave and recovery

Every successful mutation is durable immediately; snapshots are recovery
points, not a delayed persistence mechanism. The backend retains a detailed,
bounded per-workspace change log for reconstruction and audit, but does not
expose its component-level mechanics in the browser.

The Recovery panel contains whole-workspace checkpoints only. The scheduler
creates one immutable daily snapshot per active workspace (UTC), and the human
gets one manual checkpoint slot per UTC day. Pressing **Save checkpoint** again
updates that day's manual slot instead of adding another row. Checkpoints carry
the display name, interface bundle, JSON data, always-on instructions, named
memories, and schedule definitions. At most seven calendar days are retained.

Every recovery row has the same **Revert** action. Revert atomically restores
the entire checkpoint, increments both concurrency counters, recomputes future
schedule run times, and records forward history anchors. The confirmation names
all affected resources. Checkpoint creation and revert are browser-only human
controls; the agent namespace deliberately has neither route.

### Chat

The Chat panel uses Agent Chat's conversation semantics: it opens at the
newest event page, prefetches three pages, pages older history backward
without moving the reader's position, polls forward from the newest cursor,
and retains each opened workspace's loaded window and scroll position for the
life of the frame. Agent replies use the same escaped Markdown renderer. A
running Codex or Claude turn accepts synchronous steering through the same
composer; Hermes disables follow-ups until its turn ends. There is one
thread-level Stop control in the composer. Provider activity events are
shown by default as expandable cards; the Activity switch hides or restores
those complete rows without removing ordinary conversation messages. User bubbles retain the trusted
`Requested by user:` / `Requested by app:` / `Requested by schedule:` first
line, because requests need visible provenance; the injected context block is
stripped from display.

The composer uses Agent Chat's host-owned attachment bridge: selecting up to
ten files records only opaque selections, Send uploads each file (25 MiB
maximum) into `user-files/`, and the durable user message carries the returned
`[User-uploaded file: ...]` references. Switching workspaces discards unsent
selections so an attachment cannot land in another app's thread.

An existing chat reuses its session configuration unless the human explicitly
changes the runtime, model, or effort while the thread is idle. The controls
are disabled while work is active; an idle change shows the same context-loss
and provider-cache warning as Agent Chat. Unchanged human messages, generated
`app.askAgent` requests, and schedule fires omit configuration fields, so
stale or generated input cannot switch sessions implicitly.

## Security Boundary

Agent-authored JavaScript never runs in a realm with a DOM. It runs in a
dedicated Web Worker created by audited app code. The trusted app frame owns
the real DOM, sanitizes every render, translates user events into plain data,
validates worker messages, and performs typed backend calls.

This boundary is intentionally different from placing agent HTML and script in
a sandboxed iframe. A normal iframe with script can navigate itself, submit
forms, load subresources through markup and CSS, and use an expanding set of
browser network APIs. Removing `allow-same-origin` protects the parent origin,
but does not make the child a zero-network execution environment. CSP blocks
many request classes, but browser navigation directives are not a complete,
portable JavaScript execution boundary.

A dedicated worker has no `window`, `document`, anchors, forms, media elements,
top-level browsing context, cookies, or DOM storage. Agentic Web App adds four
reinforcing layers:

1. The app iframe remains opaque-origin and sandboxed. Its CSP keeps
   `connect-src 'none'`, `frame-src 'none'`, `form-action 'none'`, no inline
   scripts, and no parent credential.
2. Only this manifest opts into `worker-src blob:`. Other app CSPs remain
   unchanged.
3. The trusted bootstrap removes and seals network, code-loading,
   nested-worker, cross-context, storage, and timer globals before generated
   code executes. Dynamic import syntax is rejected when a bundle is written.
4. Generated code receives only the frozen `app` object. The trusted owner
   validates every message, bounds message rate and size, and terminates the
   worker when its turn ends. A worker that does not finish initialization or
   one event turn within three seconds is terminated.

One event turn may have only one durable mutation in flight and at most 16
mutations total. The frame also caps total worker messages per second and per
turn. A generated handler that exceeds a cap is terminated instead of queuing
unbounded backend work.

CSP is the authoritative network and code-loading boundary. Removing the global
`Function` binding does not remove `Function.prototype.constructor`, and the
worker retains `WebAssembly`. The app frame therefore keeps `unsafe-eval` and
`wasm-unsafe-eval` absent from `script-src`; its blob worker inherits that
policy and `connect-src 'none'`. The global lockdown keeps common browser
capabilities absent even before CSP evaluates a request, but it is defense in
depth rather than a substitute for those directives. Worker termination bounds
a generated infinite loop to one short-lived worker turn. The browser process
itself remains outside the generated code's control.

### Worker lifecycle and latency

Every turn still runs in its own worker that is terminated when the turn ends;
that convergence contract is unchanged. Two optimizations move cost off the
interaction's critical path without weakening it:

- The bundle's blob URL is cached per `ui_revision` instead of being revoked
  after each spawn, so the engine can reuse its compiled script across turns.
- After a turn completes, the frame eagerly spawns and initializes the next
  worker ("arming"). A user event whose workspace, `ui_revision`, and
  `data_version` still match promotes the armed worker straight into its event
  turn — no spawn, parse, or init on the critical path. Any mismatch discards
  the armed worker and cold-starts as before.

An armed worker sits idle between initialization and its event. Timer globals
are denied in the bootstrap precisely so that idle window is inert: with an
empty event loop and no way to schedule a wake-up, only trusted frame messages
run generated code. An armed worker that sends any message while idle is
terminated. Arming has its own three-second deadline, and each promoted turn
gets the standard per-turn timeout and message caps from promotion time.

## Trusted Rendering

The renderer parses generated HTML into an inert template, copies allowed nodes
into a new document fragment, and patches the generated Shadow DOM to match.
It never inserts the original parsed tree. The element allowlist covers HTML
text semantics, landmarks, disclosure, lists, tables, ruby annotations,
details, buttons, labels, datalists, meters, progress, and common form
controls. Safe relationship, constraint, accessibility, language, and table
attributes preserve native browser semantics without adding a request or
execution sink.

The renderer drops all anchors, images, audio, video, sources, links, metadata,
scripts, styles, iframes, objects, and embeds. It discards event attributes,
`href`, `src`, form targets and actions, style attributes, and every unrecognized
attribute. Buttons receive `type="button"`; input types come from a fixed inert
control allowlist. Generated markup therefore has no browser request or
navigation sink.

Rendering is incremental: both the current tree and the freshly sanitized tree
came from the same sanitizer, so the frame diffs them in place — updating
text, attributes, and control values, and replacing only mismatched nodes —
instead of rebuilding the whole shadow root. Focus, selection, and scroll
survive re-renders; the control the user is actively editing keeps its live
value. Identical render calls (same HTML and CSS) are skipped entirely, and
the sanitized CSS is memoized on the stylesheet text.

The CSS renderer parses the stylesheet through the browser CSS object model and
re-emits only style rules, media groups with bounded conditions, and keyframes.
Its visual language includes custom properties, gradients, backgrounds, grid,
flexbox, typography, filters, clipping shapes, transforms, animation, and
scroll snapping. It drops supports and import rules, font faces, namespaces,
document rules, unknown at-rules, and values containing URL, image-set,
cross-fade, element-image, or paint-worklet functions. The result is scoped to
the generated app's Shadow DOM. The trusted host is a paint-contained stacking
context, and the sanitizer rejects host-targeting and escaped selectors. Fixed
generated content therefore remains inside the canvas and below the trusted
floating admin button, status toast, and admin overlay.

The static iframe shell, admin overlay, bridge, worker bootstrap, HTML
sanitizer, and CSS sanitizer are audited release assets. The agent cannot
replace or style that trusted chrome.

## Generated JavaScript API

Generated code registers event handlers during worker startup. The worker
receives a structured clone of current data, runs one matching handler per
turn, and is terminated when the turn ends. Durable app state lives in the
backend JSON document, not worker memory.

The frozen global API is:

```text
app.onLoad(handler)
app.on(action, handler)
app.data()
app.render(html, css)
app.set(path, value)
app.delete(path)
app.append(path, value)
app.askAgent(message)
app.notify(message, level)
```

`app.onLoad` registers one renderer. The trusted frame invokes it only during a
load turn, after the worker has received the current durable data, and then
terminates the worker. The handler may render and notify, but the frame rejects
data mutations and agent requests outside a genuine user-event turn. Generated
apps render from `app.data()` in this handler so runtime mutations remain
visible after a reload or a later agent revision. A data-only change observed
by polling runs one load turn to re-render, without tearing down the canvas.

`app.on` binds a bounded action name to one handler. Generated HTML exposes an
action with `data-action="name"`. A click or change on that element becomes a
plain `{action, value, checked, draggedValue, fields}` event. Controls marked
with `data-field="name"` contribute values to `fields`. Ordinary events carry
an empty `draggedValue`. No DOM node, Event object, selector API, or browser
global crosses into the worker.

Buttons and non-control action elements dispatch from click. Inputs, selects,
and textareas ignore the preliminary click and dispatch only from their native
change event, after the checked state or selected value has changed.

An input or textarea may also carry `data-enter-action="name"`. Plain Enter
dispatches the named action with the same bounded payload and `data-field`
values as other generated interactions. Shift+Enter retains its native
multiline behavior. Modified, repeated, and IME-composition key events do not
dispatch an action.

Drag and drop uses two separate safe attributes. `data-drag-value="item-id"`
makes the sanitized element natively draggable; `data-drop-action="move"`
marks a destination and dispatches the registered `move` handler on drop.
Optional `data-drop-value="target-id"` identifies the destination through the
event's ordinary `value`, while `draggedValue` carries the bounded source
value. The trusted frame keeps drag state in memory and puts only an empty
plain-text entry in the browser `DataTransfer`, so generated values cannot
leave the app through a cross-frame or operating-system drop. During a drag it
adds the trusted-only `data-dragging` and `data-drag-over` attributes for CSS
feedback. Generated apps should retain a click or keyboard reorder path because
native browser drag and drop is not universally accessible.

`app.data()` returns a structured clone. `app.set`, `app.delete`, and
`app.append` accept a path array of string object keys and non-negative integer
array indexes. They call the fixed runtime action endpoint with the current
`data_version` and resolve with the new durable data. The mutation response
carries the counters and document only — never the bundle the app is already
running — so a data write costs kilobytes, not the whole workspace.

`app.render` requests another pass through both trusted sanitizers. `app.notify`
shows bounded plain text in the trusted status toast. Neither method creates a
URL or calls a backend chosen by generated code.

Background timers and persistent local worker state are not part of this
contract, and the timer globals are removed. An app that needs state stores it
in the JSON document. This trade keeps CPU lifetime bounded and gives every
interaction the same convergence path: recreate from the durable state, render
on load or handle one event, then tear down.

## App Buttons That Ask The Agent

Generated JavaScript can call `app.askAgent(message)` while handling a genuine
generated-app user event. This lets an agent-authored button calculate a useful
instruction from current structured data instead of storing a fixed prompt in
markup. The trusted frame ignores requests sent during worker initialization,
accepts at most one request per event turn, and bounds the encoded message.

The generated-app user action is the authorization to send the message. The
frame immediately sends the bounded message to the selected app's fixed
thread, where the host starts a turn or steers the running one. Generated
JavaScript cannot synthesize the initial trusted user event, choose a backend
route, choose session configuration, or call the parent bridge. An accepted
`app.askAgent` instruction has the same authority as the human typing that
instruction in the Chat panel. The turn runs with the app agent's normal tools
and egress, subject to the host's network policy and approval controls. Those
host controls are the security boundary; the real-event gate, one-request
limit, and message-size cap only constrain how the turn starts. Human chat
uses the workspace-scoped `/messages` route, generated interaction callbacks
use `/runtime/agent-requests`, and schedule fires go through the backend's own
scheduler. The backend prepends `Requested by user:`, `Requested by app:`, or
`Requested by schedule:`. This trusted first line gives the agent and chat
history durable provenance without creating a second thread or changing turn
authority.

## Backend State And Routes

The app schema has one `web_apps` row per workspace. Its immutable `thread_id`
is both the workspace identity and the host thread identity. The row holds the
editable name, both counters, HTML, CSS, JavaScript, JSON data,
always-on instructions with editor attribution, and timestamps. Three side
tables hold history entries, schedules, and memories, all keyed by the
workspace thread and dropped with it. `data_json` is opaque only because its
shape is authored by the agent; the backend still parses and validates it as a
JSON object on every write. The host remains authoritative for turns and
session configuration.

The app index joins these rows with the host's bulk thread summaries to show
current agent settings, last use, and idle/running status in both the library
and **All apps** sidebar. Selection is deliberately browser-local: a reload
returns to the library with the sidebar open.

Creating an app inserts a clean workspace, reserves a stable `app-N` thread
ID, seeds the internal history anchors, and creates today's daily recovery
snapshot. There is no reset or thread-rotation
operation. Renaming changes only display text. Every workspace remains in the
single app library; there is no archive state or hidden secondary index.

Browser routes require the host app proxy marker and include the workspace ID:
state, conversation and events, messages, generated-app agent requests,
runtime data actions, rename, stop, instructions, memories,
schedules, and recovery checkpoints. Agent routes require the kernel-attributed
app marker and thread, resolve exactly one workspace, and cover state reads,
the typed actions, instructions, memories with search, and schedules — but not
checkpoint save or revert. The stop route targets only the workspace's own fixed host thread.
Send retries private STARTING/FINISHING conflicts every 500 ms for up to ten
seconds; the same bounded retry serves schedule fires.

Agent data actions return only the new counters — the agent already knows what
it wrote, and echoing the document back into its context on every write is
pure token cost. Browser runtime actions return counters plus the new
document, never the bundle. Keeping state and conversation separate prevents a
maximum generated bundle from consuming the chat history's response budget;
conversation event reads page sequenced events six at a time with bounded
message bytes.

Encoded limits are 128 KiB HTML, 64 KiB CSS, 128 KiB JavaScript, 256 KiB
data, 8 KiB instructions, 16 KiB memory bodies, and 4000-byte schedule
messages, with bounded counts for memories and schedules per workspace.
Runtime paths contain 1 through 16 bounded segments. Request bodies, worker
messages, notifications, and agent-button messages have independent caps. The
complete serialized state is capped below the host proxy's response limit.
Limits use encoded bytes where representation size matters.

## Verification

Unit tests pin manifest opt-in, the split route boundary, both counters'
conflict checks, the exact agent action surface (including the removal of
whole-document replaces and recovery routes' absence from the agent
namespace), encoded caps, typed data paths, internal history recording, daily
snapshot idempotence and retention, manual checkpoint replacement, atomic
whole-workspace recovery,
schedule validation and firing rules, memory and instruction bounds, and
context injection with its display-stripping markers. App platform tests prove
that other apps do not gain blob worker permission.

Desktop browser smoke coverage renders a deliberately hostile bundle and
asserts the sanitizer boundary end to end: no hostile browser request occurs,
forbidden elements and attributes are absent, richer semantic elements and
safe visual CSS survive, and escaped resource functions remain blocked. It
also verifies worker-backed data mutation, durable rendering after a full page
reload, the admin overlay's Chat, Schedules, Memory, and Recovery panels —
including creating a schedule, editing instructions and a memory, and
restoring an earlier state — and the exact agent instruction started by a
generated button. Mobile coverage verifies the full-screen canvas, the
floating admin button, the overlay tabs, and the absence of horizontal
overflow.
