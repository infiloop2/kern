# PageSpeed Insights

Enable PageSpeed Insights in Home > Integrations and save a Google Cloud
`PAGESPEED_INSIGHTS_API_KEY` with the PageSpeed Insights API enabled. Restrict
the key to that API. No OAuth grant or reconnect is needed. The provider API
is free, subject to project quota; normal Kern runtime costs remain separate.

`analyze_page` runs directly without approval. Required `url` is a public HTTP
or HTTPS page URL with a named host and at most 2,048 UTF-8 bytes. Credentials,
IP literals, fragments and unsupported ports are rejected. Optional `strategy`
is `mobile` (default) or `desktop`. Optional `categories` is a unique, nonempty
array of up to four values: `performance`, `accessibility`, `best-practices`,
`seo`; default is all four. Unknown fields are rejected.

The host parameter guard scans the URL and every nested-decoding view of its
path and query before constructing the request. The longer-text guard tier
permits the stated URL bound; there are no identifier or machine-token
exceptions. Guard failure rejects the call without redaction or rewriting.
The configured API key is a host credential, not caller content.

Kern makes one redirect-free GET to the fixed Google `runPagespeed` endpoint;
Google fetches the public page, potentially following page redirects. The
closed JSON result contains requested/final URLs, strategy, analysis time,
Lighthouse version, category scores (nullable), available lab metrics, and
up to ten priority audits. There is no raw Lighthouse document, site crawler
or full technical SEO audit. Lab diagnostics work without real-user traffic;
one run does not guarantee rankings or user experience.

The URL, strategy, categories and key leave the host for Google. Findings are
returned to the agent and its model provider. Google controls request telemetry
retention; disabling this integration does not erase prior provider data.
Quota, authentication and provider failures return curated errors without raw
provider bodies. No automatic retries or background queries are performed.

Provider references: [setup](https://developers.google.com/speed/docs/insights/v5/get-started),
[API](https://developers.google.com/speed/docs/insights/rest/v5/pagespeedapi/runpagespeed),
[privacy](https://policies.google.com/privacy).
