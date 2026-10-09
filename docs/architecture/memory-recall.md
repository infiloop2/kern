# Memory recall

## Shared conversation context

Turn-start recall and mid-turn reevaluation use the same deterministic context
builder and Workspace recall path. The newest operator messages receive a
1,000 UTF-8 byte bucket; supporting context receives a separate 500-byte bucket.
Both buckets include role labels and prioritize newest messages. The combined
query cap is 1,500 bytes including separators. Each message is flattened to
one paragraph, preserving wording and negation. Bytes bound payload size, not
model tokens: the local embedding tokenizer still applies its 512-token ceiling.

History reads select two independent windows of at most 12 events: operator
messages, and assistant messages plus allowed input notices. Each window includes
memory-clear events, and the builder never crosses a clear. This keeps assistant
volume from evicting the operator history. Admission appends the incoming message
with its authenticated notice metadata before building the query; mid-turn checks
read it from retained events. No full transcript, tool activity or model-generated
summary is needed.

Only three `thread.notice` kinds contribute:

- `agent_message`: message text without its routing preamble, labeled `Peer`.
- `scheduled_trigger`: message text without its preamble, labeled `Scheduled request`.
- `approval_outcome`: only the short action/outcome summary, labeled `Approval outcome`.

These share the supporting bucket with `Assistant` messages. All notice details,
request/result JSON, logs, action receipts, memory injection/suggestions, history
transfers, restart notices and generic automated notices are excluded. Explicit
operator provenance wins over pasted preambles; pre-metadata user-channel history
remains as recorded, without guessing provenance from text. User messages own task direction;
supporting text cannot override them. Without user messages, the supplied request
provides task context. New notice kinds are excluded until explicitly selected.

## Mid-turn suggestions

Initial recall stays synchronous at admission. Steerable model turns seed a small
admin-process cache with the initial query and known page IDs/revisions.
Event hooks count completed operations, assistant messages and selected incoming messages;
they do no search, inference or extra database I/O. Operation text and logs never
enter the query. Reasoning, streaming updates and the monitor's own notices are
ignored. Message content remains in the
existing event history rather than a second activity buffer.

Five events make a turn eligible; incoming operator/allowed notice messages make
it immediately eligible. One serial worker selects the highest count (capped at 20),
with longest-waiting ties. Checks start at least ten seconds apart globally. Counts
are consumed before the check, so messages arriving during it count toward the next
check. The cache holds at most 128 active turns and 64 known pages per turn. Excess
monitoring can be dropped; actual messages are never dropped. There is no durable
queue, replay or automatic retry.

The worker reads the shared conversation context. An empty or unchanged query skips
search and titles. A changed query goes directly to Workspace recall: local hybrid
search and the same optional Jev/Luna selection with existing fallbacks. Up to three new page revisions are suggested as IDs and purposes,
never bodies, through same-run live steering. Central `agent_messages` helpers build
the message and `thread.notice` payload. Only accepted suggestions enter the dedupe
map; this records delivery, not reading.

Only on-demand agents also use Luna to generate a task title from this same context
and the same prompt as initial title generation. Apps, Standing and Spawned agents
do not make that call. Titles describe the overall work, not individual tool steps.
Memory suggestions work when OpenAI is disabled. Initial titles fill only an empty
title so a late initial result cannot replace a newer mid-turn title.

Incoming direction does not discard in-flight memory suggestions: the agent decides
whether these optional recommendations still apply. Delivery checks the active run
under its delivery lock. Title saving additionally checks the incoming generation
so a redirection cannot be overwritten by an outdated title. Finished
turns remove their cache and discard late results; the worker never wakes idle agents.
Codex may accept a suggestion while generating its final answer but consume it
afterward. The adapter tracks host-sent suggestions and preserves that answer if
the only follow-up is an empty final response to the optional context. Actual
incoming requests or subsequent work still invalidate the previous answer.
If ordinary input duplicates a suggestion's exact text, that text is treated as
ordinary input for the rest of the turn; the adapter does not guess delivery order.
Failures consume a batch; new messages can trigger later work. Checks, capacity skips,
failures and accepted suggestions have cache counters; provider usage stays in Host AI.
This is optional discovery: it can miss short phases, be skipped under load, or be
ignored by the receiving agent.

## Retrieval

Workspace embeds the complete query for semantic search. In parallel as a
retrieval channel, PostgreSQL parses that same text with its built-in `english`
configuration: stopwords are removed and related word forms share stems. Recall
combines the resulting lexemes as OR alternatives. There is no Python stopword
list, acronym exception, or phrase classifier. An empty keyword result never
suppresses semantic search. If embeddings are unavailable, lexical results
remain usable.

Memory keyword searches all use English normalization, including explicit
search and its weak fallback. Explicit search retains its Boolean/phrase syntax;
recall and weak fallback use alternatives. This deliberately changes matching
for English word forms and removes capitalization exceptions for words such as
IT and OR. Exact page-id/description matching remains a separate retrieval
channel. Conversation-history search is unchanged.

The existing weighted reciprocal-rank fusion combines exact, keyword, and
semantic results. Recall excludes graph-only suggestions, takes up to 20
candidates, and retains hybrid order without additional keyword boosting or a
forced-best override. Candidate channels still have their existing internal
limits (50 exact, 200 keyword, 200 semantic). Up to six relevant pages are
loaded, separately from self-memory. Admission carries all six plus self-memory;
mid-turn suggestions retain their existing limit of three.

## Optional Jev and Luna reranking

