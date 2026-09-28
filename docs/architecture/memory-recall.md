# Memory recall

At turn admission, Kern builds one deterministic query from the current message
followed by recent user messages, newest first, from at most 12
message/memory-clear events. Each message is flattened to one paragraph after
scheduled-trigger and peer routing envelopes are removed. Assistant answers are
excluded, and a working-memory clear stops history lookup. The query is capped
at 1,000 UTF-8 bytes, with the current message taking priority. This is a byte
budget, not a guarantee of 400 model tokens; the local embedding tokenizer
still applies its own ceiling. Query building uses no GPT call, persistent
summary, or phrase-based continuation classifier.

Admission builds this text once and also passes it unchanged to Luna for the
Swarm task title. The title prompt treats the first paragraph as authoritative
and earlier user messages as context. Recalled memory contents, assistant
answers, and the prepared agent handoff are not part of the title input.

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
limits (50 exact, 200 keyword, 200 semantic). Up to five relevant pages are
loaded, separately from self-memory.

## Optional reranking experiment

Provider configuration and credentials stay inside the Host AI inference
service. Workspace has no read grant on the provider configuration table.
Every recall with candidates independently chooses among
no reranking, OpenAI, and TypeSafe with probability 1/3 each, regardless of
provider enablement. The control group keeps hybrid order and makes no provider
call. If the assigned provider is disabled, that bucket also keeps hybrid
order without an external provider call. The inference service reports
`provider_disabled` over its existing socket route. Workspace never switches
to another bucket or provider.
This is random assignment per recall, not a guarantee of an exactly even count.

OpenAI uses `gpt-6-luna` with reasoning disabled and a strict JSON score map.
Jev uses `jev-latest` with one typed relevance question per candidate. Both see
the same bounded query and descriptions under short local candidate ids; page
contents and page ids are not sent. The first paragraph is the current request;
prior paragraphs only resolve references. Existing provider egress redaction
applies. See [Host AI inference](host-ai-inference.md) for the service boundary.

Only a complete set of finite scores between zero and one changes the order.
Ties preserve hybrid order. Disabled providers, transport failures, timeouts,
and malformed or incomplete results retain hybrid order. There is no retry or
second-provider attempt. Recall explicitly passes `timeout_seconds: 1.2` to either inference route;
the inference service binds that timeout to the shared HTTP transport and its socket client adds
100 ms for local dispatch and response overhead (1.3 seconds for recall). Admission allows three seconds
for the full recall, including local search and page loading. These are socket
timeouts, not a guarantee of identical elapsed latency. Luna task-title
creation remains asynchronous and explicitly passes a 20-second timeout;
it does not block agent launch. The inference API has no feature-purpose field;
OpenAI always uses Luna, and TypeSafe always uses Jev.

The socket allowance is fixed; there is no deadline accounting or per-feature
timeout logic in the inference service. Recall shares the inference service's existing four-call capacity
with other features. A busy service also keeps hybrid order; there are no
reserved recall slots, queues, or retries.

## Diagnostics

The existing `memory_recall_details` records the query, retrieval evidence,
and actual injected page ids/revisions. Its `Rerank experiment:` JSON entry
records experiment version 4, selected provider and its
assignment probability, requested model (and returned Jev model), outcome,
elapsed milliseconds, the 1.2-second timeout setting, and whether hybrid fallback
was used. A provider or client socket timeout records `outcome: "timeout"`, keeps the
assignment, and retains hybrid order with `fallback: true`. The randomized
control group records `provider: null`, `outcome: "control"`, probability 1/3,
and `fallback: false`. A disabled assigned provider keeps its provider/model
and probability, records `outcome: "provider_disabled"` and `fallback: true`,
and stays distinct from control and provider failures. Every candidate
has its local id, page id, revision, original hybrid rank/score, validated
reranker score, and final rank. `selection` is the proposed top five; the
separate `Selected` entries record what was actually loaded after revision
checks. Failed or partial scores are never applied or reported as valid scores.

These traces support comparisons of ranking, latency, and failure rates across
randomized providers. Compare all assigned recalls, including unavailable and
timed-out calls, rather than only successful provider responses. Relevance
quality still needs judgments against the task; the two providers' scores are
not calibrated to each other. Appending history
can introduce unrelated topics, particularly without reranking. Regression
tests establish behavior, not a measured claim of improved retrieval quality.
