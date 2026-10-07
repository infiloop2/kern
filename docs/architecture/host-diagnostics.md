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

Browser post preparation failures attach structural snapshots labeled
`snapshot_phase=after_failure`. Target diagnostics count approved-ID links in
the page, tweet articles and timestamp permalinks, including query-string and
extra-path variants. Reply diagnostics record viewport intersection, bounded
geometry and the center hit's tag, role and known test ID. Page diagnostics
count visible dialogs and inline/popup editors and identify visible username
inputs or the account-access route. Link scans cap at 2,000 and page-state scans
at 200 per element category, with explicit truncation flags. Unknown hit
attributes become `other`; no page text, raw HTML or links are returned. These
scripts have a 500 ms execution deadline per scan. Returned fields must pass
a Python allowlist of bounded integers, booleans and fixed labels, since page
scripts can override JavaScript built-ins. Failed or invalid snapshots set
`snapshot_incomplete` and preserve previously collected facts. These
read-only snapshots neither change the exact target selector nor click, scroll
or retry, and cannot establish what blocked an action throughout its timeout.

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

This feed is an operator boundary, not an agent-safe export. Byte and scalar
type limits do not redact secrets from exception summaries, context strings,
provider bodies or source-code traceback lines. There is no agent diagnostics
API. Any future agent view must independently select typed safe fields for
both lists and details, including historical rows, instead of forwarding the
operator response or relying on truncation as redaction.

This is a curated diagnostic view, not a replacement for the system journal.
Ordinary validation failures, user denials, and successful operational events
remain in their existing product and audit surfaces.

Dictation reports slow requests/inference and contained failures through this
same pipeline, with bounded durations, process CPU time and random request IDs.
It emits no speech, transcripts or library exception messages. See
[dictation diagnostics](../development/dictation.md#host-diagnostics) for timing
boundaries and per-outcome rate limits.

Browser session warnings and dispatch errors include best-effort resource
counters for the entire Browser service (including Chromium and Playwright):
`browser_memory_bytes`, `browser_swap_bytes`, `browser_tasks`, and
`browser_cpu_usage_usec`. Tasks include threads. CPU is cumulative service CPU
time in microseconds, not a current utilization percentage; it resets when the
service cgroup is recreated. Unavailable counters are omitted, not recorded as
zero. These snapshots read fixed cgroup files only, without process arguments,
page content, or authentication state.

Claude's `/usage` authentication check allows 60 seconds. A timeout emits a
`claude_code.usage_probe` error with its timeout and elapsed seconds and a
shared host resource snapshot: one-minute load, memory available/total, swap
used, CPU/memory/I/O pressure over the last ten seconds, and the same cumulative
CPU, memory, swap and task counters for agents and each Kern service. The
service prefixes are `agents`, `browser`, `workspace`, `embedding`,
`transcription`, `admin`, `proxy`, `tools`, `postgres`, `inference`,
`agent_network`, `diagnostics`, and `tunnel`. Agent totals include all thread
scopes; the other groups are individual services, so they do not overlap.

CPU totals from before the check are included with a `_before` suffix when
available. Compare them with the failure snapshot only if the service did not
restart. CLI output is excluded. The timeout still follows the existing runtime
error path; it does not relax authentication checks or retry a failed agent turn.
