# Standing agents and triggers

Read before creating, changing, deleting or diagnosing Standing agents.
Listing, detail reads and session-option discovery use the host guide directly.
Only the owning `schedule-N` agent may update/delete its definition; creators
request subsequent changes from that owner.

## Read, create and change

- `GET /agent/schedules?limit=40&before=...`: active summaries, newest first,
  without trigger prompts. Fetch detail before editing.
- `GET /agent/schedules/session-options`: supported runtime/model/effort choices.
- `GET /agent/schedules/{id}`: full triggers and revision; deleted agents
  return 404.
- `POST /agent/schedules`: create with `name`, `triggers`, `agent_runtime`,
  `model` and `effort`, plus optional `purpose`. Use supported settings.
  All triggers share this configuration and one thread.
- `PUT /agent/schedules/{id}`: replace the definition with all create fields
  plus `expected_revision`. Fetch current detail and preserve unmentioned
  settings/triggers. One revision covers all triggers; there are no individual
  trigger update/delete/pause routes. Runtime changes retain the thread.
- `DELETE /agent/schedules/{id}?expected_revision=N`: no body; stops future
  deliveries. A firing already claimed may still be delivered once.

`triggers` holds 0–5 entries:

- Daily: `{"type":"daily","times":["08:00","20:00"],"prompt":"Research"}`.
  Use 1–24 distinct `HH:MM` UTC times; the prompt runs at each time daily.
- Weekly: `{"type":"weekly","days":["mon","wed","fri"],"time":"16:00","prompt":"Review"}`.
  Use 1–7 distinct days from `mon,tue,wed,thu,fri,sat,sun`. Use separate entries
  for different weekday/time/prompt combinations.

There is no timezone or interval field. Each prompt is nonblank and at most
12,000 characters; the complete request must fit the Workspace request limit.
An empty trigger list stops automatic messages while leaving the agent available.

`purpose` is optional, one line and at most 100 characters. It appears in list
and detail responses; omit on update to preserve, or pass an empty string to
clear. Purpose is retained in definition history/restoration.

## Delivery and diagnosis

Each trigger delivers its prompt to the persistent `schedule-N` thread,
steering an active turn when supported. Settings changes affect the next turn;
a running turn and its follow-ups keep their current configuration. Model
threads retain conversation, working context and self-memory, and can receive
operator or peer messages without changing triggers.

Each due trigger gets one delivery attempt, even when triggers coincide. The
host advances its calendar before attempting delivery. Only the current UTC
minute is eligible; missed minutes have no catch-up or retry queue. Edits affect
future deliveries. Before acceptance, failures are operational logs, without a
thread event; after acceptance, provider/script failures use `thread.error`.
There is no separate
run/status or recent-failure API.

These are the complete agent-facing schedule routes. Agent DELETE and operator
archiving both stop triggers and hide the agent; detail reads return 404.
The operator can restore it. Revision history/restoration have no agent routes.

## Bash jobs

Use `agent_runtime="script"`, `model="bash"`, `effort="fixed"` for recurring
work needing no judgement. Each trigger's `prompt` is the absolute path of an
existing `.sh` file under `/mnt/kern-agent/agent-home`, using only letters,
numbers, `.`, `_`, `-` and `/`. Write and run the script successfully before
scheduling it.

Each firing runs the file as it then exists with `bash`, from agent home, under
the normal network policy and a fixed 15-minute limit. Editing the file changes
the next firing without changing the schedule. Combined output is an agent
message; nonzero exit, timeout or launch failure is `thread.error`, with no retry.
The persistent transcript is read-only: no operator/peer messaging or self-memory.

Before initiating work with the owner, read [agent-messaging.md](agent-messaging.md).