Provider configuration and credentials stay inside the Host AI inference
service. Workspace has no read grant on the provider configuration table.
Every recall with candidates requests TypeSafe Jev and Luna Decisions rankings
on independent copies of the same original hybrid candidates. The inference
service uses the existing provider configuration; disabled or unconfigured
providers make no external call. There is no random assignment or control bucket.

When both rankings have fully validated `outcome: "success"`, recall takes each
provider's top three and deduplicates by immutable page ID. Shared pages come
first in Jev rank order. The remaining picks alternate Jev then Luna, preserving
each provider's order. For example, Jev `[A,B,C]` and Luna `[B,D,E]` select
`[B,A,D,C,E]`. Identical lists select three pages; disjoint lists select six.
There is no refill after deduplication. With fewer candidates, recall returns
what exists. If only one provider succeeds, its top five are selected. If neither
succeeds, the original hybrid top five are selected. Self-memory stays separate.
Revision changes and missing/deleted pages keep their existing loading behavior;
filtering cannot pull pages from outside this chosen set to fill empty slots.

Jev uses `jev-latest` with one typed relevance question per candidate. It sees
the bounded query and descriptions under short local candidate ids; page
contents and page ids are not sent. Role labels distinguish operator direction from supporting messages. Each role
bucket is newest first; prior messages resolve references without overriding the latest user direction. Existing provider egress redaction
applies. Luna receives the same query and candidate descriptions as text input,
with one named `predicate` question per candidate on `gpt-6-luna`. The relevance
criteria and short local IDs match Jev's. Page contents and page IDs are never
sent to either provider. See [Host AI inference](host-ai-inference.md) for the service boundary.

Only a complete set of finite scores between zero and one changes the order.
Ties preserve hybrid order. Disabled providers, transport failures, timeouts,
refusals, and malformed or incomplete results are unusable rankings. The other provider selects its five when successful; otherwise recall retains the hybrid five.
There are no extra provider calls or retries. Recall explicitly passes `timeout_seconds: 1.2` to both inference routes;
the inference service binds that timeout to the shared HTTP transport and its socket client adds
100 ms for local dispatch and response overhead (1.3 seconds for each call).
The calls run concurrently, and recall waits for both before returning diagnostics.
Admission allows three seconds
for the full recall, including local search and page loading. These are socket
timeouts, not a guarantee of identical elapsed latency. Luna task-title
creation remains asynchronous and explicitly passes a 20-second timeout;
it does not block agent launch. The inference API has no feature-purpose field;
callers supply OpenAI model settings, and TypeSafe uses Jev.

The socket allowance is fixed; there is no deadline accounting or per-feature
timeout logic in the inference service. Jev shares the inference service's
existing four-call capacity with other applied features. Luna Decisions uses
one separate slot, so Luna ranking cannot consume Jev's capacity.
Additional overlapping Luna calls are rejected as busy and logged without a
usable Luna ranking. Busy Jev calls use Luna when successful, otherwise hybrid order. There are no queues or retries.

## Diagnostics

The existing `memory_recall_details` records the query, retrieval evidence,
and actual injected page ids/revisions. One `Rerank:` JSON entry (version 7)
records the selection strategy (`top_three_union`, `jev`, `luna`, or `hybrid`)
and chosen page IDs in applied order. The separate `Selected` entries record
what was actually loaded after revision checks.

Its `providers.jev` and `providers.luna` entries include provider,
requested/returned model, outcome, elapsed milliseconds, the 1.2-second timeout,
and whether the provider retained hybrid fallback order. Luna also records API
`decisions`. `applied` means a fully validated success contributed to selection.
Candidate identity and retrieval evidence appear once: local id, page id,
revision, original hybrid rank/score, and each provider's `score` and proposed
`rank`. These ranks record the complete individual proposals; the selection
rule uses their first three or five. Failed/partial scores remain null and are
never applied. Stable ties keep hybrid order. No duplicate top-five ID lists
are needed.

Disabled, unconfigured and missing provider rows share the existing
`provider_disabled` outcome, make no outbound calls, and log no warnings.
Workspace-rejected scores record `invalid_response`, Luna refusals record
`refusal`, and supported timeout reasons record `timeout` with a safe
`error_type`. The concrete adapters reject malformed wire responses and log
safe failure categories in existing Host diagnostics. Their socket contract
exposes only no usable result, so recall records `provider_unavailable` for
those adapter failures; it does not invent a more specific reason. Private
response/error text and provider credentials are excluded from the trace.

With no candidates, the coordinator records `Rerank skipped: no candidates.`
and returns before calling either ranking function or starting a worker.
Consolidating candidate metadata preserves both providers and the selected set
within the existing 12,000-character admission and 14,000-JSON-byte history
projection caps. The bounded-trace test covers all 20 candidates with 64-character
IDs, maximum query bytes with JSON escaping, large revisions, 128-character
response models, and six relevant pages plus self-memory. Oversized diagnostics
still use the existing caps. Event storage keeps its existing 131,072-character text cap.

The trace stays in operator diagnostics and is excluded from agent memory
injection. OpenAI usage and estimated spend appear in the existing Host AI
history. Historical `Rerank shadow:` entries remain records of the earlier
Luna comparison that was not applied.

These traces support inspection of ranking, latency, and failure rates,
including unavailable and timed-out calls. Historical `Rerank experiment:`
entries retain their original randomized assignments. Relevance quality still
needs judgments against the task. Appending history can introduce unrelated
topics, particularly without reranking. Regression tests establish behavior,
not a measured claim of improved retrieval quality.
