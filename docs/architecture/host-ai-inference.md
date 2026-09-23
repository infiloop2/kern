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
  The caller names a feature purpose, and reviewed host code chooses the OpenAI
  model for that purpose.
- `typesafe_jev_judgment(...)` sends bounded state and yes/no Jev questions and
  validates the returned probabilities.

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
GPT prompts and Jev state content. The original local input is not changed.
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

When TypeSafe Jev is enabled, memory recall sends the task query and up to 20
candidate descriptions with short local ids. Jev ranks those candidates, and
Kern loads the top five page contents locally. Page contents are never sent to
Jev. If Jev is disabled or does not return a valid answer, recall keeps its existing
deterministic order.
