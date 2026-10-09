# Host AI inference

Host AI inference is separate from agent runtimes and bundled tools. It lets
reviewed Kern host features request small, typed results from operator-enabled
providers without making either the provider account or its API key available
to an agent.

## Service boundary

`kern-host-inference` is one dedicated service for all host-owned AI
providers. It has its own pinned UID, scoped Postgres role, systemd lifecycle,
runtime directory, and Unix socket. The kernel-authenticated `kern-admin`,
`kern-workspace`, and `kern-tools` UIDs may call its fixed provider routes.
`kern-agent` cannot call the socket, and no caller receives database access to
provider credentials.

The service runs deterministic repository code and has direct DNS and HTTPS
egress, like `kern-tools`; it does not use the agent network policy. Individual
adapters still use fixed provider endpoints. The firewall identity boundary is
therefore who can execute as `kern-host-inference`, not a second dynamic domain
policy system.

## Concrete calls

There is no provider-slot abstraction. Host feature code calls the contract it
actually needs:

- `openai_text_completion(...)` sends a bounded prompt and strict JSON schema.
  The caller supplies `model`, `instructions`, `reasoning_effort`,
  `max_output_tokens` (1 to 4096), and `timeout_seconds` (0.1 to 60 seconds).
  Supported models are `gpt-6-luna` and `gpt-6.1-sol`. The shared socket client adds a fixed 100 ms
  for local dispatch and response overhead.
  There is no feature-purpose field or purpose-to-model mapping.
  Task titles keep their Luna/none/400-token settings and existing timeout.
- `typesafe_jev_judgment(...)` sends bounded state and yes/no Jev questions and
  validates the returned probabilities using `jev-latest`. Its existing timeout
  range remains 0.1 to 2 seconds.
- `openai_decisions(...)` sends bounded text and named predicate questions to
  `POST https://api.openai.com/v1/decisions`. The caller supplies `gpt-6-luna`
  and a timeout from 0.1 to 60 seconds. Responses must contain an ordered answer
  for every question, with finite probabilities from zero to one or a refusal.
  It uses the existing OpenAI provider configuration and secret.

The service binds the caller's timeout to the shared HTTP transport before
passing it to each adapter. Adapters only build provider requests and parse
responses; they do not choose or carry timeout settings.

This makes differences between providers explicit. Adding another provider
does not silently make it interchangeable with either existing contract.

## Credentials and configuration

The admin service stores provider configuration in Postgres. API keys use the
host secret box, and operator responses expose only whether a key is
configured. Saving a key and enabling or disabling a provider are separate
actions. Model selection is not operator configuration.

The scoped `kern-host-inference` database role can read provider ciphertext and
the secret-box key required to decrypt it. It cannot mutate provider settings.
Only the admin-owned API writes configuration.

## Failure behavior

Each request limits the input size, response size, number of simultaneous
calls, and network inactivity time. Before provider egress, the adapters replace
credential-shaped values with `<redacted>`. They also replace runs of at least
11 letters, digits, underscores, or hyphens that contain a digit. This applies to
GPT prompts, Decisions input and question instructions, and Jev state content.
The original local input is not changed. Host-authored Decisions question names
and types remain intact.
Recall redacts structured evidence with the same shared redactor before encoding
it as Decisions text, so embedded JSON credential fields remain recognizable.
Values whose object keys collide after redaction are retained together;
credential-field values are redacted in full. Jev question ids and OpenAI
response-schema fields are set by host code.
OpenAI response-schema keys remain intact. Provider adapters use the standard
HTTPS client with normal certificate and hostname verification and do not retry.
For OpenAI, each host feature declares the exact JSON fields, value types, and
limits it accepts, and Kern checks the returned JSON locally before using it.
Jev answers are checked against the exact typed questions. Approval annotation
sends the current approval state after egress redaction or does not call Jev at
all; it does not clip or infer fields from tool payloads. A legitimate call
that exceeds a limit, times out, receives a provider error, or returns an
invalid result records a bounded Host diagnostic and raises a
`HostInferenceError` to the calling feature, with no usable result. Successful
calls are not logged merely for operating within those limits. Features must
handle that error by preserving their last good state or using a deterministic
fallback.

