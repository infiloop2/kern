# Workspace agent API

The Workspace agent socket is the single agent-facing transport for Kern's
Workspace service. The MCP shim exposes Web Apps, first-class self-memory,
host-global memory, schedules, and thread identity through `workspace_api`, and provides typed
`search_conversation_history` and `read_thread_history` tools over the same
boundary. A compact capability map and failure-prone invariants remain in the
host-global instructions; complete App, memory, and schedule routes live in
the root-owned release references those instructions point to. Tool listing
itself is not dynamic discovery and grants no
additional identity.

The MCP shim sends `POST /call` to
`/run/kern-workspace/agent.sock`. The main `kern-workspace` process verifies
the caller is `kern-agent` with `SO_PEERCRED` before allocating a handler. It
caps connections and active calls, bounds request and response bodies, permits
only `GET`, `POST`, `PUT`, and `DELETE`, and accepts paths only below
`/agent/`. The agent cannot reach the service's browser TCP listener, admin API
socket, or PostgreSQL role.

Conversation history has two read-only routes used by the typed MCP tools:

```text
POST /agent/conversation-history/search
POST /agent/conversation-history/read
```

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
Read returns chronological, byte-bounded user/assistant messages and optional
normalized activity summaries. It can open the latest page, page before or
after an event cursor, or center context on a search hit. These routes can read
any retained host thread, including Chat, app, and schedule threads.

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

There is intentionally no mapping from the caller's conversation thread to an
app. Any agent thread can work on any existing app when it knows or lists the
app id; editable display names are never authorization or identity.

`POST /agent/apps/{app_id}/state/data/read` accepts either `path` for its
original single-branch response or `paths` for up to 16 branches read from one
consistent revision. Multi-path responses return ordered `{path, value}`
entries. `missing` defaults to `"error"`; `"null"` keeps sparse operational
reads compact by returning `null` for absent branches.

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

Memory page ids beginning with `app-`, `thread-`, or `schedule-` form the
individual-memory namespace. Agent index, search, and direct page routes expose
only swarm memory and return 404 for direct access to an individual page. App,
Chat, and persistent model schedule threads reach only their identity-derived
page through self-memory. `schedule-*` pages remain individual.
The browser API accepts `scope=swarm|individual` on memory index and search,
defaulting to `swarm`, so the operator UI can display the namespaces
separately. Existing pages keep their ids and are classified in place by this
prefix rule; the distinction makes the prior individual-memory convention an
enforced API boundary.

Every schedule owns one stable `schedule-N` thread. A firing submits its saved
message through the ordinary thread-message path with an automated-trigger
prefix, then advances the cadence after that single attempt. Model schedules
reuse their conversation and provider context; a firing steers an active turn
when the provider supports steering. Script schedules run the saved path under
the bounded Bash provider. Output is an ordinary agent message and delivery or
execution failures after acceptance are ordinary `thread.error` events.
Failures before host acceptance are logged operationally and do not create a
thread event. There is no retry queue, run record, run-status API, or
recent-failures route. Bash schedule threads remain visible as read-only
transcripts under Scheduled agents; they do not expose manual messaging or
self-memory controls.

Memory and schedule writes use an `expected_revision` compare-and-swap so
parallel agents cannot silently overwrite each other. Agents have ordinary
CRUD; the browser-only API exposes deleted resources, revision history, and
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
