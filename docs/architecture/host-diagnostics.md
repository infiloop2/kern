# Host Diagnostics

Kern records service errors and contained warnings for operator debugging in
the read-only **Host diagnostics** panel. Errors are reserved for service-level
exceptions and abnormal service exits. Warnings capture strange behavior that
the service contained, including provider and tool failures that deserve more
context than the normal product result carries.

## Capture and storage

Host services call `report_unexpected` for service-level exceptions and
`report_warning` for contained anomalies. Both emit one structured journald
record tagged `KERN_HOST_DIAGNOSTIC=1`; systemd supplies the trusted originating
unit, PID, boot id, and timestamp. Every managed service also has an
`ExecStopPost` hook that emits an error when systemd reports an abnormal result.

`kern-host-errors.service` follows tagged records from the fixed set of
Kern service units, validates every field, and writes them to
`host_diagnostics` in PostgreSQL. Records contain severity (`error` or
`warning`), component, kind, exception type, summary, optional traceback,
explicit context, host version, and fingerprint.

The reporter does not inspect locals, environment variables, or request
headers. Most provider failures retain only provider, operation, and status.
A tool integration may deliberately attach a bounded response body to a mapped
warning when it is useful to the authenticated operator; that response never
enters the agent-facing result. X write failures use this path.

Every variable diagnostic field is bounded before journald ingestion and again
by storage constraints where applicable: summaries are at most 2 KiB,
tracebacks 32 KiB, and context 4 KiB. This keeps a single unusual exception
from creating an exceptionally large row.

The collector follows only new records and has no replay spool. Repeats with
the same service and fingerprint coalesce for 60 seconds. One shared retention
cap keeps the newest 10,000 error and warning rows.

## Operator surface

The authenticated admin API exposes newest-first cursor pages and lazy detail
reads:

```text
GET /v1/host-diagnostics?before=&limit=&service=&severity=
GET /v1/host-diagnostics/{id}
```

`severity` accepts `error` or `warning`. List pages omit traceback, context,
and fingerprint; the UI loads those fields only when the operator expands a
row. The panel is display-only.

This is a curated diagnostic view, not a replacement for the system journal.
Ordinary validation failures, user denials, and successful operational events
remain in their existing product and audit surfaces.
