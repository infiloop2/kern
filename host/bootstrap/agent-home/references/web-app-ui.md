# Generated App UI reference

Read before writing or debugging generated App HTML, CSS or JavaScript.
For API reads, writes and publication, also use [web-apps.md](web-apps.md).

## Generated App runtime

Link to a file in the Files viewer with an anchor using its absolute path under
agent home, for example
`<a href="/mnt/kern-agent/agent-home/aira-studio/output/2026-09-14/aira-natural-v3.mp4">Video</a>`.
Kern opens the file in Files when clicked. Paths outside agent home are rejected.

Generated Apps normally receive the full data document once when their worker
loads. For large datasets, register
`app.onLoad(async () => { ... }, {data: "targeted"})` and use
`await app.readMany([["settings"], ["view"], ["summary"]])`; targeted mode does not load the full document
and makes `app.data()` unavailable. Mutation acknowledgements do not return
the full document in either mode. Generated Apps can call
`await app.query("leads", request)` with the collection query body to load only
one requested page. Use `fields: ["title", "status"]` with `limit: 20` to omit
large row fields; ids remain, missing fields are omitted and explicit null is
preserved. Filters/sort still see the original row. `count_only: true` returns
no rows and the matching total. Row responses have a 1 MiB byte budget; reduce
fields/limit on 413.

Generated JavaScript runs in a capability worker with no DOM, network,
storage, navigation, timers, imports, nested workers, or parent access. The
renderer sanitizes HTML and CSS. Do not use images, SVG, canvas, media,
iframes, scripts, inline styles or events, CSS URLs, external fonts, `fetch`,
timers, or third-party libraries.

Use `data-action="name"` on controls and `data-field="name"` on inputs. Put
`data-enter-action="name"` on Enter-to-submit inputs. For drag and drop use
`data-drag-value="item-id"`, `data-drop-action="name"`, and optionally
`data-drop-value="target-id"`; the handler receives `draggedValue`.

The frozen `app` global provides `app.onLoad(handler, options)`,
`app.on(action, handler)`, `app.data()`, `app.read(path)`, `app.readMany(paths)`,
`app.query(collection, request)`, `app.render(html, css)`, `app.set`,
`app.delete`, `app.append`, `app.askAgent(message)`, and
`app.notify(message, level)`. Always register `app.onLoad`. Use `app.data()` in
compatibility mode or `app.read(path)` in targeted mode. In targeted mode
`set` and `append` resolve to the submitted value and `delete` resolves to
`null`; read again when the resulting stored branch is needed. Writes apply in
the order issued. Durable state belongs in App data or a collection, never
worker memory.

## Responsiveness

A turn (onLoad or one action) has a fifteen-second total deadline after the
browser sandbox starts, including generated worker startup and all host
requests. The browser sandbox has a separate 15-second startup limit. Runtime
hydration, read, query and write requests may settle after a stopped turn
(up to 30 seconds), allowing an in-flight write to reconcile before the next action; this does not extend
the worker deadline. Each
`app.read`, `app.readMany`, `app.query`, `app.set`, `app.delete` or `app.append` is a browser
round trip. Reduce round trips and
run independent reads concurrently to finish comfortably within the deadline.

- Render first from data already in hand; fetch a tab's rows only when it is
  selected, and never issue collection queries before the first render.
- Use `Promise.all` for independent `app.read` and `app.query` calls: their
  waits overlap. Await reads before any dependent write; do not run reads
  concurrently with writes, which change the shared revision.
- Concurrent writes serialize in issue order; `Promise.all` does not make
  them faster. Combine related changes into one supported write when possible.
- A handler does at most one write, renders from the resolved value, and does
  not re-read or re-query to refresh the view.
- Keep the document small (under about 100 KB); move repeated rows into
  collections and page them 10 to 25 at a time.
- Publish a compact shell with a useful summary while the worker starts;
  never embed the last full table in the saved HTML.
- A click during a running turn is queued and runs next; do not add manual
  busy states for that. Timeouts append `[App runtime report: ...]` to the
  operator's next message: fix the named action's request count.

`app.readMany(paths)` accepts 1–16 unique valid paths and returns an array in
input order, using null for missing branches. It reads at one revision and
counts as one host request. Prefer it for targeted startup over many `read`
calls. Reads from a changed revision trigger the same refresh handling as `read`.

## Preview before publication

Save the response bodies from `GET /agent/apps/{id}/state/ui` and
`GET /agent/apps/{id}/state/data` to `ui.json` and `data.json` at the same
revision. From a Kern repository checkout run:

```sh
python3 tests/workspaces/web_apps/preview.py --ui ui.json --data data.json --collection leads=rows.json --lint
```

Collection files contain `[{"id":"lead-1","value":{"title":"Example"}}]`.
Without `--port` the preview takes the first free port from 8000 to 8015 and
prints it; several agents can preview at once. Lint reports a severity, code,
message and snippet. Forbidden globals, missing onLoad and JavaScript syntax
errors (`node --check` when Node is available) are errors that exit nonzero
before serving. Unmatched actions/handlers are warnings, skipped when action
names or registrations are built dynamically; verify those by clicking in the
preview. It is a conservative source check, not a JavaScript parser; review
findings in comments/strings too.
The preview serves the real builder with an in-memory mock; writes never reach
the saved files or live App. Open the printed loopback URL using your own
Playwright with the durable browser install, log in with mock password `dev`,
open **App preview**, capture a screenshot, and read console messages prefixed
`[app-sanitizer]` (at most 200 per render) plus `#runtime-status`. Inspect both
initial and interactive renders, then publish. Stop the preview with Ctrl-C.
The mock supports scalar collection queries for preview and orders mixed
null/string/number sort keys like jsonb; PostgreSQL remains the authority for
JSON ordering and storage behavior.

