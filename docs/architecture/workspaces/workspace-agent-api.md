# Workspace agent API

The Workspace agent socket is the single agent-facing transport for Kern's
Workspace service. The MCP shim exposes Web Apps, first-class self-memory,
host-global memory, schedules, spawned-agent discovery, and thread identity
through `workspace_api`, and provides typed
`search_conversation_history`, `read_thread_history`, `send_agent_message`,
`spawn_agent`, and `archive_spawned_agent` tools over the same boundary. A compact capability map and failure-prone invariants remain in the
host-global instructions; complete App, memory, and schedule routes live in
the root-owned release references those instructions point to. Tool listing
itself is not dynamic discovery and grants no
additional identity.

The MCP shim sends `POST /call` to
`/run/kern-workspace/agent.sock`. The main `kern-workspace` process verifies
the caller is `kern-agent` with `SO_PEERCRED` before allocating a handler. It
caps connections and active calls, bounds request and response bodies, permits
only `GET`, `POST`, `PUT`, and `DELETE`, and accepts paths only below
`/agent/`. The agent cannot reach the service's browser Unix socket, the admin API
socket, or PostgreSQL role.
The socket admits 64 active calls, with a separate eight-call cap for routes
that can return the full 24 MiB response. Identity, self-memory, and agent
message routes return bounded small objects and use the general call cap.

Conversation history has two read-only routes used by the typed MCP tools:

```text
POST /agent/conversation-history/search
POST /agent/conversation-history/read
```

Cross-thread collaboration has three peer-identity-bound mutation routes:

```text
POST /agent/messages
POST /agent/agents
POST /agent/agents/archive
```

The first sends one host-labeled message to a known eligible App, model
Schedule, or Chat. The second atomically presents one create-and-send operation
to the caller: Workspace reserves the next `thread-N` Chat id and admits its
first message with a required interactive runtime/model/effort tuple. Messaging and spawning use
the same header, which identifies the authenticated sender, explicitly denies
operator authority, and gives the `send_agent_message` reply command.

Spawning also accepts an optional display `name`, stored in the existing Chat
name field.
`archive_spawned_agent` archives only the authenticated caller's idle spawned
Chats. It explicitly checks the stored spawning thread against the authenticated
caller under the Chat send lock, then calls the existing archive operation.
Spawned Chats are temporary and auto-archive after 24 hours of inactivity,
checked at startup and hourly. Running agents are skipped. Parents should archive
agents they no longer need; persistent or recurring work belongs in Standing
agents, which can have no triggers.

`GET /agent/spawned-agents` returns active spawned Chats across all parents in
an `agents` array: thread id, display name, parent id, runtime/model/effort,
and status. Ordinary and archived Chats are excluded. Discovery grants no
archive ownership. Names appear in Chat navigation. This catalog uses large-response capacity;
spawning, messaging, and archiving return small status objects.

Search returns bounded message excerpts, the active `search_mode`, and an opaque
relevance/time cursor. Text queries automatically use local hybrid vector and
full-text ranking; timestamp-only searches remain newest-first. The semantic
index is derived asynchronously and lexical search remains the fallback.
Conversation vectors cover only the newest 250,000 user/agent message rows;
activity and lifecycle events do not consume the quota. Pruning is amortized
across batches, allowing at most roughly 800 newly indexed messages of slack.
Relevance cursors retain source-event/embedding cutoffs and the initial ordered
semantic candidate ids, so later pages never rerun HNSW against a changing
graph. They expire if
source or vector retention advances. A new search may fall back to lexical
ranking when the model is unavailable; once paging starts, its cursor keeps the
ranking mode and frozen candidate set stable without requiring inference again.
Search and read preserve the stored distinction between conversation messages
and Kern notices. Only `type: "message"` has a `role` (`user` or `assistant`).
Delivered inputs are `type: "notice"` with `notice: {kind, summary}`; they are
never relabeled as operator messages. Older untyped messages stay as recorded,
without prefix-based classification.

Both tools accept independent filters:

