"""Google Ads action and Home integration guide contract."""
from typing import cast

from host.tools.json_types import JSONObject, JSONValue
from host.tools.manifest import (
    ActionSpec, ConfigRequirement, DataSummary, DataSummaryCard, DataSummaryLink,
    SetupStep, ToolManifest, protect_inputs, validated_input,
)
from host.tools.shared import outputs
from host.tools.shared.google import google_oauth_setup_steps
from host.tools.shared.inputs import schema

ADS_SCOPE = "https://www.googleapis.com/auth/adwords"
MAX_ROWS = 500
MAX_KEYWORDS = 20
MAX_MONEY_MICROS = 1_000_000_000_000


def choice(values: tuple[str, ...], description: str) -> JSONObject:
    return {"type": "string", "enum": cast(list[JSONValue], list(values)), "description": description}


def strings(description: str, low: int, high: int) -> JSONObject:
    return {"type": "array", "items": {"type": "string"}, "minItems": low, "maxItems": high, "description": description}


CUSTOMER = outputs.text("Google Ads customer ID, exactly 10 ASCII digits without hyphens.")
SCOPE: JSONObject = {"customer_id": CUSTOMER}
LIMIT = outputs.integer("Optional maximum rows from 1 to 500; defaults to 100. A bounded snapshot, not an automatic full export.")
MONEY = outputs.integer("Positive integer from 1 to 1,000,000,000,000 in account-currency micros; 1,000,000 micros = 1 currency unit.")
CAMPAIGN = outputs.text("Existing Search campaign ID, 1 to 19 ASCII digits.")
AD_GROUP = outputs.text("Existing Search ad group ID, 1 to 19 ASCII digits.")
AD: JSONObject = {
    "final_url": outputs.text("Exact HTTPS landing page URL, at most 2,048 UTF-8 bytes. No credentials or fragment. Google may crawl this destination."),
    "headlines": strings("3 to 15 distinct headlines, each 1 to 30 characters (double-width counts as two) and at most 120 UTF-8 bytes. Plain text only; no ad customizers.", 3, 15),
    "descriptions": strings("2 to 4 distinct descriptions, each 1 to 90 characters (double-width counts as two) and at most 360 UTF-8 bytes. Plain text only; no ad customizers.", 2, 4),
}
KEYWORDS: JSONObject = {
    "type": "array", "minItems": 1, "maxItems": MAX_KEYWORDS,
    "description": "1 to 20 distinct objects with text and match_type (EXACT, PHRASE or BROAD). Text is 1 to 80 characters, at most 320 UTF-8 bytes and 10 words.",
    "items": schema({
        "text": outputs.text("Plain keyword text without surrounding match-type brackets or quotes."),
        "match_type": choice(("EXACT", "PHRASE", "BROAD"), "Google keyword match type."),
    }, ["text", "match_type"]),
}
ACCOUNT_ROW = outputs.obj({
    "customer_id": CUSTOMER, "name": outputs.text("Account display name."),
    "currency_code": outputs.text("Account billing currency."), "time_zone": outputs.text("Account reporting time zone."),
    "manager": outputs.boolean("Whether this account is a manager."),
    "test_account": outputs.boolean("Whether this is a test account."),
}, ["customer_id", "name", "currency_code", "time_zone", "manager", "test_account"])
CAMPAIGN_ROW = outputs.obj({
    "campaign_id": CAMPAIGN, "name": outputs.text("Campaign name."), "status": outputs.text("Current Google campaign status."),
    "budget_resource": outputs.text("Exact current budget resource name."),
    "budget_period": outputs.text("DAILY or CUSTOM_PERIOD, as reported by Google."),
    "daily_budget_micros": outputs.text("Average daily budget micros as a decimal string; zero for a total budget."),
    "total_budget_micros": outputs.text("Campaign total budget micros as a decimal string; zero for a daily budget."),
    "start_time": outputs.text("Google campaign start in account time zone, YYYY-MM-DD HH:mm:ss, or empty if unavailable."),
    "end_time": outputs.text("Google campaign end in account time zone, YYYY-MM-DD HH:mm:ss, or empty if indefinite."),
    "bidding_strategy": outputs.text("Google bidding strategy type; TARGET_SPEND means Maximize Clicks."),
    "primary_status": outputs.text("Google delivery eligibility summary; empty if unavailable."),
    "shared_budget": outputs.boolean("Whether this budget is explicitly shared or has multiple references."),
}, ["campaign_id", "name", "status", "budget_resource", "budget_period", "daily_budget_micros", "total_budget_micros", "start_time", "end_time", "bidding_strategy", "primary_status", "shared_budget"])
GROUP_ROW = outputs.obj({
    "ad_group_id": AD_GROUP, "campaign_id": CAMPAIGN, "name": outputs.text("Ad group name."),
    "status": outputs.text("Ad group status."), "cpc_bid_micros": outputs.text("Default CPC bid in account-currency micros as a decimal string."),
}, ["ad_group_id", "campaign_id", "name", "status", "cpc_bid_micros"])
REPORT_ROW = outputs.obj({
    "date": outputs.text("Day in the account time zone."), "campaign_id": CAMPAIGN, "campaign_name": outputs.text("Campaign name."),
    "ad_group_id": outputs.text("Ad group ID; empty for campaign reports."),
    "criterion_id": outputs.text("Keyword criterion ID; empty for other reports."),
    "keyword": outputs.text("Keyword text; empty for other reports."),
    "match_type": outputs.text("Keyword match type; empty for other reports."),
    "search_term": outputs.text("Search term Google disclosed; empty for other reports."),
    "impressions": outputs.text("Exact integer impression count as a decimal string."),
    "clicks": outputs.text("Exact integer click count as a decimal string."),
    "cost_micros": outputs.text("Exact cost in account-currency micros as a decimal string."),
    "conversions": outputs.number("Google attributed conversions; may be fractional."),
    "conversion_value": outputs.number("Attributed conversion value in account currency."),
}, ["date", "campaign_id", "campaign_name", "ad_group_id", "criterion_id", "keyword", "match_type", "search_term", "impressions", "clicks", "cost_micros", "conversions", "conversion_value"])


