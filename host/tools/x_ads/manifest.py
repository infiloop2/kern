"""The fixed X Ads action surface and its Home > Integrations guide."""

from __future__ import annotations

from typing import cast

from host.param_guard import PARAM_GUARD_PROTECTION, PARAM_GUARD_TECHNICAL_DETAIL
from host.tools.json_types import JSONObject
from host.tools.manifest import (
    ActionSpec, ConfigRequirement, DataSummary, DataSummaryCard, DataSummaryLink,
    DataSummaryPoint, SetupStep, ToolManifest, guarded_input, protect_inputs, validated_input,
)
from host.tools.shared.inputs import schema
from host.tools.shared import outputs as out
from host.tools.x_ads import shapes as s
from host.tools.x_ads.api import CONFIG_KEYS

ID: JSONObject = {"type": "string", "description": "1-32 lowercase base-36 characters, as returned by this tool; not the decimal Ads Manager URL id."}
ACCOUNT: JSONObject = {"account_id": {**ID, "description": "Authorized advertiser account id from list_accounts."}}
PAGE: JSONObject = {
    "count": {"type": "integer", "description": "Page size 1-50, default 20."},
    "cursor": {"type": "string", "description": "Opaque next_cursor from the preceding page, at most 1024 UTF-8 bytes. Guarded before egress."},
}
TIME: JSONObject = {"type": "string", "description": "A valid UTC second, YYYY-MM-DDTHH:MM:SSZ."}
MICROS: JSONObject = {"type": "string", "description": "Positive integer micros in the funding currency, at most 1000000000000 (one million currency units). 1000000 micros = 1 currency unit, including currencies without cents. No leading zero, sign or decimal point."}
NAME: JSONObject = {"type": "string", "description": "1-255 characters and at most 1024 UTF-8 bytes, nonblank and without control characters; exact name shown in approval."}
# Goals stay internal. Billing defaults are verified in the returned ad group
# before either entity is activated; pay_by is never an arbitrary input.
OBJECTIVES = {
    "ENGAGEMENTS": ("ENGAGEMENT", "ENGAGEMENT", "engagements"),
    "REACH": ("MAX_REACH", "IMPRESSION", "impressions"),
    "WEBSITE_CLICKS": ("LINK_CLICKS", "IMPRESSION", "impressions, optimized for link clicks"),
    "VIDEO_VIEWS": ("VIDEO_VIEW", "VIEW", "video views"),
}
TARGET_INPUT: JSONObject = schema({
    "type": {"type": "string", "enum": ["LOCATION", "LANGUAGE", "INTEREST", "BROAD_KEYWORD", "PHRASE_KEYWORD", "SIMILAR_TO_FOLLOWERS_OF_USER"], "description": "Supported inclusion targeting type. Follower lookalikes target people similar to a seed user's followers."},
    "value": {"type": "string", "description": "LOCATION: 16 lowercase hex characters; LANGUAGE: 2-3 letter code with optional language-region suffix; INTEREST: 1-12 digits. SIMILAR_TO_FOLLOWERS_OF_USER: numeric X user ID, 1-25 digits without leading zero, never a handle or URL. Otherwise exact keyword text. Nonblank, no controls, at most 80 characters and 320 UTF-8 bytes."},
}, ["type", "value"])
TARGETS: JSONObject = {"type": "array", "minItems": 0, "maxItems": 20, "items": TARGET_INPUT, "description": "Complete explicit audience: 0-20 unique inclusion criteria. LOCATION is optional. Without LOCATION, approval shows Worldwide, no location restriction; [] means no criteria, worldwide. Same type is OR; broad types intersect, primary interests/keywords/follower lookalikes form a union."}
LAUNCH: JSONObject = {
    **ACCOUNT,
    "funding_instrument_id": ID,
    "promotable_user_id": {**ID, "description": "FULL promotable record id from list_promotable_users; distinct from its numeric user_id."},
    "post_id": {"type": "string", "description": "1-25 decimal digits without leading zero: existing published organic post id by that user. Text, images, or video already attached to it. Cards, quote posts, reposts, replies and truncated content are excluded."},
    "name": NAME,
    "daily_budget_amount_local_micro": {**MICROS, "description": str(MICROS["description"]) + " Applied to the campaign; the single ad group has no separate budget."},
    "total_budget_amount_local_micro": {**MICROS, "description": str(MICROS["description"]) + " Applied to the campaign; the single ad group has no separate budget."},
    "start_time": TIME, "end_time": TIME, "targeting": TARGETS,
    "objective": {"type": "string", "enum": list(OBJECTIVES), "description": "Optional, default ENGAGEMENTS. REACH has no additional creative requirement; WEBSITE_CLICKS requires a usable website URL already in the post; VIDEO_VIEWS requires an attached real video. AUTO bidding for every objective. No new URL, media, pixel or conversion input."},
    "audience_expansion": {"type": "string", "enum": ["DEFINED", "EXPANDED", "BROAD"], "description": "Optional X audience expansion level, shown exactly in approval. Omit for no expansion. X validates availability for the selected audience."},
}
LAUNCH_REQUIRED = [key for key in LAUNCH if key not in ("audience_expansion", "objective")]
READ_POLICY = "Read-only OAuth 1.0a signed request to ads-api.x.com. The selected advertiser/entity ids, filters and cursors leave this host. Account, post or advertising data enters model context and host call history without approval. Uses X Ads API rate limits."
WRITE_POLICY = "Preparation sends advertiser, funding, user, post or campaign ids to X for read-only validation before approval. Exact settings, creative, numeric follower seeds, expansion and geographic scope are shown in Approvals > View exact request. Writes occur once after approval, bound to the same credentials and authenticated user. Launch creates new paused entities, verifies them and activates delivery under one approval. End rechecks ownership and role while allowing delivery/settings drift. Advertising spend is billed by X to the selected funding source."
READ_COST = "No per-request Ads API price is asserted. Provider quota and your X agreement apply. This tool does not report advertising spend to Kern Tools costs; performance returns X's provisional billing metrics separately."
WRITE_COST = "Preparation/revalidation uses Ads API reads. Launch can spend immediately or at its approved start, charged by X in the selected funding currency. AUTO bidding provides no per-engagement maximum bid. Explicit daily and total budgets apply. API charges, taxes and final invoices follow your X agreement and are not estimated as Kern tool costs."
METRICS = ("impressions", "engagements", "clicks", "url_clicks", "likes", "retweets", "replies", "follows", "video_total_views")
STATS_SCHEMA = out.obj({
    **{key: out.nullable({"type": "number"}, "X's TOTAL metric; null when absent, withheld or incomplete.") for key in METRICS},
    "billed_charge_local_micro": out.nullable({"type": "string"}, "Provisional advertiser spend in funding-currency micros, or null. Generally final within three days, but X can correct billing for up to 14 days. Not an API read fee or final invoice."),
}, [*METRICS, "billed_charge_local_micro"])


