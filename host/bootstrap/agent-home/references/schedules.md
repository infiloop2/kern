# Schedules Workspace reference

Read this file before listing, creating, editing, deleting, or diagnosing
schedules. Schedules are shared by every thread. Each firing starts an
independent host thread; it never resumes the thread that created the schedule.

- `GET /agent/schedules?limit=40&before=...` lists active schedules.
- `GET /agent/schedules/recent-failures?limit=40&before=...` lists retained
  failed runs for active schedules, newest first. Each summary includes the
  schedule name, thread id, runtime selection, timestamps, and bounded error;
  prompts and deleted schedules are excluded.
- `GET /agent/schedules/session-options` lists currently valid runtime, model,
  and effort combinations.
- `GET /agent/schedules/{id}` fetches one schedule.
- `POST /agent/schedules` creates a schedule with `name`, `message`, `cadence`,
  `agent_runtime`, `model`, and `effort`. Interval schedules also require
  `interval_minutes` (5–10,080); daily schedules require `daily_time` as
  `HH:MM` UTC.
- `PUT /agent/schedules/{id}` replaces the whole definition and requires all
  create fields plus `expected_revision`. Fetch it first and preserve fields
  that are not changing.
- `DELETE /agent/schedules/{id}?expected_revision=N` stops future occurrences;
  DELETE takes no body.

Schedule messages may contain up to 12,000 characters. Edits affect future
runs only. Kern never overlaps two runs of one schedule or silently substitutes
a runtime, model, or effort; an unavailable setting produces a visible failed
run.

## Script schedules

The `script` runtime (`model` `bash`, `effort` `fixed`) runs a static bash
script instead of a model turn. Use it for recurring work that needs no
judgement, such as a backup, sync, or health check; use a model runtime when
judgement is required.

For a script schedule, `message` is the script's absolute path rather than a
prompt: an existing `.sh` file under `/mnt/kern-agent/agent-home`, spelled with
letters, numbers, `.`, `_`, `-`, and `/` only. Write the script first and make
it work when run directly before scheduling it.

Each firing executes the file as it exists at that moment with `bash`, from
agent home, under the same network policy and 15-minute budget as other runs.
Combined output becomes the run's message, and a non-zero exit fails the run
visibly; nothing is retried. Editing the file changes the next run without
editing the schedule.
