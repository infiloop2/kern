# Web Apps Workspace reference

Read when the host guide directs: App creation, `/agent/apps/{app_id}/...`
operations or diagnosis. For generated UI, also read [web-app-ui.md](web-app-ui.md).

Use immutable `app-N` ids. Any agent may read Apps; only the owner may change
its active, unlocked App, except for explicitly granted collection writes. Archived or agent-locked Apps are read-only.

## Create and configure

- `POST /agent/apps` takes no body and requires an explicit operator request.
  It returns `app_id` and the host-selected `agent_settings`. Message that
  App agent to build/configure it; creation gives you no write access.
- `PUT /agent/apps/{app_id}/name` takes `{"name":"Research desk"}` and optionally
  `"purpose":"Review company research"`. Name is nonblank, at most 100
  characters. Purpose is one line, at most 100 characters; omit to preserve,
  or use an empty string to clear. List summaries include name, purpose,
  `archived`, `agent_updates_locked` and `agent_settings`.
- `GET /agent/apps/session-options` returns `session_options` (runtime → model
  → supported efforts) and `active_runtimes`. Use these choices, not guessed
  model names.
- `PUT /agent/apps/{app_id}/agent-settings` requires all three fields:
  `{"agent_runtime":"codex","model":"gpt-6.1-sol","effort":"high"}`.
  Use a currently supported combination. Saving does not start a turn;
  the current turn and its follow-ups keep their configuration, and the next
  turn uses the new settings.

Both PUTs return `{"app":...}` including name/settings; neither takes
`expected_revision` nor advances the UI/data revision.

For all App writes, inspect 409 errors: archived Apps are read-only (check
`archived` in `GET /agent/apps`); only revision conflicts call for re-reading
relevant state and retrying. Stop on an archived App or a 423 lock.

## Read state

- `GET /agent/apps/{app_id}/state/meta`: revision, update time, byte sizes and
  `agent_updates_locked`, plus `last_runtime_report` and `last_runtime_report_at`
  (null until a runtime failure is reported; report text is at most 400 UTF-8 bytes).
- `GET /agent/apps/{app_id}/state/ui`: revision, HTML, CSS and JavaScript.
- `GET /agent/apps/{app_id}/state/data`: revision and full JSON data.
- `GET /agent/apps/{app_id}/state/data/shape`: keys, types and branch `bytes`
  without values, except possible `enum` samples of repeated short strings.
  `truncated` or `sampled` means partial coverage. Object keys are path
  segments; array `items` describes elements, so `leads.items.status` maps to
  `["leads",0,"status"]`. `addressable: false` means no narrow path reaches
  that key; use a full data read. Shape is derived, never written.
- `POST /agent/apps/{app_id}/state/data/read` with `{"path":["projects",0]}`
  reads a branch. `{"paths":[["config"],["next_id"]],"missing":"null"}` reads
  up to 16 branches at one revision; `missing` defaults to `"error"`. Responses default to
  8 KiB of compact JSON. Oversized branches return `truncated: true`, `bytes`
  and `shape` instead of `value`; large shapes may also be truncated. Read a
  narrower path or increase `max_bytes` (up to 23 MiB) for the full branches.

## Write state

`POST /agent/apps/{app_id}/actions` accepts:

- `{"action":"publish_ui","expected_revision":7,"html":"...","css":"...","javascript":"...","data_operations":[...]}`: replace the full UI with optional 0–32 atomic data operations.
- `{"action":"set","expected_revision":7,"path":["projects",0,"status"],"value":"done"}`
- `{"action":"delete","expected_revision":7,"path":["projects",0]}`
- `{"action":"append","expected_revision":7,"path":["activity"],"value":{...}}`
- `{"action":"batch","expected_revision":7,"operations":[...]}`: 1–32 atomic
  data operations, each using `action`, never `op`.

Success increments the shared App revision once and preserves unmentioned
data. Carry its returned revision forward. Paths contain 1–16 object keys or
non-negative array indexes; parents must exist. `set` cannot append an array
item: use `append`.