def page_output(key: str, row_schema: JSONObject) -> JSONObject:
    return out.obj({key: out.array_of(row_schema, "One bounded provider page."), "next_cursor": out.nullable({"type": "string"}, "Continue explicitly; null means no next page.")}, [key, "next_cursor"])


def read(action: str, description: str, properties: JSONObject, result: JSONObject, required: list[str] | None = None) -> ActionSpec:
    return ActionSpec(id=action, description=description, data_policy=READ_POLICY, cost_description=READ_COST, input_schema=schema(properties, required), output_schema=result)


DIRECT = (
    read("list_accounts", "List authorized advertiser accounts for the configured OAuth1 user. Select an account_id explicitly for subsequent calls.", PAGE, page_output("accounts", s.ACCOUNT_SCHEMA)),
    read("get_account", "Check advertiser identity, timezone and live authenticated-user permissions. Ads API access is separate from organic X access.", ACCOUNT, out.obj({
        "account": s.ACCOUNT_SCHEMA, "user_id": out.text("Authenticated numeric X user id."),
        "permissions": out.array_of(out.text("X advertiser role."), "Live account-level permissions."),
    }, ["account", "user_id", "permissions"]), ["account_id"]),
    read("list_funding_sources", "List existing funding instruments and eligibility in their own currency. Kern cannot add cards or credit lines.", {**ACCOUNT, **PAGE}, page_output("funding_sources", s.FUNDING_SCHEMA), ["account_id"]),
    read("list_promotable_users", "List authorized promotion identities. FULL records can promote their own posts. The Ads API returns numeric user ids; list_posts includes handles when available.", {**ACCOUNT, **PAGE}, page_output("promotable_users", s.PROMOTABLE_SCHEMA), ["account_id"]),
    read("list_posts", "List published organic posts for one FULL promotable user. Review exact text and existing media content digest before selecting a post. Cards, quote posts, reposts and replies can appear here but launch rejects them.", {**ACCOUNT, **PAGE, "promotable_user_id": ID}, page_output("posts", s.POST_SCHEMA), ["account_id", "promotable_user_id"]),
    read("lookup_targeting", "Find location, language or interest values for targeting. Query is guarded, at most 80 characters and 320 UTF-8 bytes. Location options apply only to locations.", {
        **PAGE, "kind": {"type": "string", "enum": ["LOCATION", "LANGUAGE", "INTEREST"], "description": "Which official audience catalog to search."},
        "query": {"type": "string", "description": "Nonblank lookup text, at most 80 characters and 320 UTF-8 bytes, without control characters."},
        "location_type": {"type": "string", "enum": ["COUNTRIES", "REGIONS", "METROS", "CITIES", "POSTAL_CODES"], "description": "Location level; default COUNTRIES."},
        "country_code": {"type": "string", "description": "Two uppercase ISO country-code letters; locations only."},
    }, page_output("options", out.obj({
        "name": out.text("X's audience name."), "type": out.text("Targeting type."), "value": out.text("Exact targeting value to pass in launch."),
        "country_code": out.text("Country code, empty if omitted."), "location_type": out.text("Location level, empty if omitted."),
    }, ["name", "type", "value", "country_code", "location_type"])), ["kind", "query"]),
    read("list_campaigns", "List a bounded page of campaigns, budgets, pacing and provider serving eligibility/reasons, including paused campaigns.", {**ACCOUNT, **PAGE}, page_output("campaigns", s.CAMPAIGN_SCHEMA), ["account_id"]),
    read("get_campaign", "Read one campaign including pacing, provider serving eligibility/reasons and one page of its ad groups. Continue explicitly with next_cursor; this is not proof the first page is complete.", {**ACCOUNT, **PAGE, "campaign_id": ID}, out.obj({
        "campaign": s.CAMPAIGN_SCHEMA, "ad_groups": out.array_of(s.GROUP_SCHEMA, "One page of ad groups."), "next_cursor": out.nullable({"type": "string"}, "Next ad-group page."),
    }, ["campaign", "ad_groups", "next_cursor"]), ["account_id", "campaign_id"]),
    read("get_ad_group", "Read one ad group with provider serving eligibility/reasons and complete targeting and promoted-post associations, up to 100 each. Fails if either list is incomplete.", {**ACCOUNT, "line_item_id": ID}, out.obj({
        "ad_group": s.GROUP_SCHEMA, "targeting": out.array_of(s.TARGET_SCHEMA, "Complete targeting."), "promoted_posts": out.array_of(s.PROMOTED_SCHEMA, "Complete promoted-post associations."),
    }, ["ad_group", "targeting", "promoted_posts"]), ["account_id", "line_item_id"]),
    read("get_performance", "Read TOTAL campaign metrics across ALL_ON_TWITTER, SPOTLIGHT and TREND for a whole-hour UTC window of at most seven days, using three bounded analytics reads. Return each placement and sums of reported values; all-missing totals remain null. Malformed metric series or values fail explicitly with metric/placement names, without raw values. Spend is generally final within three days but billing can be corrected for up to 14 days; this is not a final invoice.", {
        **ACCOUNT, "campaign_id": ID, "start_time": TIME, "end_time": TIME,
    }, out.obj({"campaign_id": ID, "currency": out.text("Funding currency."), "start_time": TIME, "end_time": TIME, "metrics": STATS_SCHEMA,
        "placement_metrics": out.array_of(out.obj({"placement": {"type": "string", "enum": ["ALL_ON_TWITTER", "SPOTLIGHT", "TREND"]}, "metrics": STATS_SCHEMA}, ["placement", "metrics"]), "All three placement buckets. Missing values remain null; aggregate metrics sum reported values and remain null when every bucket is missing."),
    }, ["campaign_id", "currency", "start_time", "end_time", "metrics", "placement_metrics"]), ["account_id", "campaign_id", "start_time", "end_time"]),
)
PROTECTIONS = {
    action.id: {
        key: guarded_input(allow_machine_tokens=True) if key == "cursor" else guarded_input() if key == "query" else validated_input(
            "Bounded integer page size, fixed enum, base-36 id, UTC timestamp, or two-letter country code; semantic validation before egress."
        )
        for key in cast(JSONObject, action.input_schema["properties"])
    } for action in DIRECT
}

