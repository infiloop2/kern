# Web Apps Workspace reference

Read this file before using App routes or writing generated App code. The
always-loaded host guide carries the failure-prone invariants; this file is the
complete route and runtime reference.

Web Apps have immutable ids such as `app-1`, separate from editable display
names. `GET /agent/apps` lists active and archived apps, including each App's
complete `agent_settings` (`agent_runtime`, `model`, and `effort`) and
`agent_updates_locked` state. Any agent may read an App by id and may update an
active, unlocked App; archived and agent-locked Apps are read-only. Use the id
the operator gives you, or list Apps and confirm the immutable id; never choose
an App from its editable name alone. Migrated Apps inherit the configuration of
their linked host thread, or the pinned Codex default if they have no thread.

Create a new App with `POST /agent/apps` without a request body, but only when
the operator explicitly asks you to create one. App creation never accepts an
agent configuration from the browser or an agent. The backend selects and
persists the first active runtime, its named default model, and High effort (or
Codex when none is active). The response contains the new immutable `app_id`
and complete `agent_settings`; use the id for every subsequent read and write.

## State reads

For an App id `{app_id}`, read only what the task needs:

- `GET /agent/apps/{app_id}/state/meta` — revision, update time, byte sizes, and
  `agent_updates_locked`.
- `GET /agent/apps/{app_id}/state/ui` — `revision`, HTML, CSS, and JavaScript.
- `GET /agent/apps/{app_id}/state/data` — `revision` and full JSON data.
- `GET /agent/apps/{app_id}/state/data/shape` — data keys, types, and per-branch
  `bytes` without values. Object keys are path segments. An array's `items`
  describes its elements rather than naming a segment, so
  `leads.items.status` maps to `["leads",0,"status"]`. Repeated short strings
  may appear as `enum`; `truncated` or `sampled` means the map is partial;
  `addressable: false` means no narrow path can reach the key, so fetch that
  branch with a full data read. Shape is derived on every call and is never
  written.
- `POST /agent/apps/{app_id}/state/data/read` with
  `{"path":["projects",0]}` reads one branch. Use
  `{"paths":[["config"],["next_id"]],"missing":"null"}` to read up to 16
  branches from one consistent revision; `missing` defaults to `"error"`.

## State writes

Write with `POST /agent/apps/{app_id}/actions`:

- `{"action":"publish_ui","expected_revision":7,"html":"...","css":"...","javascript":"...","data_operations":[...]}` replaces the full UI and may apply 0–32 targeted data operations atomically.
- `{"action":"set","expected_revision":7,"path":["projects",0,"status"],"value":"done"}`
- `{"action":"delete","expected_revision":7,"path":["projects",0]}`
- `{"action":"append","expected_revision":7,"path":["activity"],"value":{...}}`
- `{"action":"batch","expected_revision":7,"operations":[...]}` applies
  1–32 data operations atomically. Every nested operation uses `action`, never
  `op`.

Every successful request increments the one App `revision` exactly once and
preserves unmentioned data. Carry the returned revision forward rather than
re-reading. A 409 means another writer changed the App: read the relevant
resource and retry. A 423 means the operator temporarily locked agent updates:
stop, tell them the App is locked, and retry only after they unlock it.

Paths are object keys and non-negative numeric array indexes, 1–16 segments.
The parent path must exist. `set` replaces an existing array index and does not
append at index equal to the array length; use `append` for a new item. Limits:
128 KiB HTML, 64 KiB CSS, 128 KiB JavaScript, and 10 MiB total data. Individual
agent requests remain capped at 256 KiB, so grow large documents through
targeted operations.

## Collections

Use collections for repeated records that the UI must filter or page without
loading the whole App document:

- `GET /agent/apps/{app_id}/collections` lists collection names, row counts,
  byte sizes, and the App's current `revision`.
- `POST /agent/apps/{app_id}/collections/leads/query` accepts optional
  `filters` (up to 8 `eq`, `ne`, `exists`, or `missing` operations on top-level
  fields), `ids`, one `sort`, `limit` (1–100), and `offset`. It returns matching
  rows, `total`, and `next_offset`.
- `POST /agent/apps/{app_id}/collections/leads/actions` with
  `{"expected_revision":3,"operations":[{"action":"upsert","id":"lead-1","value":{"status":"new"}},{"action":"delete","id":"lead-2"}]}`
  applies up to 100 row operations atomically. It advances the same App
  revision used by UI and document writes; on 409, read the relevant state and
  retry.

Collection names and row ids are stable identifiers. Collection changes are
part of the App's one combined revision, and retained recovery points include
all collection rows. An App may retain 64 collections, 100,000 rows, and 50
MiB of collection data; one row is capped at 128 KiB. Keep small configuration
and cohesive state in the App document, and put queryable repeated records in
collections.

## Generated App runtime

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
