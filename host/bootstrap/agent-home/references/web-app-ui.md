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
`null`; read again when the resulting stored branch is needed. A worker turn
is terminated after three seconds; durable state belongs in App data or a
collection, never worker memory.
