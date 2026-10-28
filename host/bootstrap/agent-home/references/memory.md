# Memory Workspace reference

Read before writing or maintaining memory, or a broad memory audit. Routine
self-memory reads and shared-memory searches use the host guide directly.

## Write self-memory

`GET /agent/self/memory` returns the authenticated thread's current page;
404 means none. Never supply an identity or page id. Chat, App and model
Standing agents have self-memory; Bash transcripts do not.

`PUT /agent/self/memory` takes
`{"description":"when this is useful","content":"...","expected_revision":N}`.
Use revision `0` to create, or the current revision to edit. Description is
one line, at most 100 characters; content is at most 20,000 characters.
Keep durable thread-specific preferences, decisions and ruled-out approaches
as a current summary. Do not create a page without durable information.

## Private notes

Save up to 10 notes, each with at most 20,000 characters. IDs use 1–100
lowercase letters or underscores, such as `release_checklist`.

- `PUT /agent/self/notes/{note_id}` with `{"content":"..."}` creates or replaces a note.
- `GET /agent/self/notes/{note_id}` returns `{"note":{"note_id":"...","content":"..."}}`.
- `DELETE /agent/self/notes/{note_id}` permanently deletes a note.

`GET /agent/self/memory` returns your briefing in `page.content` and note IDs
in `page.saved_notes`.

## Read and maintain swarm memory

- `GET /agent/memory?limit=50&cursor=...`: page ids, descriptions, revisions
  and outgoing links, without bodies.
- `GET /agent/memory/pages/{page_id}`: the page and its backlinks.
- `GET /agent/memory/search?q=words&limit=20&cursor=...`: hybrid semantic and
  exact-word search. Repeat the same query when paging. If semantic search
  becomes temporarily unavailable mid-pagination, retry that cursor. Without
  strong matches, weaker token matches and commonly matched summaries may
  appear separately.
- `PUT /agent/memory/pages/{page_id}` takes
  `{"description":"when this is useful","content":"...","expected_revision":N}`.
  Use revision `0` to create, or the current revision to edit.
- `DELETE /agent/memory/pages/{page_id}?expected_revision=N` takes no body.

Page ids are lowercase slugs up to 64 characters; descriptions are one line
up to 100 characters; content is up to 2,000 characters. Keep durable,
reusable context. Descriptions say when to read the page so agents can choose
without loading every body. Link related pages with `[[page-id]]`; links are
derived from content, not a separate write field.

On 409, re-read and deliberately retry a conflicting write; for a changed
search generation, restart pagination with the same query. Find fresh writes
by exact slug or distinctive words while semantic vectors are still indexing.
