# Schedules Workspace reference

Read this file before listing, creating, editing, deleting, or diagnosing
schedules. Schedules are shared by every thread.

Every schedule owns one stable `schedule-N` thread and one recurring automated
message. Each firing sends `This is an automated trigger.` followed by the
saved message through the ordinary thread-message path. It steers an active
turn when the runtime supports steering.

Each firing makes one delivery attempt and advances the cadence immediately.
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
  newest first. Summaries omit the saved `message`; fetch the detail when
  editing it.
- `GET /agent/schedules/session-options` lists currently valid runtime, model,
  and effort combinations.
- `GET /agent/schedules/{id}` fetches one active schedule, including its saved
  message and current revision. Deleted schedules return 404 to agents.
- `POST /agent/schedules` creates a schedule with `name`, `message`, `cadence`,
  `agent_runtime`, `model`, and `effort`. Interval schedules also require
  `interval_minutes` (5–10,080); daily schedules require `daily_time` as
  `HH:MM` UTC.
- `PUT /agent/schedules/{id}` replaces the whole definition and requires all
  create fields plus `expected_revision`. Fetch it first and preserve fields
  that are not changing. Runtime changes keep the same stable schedule thread.
- `DELETE /agent/schedules/{id}?expected_revision=N` stops future occurrences;
  DELETE takes no body. A firing already claimed by the scheduler may still be
  delivered once.

These are the complete agent-facing schedule routes. Revision history and
restoration belong to the operator UI. There are no per-run or
recent-failure routes.

Schedule messages may contain up to 12,000 characters. Kern never silently
substitutes a runtime, model, or effort. Deleting a schedule removes only its
cadence and saved automated message. Its persistent thread remains retained
but hidden; it does not move into Chat, and restoring the schedule reveals it
under Scheduled agents again during the 90-day restoration window. After the
definition is pruned, any remaining host thread data follows ordinary host
retention and stays absent from both navigation indexes.

## Script runtime

The `script` runtime (`model` `bash`, `effort` `fixed`) runs a static Bash
script instead of a model turn. Use it for recurring work that needs no
judgement, such as a backup, sync, or health check; use a model runtime when
judgement is required.

For a script schedule, `message` is the script's absolute path rather than a
prompt: an existing `.sh` file under `/mnt/kern-agent/agent-home`, spelled with
letters, numbers, `.`, `_`, `-`, and `/` only. Write the script first and make
it work when run directly before scheduling it.

Each firing executes the file as it exists at that moment with `bash`, from
agent home, under the same network policy and a fixed 15-minute budget.
Combined output becomes an ordinary agent message in the persistent schedule
thread. A non-zero exit, timeout, or launch failure becomes an ordinary
`thread.error`; nothing is retried and no run/status row is created. Editing
the file changes the next firing without editing the schedule.
