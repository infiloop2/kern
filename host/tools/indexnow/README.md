# IndexNow

Enable IndexNow in Home > Integrations. There is no manual configuration,
OAuth, reconnect or search engine dashboard registration.

`get_verification_file` accepts `{}` only (unknown fields are rejected) and
runs directly without approval or provider requests. On first retrieval or
valid submission proposal, Kern generates a random 32-character lowercase
hexadecimal key and persists it in its encrypted, tool-scoped database
storage. It survives calls, process restarts and host upgrades; disabling and
re-enabling the integration does not rotate it. Concurrent handlers share the
same key. There is no rotation action.

The closed JSON result contains exactly `key`, `filename`, `content` and
`path`. For a generated key `0123456789abcdef0123456789abcdef`, the result is:

```json
{
  "key": "0123456789abcdef0123456789abcdef",
  "filename": "0123456789abcdef0123456789abcdef.txt",
  "content": "0123456789abcdef0123456789abcdef",
  "path": "/0123456789abcdef0123456789abcdef.txt"
}
```

Publish the returned UTF-8 file at `https://<host>/<key>.txt` on each owned
site. One stored key serves all submitted hosts. The action returns deployment
details; the agent needs separate site repository or hosting access to publish
it. Previous manual `INDEXNOW_KEY` configuration is no longer read: deploy the
returned file before submitting, and re-propose any old pending approvals.

Retrieval exposes this intentionally published verification key and file
details to the calling agent and its selected model provider. The private
approval-binding salt is never returned. No caller strings need parameter
guarding because retrieval has no inputs or provider request. Both actions
have no IndexNow fee; normal Kern runtime costs are separate.

`submit_urls` requires only `urls`: a flat array of 1–100 distinct HTTPS URL
strings, each at most 2,048 UTF-8 bytes, on the same exact named host. Complete
compact UTF-8 approval JSON, including the exact URL list, host and key-binding
metadata, must fit 65,536 bytes (64 KiB). Oversized batches fail before queuing;
split them into smaller exact batches. Unknown
fields, nested arrays/objects, credentials, IP literals, non-443 ports,
fragments and path traversal are rejected. Only added, materially changed or
deleted public pages should be submitted. Caller input cannot supply a key,
key location or submission endpoint.

Before queuing approval, the existing host parameter guard scans **every**
full wire URL plus every nested-decoding view of its path and query. The
longer-text guard tier is used under the stricter URL byte bound, with no
identifier or machine-token exceptions. Guard failures reject the batch;
there is no rewriting, redaction, page fetch or submission on this path.
The host-generated stored key is not caller content and is not scanned.

Every batch queues operator approval. Home > Approvals > View exact request
displays the full exact URL list, including paths and queries. The immutable
approval payload binds that batch and host to a key fingerprint computed with
a persistent private host HMAC secret. Neither the key nor the HMAC secret is
in the approval payload. The full approval batch may be sent to the configured
host approval-assessment provider before a decision. Execution rechecks URL
structure, every guard and key binding; a changed key or missing binding fails
closed. The host's single-use lifecycle prevents a second execution.

After approval, Kern makes one redirect-free JSON POST of the exact URL list,
host and stored key to `https://api.indexnow.org/indexnow`. Provider
ownership verification fetches the key file. The host's normal 64 KiB JSON
input/approval bounds still apply to the complete batch. There are no automatic
retries. Errors are curated without returning raw provider bodies.

The action returns an approval ID and summary, then a user-visible receipt
message or failure; it has no direct JSON result. HTTP 200 confirms receipt;
202 confirms receipt with key validation pending. Neither proves crawling or
indexing. IndexNow is free, subject to rate limits, and reaches Bing and other
participating engines, not Google. Normal Kern runtime costs remain separate.
Participants control notification retention; disabling cannot retract past
submissions. No site changes or live submissions are performed by setup docs.

Provider references: [protocol](https://www.indexnow.org/documentation),
[FAQ](https://www.indexnow.org/faq), [website terms](https://www.indexnow.org/terms).
