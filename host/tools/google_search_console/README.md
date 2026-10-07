# Search Console URL-list indexing summary

`inspect_urls` extends the existing connected Google Search Console integration.
Existing `GOOGLE_OAUTH_CLIENT_ID`/`GOOGLE_OAUTH_CLIENT_SECRET`, account connection
and `openid`, `email`, `webmasters` scopes suffice; no reconnect or new setup is
needed. The integration retains the full scope for its existing approval-gated
sitemap submission; this new action is a direct read without approval.

Required `site_url` is an exact accessible property returned by `list_properties`
(`sc-domain:example.com` or a URL-prefix property). Required `urls` is a flat
array of 1–5 distinct exact HTTP(S) URLs under that property. Optional
`language_code` defaults to `en-US` and uses the same language-tag validation as
`inspect_url`. Unknown fields, nested values, duplicates, surrounding whitespace
and out-of-property URLs are rejected. Existing structure/ownership checks also
reject credentials, fragments and ambiguous/traversing paths.

Every URL and nested-decoded path/query view passes the shared parameter guard
before any provider read, including authentication refresh and the live property
lookup. There are no guard exceptions. The existing 2,048 UTF-8 byte structural
limit and stricter 1,024-byte standard guard limit both apply. Property syntax is
validated locally, then matched against the connected account's live properties.
The host-selected account/token supplies all requests; tokens never reach the
agent. Caller URLs are not fetched as pages or sitemaps.

There is one property-list request, then one sequential URL Inspection request
per attempted URL to Google's fixed endpoint. Each provider request uses the
existing 30-second timeout. A 180-second budget includes validation/authentication
and property lookup; no further inspection starts with less than 30 seconds
remaining. The five-URL cap leaves margin within the 300-second host MCP timeout.
There are no retries, concurrent fan-out, discovery or scheduled reports.

The closed JSON result includes the exact property, one row per requested URL in
input order, and counts over those rows. Successful rows retain the existing
normalized inspection evidence: coverage/verdict, canonical URLs, crawl time,
fetch/robots/indexing states, referring URLs/sitemaps and rich-result findings.
Grouping uses only the indexing verdict: `PASS` → `indexed`, `NEUTRAL`/`FAIL` →
`not_indexed`; missing, null, unspecified, reserved or new verdicts → `unknown`.
Indexing-allowed and successful-fetch fields alone do not prove indexing.

Any mapped inspection API failure produces an `error` row and stops further calls.
Remaining rows are `unprocessed`, with `quota`, `authorization`, `provider_error`,
`invalid_response` or `time_budget` reasons; their inspection evidence is null.
No failed or unattempted URL counts as not indexed. Failure before inspections
(such as inaccessible property or disconnected account) fails the whole action.
Unmapped transport failures instead fail the entire action through the existing
Host diagnostics boundary; no partial report is presented as successful.
After fixing quota/authentication issues, callers may explicitly inspect only
the unprocessed URLs; the tool never resumes automatically.

This is coverage of the **submitted list**, not Google's full Page indexing
report, a live-page test or an indexing request. Google controls data freshness
and retention. URLs/property and language reach Google; results reach the agent
and its model provider. Disconnecting removes local OAuth tokens, not Google data.

Google charges no Search Console API fee. Inspection quota is shared per site:
2,000/day and 600/minute; per project: 10,000,000/day and 15,000/minute. Normal Kern
runtime costs remain separate. No live inspection calls are made by these docs.

References: [inspection API](https://developers.google.com/webmaster-tools/v1/urlInspection.index/inspect),
[status evidence](https://developers.google.com/webmaster-tools/v1/urlInspection.index/UrlInspectionResult),
[quotas](https://developers.google.com/webmaster-tools/limits).
