# Prolific REST contract

`host/tools/prolific` provides 17 native research-workflow actions, with four
operator approvals. It calls the [Prolific REST API](https://docs.prolific.com/api-reference)
directly; it does not run an MCP server. The package's Home > Integrations guide
owns setup and usage instructions.

## Authentication and boundaries

`PROLIFIC_TOKEN` is the sole write-only config secret. Every API request uses
`Authorization: Token <token>` at `https://api.prolific.com/api/v1` through the
existing tools-service HTTPS egress. No configurable origin, migration, manual
registry, agent domain grant or runtime/shim change is needed; package discovery
finds `BUNDLED_TOOL`. See [host integration](host-integration.md).

API timeout is 30 seconds per request, with no redirects or retries.
Normal API responses are capped at 1 MiB. Account discovery joins
at most 100 workspaces and 100 projects per workspace and rejects aggregate data above 1 MiB. Normal JSON
results above 2 MiB + 64 KiB fail. Unknown fields/type changes are preserved as
complete redacted JSON in `provider_details_json`; known fields use closed
schemas. The four approval actions return curated execution messages without a
JSON output schema, following the host tool contract. `export_demographics` and
`save_collection_export` are exclusive file results with `returns_asset=True`
and no JSON output schema. Other direct actions return closed JSON.

Filter discovery is a deliberate bounded projection: the full catalogue fetch
permits 8 MiB, search runs locally, and only matched filters are returned. Search
is a required non-whitespace, case-insensitive substring over `filter_id`, title,
question and choice labels. `limit` is 1–100, default 50; each filter includes
`id`, title, type, question, at most 50 `{id,label}` choices, `choice_count` and a
`truncated` flag. Matching choice labels come first. Range/data-type metadata is
retained when present. The outer `match_count` and `truncated` disclose omitted
filters. No full-catalogue fallback is returned. Results above 256 KiB are
rejected with a narrower-search instruction; individual strings are never
clipped. Filter search text and limit are not sent to Prolific. Optional `workspace_id`
is sent to select contextual previous-study/group choices.

## Actions and endpoints

Paths are relative to `/api/v1`; `?` marks optional input. All actions report
cost: only successful `start_study` reserves estimated USD; the others report
zero additional reservation. Direct does not mean no egress or no provider
quota usage. Both export actions initiate provider jobs/data exports directly.

| Action | Required inputs | Optional inputs / defaults | HTTP requests | Approval |
| --- | --- | --- | --- | --- |
| `get_account` | None | None | `GET /workspaces/`, then `GET /workspaces/{id}/projects/` for each | direct |
| `get_filters` | `search` | `limit=50`, `workspace_id` | `GET /filters/`, then local search | direct |
| `get_eligibility_count` | None | `filters=[]`, `workspace_id`, `study_type=STUDY_TYPE_SINGLE` | `POST /eligibility-count/` with those fields | direct |
| `list_studies` | None | `state`, `page=1`, `page_size=50`, `search` | `GET /studies/` with query fields | direct |
| `get_study` | `study_id` | None | `GET /studies/{id}/`; for UNPUBLISHED/PAUSED also `GET /studies/{id}/cost/?is_projected=true` | direct |
| `list_submissions` | `study_id` | `limit=200`, `offset=0` | `GET /studies/{id}/submissions/` with pagination, then `GET /studies/{id}/submissions/counts/` | direct |
| `export_demographics` | `study_id` | `filters=[]` | `POST /studies/{id}/demographic-export/` with `{filters}`; CSV workspace file | direct |
| `list_collections` | `workspace_id` | `limit=200`, `offset=0` | `GET /data-collection/collections` with query fields | direct |
| `get_collection` | `collection_id` | None | `GET /data-collection/collections/{id}` plus local preview URL | direct |
| `export_collection` | `collection_id` | None | `POST /data-collection/collections/{id}/export` without a body | direct |
| `get_collection_export_status` | `collection_id`, `export_id` | None | `GET /data-collection/collections/{id}/export/{export_id}` | direct |
| `save_collection_export` | `collection_id`, `export_id` | None | `GET /data-collection/collections/{id}/export/{export_id}`, then anonymous GET of provider-issued signed URL; ZIP workspace file | direct |
| `create_study` | `study` | Nested fields below | `POST /studies/` with decoded study | direct |
| `halt_study` | `study_id` | None | Read target study before/after approval; `POST /studies/{id}/transition/` with STOP | operator |
| `save_collection` | `collection` | `collection_id` | After approval, absent ID: POST collections; present ID: PUT collection with full decoded content. Replacement reads target before/after approval. | operator |
| `start_study` | `study_id` | None | Read study and projected cost before/after approval; `POST /studies/{id}/transition/` with PUBLISH for UNPUBLISHED or START for PAUSED | operator |
| `approve_submissions` | `submission_ids` | None | `POST /submissions/bulk-approve/` with `{submission_ids}` | operator |

`get_account` returns `workspaces`, each with its `projects` listing and any
wallet/currency fields supplied by the workspace response. It does not make a
separate wallet/balance call, infer balances or fill missing currency values.
Listing metadata is preserved. `list_submissions` combines `counts` with the
submission `results`/pagination; the requests are sequential, not an atomic
provider snapshot. `get_collection` adds
`https://app.prolific.com/data-collection-tool/collections/{id}?preview=true`.
export_collection starts once and returns status/export ID; callers use
get_collection_export_status with both IDs to poll every
10–30 seconds rather than reissuing creation. Signed URLs are removed before
normalization and fallback. Once complete, `save_collection_export(collection_id,
export_id)` re-reads status to refresh the provider URL, downloads the original
ZIP and returns a workspace file via the existing streaming result pipeline.
Generating/failed exports return clear file-action errors with the export ID;
only `export_collection` creates jobs. No caller URL is accepted. Both IDs are
validated at input; any echoed export ID must match.

The provider URL must be public HTTPS on the default port, without userinfo,
IP literals or fragments, at most 8192 characters. It is provider-echoed, not
agent free text, so it does not pass the parameter guard. Downloads use existing
kern-tools HTTPS egress, no API Authorization header, no redirects/retries,
a 120-second socket timeout, identity encoding, and declared size 22 bytes–200 MB.
ZIP-compatible content types are required. Host streaming rejects incomplete or
oversized bodies and atomically saves a generated workspace filename. All ZIP
bytes are preserved, never redacted, extracted or executed: responses.jsonl,
collection.json, README.md and optional files/ remain intact for local extraction.
The signed link lasts one hour; GET refreshes it. No Get Collection Responses
endpoint is implemented (provider documentation marks it coming soon).

`export_demographics` saves the complete credential-redacted UTF-8 CSV directly
as a `.csv` workspace file, without polling. Both fetched and redacted CSV are
bounded to 1 MiB and rejected rather than clipped. Both file actions return the
host-generated path, filesystem_path, media_type, size_bytes and short summary;
files remain until removed. The file summaries do not expose provider URLs.

Eligibility preserves Prolific's privacy-safe count: zero means fewer than five,
not necessarily no participants. Pass the target study workspace explicitly because
workspace country/tax rules affect eligibility. For compatibility workspace_id
remains optional; omission uses the provider account's current workspace, whose
identity is not returned. Results include workspace_id (null when omitted),
workspace_scope (requested_workspace or provider_current_workspace_unverified),
and count_note. Treat an unverified current-workspace estimate as unsuitable for
planning another workspace's study.

## Input validation

Study creation supports only hosted AI Task Builder collections and batches.
Both data_collection_method (AI_TASK_BUILDER_BATCH or AI_TASK_BUILDER_COLLECTION)
and data_collection_id are required. external_study_url, access_details and
completion_codes are rejected even when null or supplied through JSON text.
Prolific generates the default COMPLETED code with MANUALLY_REVIEW. Batch places
may be omitted for provider calculation; collection studies require places.
Collection task_details is optional; when supplied all three instruction fields
are required.

Unknown top-level arguments fail; optional nulls mean omission. IDs must be 24
hexadecimal characters or canonical UUIDs, normalized to lowercase before constructing paths or comparing provider
echoes. Pagination excludes booleans:
`limit` 1–500 (filter discovery 1–100), `offset` 0–2147483647, study `page`
1–2147483647 and `page_size` 1–200. `state` accepts a documented status or a
parenthesized `|` group. `study_type` accepts
STUDY_TYPE_SINGLE/STUDY_TYPE_REP_SAMPLE. Bulk approval requires 1–1000 submission IDs; study_id and participant_ids
are rejected for this action. `halt_study` accepts only `study_id`; the removed `action`
selector is rejected. It permanently stops recruitment after operator approval.

Closed `study`, `collection` and `filters` schemas also accept complete JSON
text for provider extensions. Decoded JSON receives the same structural validation; direct actions also use the
parameter guard; it is sent as an object/array, never as JSON-in-a-string. Typed
collection `page_items` use JSON object text; full collection JSON also accepts
ordinary content objects. Unknown provider fields in these alternatives are
preserved. There is no arbitrary endpoint action.

Study requires `name`, `description`, `estimated_completion_time`,
`reward`, `data_collection_method` and `data_collection_id`. Optional internal_name,
workspace_id and project are omitted by default; omitted devices/peripherals
remain absent for project inheritance. Filters default to [] unless a saved filter
set is used. Reward is integer cents/pence >=0; places/minutes are integers >=1,
all at most 2147483647. Devices: desktop/tablet/mobile. Peripherals:
audio/microphone/camera/download.

Kern always sends `prolific_id_option=not_required` for hosted tasks and rejects
caller-supplied values, including JSON extensions and nulls. This is an explicit
integration choice using the documented enum, not a claimed provider default.
The option controls separate ID capture in a survey; provider participant and
submission IDs remain available via list_submissions. It does not enable
follow-up messaging.
Known nested IDs, including participant-group IDs, reject every non-string
shape. Credential-pool IDs additionally accept `cred_pool_` followed by 1–64
ASCII letters/digits/underscores/hyphens, with suffix casing preserved; this
scoped ID exception also applies to response redaction. Credential values stay
redacted. Numeric extensions beyond the safe JSON-number bound fail cleanly
before float conversion. Null optional study fields are omitted. For JSON studies with `filter_set_id`,
inline filters must be absent or null and remain that way on the wire; the
empty-list default applies only without a saved filter set.

Collection requires `name` and nonempty `collection_items` with nonnegative integer `order` and
`page_items`. Creation requires `workspace_id`; replacement rejects it. Read the
collection first and retain entity IDs; omitted content is deleted by replacement.

Direct-action free text and decoded JSON keys/text/numeric extension values are guarded.
Approved collection content instead uses structural bounds and operator review; no
direct-action text guard or 5120-byte per-value limit applies. Its whole input and
approval payload must fit 64 KiB; IDs, shapes, nesting and safe numbers are revalidated.
Strict IDs and ID-shaped filter choices use grammar validation instead. Free
text in direct actions has no identifier/machine-token exemption. Each guarded value permits 5120 UTF-8
bytes; input has a 64 KiB cap, nesting limit 16, 10000-node bound and at most 1000
items per list. Demographic exports permit 15 filters. Decoded URL-like values in
direct-action text still receive the ordinary parameter guard; there is no
external-survey URL validation or creation path.

## Cost preview and approvals

`get_study` returns `{study, cost}`. Cost is null outside UNPUBLISHED/PAUSED.
For those two states it always reads projected cost and returns currency, reward,
places, `projected_total_minor_units`, `estimated_usd`, and `usd_per_currency_unit`.
Nullable or absent filters are accepted for studies using `filter_set_id`;
the full study definition and filter-set ID remain visible in the approval.
The last three are decimal strings. This uses exactly the same function as the
start approval, so the agent can show the price before requesting permission.

Use the maximum of projected rewards/fees/tax/bonuses, study `total_cost` when
present, and reward × places × 1.42857 for the whole-study reservation. The
cost block's projected total includes this floor. Divide minor units by 100;
USD uses 1 and GBP a fixed conservative planning rate of 1.50 USD/GBP; round
up to USD cents. Conflicts between study `currency`/`currency_code` or projected currency,
unknown currencies and malformed costs fail
closed. This is a planning estimate, not a live FX quote or settled invoice.

Every approval binds normalized exact parameters and a SHA-256 token fingerprint,
never the token. `start_study` additionally shows/binds name, reward, places,
duration, filters, full redacted study definition, state, chosen transition,
currency, FX rate and USD estimate. Approved execution repeats both reads and
requires the same definition hash (including ID/reward/places), state/transition
and complete preview. A change requires a new approval, including a change from
UNPUBLISHED to PAUSED. Prolific provides no conditional transition parameter, so
changes between the final GET and POST remain a provider race.

`halt_study` shows the study ID, redacted name, current status and permanent-stop
effect. It binds the exact normalized study ID and token, reads the study again
before execution and requires the provider to echo the same ID. It does not bind
the entire definition or freeze the study status; Prolific validates whether STOP
is legal in its current state. No PAUSE transition is exposed. The initial call
only requests approval; only approved execution sends STOP.

Successful start records decimal USD with `charge_id=study_id`; publish/resume
share the ID so the host records only the first reservation. It does not account
for later budget increases, refunds, external funding or settled participant
payments. Other actions report zero additional reservation, although approving
submissions pays participants from reserved funds. Pending approvals record no
cost. No rate secrets or cost details blobs are stored.

`save_collection` binds its exact proposed content in parameters.collection, the
normalized creation workspace or replacement collection ID, and token fingerprint.
Creation previews its workspace; replacement reads the current collection and shows
its ID/name and full-replacement/omission-deletion effect. Both modes require approval.
Execution revalidates structure and token; replacement re-reads and verifies the target
ID before PUT. It does not freeze the prior content/version, and no conditional update
is available; omitted content is intentionally deleted. No provider write occurs while
requesting approval. Retain entity IDs when preparing replacements.

Use list_submissions results[i].id for approve_submissions.submission_ids.
The listing also exposes scalar participant_id for research context, but submission
approval does not accept participant IDs or a study ID.

Bulk submission approval acknowledges a queued request, not individual completion;
confirm outcomes with `list_submissions`. Plan demographic exports before calling:
Prolific permits only two different filter export requests per study, up to 15
filters each. Identical requests can be downloaded again.

Account discovery uses fixed-path limit/offset pagination with meta.count; it
rejects incomplete, repeated or over-bound pages rather than returning partial IDs.
Account results redact workspace and project users (member names/emails/roles).
Submission listings redact the provider ip and strata blocks before projection or
JSON fallback; demographic exports remain the explicit demographic-data route.

Tokens and credential values, including encoded echoes, are redacted from
responses/approvals/diagnostics. Safe credential-pool metadata remains visible.
401 asks to reconfigure; 402 and recognized insufficient-funds 403 ask to fund
the wallet; other 403 errors identify permissions/features/wallet access. 429
asks to back off. Unknown provider failures retain no provider body in diagnostics.
Never automatically retry writes or reissue a pending approval.

## References and validation scope

Endpoint and model references were checked on 2026-10-14 against the public API
and [official server source](https://github.com/prolific-oss/prolific-mcp/tree/2337c366707eccbac2afb1cc55644d8751465250).
The public docs cover [filters](https://docs.prolific.com/api-reference/filters/get-filters),
[study creation](https://docs.prolific.com/api-reference/studies/create-study),
[transitions](https://docs.prolific.com/api-reference/studies/publish-study),
[cost](https://docs.prolific.com/api-reference/studies/get-study-cost), and
[collections](https://docs.prolific.com/api-reference/ai-task-builder/create-collection).
Kern intentionally groups these endpoints into 17 workflow actions.

The deprecated `state` wire field, pagination/search inputs, eligibility
`study_type`, study `workspace_id` and collection `order` retain official-server
provenance where public documentation is incomplete. Collection PUT retains its
trailing slash; redirects are not followed. Study creation supports the documented
AI Task Builder collection/batch delivery alternatives.

Explicit study/collection definitions, filters and participant/submission IDs
leave the host. Prolific's [privacy policy](https://www.prolific.com/privacy) and
research/account obligations govern retention; disabling the integration does
not delete provider records. Studies must not collect PII. Provider text is
untrusted. Mocked tests cover all action paths, combined reads, catalogue limits,
request shapes, guards, costs, redaction and approval revalidation. The package
ships disabled; no token is configured here and live calls are untested. CI owns
full repository validation.

The credential-pool prefix support uses the review-cited official
`cred_pool_12345` example. Direct retrieval of that credentials reference URL was
blocked by the host parameter guard; the cached study references establish the
field but do not enumerate every possible ID format. The suffix bound above is
a local validation policy, not a claimed provider limit.
