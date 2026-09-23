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
`await app.read(["path", 0])`; targeted mode does not load the full document
and makes `app.data()` unavailable. Mutation acknowledgements do not return
the full document in either mode. Generated Apps can call
`await app.query("leads", request)` with the collection query body to load only
one requested page.

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
`app.on(action, handler)`, `app.data()`, `app.read(path)`,
`app.query(collection, request)`, `app.render(html, css)`, `app.set`,
`app.delete`, `app.append`, `app.askAgent(message)`, and
`app.notify(message, level)`. Always register `app.onLoad`. Use `app.data()` in
compatibility mode or `app.read(path)` in targeted mode. In targeted mode
`set` and `append` resolve to the submitted value and `delete` resolves to
`null`; read again when the resulting stored branch is needed. Writes apply in
the order issued. Durable state belongs in App data or a collection, never
worker memory.

## Responsiveness

A turn (onLoad or one action) has a five-second total deadline after the
browser sandbox starts, including generated worker startup and all host
requests. The browser sandbox has a separate 15-second startup limit. Each
`app.read`, `app.query`, `app.set`, `app.delete` or `app.append` is a browser
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
- Publish the last rendered HTML as the saved bundle, never a placeholder, so
  the operator sees content while the worker starts.
- A click during a running turn is queued and runs next; do not add manual
  busy states for that. Timeouts append `[App runtime report: ...]` to the
  operator's next message: fix the named action's request count.
