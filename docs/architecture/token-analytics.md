# Token analytics

Analytics appears below Memory in the operator sidebar. It shows today and
the previous six UTC dates, with daily usage by Chats, Apps, and Schedules,
a 24-hour UTC profile combined across those seven dates, provider/model
summaries, and threads ranked by known token consumption. Selecting a thread
filters both charts; active threads also link to their conversation. Data starts with new turns after deployment. There is no backfill,
provider-quota polling, attribution estimate, or token-to-dollar conversion.

## Accounting unit

One `turn_usage` row belongs to one orchestrator `(thread_id, run_number)`.
Steering adds work to that same execution and row. A schedule firing is not
necessarily a new turn. Script executions have no model accounting row.

The row is created at admission with unknown counters. The existing provider
message callback accepts an internal `token_usage` record and updates its
turn's totals as measurements arrive. Stop/failure can still drain a final
usage record into that same row. An abrupt kill can lose unreported usage;
analytics does not retry or block the agent on a measurement-write failure.
Failures go to host diagnostics. There is no new run state machine.

Measurements are deduplicated by provider response/prompt ID within the live
execution; a corrected measurement replaces that response's earlier values.
Only turn totals are stored, not every response or tool activity. No attempt
is made to price individual shell commands or attribute outside-account usage.

## Four non-overlapping buckets

- `input_tokens`: input processed without a cache read or write.
- `cached_input_tokens`: input read from cache.
- `cache_write_tokens`: input written into cache.
- `output_tokens`: generated tokens, including reasoning where the provider
  includes it. Reasoning is not added again.

Codex and Grok input totals include cache reads/writes, so the adapter
subtracts those to obtain plain input. Claude and Hermes already split the
buckets. Unknown fields remain null. If any response lacks a bucket, that
turn's bucket remains unknown; the UI sums known turns and marks partial
coverage with an asterisk. A zero is a reported zero, not missing telemetry.
Codex's protocol explicitly defaults omitted cache-write counts to zero.

## Provider sources

- Codex (all three accounts): `thread/tokenUsage/updated`, scoped to the current
  provider thread and turn. Use `last`, not the resumed session's lifetime
  total. The cumulative total fingerprints repeated notifications. This is
  response accounting from the protocol, not context-window occupancy.
- Claude Code: `assistant.message.usage`, keyed by `message.id`. Repeated
  content blocks for the same response must not multiply its token usage.
- Grok (both runtimes): the `session/prompt` response's `_meta`, preferring the
  nested `usage` object over the flat fields repeated beside it, keyed by
  `promptId`. Grok announces a turn's counts in no session update, so a killed
  prompt that never returns a response remains unmeasured.
- Hermes: `post_api_request` hook's normalized `response.usage`, keyed by
  `api_request_id`, carried through the existing nonce-framed wrapper output.
  The host validates its numeric fields before counting them.

The row records the selected runtime/model, not a reconstruction of any
provider-internal model routing or delegated work. Child work is included only
to the extent the runtime reports it in that stream.

## Time and retention

A turn's total is assigned to its latest measurement timestamp. A long-running
turn can move between day or hour buckets while it runs; this is deliberately
approximate. The page refreshes on entry and with Refresh. SQL aggregates the
seven-day window without loading conversation contents. Known partial totals
are visible alongside measured-turn counts in the response and UI tooltips.

Retain usage rows for 90 days under the normal retention loop. Recent usage
survives deletion of its originating conversation; missing names appear as
Deleted thread. The report derives `active` from current source records: Chats
and Apps must not be archived, and schedules must exist and not be deleted.
A schedule stays inactive after its definition is pruned. All recorded usage
and threads remain in the seven-day view. Inactive rows have no conversation
link and show Archived for Chats/Apps or Inactive for schedules; their usage
can still be inspected. The seven-day UI range is fixed and is not a retention limit.

## Lifetime totals on Home

Home shows Input, Cached input, Cache write, and Output below the existing
thread/message/activity stats. These combine all providers, accounts, and
workspace kinds since tracking began, with no historical backfill. Four named
rows in the existing `counters` table start at zero. Each turn measurement
updates them by the difference from its previous stored totals, in the same
transaction as the turn row. Repeated measurements do not add usage twice;
corrections can lower totals. Unknown turn buckets contribute no known tokens,
consistent with Analytics. These are reported consumption, not complete coverage
or billable totals. The lifetime counters survive the 90-day turn-row pruning.

Telemetry uses each runtime's existing reporting channel. In particular, the
Hermes stdout marker separates hook output from ordinary output; it is not an
authentication boundary against an agent intentionally forging host telemetry.
No new trusted execution channel is introduced for this approximate report.
Home formats large totals as JavaScript numbers; beyond the safe-integer range,
the least significant digits can round, but the total must not display as zero.
