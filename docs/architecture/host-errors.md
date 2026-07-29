# Host Error Diagnostics

Kern records unexpected host-service failures for operator debugging in the
read-only **Host errors** panel. This is a bug log, not a second copy of normal
product state: thread failures, provider errors, denied network requests, tool
failures, validation errors, and other expected operational outcomes remain in
their existing session and audit logs.

## Capture and storage

Host services report an exception only after their typed, expected error paths
have been excluded. The reporter sends one structured record to journald with
`KERN_HOST_ERROR=1`; systemd supplies the trusted originating unit, PID, boot
id, and timestamp. Every managed service also has an `ExecStopPost` hook that
emits an error when systemd reports an abnormal service result.

`kern-host-errors.service` asks journald for tagged records originating from
only the fixed set of Kern units and installed app units, then checks the
trusted `_SYSTEMD_UNIT` field again before any database work. It validates and
bounds every field before writing to `host_errors` in PostgreSQL. The record
contains a component, kind, exception type, short summary, traceback frames, a
small callsite-selected context object, host version, and a stable fingerprint.
The reporter does not inspect or serialize locals, environment variables,
request headers or bodies, or arbitrary object representations.

The collector follows new matching journal records without a durable replay
cursor or a second spool. If the collector or PostgreSQL is unavailable, some
diagnostics during that window may be missed; after recovery the collector
simply follows new records again. This keeps host diagnostics best-effort and
isolated from product correctness.

Repeated errors with the same service and fingerprint coalesce for 60 seconds,
updating `last_seen_at` and `occurrence_count` and rotating a separate ordering
sequence so the stable row id and any open detail link remain valid while the
row returns to newest-first position. PostgreSQL retains the newest 10,000
rows.

## Operator surface

The authenticated admin API exposes newest-first cursor pages and a separate
detail read:

```text
GET /v1/host-errors?before=&limit=&service=
GET /v1/host-errors/{id}
```

List pages omit traceback, context, and fingerprint; the UI loads those fields
only when an operator expands a row. The panel has no resolve, dismiss, delete,
or report action. A future report flow can build on the stored diagnostics
without changing capture or retention.

For failures that prevent either journald or the host from running, operators
must still use lower-level system and cloud diagnostics. This feature is not a
replacement for the system journal; it is a curated view of unexpected Kern
failures.