Memory recall uses TypeSafe Jev when enabled to score the bounded task query
and up to 20 candidate descriptions under short local ids. Every recall with
candidates requests Jev and, in parallel, Luna Decisions using the same original
hybrid candidates. OpenAI must be enabled and configured; otherwise its adapter
is not called. With both validated successes, recall selects the top-three union
(shared pages first, then alternating Jev/Luna picks) without refill. One success
selects that provider's top five; neither success keeps the hybrid top five.
There is no random assignment. Decisions has one separate concurrency slot;
it cannot consume the existing four slots used by Jev and text completion.
Extra calls fail as busy without queuing. The inference service owns enablement
checks and reports `provider_disabled` or `timeout` to callers without exposing
configuration tables or credentials. Page contents are never sent to either provider.
Swarm task titles use the same bounded query text as recall, passed to Luna
with task-title instructions. See [Memory recall](memory-recall.md) for
retrieval, reranking, and diagnostics.

Decisions usage joins the existing OpenAI/Luna usage history. Its price differs
from text completion: $0.10 per million uncached input tokens, with no cache-read,
cache-write surcharge, or output-token charges, reviewed against
[OpenAI's Decisions pricing](https://developers.openai.com/api/docs/guides/decisions#pricing-and-availability)
on October 6, 2026. Stored costs for prior calls remain unchanged.

## Auto-approval

Approvals > Auto-approval manages one natural-language policy per bundled
operator-gated tool action. Saving activates it; deleting removes it. There
are no rule names, enabled flags, priorities, account selectors, or policy
version states. The operator may restrict accounts and other conditions in
the policy text. Tool/action scope is enforced by the host; textual conditions
are model judgments, not deterministic spending limits or verified external facts.
GitHub pushes are outside this feature.

The admin service starts one serial review loop after binding its listener.
It schedules checks 25 to 35 minutes apart, after the previous run finishes,
and pauses during a saved daily sleep window (00:00 to 08:00 UTC by default).
Operators can set sleep and wake times in Approvals > Auto-approval. The window
may cross midnight and must last at least six hours; equal times are invalid.
The settings persist across restarts and redeploys. Saving reschedules the worker
without restarting it, and each request checks the current window before review.
An in-progress review may finish after a settings change.
Startup schedules the next check rather than
replaying missed runs. Each run takes up to 20 eligible requests in oldest-first order. A provider
failure ends the batch after recording that failed check. A disabled or
unconfigured provider records that specific reason for each request with a policy,
without calling inference. The batch continues recording these checks.
Each approval is checked once. Any recorded result, including no policy found or
a failure, excludes it from future checks. Policy edits,
deletions and additions never make checked requests eligible again. The page
exposes the next check time, provider availability,
policies and paginated review history. Ordinary approvals show their last check
and a Set/Edit policy action. Saving a policy takes effect at the next scheduled
review; the editor does not run inference or approve requests.

The auto-approver owns its prompt, strict `{approve, reason}` schema, and
`gpt-6.1-sol`/medium/4096-token/60-second settings. It uses the shared
`/openai/text-completion` socket route, with the same credential redaction,
request bounds and usage metering as other Host AI calls. Old Sol is no longer accepted
for new requests or metering; historical review and usage records retain their original
model and stored cost. Sol 6.1 uses its own published cached-input price. The inference service
has no approval-specific route or logic. Operator instructions and exact request data are separate
JSON fields under host-authored instructions that treat request content as
untrusted evidence. The reviewer is told that redaction can hide public IDs,
account IDs, URL components and parts of timestamps. It judges whether the
remaining evidence satisfies the operator's policy; redaction alone does not
require a negative decision. When the available evidence is insufficient for
a policy condition, the request stays
pending with an explanation of that condition. Oversized requests fail rather
than being truncated into a potentially misleading approval. The reviewer has
no tools or external lookup.

A positive review goes through the existing admin-to-tools approval path.
Policy text, OpenAI availability and quiet hours are checked before review.
The returned decision uses that policy snapshot, without rechecking conditions
after inference. The tools service knows nothing
about AI reviews or policies; its existing pending-to-approved transition
prevents duplicate execution, including races with manual decisions.

The worker saves its decision and reason before calling tools. This recorded
check prevents later batches from picking up the approval, even if admin crashes
before making the call. If saving fails, tools is not called. The tools service
updates only its existing request status and execution result.

The AI decision and execution status are independent. A crash after saving can
leave an approve decision with a pending request; no retry or recovery follows.
Approval-call errors are recorded separately without replacing the decision or
reason. A lost response can leave execution unconfirmed. The worker does not
replay the call, reread the outcome, or send a recovery notification. Policy or availability changes during
review do not invalidate its decision; a review already started can approve
after quiet hours begin.

Review records retain the timestamp, exact policy text, model, explanation and
outcome. They reference the immutable original approval payload and follow its
existing retention through a cascading foreign key. Later policy edits do not
rewrite these records. Approved decisions and tool execution outcomes remain
separate. Policy routes are authenticated operator-only admin
routes, unavailable to Workspace agents. Agents cannot edit their own approval
policies or call the inference socket.