MANIFEST = ToolManifest(
    tool_id="x_ads", display_name="X Ads", connection="enable_only",
    description="Discover X advertisers, funding and existing posts; approve creating and launching a new campaign, end delivery, and read performance with dedicated Ads API credentials.",
    actions=(*protect_inputs(DIRECT, PROTECTIONS),
        ActionSpec(id="launch_campaign", description="Approve one new existing-post campaign, default ENGAGEMENTS, with AUTO bidding and standard TWITTER_TIMELINE delivery. Creates a PAUSED campaign with explicit daily/total caps and standard pacing, plus one ad group with the approved flight, targeting and one organic post; verifies exact state and explicit ACCEPTED/PENDING X creative review, then configures ACTIVE under the same approval. Can spend immediately, at the approved start or when X approves pending creative review, without another Kern approval. Configured ACTIVE is not proof of delivery. No resume-existing mode. A partial failure leaves confirmed IDs for inspection and requires a fresh proposal.", data_policy=WRITE_POLICY, cost_description=WRITE_COST, input_schema=schema(LAUNCH, LAUNCH_REQUIRED), approval="operator"),
        ActionSpec(id="end_campaign", description="Approve pausing parent campaign delivery, retaining the campaign and reporting. No Kern resume capability or permanent deletion. Someone with X Ads Manager access can resume it there; stopping may take time and past delivery remains billable.", data_policy=WRITE_POLICY, cost_description=WRITE_COST, input_schema=schema({**ACCOUNT, "campaign_id": ID}, ["account_id", "campaign_id"]), approval="operator"),
    ),
    config=tuple(ConfigRequirement(key, description) for key, description in zip(CONFIG_KEYS, (
        "OAuth 1.0a API Key (consumer key) from the exact developer app with Ads campaign access. Not the OAuth2 Client ID.",
        "Matching OAuth 1.0a API Key Secret (consumer secret).",
        "OAuth 1.0a user Access Token from that app, for a user with advertiser access. Not an OAuth2 bearer token.",
        "Matching OAuth 1.0a Access Token Secret.",
    ))),
    protections=(PARAM_GUARD_PROTECTION,
        "Every advertising write requires one single-use operator approval. Launch creates new paused entities, verifies them and activates the parent last under that same approval.",
        "Approvals bind credentials, authenticated user, advertiser, funding currency, full existing post content and exact launch terms, including objective, numeric follower seeds, geographic scope and audience expansion. Changes require a fresh proposal.",
        "No request retries or automatic rollback. Partial/unknown outcomes report confirmed entity ids and the attempted stage for inspection before a new proposal.",
    ),
    technical_details=(PARAM_GUARD_TECHNICAL_DETAIL,
        "Contained failures record the action, failed phase, HTTP status/request operation when available and confirmed IDs in Host diagnostics. Reference and configured-state mismatches additionally identify the comparison and up to eight differing field paths with JSON types, without compared values; omitted differences are marked as truncated. Bounded provider error responses appear only in the authenticated diagnostic detail, never Chat results. Logging does not retry or recover prior discarded errors.",
        "Official X Ads REST API v12 with OAuth 1.0a HMAC-SHA1. Four credentials are encrypted write-only settings scoped to this tool. No browser OAuth callback or scope strings are used, and organic X OAuth connections are not reused. Changing credentials invalidates pending approvals.",
        "Read pages default to 20, maximum 50, with explicit cursor continuation. Cursor guard permits provider machine tokens only; secret and personal-identifier checks remain. Lookup query has no guard exceptions. Unknown fields are rejected, including nested targeting fields. All approval names/keywords are bounded and shown as approved copy.",
        "Micros always mean one millionth of the funding currency. Positive daily/total caps are strings up to 1000000000000; daily <= total. The campaign alone holds both explicit caps and standard pacing. Its single ad group uses the approved flight without separate budget or pacing setters. Execution requires returned CAMPAIGN budget optimization and exact caps/pacing before creating the child and at both activation barriers; unexpected modes fail without activation. Funding source is required and never selected automatically. AUTO bidding has no caller bid amount or maximum price per result. X validates eligibility, audience availability and currency-specific minimum budgets.",
        "One optional objective enum defaults to ENGAGEMENTS. Internal goal/billing pairs are ENGAGEMENTS: ENGAGEMENT/ENGAGEMENT; REACH: MAX_REACH/IMPRESSION; WEBSITE_CLICKS: LINK_CLICKS/IMPRESSION; VIDEO_VIEWS: VIDEO_VIEW/VIEW. All use AUTO, PROMOTED_TWEETS, TWITTER_TIMELINE and standard delivery. X derives pay_by from objective/goal; returned billing must match before activation. Website clicks under AUTO is billed by impressions and optimized for link clicks. No caller goal, pay_by, conversion/pixel or app fields.",
        "Promote one existing organic text/image/video post by a FULL promotable user. WEBSITE_CLICKS requires a usable HTTP(S) website URL entity already in that post; VIDEO_VIEWS requires an attached real video. No quoted/reposted/reply creative, promoted-only posts, new organic posts, uploads, cards, custom audiences, conversions or frequency caps. Mutable card content is excluded.",
        "Targeting is a required explicit array of 0-20 unique LOCATION, LANGUAGE, INTEREST, BROAD_KEYWORD, PHRASE_KEYWORD or SIMILAR_TO_FOLLOWERS_OF_USER inclusions. LOCATION is optional. With no LOCATION, approval plainly says Worldwide, no location restriction; [] means no targeting criteria. Follower seeds are numeric X user IDs, never handles/URLs or claims of verified handle resolution. Use an existing organic user lookup tool to find an ID when needed; this integration adds no organic endpoint or credential dependency. Optional audience_expansion is DEFINED, EXPANDED or BROAD; omission applies no expansion. X validates audience compatibility.",
        "Inspect Approvals > View exact request for the complete new campaign plan, objective, AUTO bidding, billing basis, post text/URL and media/destination digest, numeric seeds, explicit criteria, expansion, geographic scope and funding currency. Execution rechecks credentials, authenticated user/role, accepted advertiser, live funding, post content and an unexpired flight before creating anything. The new campaign and ad group start PAUSED; all returned settings, complete targeting and one ACTIVE post association with explicit ACCEPTED or PENDING X review must match before activation. Unknown, missing or rejected review fails with the parent paused.",
        "Launch never resumes an existing campaign. Creation responses and verification bind configured campaign/ad-group/targeting/association fields, including unknown settings, with canonical digests. Only updated_at and derived effective_status, servable and reasons_not_servable are excluded. Known money integer/string representations and equivalent timezone-aware whole-second flight timestamps compare by value. Explicitly checked optional null/absent fields, manual creative defaults, default EQ inclusion and inherited parent funding compare consistently; a different funding ID, actual setting change or new unknown field still stops activation. Campaign and ad-group creation responses are checked before proceeding; activation replies must confirm the requested state and settings. The approved child activation is the sole allowed configured-state change before parent activation. Recheck creative, funding, identity/role and end time immediately before activating the parent. X blocks delivery while creative review is PENDING and may start it automatically once ACCEPTED, within the approved flight, without another Kern write or approval. Only PENDING to ACCEPTED review transitions are permitted during verification. Configured ACTIVE is not proof of delivery; results report the last observed review state. Failed/unknown review stops the launch with created IDs for inspection; there is no wait loop, retry or resume-existing mode.",
        "Campaign and ad-group reads expose effective_status (bounded provider code), nullable servable and up to 32 reasons_not_servable codes with an explicit truncation flag. Missing/null provider fields remain null; [] means X supplied an empty reason list. Malformed flags/codes fail the read without echoing values. X may omit these fields, particularly on ad groups. ACTIVE, servable=true and an empty reason list establish configuration/eligibility only; positive reported impressions establish observed delivery, while billed_charge_local_micro establishes provisional spend. No extra provider requests or campaign changes are made to inspect these fields.",
        "End pauses parent delivery and retains the campaign/reporting. Its approval binds credentials, authenticated user, advertiser and campaign identity, rechecking ownership and role without blocking on volatile delivery/status or campaign-settings drift. Kern offers no resume, update or permanent deletion; someone with X Ads Manager access can resume there. Stopping can take time, and past spend remains billable. To change approved terms, end delivery and launch a new campaign with a new ID and separate reporting history.",
        "The single approved launch has multiple sequential writes without provider idempotency. Any partial/unknown outcome stops further work, identifies confirmed ids and the attempted stage, and requires inspection before a fresh approval. No automatic retry, rollback, deletion or hidden activation. A failed final activation response may still mean delivery is active; inspect X Ads immediately.",
        "No live Ads activation or spend is exercised by tests; objective/audience behavior is documentation-backed and mocked, not live-validated. Reads disclose data and consume provider quota without approval; no per-call API price is invented. Performance covers ALL_ON_TWITTER, SPOTLIGHT and TREND in three bounded reads with a placement breakdown. Missing metrics, explicit nulls and [null] remain null; reported zero stays zero. Malformed TOTAL series or invalid values fail the read with a controlled metric/placement label and one Host diagnostic instead of returning null or partial totals. Totals sum reported values and stay null when all buckets are missing. Advertising spend is in the funding currency and is not a USD Kern Tools API charge. Spend is generally final within three days, but billing corrections can continue for up to 14 days.",
    ),
    setup_steps=(
        SetupStep("Confirm the approved app", "Use the exact developer app approved for Ads campaign management (Standard Access), with OAuth1 Read and Write permission. Conversion-only access, an active project, ordinary X API access or Browser sign-in does not establish Ads access. Verify the App ID in your developer console; Kern does not infer which app was approved.", "https://developer.x.com/", "Open X Developer Console"),
        SetupStep("Check advertiser access and billing", "The token's user needs ACCOUNT_ADMIN or AD_MANAGER to launch or end campaigns. Analysts can read what X permits. Establish advertiser eligibility and a funding instrument in X Ads. FULL promotable users are the identities whose existing posts can be promoted.", "https://ads.x.com/", "Open X Ads"),
        SetupStep("Save the four Ads credentials", "For your own account, use the approved app's OAuth1 API Key/Secret and user Access Token/Secret. For another authorized user, obtain its pair through X's supported three-legged OAuth1 flow outside this initial integration. Enter secrets only in these write-only fields, never in Chat. Set app Read and Write before issuing the token; if you change app permissions, issue a new token in X and replace its matching pair here. Kern does not rotate credentials or import organic X connections.", show_config=True),
        SetupStep("Enable and verify", "Enable X Ads, then ask the agent to run list_accounts, get_account, list_funding_sources and list_promotable_users for your selected advertiser. These are live reads, not a billing test. Tools-service requests reach only ads-api.x.com; enabling agent Network access to the Ads API is not needed for this bundled tool."),
        SetupStep("Approve launch or end", "Select an explicit funding source and existing post with list_posts; find audience values with lookup_targeting and numeric follower seeds with an existing organic lookup tool if needed. Supply explicit targeting, including [] for no criteria, and optional objective/expansion. launch_campaign queues the complete proposal. Inspect View exact request, then approve once to create paused entities, verify and activate. end_campaign pauses parent delivery while retaining reporting; Kern has no resume/update/delete. Inspect any partial IDs before another proposal."),
    ),
    data_summary=DataSummary(cards=(
        DataSummaryCard("What leaves this host", points=(DataSummaryPoint("Reads", "Signed authentication, selected account/entity/user ids, lookup text, filters and cursors reach X before approval; advertiser, funding, post and metrics data enters Chat context/history."), DataSummaryPoint("Approved writes", "Campaign names, budgets, dates, objective, numeric follower seeds, expansion, targeting values, existing post ids and state changes reach X only after approval. Existing attached media is promoted; Kern uploads no media."))),
        DataSummaryCard("Where it can go", "Only the official ads-api.x.com REST endpoint. Promoted existing posts are shown to the approved audience through X's advertising delivery.", links=(DataSummaryLink("Ads API account access", "https://docs.x.com/x-ads-api/fundamentals/accessing-ads-accounts"), DataSummaryLink("Campaign API reference", "https://docs.x.com/x-ads-api/campaign-management/reference"), DataSummaryLink("Ads analytics", "https://docs.x.com/x-ads-api/analytics"))),
        DataSummaryCard("What X can do with it", "X authenticates requests, operates/moderates ads, delivers promotions, measures engagement and charges the advertiser's funding instrument under its terms. Read-only access still sends data and consumes rate limits.", links=(DataSummaryLink("X Ads terms", "https://legal.x.com/en/ads-terms.html"), DataSummaryLink("X privacy policy", "https://x.com/en/privacy"))),
        DataSummaryCard("How long it is retained", "Kern retains write-only encrypted configuration until replaced/cleared and audit/approval records under host retention. X retains advertising, billing and account data under its policies; this integration promises no fixed provider deletion deadline.", links=(DataSummaryLink("X privacy policy", "https://x.com/en/privacy"),)),
    )),
    agent_notes="Start with account, permissions, funding and promotable-user discovery. Ads ids are base-36; post/user ids are decimal. Never infer approval from read access. Do not reissue pending or failed submissions: inspect confirmed ids, get_campaign/get_ad_group and X Ads first. This tool has no auto retry, dedupe, arbitrary endpoint, media upload or creative publishing action. launch_campaign always creates a new campaign, verifies while paused and activates under one exact approval. It cannot resume any existing or partially created campaign. end_campaign pauses delivery and retains reporting, with no Kern resume or permanent deletion. Use numeric follower seed IDs; no handle resolution occurs here. Unsupported delivery setups should be managed in X Ads.",
)