## Starter examples

Each example has a saved HTML shell, CSS, and worker JavaScript. Escape all
record text before inserting it into HTML. Buttons carry paging state because
worker globals do not survive turns.

### Paged collection table (targeted)

Use targeted mode when records live in `leads`; full mode is only useful if a
small document is also needed. The empty state and request error stay visible.

```html
<section class="panel"><h2>Leads</h2><p>Browse saved leads, 20 per page.</p><a href="https://example.com">About this demo</a></section>
```
```css
.panel { padding: 1rem; } table { width: 100%; } th, td { text-align: left; padding: .5rem; } .error { color: #b91c1c; }
```
```javascript
const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
async function showPage(offset) {
  app.render('<section class="panel"><h2>Leads</h2><p>Loading page…</p></section>');
  try {
    const page = await app.query("leads", {fields: ["title", "status"], limit: 20, offset});
    const rows = page.rows.map(row => `<tr><td>${esc(row.value.title)}</td><td>${esc(row.value.status)}</td></tr>`).join("");
    app.render(`<section class="panel"><h2>Leads</h2><a href="https://example.com">About this demo</a>${rows ? `<table><thead><tr><th>Title</th><th>Status</th></tr></thead><tbody>${rows}</tbody></table>` : '<p>No leads yet.</p>'}<button type="button" data-action="page" value="${Math.max(0, offset-20)}">Previous</button>${page.next_offset === null ? '' : `<button type="button" data-action="page" value="${page.next_offset}">Next</button>`}</section>`);
  } catch (error) {
    app.render(`<section class="panel"><p class="error">${esc(error.message)}</p><button type="button" data-action="page" value="0">Retry</button></section>`);
  }
}
app.onLoad(() => showPage(0), {data: "targeted"});
app.on("page", event => showPage(Math.max(0, Number(event.value) || 0)));
```

### Kanban (targeted)

Cards come from `cards`, so targeted mode avoids a full document. A full mode
variant can read small display settings with `app.data()`. Runtime collection
writes are not exposed: dropping a card asks the owner to persist its new lane.
`app.askAgent` returns no promise or delivery acknowledgement; inspect Chat for
delivery errors and the agent's progress.
Render the summary first; show an empty board or escaped query error as needed.

```html
<section class="board"><h2>Work board</h2><p>Drag a card to request a lane change.</p></section>
```
```css
.board { padding: 1rem; } .lanes { display: flex; gap: 1rem; } .lane { flex: 1; padding: 1rem; border-width: 1px; border-style: solid; border-color: #888; } .card { display: block; padding: .5rem; } .error { color: #b91c1c; }
```
```javascript
const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
app.onLoad(async () => {
  app.render('<section class="board"><h2>Work board</h2><p>Loading cards…</p></section>');
  try {
    const page = await app.query("cards", {fields: ["title", "lane"], limit: 20});
    const lanes = ["todo", "doing", "done"].map(lane => `<section class="lane" data-drop-action="move" data-drop-value="${lane}"><h3>${esc(lane)}</h3>${page.rows.filter(row => row.value.lane === lane).map(row => `<button class="card" data-drag-value="${esc(row.id)}">${esc(row.value.title)}</button>`).join("") || '<p>No cards.</p>'}</section>`).join("");
    app.render(`<section class="board"><h2>Work board</h2><div class="lanes">${lanes}</div><p>Showing up to 20 cards. Drop to request a saved change.</p></section>`);
  } catch (error) { app.render(`<section class="board"><p class="error">${esc(error.message)}</p></section>`); }
}, {data: "targeted"});
app.on("move", event => {
  app.askAgent(`Move card ${JSON.stringify(event.draggedValue)} to lane ${JSON.stringify(event.value)} in the cards collection.`);
});
```

### Summary dashboard (full document)

Use full mode for a small document such as
`{"summary":{"completed":7,"total":10,"label":"This week"}}`.
For a large document choose targeted mode and replace `app.data().summary` with
`(await app.readMany([["summary"]]))[0]`. Summary counters are not repeated rows.
Widths use generated classes and CSS, never inline styles.

```html
<section class="dashboard"><h2>Progress</h2><p>Saved summary of completed work.</p></section>
```
```css
.dashboard { padding: 1rem; } .track { background: #ddd; } .bar { height: 1rem; background: #2563eb; } .error { color: #b91c1c; }
```
```javascript
const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const css = '.dashboard{padding:1rem}.track{background:#ddd}.bar{height:1rem;background:#2563eb}.error{color:#b91c1c}' + Array.from({length:101}, (_, n) => `.width-${n}{width:${n}%}`).join("");
app.onLoad(() => {
  try {
    const summary = app.data().summary;
    if (!summary || !Number.isFinite(summary.total) || summary.total <= 0) {
      app.render('<section class="dashboard"><h2>Progress</h2><p>No completed work to summarize yet.</p></section>', css);
      return;
    }
    const percent = Math.round(Math.max(0, Math.min(100, (Number(summary.completed) || 0) / summary.total * 100)));
    app.render(`<section class="dashboard"><h2>${esc(summary.label)}</h2><p>${percent}% complete</p><div class="track"><div class="bar width-${percent}"></div></div></section>`, css);
  } catch (error) { app.render(`<section class="dashboard"><p class="error">${esc(error.message)}</p></section>`, css); }
});
```
