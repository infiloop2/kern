# Workspace service

Chat and Web Apps are built-in Kern product workspaces. They are not installable
packages and there is no generic app platform, manifest registry, dynamic UID
range, or per-app service. Both render inside the authenticated admin page and
share `kern-workspace.service`.

## Operator UI

The admin sidebar has release-owned **Chat**, **Apps**, **Memory**, and
**Schedules** sections. Chat rows
represent conversations; App rows represent generated Web App workspaces.
Each section can create, select, rename, archive, restore, and show running
state for its own records. Archive is idle-only and retains all durable state.

The trusted Workspace HTML, CSS, and JavaScript are fixed admin assets mounted
into Shadow DOM. They are separate source files for maintainability, but are
served by the admin UI service rather than an app server or iframe bridge.
Home remains the default panel.

Memory is host-global, not attached to an App. It is a paginated set of small,
revisioned pages with descriptions, lexical search, soft-delete, history, and
operator restore. Swarm pages also form a `[[page-id]]` link graph; individual
pages do not participate in that graph. Schedules are also host-global.
Each definition stores its own agent runtime, model, effort, cadence, and
message. Every firing creates a fresh `schedule-ID-run-ID` host thread; runs never
reuse a provider session or overlap another run of the same schedule. The run
record snapshots the submitted message and runtime configuration alongside that
thread id; the UI reads agent responses and activity directly from retained host
events rather than copying them. Definitions validate only bounded
field shapes when written; runtime availability is checked when each run starts,
and an unavailable configuration becomes a failed run with a visible error.
Schedules have no separate pause flag: deleting one stops future occurrences,
and restoring it schedules the next occurrence from restoration time.

Workspace retains at most 10,000 memory pages and 100 schedules. Each resource
keeps its latest 100 revisions; deleted resources remain restorable for 90
days. Terminal schedule runs remain for 90 days and are also capped to the
latest 1,000 per schedule. PostgreSQL sequences are monotonic but intentionally
allow gaps.

## Browser path

The operator page calls:

```text
/v1/workspace/chat/...
/v1/workspace/web-apps/...
/v1/workspace/memory/...
/v1/workspace/schedules/...
```

The admin API authenticates the operator and validates CSRF before proxying to
path-prefixed `/chat/...` and `/apps/...` routes on `127.0.0.1:7450`. Cookies,
CSRF values, and identity headers are not forwarded. nftables permits only
`kern-admin` to connect to that port.

The backend calls host thread operations through
`/run/kern-admin-api/workspace.sock`. Filesystem permissions and
`SO_PEERCRED` admit only `kern-workspace`; a fixed allowlist exposes thread
list, detail, message, stop, event, and bounded conversation-history operations. The admin API passes thread
ids unchanged and performs no product prefixing or product ownership check.
The optional thread-list `prefix` query is an index/filter optimization, not
an authorization claim.

Chat directly owns ids such as `thread-1`; Web Apps directly owns ids such as
`app-1`. Those immutable ids are also the host thread ids. Editable names are
stored only as presentation metadata in Workspace-owned tables.

## Agent path

The MCP shim always lists `workspace_api`, `search_conversation_history`, and
`read_thread_history`. Calls go to
`/run/kern-workspace/agent.sock`, which is owned by the main service. The
server authenticates the `kern-agent` uid with `SO_PEERCRED` before allocating
a bounded handler, accepts a bounded `POST /call` envelope, and routes only
validated `/agent/...` requests. `GET /agent/identity` derives the current host
thread from the peer process's root-created cgroup; it is informational and
does not select or authorize an App.

The two typed history tools search user/assistant messages across retained
Chat threads and read bounded chronological pages with optional normalized
activity. An agent selects an existing Web App explicitly through routes under
`/agent/apps/{app_id}/...`. Any agent thread may read any existing app and
write any active app. Archived apps remain readable but reject every agent
mutation. Chat has no agent-callable product API.
Agents can also list, search, fetch, create, edit, and delete swarm memory
pages. Individual `app-*`, `thread-*`, and `schedule-*` pages are absent from
those routes; App and Chat threads reach only their own page through
self-memory, while schedule self-memory is disabled. Agents perform ordinary
CRUD on global schedules. Revision history and restore stay operator-only.
The host-wide conversation-history tools are read-only and are not a
Chat product mutation API.

## Service and database boundary

`kern-workspace` is a fixed Linux and PostgreSQL identity with no internet or
general loopback egress. One process serves the browser TCP endpoint, the
agent Unix socket, generated Web Apps, and the global schedule runner. All
tables live in the admin database's `public` schema: Chat uses `chat_threads`;
Web Apps uses `web_apps` and `web_app_revisions`; global resources use
`memory_pages`, `memory_page_revisions`, `schedules`, `schedule_revisions`, and
`schedule_runs`.

All schema changes live in the single immutable `host/migrations` stream and
its `schema_migrations` ledger. `kern-admin` owns every table and performs DDL.
Migrations grant `kern-workspace` DML only on those eight tables and their
five sequences; it cannot read any other admin, credential, network, or tool
state and has no DDL rights.

Migration `0027_global_memory_schedules.sql` imports current live per-App
memory and schedules into the global model. Duplicate page ids retain the
newest row (lowest App id breaks timestamp ties); descriptions and bodies are
bounded to the new 100/1,000-character limits, and the newest 1,000 unique
pages are retained. Schedules without a host thread configuration are
discarded; the newest 100 configured definitions are retained. They snapshot
their thread configuration, migrate as deleted when they were disabled or their
App was archived, and make their former target visible in the stored message.
Per-App instructions and old side-system history are intentionally discarded.

## Upgrade from the generic app platform

Bootstrap idempotently reassigns the retained `kern-app-0` and `kern-app-6`
database objects to `kern-admin`, removes obsolete generic-app schemas and
roles, and creates the fixed runtime grants. The migration bootstrap adopts
already-applied Chat and Web App ledger rows into their renumbered entries in
the host ledger before running anything pending. This supports both fresh
hosts and partially applied pre-1.6 app histories without replaying SQL. Once
both histories are complete, migration 0026 atomically moves their tables into
`public`, renames Chat's generic `threads` table to `chat_threads`, and drops
both legacy schemas and their ledger.

Migration `0014_direct_workspace_thread_ids.sql` changes
`agent_chat__thread-N` to `thread-N` and
`personal_web_app_builder__app-N` to `app-N` in sessions and events. It first
rejects collisions across both tables. Saved provider-session pointers for
the migrated threads are deliberately preserved: changing Kern's durable
thread key does not invalidate an opaque provider session, and retaining it
avoids an unnecessary context handoff and cache loss. Agents discover the
current host-global Workspace contract during subsequent work.

## Generated Web App code

Generated HTML and CSS are sanitized into a nested ShadowRoot. Generated
JavaScript runs in a Worker created inside a hidden opaque-origin sandboxed
iframe whose CSP denies network access and dynamic evaluation. A trusted
broker exposes only bounded render, data, notification, and ask-agent
capabilities. Workspace and revision checks fence late responses, and
switching or archiving a workspace terminates its Worker.
