# Standing agents and triggers

Read this file before listing, creating, editing, deleting, or diagnosing
standing agents and triggers. All agents may discover/read them; only the owning
standing agent can update or delete its own definition through agent tools.
Agents can create standing agents. The operator can create, edit, archive and
restore them.
Other agents request changes through `send_agent_message`.

Each standing agent owns one stable `schedule-N` thread, independently of its
zero or more triggers. Each trigger sends its saved prompt to that thread,
steering an active turn when the runtime supports it.

All times are fixed UTC calendar times. Each due trigger makes one independent
delivery attempt, including when several triggers coincide. The host advances
to the next future calendar time before attempting delivery. Only the current
UTC minute is eligible; earlier missed minutes are skipped without catch-up.
There is no retry queue, separate run record, success status, or recent-failure
API. A failure before the host accepts the message is logged operationally and
does not create a thread event. Once accepted, ordinary provider and script
failures use the normal `thread.error` path. Edits affect future deliveries
only.

For model runtimes, the persistent thread keeps its conversation, working
context, and self-memory. The operator can message it normally at any time,
and the UI presents the saved human name instead of its internal identity. The
script runtime uses the same delivery path but runs the saved file under a
fixed time limit. Its persistent Chat is a read-only transcript: the operator
can inspect activity, output, and errors, but its transcript exposes neither
manual messaging nor self-memory controls.

## Agent-facing API

- `GET /agent/schedules?limit=40&before=...` lists active schedule summaries,
  newest first. Summaries omit trigger `prompt` values; fetch the detail before editing.
- `GET /agent/schedules/session-options` lists currently valid runtime, model,
  and effort combinations.
- `GET /agent/schedules/{id}` fetches one active schedule, including its full
  triggers and current revision. Deleted schedules return 404 to agents.
- `POST /agent/schedules` creates a standing agent with `name`, `purpose`,
  `triggers`, `agent_runtime`, `model`, and `effort`. The new agent owns its
  definition; its creator requests subsequent changes by messaging its
  `schedule-N` identity. A standing agent updates its own definition using PUT.
  Runtime/model/effort are shared by all triggers.
  `triggers` is an array of 0–5 entries:
  - Daily: `{"type":"daily","times":["08:00","20:00"],"prompt":"Research"}`.
    Use 1–24 distinct `HH:MM` UTC times. The prompt runs at each time every day.
  - Weekly: `{"type":"weekly","days":["mon","wed","fri"],"time":"16:00","prompt":"Review"}`.
    Use 1–7 distinct days from `mon,tue,wed,thu,fri,sat,sun` and one `HH:MM` UTC
    time. Use separate triggers for different weekday/time/prompt combinations.
  There is no timezone or interval field. An empty list stops automatic
  messages while leaving the agent and conversation available.
- `PUT /agent/schedules/{id}` replaces the whole definition and requires all
  create fields plus `expected_revision`. Fetch it first and preserve fields
  that are not changing. All triggers share one revision; there are no
  per-trigger update/delete/pause routes. Runtime changes keep the same thread.
- `DELETE /agent/schedules/{id}?expected_revision=N` stops future occurrences;
  DELETE takes no body. A firing already claimed by the scheduler may still be
  delivered once.

These are the complete agent-facing schedule routes. Revision history and
restoration belong to the operator UI. There are no per-run or
recent-failure routes.

Each trigger prompt must be nonblank and may contain up to 12,000 characters.
The complete request must also fit the Workspace request size limit. Kern never silently
substitutes a runtime, model, or effort. Archiving a standing agent stops its
triggers. Its persistent thread remains retained
but hidden; it does not move into Chat, and restoring the schedule reveals it
under Standing agents again during the 90-day restoration window. After the
definition is pruned, any remaining host thread data follows ordinary host
retention and stays absent from both navigation indexes.

## Script runtime

The `script` runtime (`model` `bash`, `effort` `fixed`) runs a static Bash
script instead of a model turn. Use it for recurring work that needs no
judgement, such as a backup, sync, or health check; use a model runtime when
judgement is required.

For a script schedule, each trigger's `prompt` is the script's absolute path rather than a
prompt: an existing `.sh` file under `/mnt/kern-agent/agent-home`, spelled with
letters, numbers, `.`, `_`, `-`, and `/` only. Write the script first and make
it work when run directly before scheduling it.

Each firing executes the file as it exists at that moment with `bash`, from
agent home, under the same network policy and a fixed 15-minute budget.
Combined output becomes an ordinary agent message in the persistent schedule
thread. A non-zero exit, timeout, or launch failure becomes an ordinary
`thread.error`; nothing is retried and no run/status row is created. Editing
the file changes the next firing without editing the schedule.

## Discovery purpose

Schedules have an optional, single-line `purpose` of at most 100 characters,
returned in list and detail responses. Include it in create/update requests
to describe the ongoing work in one sentence. Omitting it on update preserves
its value; an empty string clears it. Purpose is included in schedule history
and restored with the definition. Model schedules can receive messages through
`send_agent_message` without changing their triggers; Bash schedules cannot.

For cross-thread requests and replies, see [agent messaging](agent-messaging.md).