def rows_output(row: JSONObject) -> JSONObject:
    return outputs.obj({
        "account": {**ACCOUNT_ROW, "description": "Selected account customer_id, name, currency_code, time_zone, manager and test_account."}, "rows": outputs.array_of(row, "Bounded rows in the declared order. Fields: " + ", ".join(cast(JSONObject, row["properties"])) + "."),
        "truncated": outputs.boolean("True when more rows may exist; narrow the date range or campaign filter."),
    }, ["account", "rows", "truncated"])


READ_POLICY = "Runs directly without approval. Sends validated account IDs, typed filters and a fixed report query to Google Ads. Returned account data and reports reach the agent and its model provider. OAuth refresh/profile reads may precede Ads calls."
WRITE_POLICY = "Reads the selected account and current target before proposing a change. Sends the exact account, target and content to Google Ads only after approval. Launch authorizes paid delivery during the approved flight, including during or after Google ad review. End stops delivery; past delivery remains billable. Google may crawl approved landing pages."
READ_ACTIONS = (
    ActionSpec(id="list_locations", description="List or filter the 219 active countries in Google's bundled 2026-08-12 geo-target snapshot. Returns IDs for geo_target_ids; regions and cities are not included. Snapshot status is not a live eligibility check.", data_policy="Reads a bundled public country snapshot directly without approval, OAuth, network requests or provider quota. Query and country filter stay on the host. Matching public location data reaches the agent and its model provider.", input_schema=schema({
        "query": outputs.text("Optional case-insensitive substring of the country name, at most 80 characters and 320 UTF-8 bytes, without control characters. Surrounding whitespace is trimmed. Empty or omitted returns all countries, subject to limit."),
        "country_code": outputs.text("Optional two uppercase ASCII letters from Google's country code field, such as GB or US. Empty or omitted applies no country filter."),
        "limit": outputs.integer("Optional maximum rows from 1 to 250; defaults to 250. Countries are ordered by name. No pagination; narrow filters if truncated."),
    }), output_schema=outputs.obj({
        "snapshot_date": outputs.text("Date of Google's bundled country snapshot, YYYY-MM-DD; not live data freshness."),
        "source_url": outputs.text("Official dated Google geo-target ZIP used to produce the snapshot."),
        "rows": outputs.array_of(outputs.obj({
            "geo_target_id": outputs.text("Google geo target constant ID to pass in launch_campaign.geo_target_ids."),
            "name": outputs.text("Google's country name."),
            "country_code": outputs.text("Google's two-letter country code."),
            "target_type": outputs.text("Country. This snapshot includes only country targeting."),
            "status": outputs.text("Active at snapshot time. Google validates current campaign targeting eligibility."),
        }, ["geo_target_id", "name", "country_code", "target_type", "status"]), "Matching countries, ordered by name."),
        "truncated": outputs.boolean("Whether more matches exist than the requested limit."),
    }, ["snapshot_date", "source_url", "rows", "truncated"])),
    ActionSpec(id="list_accounts", description="List up to 50 directly accessible Google Ads account IDs. Select an advertiser account for campaign operations; manager accounts are not supported.", data_policy="Reads directly accessible customer IDs from Google without approval. No agent-supplied content goes to Google.", input_schema=schema({}), output_schema=outputs.obj({"customer_ids": outputs.array_of(CUSTOMER, "Up to 50 directly accessible customer IDs."), "truncated": outputs.boolean("Whether the returned list was capped at 50.")}, ["customer_ids", "truncated"])),
    ActionSpec(id="list_campaigns", description="List existing Search campaigns with status, daily or total budget, flight and bidding strategy.", data_policy=READ_POLICY, input_schema=schema({**SCOPE, "limit": LIMIT}, ["customer_id"]), output_schema=rows_output(CAMPAIGN_ROW)),
    ActionSpec(id="list_ad_groups", description="List Search ad groups for one campaign, including default CPC bid.", data_policy=READ_POLICY, input_schema=schema({**SCOPE, "campaign_id": CAMPAIGN, "limit": LIMIT}, ["customer_id", "campaign_id"]), output_schema=rows_output(GROUP_ROW)),
    ActionSpec(id="report", description="Read daily Search campaign, keyword or disclosed search-term performance for an explicit date range.", data_policy=READ_POLICY, input_schema=schema({
        **SCOPE, "report_type": choice(("campaigns", "keywords", "search_terms"), "Required report grouping."),
        "start_date": outputs.text("Inclusive YYYY-MM-DD date in account time zone. Daily reporting is limited by Google to the last 37 months; older dates fail at the provider."),
        "end_date": outputs.text("Inclusive YYYY-MM-DD date, no earlier than start_date; maximum span 366 days."),
        "campaign_id": outputs.text("Optional Search campaign ID filter, 1 to 19 ASCII digits."), "limit": LIMIT,
    }, ["customer_id", "report_type", "start_date", "end_date"]), output_schema=rows_output(REPORT_ROW)),
)
TIME = outputs.text("Required RFC3339 timestamp with seconds and an explicit UTC offset, for example start 2026-10-15T00:00:00+05:30 and end 2026-10-22T23:59:59+05:30 for Asia/Kolkata. No fractional seconds. Normalized to UTC and shown in the account time zone. In the account time zone, start must be 00:00:00 and end 23:59:59, covering 3 to 90 inclusive days. Flight must be unexpired and start date today or later. Google enforces account eligibility.")
WRITE_ACTIONS = (
    ActionSpec(id="launch_campaign", description="Launch one NEW Google Search campaign after one exact approval. Creates a dedicated total budget, paused campaign, one ad group, keywords and responsive ad atomically, verifies the configuration, then enables within the same approval. Automatic Maximize Clicks bidding with no maximum CPC input. Fixed declaration: no EU political advertising; political campaign launches are unsupported. Google review may delay delivery; approval permits later delivery during the flight. No existing-campaign or resume mode.", data_policy=WRITE_POLICY, approval="operator", input_schema=schema({
        **SCOPE, "name": outputs.text("Campaign name, 1 to 128 characters and at most 255 UTF-8 bytes."),
        "total_budget_micros": MONEY, "start_time": TIME, "end_time": TIME,
        "geo_target_ids": strings("Optional, defaults to []. Up to 10 distinct geo target constant IDs, 1 to 19 ASCII digits each. Empty means worldwide, no location restriction. Specified locations use presence targeting. Use list_locations for country IDs; regions/cities require Google's geo-target catalog at https://developers.google.com/google-ads/api/data/geotargets.", 0, 10),
        "keywords": KEYWORDS, **AD,
    }, ["customer_id", "name", "total_budget_micros", "start_time", "end_time", "keywords", *AD])),
    ActionSpec(id="end_campaign", description="Stop a Search campaign after approval by setting its status to PAUSED. Preserves the campaign and reporting. Kern offers no resume, edit or delete action; someone can resume it in Google Ads. Stopping may take time and past delivery remains billable.", data_policy=WRITE_POLICY, approval="operator", input_schema=schema({**SCOPE, "campaign_id": CAMPAIGN}, ["customer_id", "campaign_id"])),
)
READ_PROTECTIONS = {
    "list_locations": {"query": validated_input("Local-only country-name substring; at most 80 characters/320 UTF-8 bytes, no control characters."), "country_code": validated_input("Empty or two uppercase ASCII letters; local-only country filter."), "limit": validated_input("Integer from 1 to 250; default 250.")},
    "list_campaigns": {"customer_id": validated_input("10 ASCII digits and live account access."), "limit": validated_input("Integer from 1 to 500; default 100.")},
    "list_ad_groups": {"customer_id": validated_input("10 ASCII digits and live account access."), "campaign_id": validated_input("1 to 19 ASCII digits, matched to a Search campaign in this customer."), "limit": validated_input("Integer from 1 to 500; default 100.")},
    "report": {"customer_id": validated_input("10 ASCII digits and live account access."), "report_type": validated_input("One of campaigns, keywords, search_terms."), "start_date": validated_input("YYYY-MM-DD calendar date."), "end_date": validated_input("YYYY-MM-DD calendar date with ordered span at most 366 days."), "campaign_id": validated_input("Optional 1 to 19 ASCII digits; fixed Search query filter."), "limit": validated_input("Integer from 1 to 500; default 100.")},
}
MANIFEST = ToolManifest(
    tool_id="google_ads", display_name="Google Ads",
    description="Connect Google Ads to read Search campaign performance and launch or end Search campaigns with your approval.",
    connection="oauth", actions=protect_inputs(READ_ACTIONS, READ_PROTECTIONS) + WRITE_ACTIONS,
    config=(ConfigRequirement("GOOGLE_OAUTH_CLIENT_ID", "Web application OAuth client ID in your Ads API approved Google Cloud project."), ConfigRequirement("GOOGLE_OAUTH_CLIENT_SECRET", "That Web application client's secret.")),
    protections=(
        "Google OAuth tokens stay in Kern's credential store and are never returned to agents.",
        "Every customer is checked against live direct access for the connected Google user.",
        "Every change waits for approval bound to the Google identity, customer, exact content and current target. These are rechecked before execution.",
        "Launch creates a new campaign paused, verifies its configuration, then enables under the same approval. End pauses delivery; no Kern resume/edit/delete actions. Only Search campaigns are supported.",
    ),
    agent_notes="Use list_locations to look up country IDs locally before launch_campaign; optional query, country_code and limit filter a dated public snapshot. Regions and cities are not bundled; snapshot Active status does not prove live eligibility. Start with list_accounts and select a directly accessible advertiser account. Manager discovery and routing are not supported. IDs have no hyphens. Use report for campaigns, keywords or search_terms. launch_campaign always creates a NEW campaign with a total budget and explicit start/end times, Maximize Clicks auto bidding, one group and responsive ad. Omitted/empty geo_target_ids means worldwide; keywords still required. One approval covers paused creation, readback and enabling, including delivery during or after Google review. Configured ENABLED is not proof of delivery. end_campaign stops via PAUSED with no Kern resume; change a campaign by ending it and launching another. Money uses account-currency micros. No automatic retries or recovery actions: inspect any confirmed IDs and uncertain outcome before proposing again. Explorer covers this surface; Keyword Planner, billing, account creation, conversion setup/uploads and other campaign channels are absent.",
    setup_steps=google_oauth_setup_steps(
        project_step_description="Select the Google Cloud project approved for Google Ads API access. You can reuse a Google OAuth project only if that same project has the required Ads API access level.",
        enable_api_step=SetupStep(title="Enable Google Ads API and obtain production access", description="Enable Google Ads API, then open its API Overview and apply for Explorer access to use real accounts. Explorer permits 2,880 production operations per rolling 24 hours and 15,000 test operations. Basic is needed for Keyword Planner or higher usage, but these actions do not use Keyword Planner. Developer tokens were retired on September 9, 2026; no developer token is configured in Kern.", link_url="https://console.cloud.google.com/apis/library/googleads.googleapis.com", link_label="Open Google Ads API"),
        scopes_step=SetupStep(title="Declare Google Ads permissions", description="In Google Auth Platform > Data Access, add openid, email and https://www.googleapis.com/auth/adwords. Google offers this Ads scope for both reporting and management. Kern exposes writes only through approvals. Use a Google user with direct access to the intended advertiser Ads account.", link_url="https://developers.google.com/google-ads/api/docs/oauth/overview", link_label="Review Ads authorization"),
        connect_step_description="Save the client ID and client secret below, enable Google Ads, and connect your Google account. Copy the callback URI from this guide into the OAuth client. Existing Gmail or Search Console connections do not connect Ads; authorize Ads separately. Then ask the agent to list accounts and choose the advertiser account. The connected Google user needs direct access to that advertiser account. Manager-only access is not supported. Reconnect Ads if you revoke permissions or change OAuth clients.",
        include_images=False,
    ),
    data_summary=DataSummary(cards=(
        DataSummaryCard(title="What leaves this host", description="Account IDs and report dates/filters go to Google on reads. Approved changes send names, keywords, targeting IDs, ad copy, landing URL, status, total budget and flight times. Account details, reports and disclosed search terms return to your agent and its model provider. OAuth credentials never reach the agent."),
        DataSummaryCard(title="Where it can go", description="API requests go to Google's fixed Google Ads endpoint. OAuth and identity checks use Google's fixed authentication endpoints. Google may crawl the approved landing page and serve approved ads through Google Search under your account settings."),
        DataSummaryCard(title="What Google can do with it", description="Google processes account data for reporting, ad review and delivery under its Ads terms and privacy policy. Enabling a campaign can incur advertising charges billed by Google. Launch uses a campaign total budget for the approved flight, with Google pacing spend and choosing CPC bids. There is no daily cap or maximum price per click. Google reviews eligibility and policies; enabling does not guarantee approval or delivery.", links=(DataSummaryLink("Google Ads terms", "https://ads.google.com/home/terms/"), DataSummaryLink("Campaign total budgets", "https://support.google.com/google-ads/answer/10486938"), DataSummaryLink("Google Privacy Policy", "https://policies.google.com/privacy"))),
        DataSummaryCard(title="How long Google retains it", description="Google controls account, reporting and advertising data retention. Disconnecting removes Kern's local connection; it does not delete campaigns, stop live ads or delete Google's data. Use end_campaign or stop delivery in Google Ads if you want ads to stop.", links=(DataSummaryLink("Google retention policy", "https://policies.google.com/technologies/retention"),)),
    )),
    technical_details=(
        "list_locations adds one direct local read with no approval, OAuth refresh, provider quota, network access or cost. query is optional (80 characters/320 UTF-8 bytes, trimmed, case-insensitive name substring); country_code is optional (empty or two uppercase ASCII letters); limit is optional (1 to 250, default 250). It returns name-sorted country IDs, names, codes, Country type and snapshot status, plus source_url, snapshot_date and truncated. Unknown fields fail. All inputs are locally validated and have no egress. No new configuration or OAuth scope; no reconnect needed.",
        "The bundled country data was extracted once from Google’s official 2026-08-12 geo-target ZIP: https://developers.google.com/google-ads/api/data/geotargets. Only the 219 Active Country rows are retained, with source attribution and ZIP SHA-256 in countries.json. It is a dated snapshot, not a live Google lookup. Regenerate the file deliberately from a newer official CSV when needed, selecting Active Country rows and updating date, source URL, checksum and ordering. There is no runtime download, refresh, fallback or shell allowlist requirement. Regions/cities and other target types are not included; Google validates IDs at campaign creation.",
        "Contained failures record the action, failed phase, HTTP status/request operation when available and confirmed resources in Host diagnostics. Bounded provider error responses appear only in the authenticated diagnostic detail, never Chat results. Logging does not retry or recover prior discarded errors.",
        "If paused-campaign readback finds an unexpected targeting criterion, the failed action and Host diagnostic summary show its fixed-label type, exclusion flag, status, device type and bounded bid modifier. Missing or unrecognized values are marked; raw criterion rows and arbitrary provider text are not included. This evidence does not relax targeting checks or authorize retry/resume of a failed launch.",
        "Google may return default device criteria when creating a Search campaign. Verification accepts at most one enabled, positive criterion for each known device type (desktop, mobile, tablet, connected TV or other), with an absent bid modifier or an explicit neutral 1. Device exclusions, zero/changed/invalid modifiers, unknown devices, duplicate devices and other extra criteria block activation. Location IDs must still exactly match the approved plan. Readback is bounded to 16 rows, including a truncation sentinel for at most 10 locations and 5 devices; an incomplete read blocks activation.",
        "Search campaign language criteria were retired on September 30, 2026. Ad creative and landing-page language determine matching. launch_campaign sends no language criterion. https://developers.google.com/google-ads/api/docs/deprecations",
        "Launch has 9 required fields: customer_id, name, total_budget_micros, start_time, end_time, keywords, final_url, headlines, descriptions. geo_target_ids is the only optional launch field. EU political advertising is fixed to no, shown in approval and verified before activation; this tool does not launch EU political advertising. Maximize Clicks uses Google's TARGET_SPEND strategy; no manual CPC, objective, funding, conversion or audience-expansion input. Total budgets use CUSTOM_PERIOD and total_amount_micros with start_date_time/end_date_time in account time zone. Google permits Search total budgets with Maximize Clicks. https://developers.google.com/google-ads/api/docs/campaigns/budgets/overview",
        "Times require RFC3339 seconds and explicit offset, normalize to UTC, and convert to account local time for Google. The account time zone and exact local/UTC flight are bound to approval. Expired flights, start dates before today, flights outside 3 to 90 inclusive account-local days, arbitrary time-of-day starts/ends and ambiguous daylight-saving boundaries are refused. Search uses start 00:00:00 and end 23:59:59 in the account time zone. Google validates minimum-budget eligibility. https://support.google.com/google-ads/answer/15137812",
        "All schemas reject unknown fields, including keyword objects. Names allow 128 characters/255 UTF-8 bytes; keywords 80 characters/320 bytes/10 words; headlines 30 characters/120 bytes; descriptions 90 characters/360 bytes; double-width characters count as two in ad text; URL 2048 UTF-8 bytes. API read inputs are validated IDs, dates, enums and integers. Location query text is bounded and locally validated; it is never sent to a provider. Approved input text is bound verbatim. Geo IDs default to [] and allow 0 to 10 unique entries; empty means worldwide. Micros are positive integers up to 1000000000000. Google applies policy and character counting rules.",
        "Launch execution refreshes identity, verifies live direct account access and exact currency/time-zone/flight before atomic paused creation (partialFailure=false). Identity/account access is rechecked after creation, followed by final readback of campaign budget, flight, Maximize Clicks, Search-only network, geography, group, keywords and responsive ad. Unexpired flight is rechecked before enabling the new parent. No mutation happens before approval; no auto retry, rollback or background activation. A failure reports its phase and confirmed resources; an uncertain enable can already have started spending. Inspect Google Ads before another proposal. Google controls ad review and delivery; approval permits delivery during or after review within the approved flight without another Kern action.",
        "End approval binds connected Google identity, selected customer and stable campaign ID. Execution rechecks live access and non-removed Search ownership, but allows mutable campaign name, status, budget and flight changes so stopping is not blocked by drift. It sets PAUSED only, retains history and does not refund past delivery or permanently prevent a Google Ads user from resuming.",
        "The API has no per-call usage fee. Reads and pre-approval/revalidation checks consume project-wide Explorer quota even without approval. reports_cost is false: advertising spend billed by Google and Kern runtime costs are separate from tool-call costs. Fixed REST v25 calls use 30-second timeouts and the shared 8 MiB response limit, sequentially without retries. Reads return one bounded page, up to 501 rows including the truncation sentinel; report dates are account-local and some search terms are withheld. Google enforces a 37-month daily-report lookback; Kern validates date format/order/span but leaves this rolling provider limit to Google, so an older read can fail after consuming quota. OAuth/profile calls may precede each phase. https://developers.google.com/google-ads/api/docs/oauth/cloud-project",
    ),
)
