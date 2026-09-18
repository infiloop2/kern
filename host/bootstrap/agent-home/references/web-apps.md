# Web Apps Workspace reference

Read before App API operations. For writing or debugging generated UI, also
read [web-app-ui.md](web-app-ui.md). Data-only operations need only this file.

Web Apps have immutable ids such as `app-1`, separate from editable display
names. `GET /agent/apps` lists active and archived apps, including each App's short `purpose` (up to 100 characters) and
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

## App name and agent settings

- `PUT /agent/apps/{app_id}/name` with `{"name":"Marketing HQ"}` renames the
  App without changing its immutable id. Names must be nonblank and at most
  100 characters.
- `GET /agent/apps/session-options` returns `session_options` (runtime keys
  mapping to model keys and their supported effort lists) and `active_runtimes`.
  Read these choices before configuring an App; do not guess model names.
- `PUT /agent/apps/{app_id}/agent-settings` with
  `{"agent_runtime":"codex","model":"gpt-5.6-sol","effort":"high"}` saves the
  complete configuration for the App's next agent message. All three fields
  are required and validated against the session options. This does not start
  an agent turn. A running App agent returns 409; wait until it is idle before
  changing settings, including when changing the current App's own settings.

Both PUT routes return `{"app":...}` with the App summary, including `name`
and `agent_settings`. They reject archived Apps (409) and agent-locked Apps
(423). These metadata changes do not take `expected_revision` or advance the
UI/data revision.

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

## Discovery purpose

Apps have an optional, single-line `purpose` of at most 100 characters. It is
returned in App lists, separate from the name and data. Set it with
`PUT /agent/apps/{app_id}/name`, using
`{"name":"Research desk","purpose":"Review company research"}`. Omitting
`purpose` preserves it; an empty string clears it. The App details dialog
edits both fields. Existing archive and agent-update locks apply.

For cross-thread requests and replies, see [agent messaging](agent-messaging.md).
