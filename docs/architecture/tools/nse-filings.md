# NSE public filing adapter

The `nse_filings` package provides issuer lookup, raw filing indexes and
index-verified document downloads. Operator setup, terms responsibility and
action schemas live in **Home > Integrations > NSE India Filings**, rendered
from its manifest. No account, paid subscription, key or OAuth is configured.

## Source mapping

All index requests are anonymous GETs to `https://www.nseindia.com/api/`.

| Action/category | Route | Source shape |
| --- | --- | --- |
| `lookup_company` | `NextApi/globalSearch/equity?symbol=…` | `data` array, filtered to exact symbol and equity segment |
| `announcements` | `corporate-announcements` | Array; `attchmntFile` attachment |
| `annual_reports` | `annual-reports` | `data` array; `fileName` attachment |
| `financial_results` | `corporates-financial-results` | Legacy quarterly array; `xbrl` attachment |
| `integrated_financials` | `integrated-filing-results` | `data`, `size`, `page`, `totalCount`; `xbrl`, `ixbrl`, `pdf_attach` |

Listing requests fix `index=equities`. Legacy results fix `period=quarterly`;
integrated results fix `type=Integrated Filing- Financials`. The new integrated
feed is necessary: the tested Infosys legacy response stopped in January 2025,
while integrated results included October 2026. Neither feed is Company Facts.

Inputs use explicit ISO publication/broadcast-date windows except annuals,
which return the available archive without date parameters. Integrated input
pages start at 1 with fixed size 20; source `page` starts at 0. The adapter
checks returned page/size/count consistency and reports the next page. It does
not loop over pages automatically. Other indexes expose no authoritative
total; successful downloads alone do not prove complete history.

## Boundaries and output

The directory validates the current issuer before listing. Annual records
identify issuers by company name; other families must return the requested
symbol. Renames, delisted issuers and historical symbol resolution are not
implemented. A mismatch or unexpected response shape fails, rather than
returning an apparently empty history.

Raw JSON is returned as a complete source asset, preserving numeric spellings,
source timestamps, standalone/consolidated labels, revisions and unknown
fields. It is not converted to a normalized financial dataset. Index assets
carry source URL, retrieval time, length, SHA-256 and coverage qualifications.

`download_document` repeats the selected issuer/index request and matches the
exact basename against its attachment fields. Only canonical HTTPS paths under
`nsearchives.nseindia.com/corporate/` or `/annual_reports/` are fetched. Safe
ASCII basenames, supported extensions and unambiguous membership are required.
Other attachment hosts/names remain visible in raw indexes but unsupported by
this download action. `corporate/null` is not a downloadable PDF; `hasXbrl`
does not by itself identify an XML URL.

The client sends an identified User-Agent, without cookies, impersonation,
arbitrary headers, redirects or automatic retries. A process-wide one-second
request-start interval limits local traffic; it is not an NSE quota guarantee.
JSON is capped at 16 MiB and documents at 200,000,000 bytes. Failed, encoded,
oversized or length-mismatched responses do not return partial assets. XML and
HTML download assets use text/plain so source markup is not treated as active
content. Download format checks do not certify document semantics.

## Research and validation, 30 October 2026

Anonymous public access was tested for Infosys (`INFY`), TCS (`TCS`), Reliance
(`RELIANCE`) and HDFC Bank (`HDFCBANK`). October 1–29 announcement responses
contained respectively 17, 19, 13 and 16 issuer-matching records. Actual tool
execution reproduced those counts. Infosys's five-year announcement response
contained 1,140 records and matched all fields of the union of six disjoint
calendar-year requests. This is a bounded consistency check, not an exchange
completeness certification.

Infosys annual, legacy financial and integrated financial indexes returned 17,
26 and 14 records. Integrated pages with size two were checked separately:
input page 1 returned source page 0, and page 2 returned source page 1 with
different records. Raw XML and inline XBRL downloads succeeded. The real tool
also downloaded the index-linked 8,985,411-byte annual PDF, a 152,312-byte
integrated XBRL document and a 284,012-byte Reliance announcement PDF.

Live action checks used the same package with the test harness's HTTP opener
configured for the agent's required Kern policy proxy. Production tools use
their existing service transport. These checks do not certify a deployed
integration; service-level smoke is a separate release check. Unit tests mock
the transport and cover source identity, malformed indexes, pagination,
request validation, attachment membership and incomplete/error responses.

BSE is not implemented. Its current public website configuration and scripts
still reference `api.bseindia.com/BseIndiaAPI/api`, but tested lookup and
announcement requests redirected to a member-interest HTML page rather than
JSON. This does not establish that the routes are obsolete or identify the
reason for the redirect. No cookie, geographic or anti-bot workaround was used.

Public accessibility does not establish automated-use permission. NSE's
[terms of use](https://www.nseindia.com/static/nse-terms-of-use) prohibit
systematic or automated collection; the operator is responsible for arranging
permissions and compliance. That responsibility is documented in the
integration guide; there is no added per-call approval or payment workflow.

Useful official pages: [announcements](https://www.nseindia.com/companies-listing/corporate-filings-announcements),
[integrated financials](https://www.nseindia.com/companies-listing/corporate-integrated-filing).