Limits: 1 MiB HTML, 64 KiB CSS, 128 KiB JavaScript, 10 MiB document data,
and 256 KiB per agent request. UI publication (`publish_ui`) allows an 8 MiB
request for the bundle and JSON escaping; individual field limits still apply.
Grow large documents with targeted operations.

## Collections

Schema rules:
- The document holds settings, view state and an optional small summary, never repeated records.
- Repeated records belong in collections with stable ids. Keep filter/sort fields as top-level scalars and large text in separate fields so queries can omit it.
- Published HTML is a compact shell with a summary, never a copy of the last full table.

- `GET /agent/apps/{app_id}/collections`: names, row counts, byte sizes and
  current App revision.
- `POST /agent/apps/{app_id}/collections/leads/query`: optional `filters`
  (up to 8 `eq`, `ne`, `exists` or `missing` operations on top-level fields),
  `ids`, one `sort`, `limit` (1–100) and `offset`. Optional `fields` selects
  1–32 top-level field names in SQL; row ids remain, missing fields are omitted,
  and explicit nulls remain null. Filters and ordering use the original row.
  `count_only: true` returns `rows: []`, `total` and `next_offset: null` without
  fetching rows. Returns rows, `total` and `next_offset`. The sum of compact
  serialized row bytes may not exceed 1 MiB (1,048,576 bytes); a 413 response
  asks you to use `fields` or a smaller `limit`. Unknown query keys are rejected.
- `POST /agent/apps/{app_id}/collections/leads/actions` with
  `{"expected_revision":3,"operations":[{"action":"upsert","id":"lead-1","value":{"status":"new"}},{"action":"merge","id":"lead-3","value":{"score":7}},{"action":"delete","id":"lead-2"}]}`
  applies up to 100 row operations atomically and advances the same revision
  as UI/document writes. `upsert` replaces the whole row; `merge` replaces only
  the patch's top-level fields and keeps the rest (a missing row becomes the
  patch; a null field is stored as null). The merged row must stay within the
  128 KiB row cap. The same archived/locked/conflict rules apply.

Collection names and row ids are stable. Limits per App: 64 collections,
100,000 rows and 50 MiB of collection data; 128 KiB per row. Recovery points
include collection rows.

Before initiating work with the owner, read [agent-messaging.md](agent-messaging.md).

## Collection-only grants

Any agent may read `GET /agent/apps/{app_id}/grants`, returning
`{"grants":{"thread-42":{"collections":["leads"],"actions":["merge"]}}}`.
Only the owner can `PUT /agent/apps/{app_id}/grants` with
`{"thread_id":"thread-42","collections":["leads"],"actions":["merge"]}` to
replace that grantee's entry, or `DELETE /agent/apps/{app_id}/grants/thread-42`
to revoke it. An empty collection list also revokes. `actions` is optional and
defaults to all of `upsert`, `merge`, `delete`; list only `merge` when the
grantee owns some fields of rows you curate, so it can never replace or delete
a row. Up to 16 grantees and 16 unique collection names per grantee are
allowed. Identities are `thread-N`, `schedule-N` or `app-N` with a positive
integer; the owner cannot be a grantee. Grants do not advance the App revision.

A grantee can only POST the allowed row actions to the named collections. UI,
document, metadata and grants remain owner-only. Each batch targets one
collection; operations cannot name another collection. Grant authorization and
row writes share the App's database lock, so a revoked grant cannot authorize a
later write. Archived, locked and expected-revision rules still apply. Notices
record the authenticated caller, never a caller identity supplied in the body.
A successful granted write also leaves an `app_collection_changed` notice in
the owner's thread, caller first, as an audit record: it starts no turn and is
not delivered to the owner's model. Nothing needs to be delivered: the App UI
reads collections live, and an owner that keeps derived state checks for rows
without its own validation stamp when it next runs. Agree the collection, id
scheme and merged fields before granting; no per-batch message is needed.
