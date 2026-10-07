# IndexNow

Generate a random 8–128 character key using ASCII letters, digits and hyphens.
Publish a UTF-8 `https://<host>/<key>.txt` file containing the exact key on each
site, then save `INDEXNOW_KEY` and enable IndexNow in Home > Integrations. The
same configured key serves all submitted hosts. No OAuth, reconnect or search
engine dashboard registration is required. Site deployment and host key
configuration are operator steps, not tool actions.

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
The host-owned configured key is not caller content and is not scanned.

Every batch queues operator approval. Home > Approvals > View exact request
displays the full exact URL list, including paths and queries. The immutable
approval payload binds that batch and host to a key fingerprint computed with
a persistent private host HMAC secret. Neither the key nor the HMAC secret is
in the approval payload. The full approval batch may be sent to the configured
host approval-assessment provider before a decision. Execution rechecks URL
structure, every guard and key binding; a changed key or missing binding fails
closed. The host's single-use lifecycle prevents a second execution.

After approval, Kern makes one redirect-free JSON POST of the exact URL list,
host and configured key to `https://api.indexnow.org/indexnow`. Provider
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