| Field | Default | Meaning |
| --- | --- | --- |
| `roles` | `["user", "assistant"]` | Select ordinary conversation messages. `[]` excludes them. |
| `notice_kinds` | All four [input kinds](../agent-messages.md#event-contract) | Select notices independently of roles. `[]` excludes them. Search accepts input kinds; read accepts every catalogued kind. |

For operator messages only, pass `roles: ["user"], notice_kinds: []`. For peer
requests only, pass `roles: [], notice_kinds: ["agent_message"]`. Notice filters
apply before lexical/vector ranking and before every read pagination mode and
cursor-bound query. Empty arrays are valid. Filter ordering does not change a
search cursor's identity. Unknown kinds and duplicate values are rejected.
The old `exclude_automated_triggers` and `include_context` flags are removed.

Search returns at most 25 matches (default 10), each with `thread_id`,
`event_id`, `timestamp`, `type`, `excerpt`, and `excerpt_truncated`, plus either
`role` or `notice`. Excerpts are capped at 2 KiB of JSON-encoded text. Context,
action, activity, and lifecycle events are not indexed by conversation search.
Relevance cursors require the signed snapshot format; obsolete rank-only
cursors must start a new search. The MCP cursor limit matches the API's 8 KiB
limit, including searches with large frozen semantic candidate lists.

Read returns at most 50 chronological events (default 20) from any retained
Chat, App, or Standing-agent thread. With no cursor it returns the newest page;
`before`, `after`, or `around_event_id` select another page and are mutually
exclusive. Each event has `event_id`, `timestamp`, `type`, and `truncated`:

| Type | Returned fields |
| --- | --- |
| `message` | `role`, `content` |
| `notice` | `notice: {kind, summary}`; incoming input also has its exact delivered `content`, including the routing preamble |
| `activity` | Bounded `activity` summary, only with `include_activity: true` |

Context and action notices are compact by default. Selecting their kinds does
not include large diagnostic text. `include_details: true` additionally returns
recorded `memory_page_ids`, `memory_recall_details` (14 KB), `historical_context`
(24 KiB beginning/end preview), or `notice.details` (20 KB of action request and
result). An empty page-id array means zero recalled pages; an absent field means
it was not recorded. Page ids do not capture historical revisions or page text.
Message and incoming-input content is always included, bounded to 16 KiB per
event. The full response stays below 256 KiB, with explicit truncation markers
and flags. Omitted details are intentional selection, not truncation.
Lifecycle events such as errors, stops, and clears are outside these tools;
clearing working memory does not delete retained history.

For example, inspect recent memory writes without tool logs:

```json
{"thread_id":"thread-42","roles":[],"notice_kinds":["self_memory_saved","shared_memory_saved"],"limit":10}
```

Then request the recorded details around a returned event:

```json
{"thread_id":"thread-42","roles":[],"notice_kinds":["shared_memory_saved"],"around_event_id":"event_123","include_details":true,"limit":1}
```

Workspace is a deliberately narrow transport proxy here: its peer identity,
connection/call limits, 256 KiB request cap, 24 MiB response cap, fixed route
map, method check, and query-string rejection apply before the request reaches
the host. The host Admin API owns the one public field-validation contract,
opaque cursor creation and validation, indexed `agent_events` query, role
projection, and tighter conversation response budgets. No caller-controlled
value becomes an Admin path or SQL fragment.

Both responses explicitly identify their provenance as `retained_conversation_history`,
their trust as `untrusted`, and their instruction authority as `none`. Message
and activity contents remain unchanged; structured event `type` and `role`
fields distinguish their original kind without turning history into a live
user message.

The Codex-visible `search_conversation_history` declaration states its 1–25
result limit and pagination requirement in both JSON Schema (`minimum` and
`maximum`) and prose, so clients that omit schema bounds from rendered
signatures still receive the constraint.

For Web Apps, `GET /agent/apps` returns active and archived apps. Every other
route for App data contains an immutable id:

```text
/agent/apps/{app_id}/state/meta
/agent/apps/{app_id}/state/ui
/agent/apps/{app_id}/state/data
/agent/apps/{app_id}/state/data/shape
/agent/apps/{app_id}/state/data/read
/agent/apps/{app_id}/actions
```

The backend verifies the app exists. Reads are allowed for active and archived
apps. Every mutation takes the same bounded per-app lock used by browser
archive and other writes, then rechecks that the app is active. Optimistic UI
and data counters reject stale changes. Restore remains operator-only because
it rewinds the App's UI/data state.

The agent API requires the authenticated caller's thread id to equal the App
id for mutations, including metadata, UI, data, and collections. Other agents
may read/discover the App and message its owner to request changes. Editable
display names never grant authority; operator browser controls remain available.

`POST /agent/apps/{app_id}/state/data/read` accepts either `path` for its
original single-branch response or `paths` for up to 16 branches read from one
consistent revision. Multi-path responses return ordered `{path, value}`
entries. `missing` defaults to `"error"`; `"null"` keeps sparse operational
reads compact by returning `null` for absent branches.

Agent read responses share a `max_bytes` budget, default 8 KiB of compact JSON
(including metadata, excluding the outer `app` wrapper). Largest branches are
replaced first with `truncated: true`, serialized `bytes`, applied `max_bytes`
and `shape` instead of `value`. Shapes may themselves be truncated to keep the
response bounded. Read narrower paths or explicitly raise `max_bytes`, up to
`MAX_STATE_RESPONSE_BYTES`, for full values. A budget too small for the paths
and metadata is rejected with 422. App UI runtime reads retain full values
under the existing state-response limit and do not accept `max_bytes`.

`GET /agent/apps/{app_id}/state/data/shape` answers the question a narrow read
requires an answer to first: which branches exist, and which are worth the
tokens. It returns per node a `type`, an object's `keys`, and an array's merged
`items`. Array elements merge into one `items` node, so describing a thousand
records costs one record.

Object keys in the map are read-route path segments. An array's `items` node
describes that array's elements rather than naming a segment, so a caller
substitutes an index for it: the map's `leads.items.status` is read as
`["leads", 0, "status"]`. Being spendable on a narrow read is the only reason
the map is worth returning.

A write validates the path it targets but not the object keys inside the value
it stores, so a document can hold an empty or oversized key that
`_validated_path` refuses as a segment. Such a key is marked `addressable:
false` rather than omitted: the branch exists, and hiding it would make the map
lie about the document, while marking it says only that a full data read is the
way to reach it.

Sizes — an array's `length` and an object, array, or string's encoded `bytes` —
describe a single observed value only. A merged position holds one value per
observed record, so no single size is true of all of them; a summed `length`
would advertise an index that the record a caller actually reads does not have.

The shape is derived from the stored document on every call. There is
deliberately no route that writes it: a stored map would need to be kept in
sync with `data_json`, and a map that is wrong is worse than none because
callers act on it. For the same reason every bound it applies is marked in
place — `truncated` where depth, key count, or the node budget cut the walk,
`sampled` where only an array prefix was read — so a partial map is never
mistaken for a total one.

Values are summarized, never copied. Strings collapse to an `enum` only when a
position was observed at least four times, holds at most eight distinct short
values, and averages at least two observations per distinct value. Categories
repeat and identifiers do not, and returning identifiers would rebuild the
document the route exists to avoid returning; requiring only one repeated value
would let a field of names with a single coincidental duplicate publish every
name it holds. Keys absent from some merged records are marked `optional`.

Global routes are:

```text
/agent/identity
/agent/self/memory
/agent/self/notes/{note_id}
/agent/memory[?limit=...&cursor=...]
/agent/memory/search?q=...
/agent/memory/pages/{page_id}
/agent/schedules[?limit=...&before=...]
/agent/schedules/session-options
/agent/schedules/{schedule_id}
```

`GET` and `PUT /agent/self/memory` resolve the page id exclusively from the
kernel-attributed peer thread. They delegate to the ordinary memory page load
and save behavior, including 404, optimistic revision checks, and size limits;
the request has no identity or page-id field. Chat, App, and persistent model
schedule threads all use this same identity-derived self-memory path.
Self-memory content is limited to 20,000 characters; shared swarm memory
remains limited to 2,000. Both use a one-line description of up to 100 characters.

Memory page ids beginning with `app-`, `thread-`, or `schedule-` form the
individual-memory namespace. Agent index, search, and direct page routes expose
only swarm memory and return 404 for direct access to an individual page. App,
Chat, and persistent model schedule threads reach only their identity-derived
page through self-memory, and their own topic notes through the notebook routes.
`schedule-*` pages remain individual.
The browser API accepts `scope=swarm|individual` on memory index and search,
defaulting to `swarm`, so the operator UI can display the namespaces
separately. Existing pages keep their ids and are classified in place by this
prefix rule; the distinction makes the prior individual-memory convention an
enforced API boundary.

### Private notebooks

Self-memory is a short briefing and index. Notes hold durable topic detail that
the agent reads when needed; App records remain authoritative for live jobs,
and files hold artifacts and evidence. There is no automatic migration,
summarization, or trimming of existing self-memory.

The notebook has only `GET`, `PUT`, and `DELETE /agent/self/notes/{note_id}`.
`PUT` takes `{"content":"..."}` and creates or replaces the note. GET and PUT
return `{"note":{"note_id":"...","content":"..."}}`; DELETE takes no body and
returns `{"deleted":true,"note_id":"..."}`. Missing reads/deletes return 404.
No description, revision, owner argument or query parameter is accepted.
There is no list/search/pagination API. IDs contain 1–100 lowercase ASCII
letters or underscores, and content allows at most 20,000 Unicode characters.
Each agent has at most 10 notes; an eleventh creation returns 409. Replacing an
existing note remains allowed at capacity. Delete is permanent and immediately
frees capacity. Concurrent writes use last-successful-write semantics.

The `private_notes` table stores current text keyed by owner and ID. Writes
serialize under an owner-specific transaction lock and check the owner's row
count before inserting a new note. Notes survive context clears, runtime
changes and archival. Ownership comes from the authenticated agent.

Self-memory reads return the editable briefing in `content` and the complete
alphabetic list of note IDs in `saved_notes`. Turn injection, including fallback
and periodic reads, carries both fields separately. Note bodies are fetched
explicitly. The delivery cache includes the note IDs so additions and deletions
are reflected before its usual one-hour expiry. Self-memory keeps its existing
history, revision checks, and 20,000-character content limit.

If notes exist before a briefing, the loader returns empty `content`, the saved
IDs, and revision 0. The page appears in the individual-memory index and has no
history until saved. A retained deleted briefing keeps its deleted state and
revision; agent reads return 404 until the operator restores it.

The operator UI edits the briefing and displays a separate Saved notes section
below memory history. `GET /memory/pages/{owner}/notes` supplies its note cards
with IDs and bodies. Notes are not private from the operator or retained history.

### Standing agents and shared resources

Every schedule owns one stable `schedule-N` thread. A due calendar trigger submits its saved
prompt through the thread-message delivery path with an automated-trigger
prefix and explicit `scheduled_trigger` metadata, recorded as `thread.notice`.
Daily triggers choose up to 24 UTC times; weekly triggers choose weekdays
and one UTC time. All triggers share one revision and one runtime/model/effort
configuration. An empty list stops automatic messages. Coincident triggers are
attempted independently; missed minutes are skipped and failures are logged
without retries. The next fixed calendar time advances before delivery. Model schedules
reuse their conversation and provider context; a firing steers an active turn
when the provider supports steering. Script schedules run the saved path under
the bounded Bash provider. Output is an ordinary agent message and delivery or
execution failures after acceptance are ordinary `thread.error` events.
Failures before host acceptance are logged operationally and do not create a
thread event. There is no retry queue, run record, run-status API, or
recent-failures route. Bash schedule threads remain visible as read-only
transcripts under Standing agents; they do not expose manual messaging or
self-memory controls.

Memory and schedule writes use an `expected_revision` compare-and-swap so
concurrent edits cannot silently overwrite each other. Agents may update only
their own standing-agent definition. Agents can create new standing agents;
the new agent owns subsequent edits. Memory
CRUD remains shared; the browser-only API exposes deleted resources, revision history, and
restoration. The stable schedule conversation uses the ordinary Chat event
API. Memory
list/search responses are paginated and omit page bodies. Swarm page content
may link to another swarm page as `[[page-id]]`; Kern maintains a derived link
index without a separate graph database. Individual pages are excluded from
all links and backlinks. Agent memory search combines exact page-id and
description phrases, PostgreSQL full-text ranking, and local pgvector cosine
ranking with bounded reciprocal-rank fusion. It then adds a low-weight,
one-hop expansion over links and backlinks. Current pages are embedded
asynchronously; lexical and weak/popular fallback remain available during
backfill or model failure, and page content is never sent to a remote embedding
provider. Search always returns current pages, so writes can still shift a
best-effort cursor; however, a cursor keeps its initial hybrid or lexical-
fallback ranking mode and asks the caller to retry if hybrid inference becomes
unavailable instead of silently changing order.

Schedule `PUT` replaces the complete definition. Runtime/model/effort values
are stored as bounded strings and checked by the host only when delivery starts;
an unavailable configuration is logged as a failed delivery attempt rather
than being rejected when the definition is edited.

The agent socket obtains its peer PID and UID through `SO_PEERCRED`.
`GET /agent/identity` and the self-memory alias read the peer's cgroup and
accept only the `kern-agent-thread-<thread_id>.scope` unit created by the root
launcher. The identity route returns that thread id, while the self-memory
alias selects only its corresponding page. Callers cannot supply or override
the value. It does not authorize access to a Web App.

## One-time reminders

`GET|POST /agent/self/reminders` uses the authenticated thread identity.
POST requires exactly `message`
(nonblank, at most 1,000 Unicode characters) and either `at` (a future timestamp
with timezone) or `after` (a positive integer followed by `s`, `m`, `h` or `d`).
Delivery must be scheduled within the next 7 days. Times are stored as UTC seconds. GET returns `reminders` (100 entries ordered by
immutable id descending), `next_before` (pass as `?before=ID` for the next page),
and the complete `pending` list ordered by due time with `pending_count`. Pending
entries may also appear in `reminders`; the UI uses `pending` for its Pending section.
The id cursor stays stable when entries are delivered.
Each agent may have at most 5 pending reminders; creation checks this limit
under the parent row lock. The existing reminder worker cleans up history once daily, retaining
the 1,000,000 most recently finished records across all agents. Pending reminders
are never pruned. A reminder has `id`, `thread_id`, `message`, `due_at`, `status`, `error`,
`created_at` and `finished_at`. Once created, the message and delivery time cannot be changed. There is no editing, rescheduling or cancellation API or UI. The agent can ignore a delivered reminder if it is no longer useful.

The operator uses `/v1/workspace/reminders/{thread_id}` with the same
verbs and bodies. The shared Reminders toolbar panel appears on Chat, spawned,
App and model Standing agents, including archived agents. It only displays Pending
and Completed records (delivered or failed); creation is available through the API.
Bash agents are excluded.

An independent Workspace worker scans durable overdue entries every 30 seconds,
examining up to 100 candidates per poll in due-time order. It starts at most
four concurrent delivery attempts, with one in flight per agent and no queued
batch; slow attempts do not block polling or delivery to other agents while
capacity is available. Remaining overdue entries stay pending for a later poll. Each reminder gets one delivery
attempt through existing agent message admission using current session settings
and active-turn steering. Acceptance marks it delivered; every admission or
transport error marks it failed, including busy agents, capacity limits and
ambiguous timeouts. Failed reminders are not retried. A timeout can mean the
message was accepted even though its reminder is marked failed.
Completion timestamps are captured after each attempt. A delivered reminder means its
message was accepted, not that the requested work completed. A reminder fails if its agent is archived when delivery is attempted; archive
itself does not change reminder records. Spawned auto-archive skips agents with pending reminders.
Reminders also fail if their target has become a Bash agent. Pruning a deleted schedule also removes its reminder history.
There is no additional pause/lock state.

Reminder notices use the same `kind` and `summary` fields and ordinary Admin
message path as other automated inputs. There is no reminder-specific ID,
validation or duplicate-detection logic in Admin. A crash after acceptance but
before Workspace records the result can leave a pending reminder that is sent
again after restart; the operator accepts this rare duplicate case.
