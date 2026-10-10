"""Fixed Instagram Ads actions and their Home > Integrations guide."""

from host.param_guard import PARAM_GUARD_PROTECTION, PARAM_GUARD_TECHNICAL_DETAIL
from typing import cast

from host.tools.json_types import JSONObject, JSONValue
from host.tools.manifest import (
    ActionSpec, ConfigRequirement, DataSummary, DataSummaryCard, DataSummaryLink,
    DataSummaryPoint, SetupStep, ToolManifest, InputProtection, guarded_input, protect_inputs, validated_input,
)
from host.tools.shared import outputs

DOCS = "https://developers.facebook.com/documentation/ads-commerce/"


def _nullable(value: JSONObject) -> JSONObject:
    return outputs.nullable(value, "Null when Meta does not report this field.")


def _array(value: JSONObject, description: str = "Provider values.") -> JSONObject:
    return outputs.array_of(value, description)


ID: JSONObject = {"type": "string", "description": "Numeric Meta id, without an act_ prefix."}
PAGE: JSONObject = {
    "limit": {"type": "integer", "description": "One page of 1-20 rows; default 10."},
    "after": {"type": "string", "description": "Opaque next_cursor from the preceding page; never a URL."},
}


def input_schema(required: list[str], fields: JSONObject, *, page: bool = False) -> JSONObject:
    return {"type": "object", "properties": {**fields, **(PAGE if page else {})},
            "required": cast(list[JSONValue], required), "additionalProperties": False}


def text_fields(*names: str) -> JSONObject:
    return {name: outputs.text(name.replace("_", " ")) for name in names}


def rows_schema(row: JSONObject) -> JSONObject:
    return outputs.obj({"message": outputs.text("Read outcome and limits."),
                        "items": _array(row, "One bounded provider page."),
                        "next_cursor": _nullable(outputs.text("Next after value."))},
                       ["message", "items", "next_cursor"])


ACCOUNT = outputs.obj({**text_fields("account_id", "name", "currency", "timezone_name"),
    "account_status": _nullable(outputs.integer("Meta account status code.")),
    "disable_reason": _nullable(outputs.integer("Meta account disable reason.")),
    "user_tasks": _array(outputs.text("Granted ad account task.")),
    "billing_configured": _nullable(outputs.boolean("Meta reports a funding source; this does not guarantee delivery or available funds.")),
    "currency_offset": _nullable(outputs.integer("Meta budget units per whole currency unit, from the documented currency table.")),
    "minimum_daily_budget": _nullable(outputs.text("Account minimum in Meta budget units; provider also validates the actual flight.")),
    **{name: _nullable(outputs.text("Account's configured DSA name, when supplied."))
       for name in ("default_dsa_beneficiary", "default_dsa_payor")},
}, ["account_id", "name", "currency", "timezone_name", "account_status", "disable_reason", "user_tasks", "billing_configured", "currency_offset", "minimum_daily_budget", "default_dsa_beneficiary", "default_dsa_payor"])
IDENTITY = outputs.obj(text_fields("page_id", "page_name", "instagram_user_id", "username", "account_type"),
                       ["page_id", "page_name", "instagram_user_id", "username", "account_type"])
FIELD_STATE: JSONObject = {"type": "string", "enum": ["missing", "null", "empty", "invalid", "present"],
                          "description": "Shape of the requested provider field before normalization."}
DIAGNOSTIC_PAGE = outputs.obj({
    **{key: _nullable(outputs.text("Selected provider identity field, when it is a string."))
       for key in ("page_id", "page_name", "instagram_user_id", "username", "account_type")},
    "linked_identity_state": FIELD_STATE, "account_type_state": FIELD_STATE,
    "identity_filter_reason": {"type": "string", "enum": ["none", "linked_identity_missing", "linked_identity_null", "linked_identity_invalid", "account_type_missing", "account_type_null", "account_type_invalid", "account_type_unsupported", "invalid_id"],
                               "description": "Why the professional-identity filter would exclude this Page, or invalid_id when ids fail validation. Does not establish advertiser Page membership."},
}, ["page_id", "page_name", "instagram_user_id", "username", "account_type", "linked_identity_state", "account_type_state", "identity_filter_reason"])
DIAGNOSTIC_USER_PAGE = outputs.obj({
    **cast(JSONObject, DIAGNOSTIC_PAGE["properties"]),
    "tasks": _array(outputs.text("Facebook user's Page task, as reported by Meta; separate from ad account user_tasks.")),
    "tasks_state": FIELD_STATE,
}, [*cast(list[str], DIAGNOSTIC_PAGE["required"]), "tasks", "tasks_state"])


def diagnostic_edge(row: JSONObject) -> JSONObject:
    return outputs.obj({
        "status": {"type": "string", "enum": ["ok", "failed"]},
        "items": _array(row, "Unfiltered rows from one bounded provider page. Failed stages return no rows, not evidence of an empty edge."),
        "next_cursor": _nullable(outputs.text("Continuation for this edge only; null on failure does not establish completeness.")),
        **{key: _nullable(outputs.integer("Numeric provider error metadata only; no provider message or request values."))
           for key in ("http_status", "error_code", "error_subcode")},
    }, ["status", "items", "next_cursor", "http_status", "error_code", "error_subcode"],
       "One bounded identity lookup: status, items, next_cursor, http_status, error_code and error_subcode. Failed lookups are unavailable.")


DIAGNOSTIC = outputs.obj({
    "message": outputs.text("Evidence limits and next steps."),
    "connection": outputs.obj({**text_fields("facebook_user_id", "name"), "scopes": _array(outputs.text("Live granted OAuth permission."))}, ["facebook_user_id", "name", "scopes"], "Live Facebook user id, name and granted scopes; no credential fingerprint or token."),
    "account": ACCOUNT, "user_tasks_state": FIELD_STATE,
    "launch_task_check_passes": outputs.boolean("Whether the current Kern ADVERTISE/MANAGE guard passes; not proof of provider authorization or delivery."),
    "pages": diagnostic_edge(DIAGNOSTIC_PAGE),
    "user_pages": diagnostic_edge(DIAGNOSTIC_USER_PAGE),
    "instagram_accounts": diagnostic_edge(outputs.obj({"instagram_user_id": _nullable(outputs.text("Advertising Instagram id.")), "username": _nullable(outputs.text("Username."))}, ["instagram_user_id", "username"])),
}, ["message", "connection", "account", "user_tasks_state", "launch_task_check_passes", "pages", "user_pages", "instagram_accounts"])
POST = outputs.obj({**text_fields("media_id", "instagram_user_id", "caption", "media_url", "permalink", "timestamp"),
    "eligible_to_boost": _nullable(outputs.boolean("Provider boost eligibility, not ad review acceptance.")),
    "boost_ineligible_reason": _nullable(outputs.text("Meta's explanation, when supplied.")),
}, ["media_id", "instagram_user_id", "caption", "media_url", "permalink", "timestamp", "eligible_to_boost", "boost_ineligible_reason"])
TARGET = outputs.obj(text_fields("id", "name", "type"), ["id", "name", "type"])
CAMPAIGN = outputs.obj({**text_fields("id", "account_id", "name", "objective", "status", "effective_status"),
    "configuration_json": outputs.text("Provider campaign budget, buying type, category and issues fields. Missing fields remain absent."),
}, ["id", "account_id", "name", "objective", "status", "effective_status", "configuration_json"])
AD = outputs.obj({**text_fields("id", "name", "status", "effective_status"),
    "configuration_json": outputs.text("Ad and creative configuration and review issues, as returned by Meta."),
}, ["id", "name", "status", "effective_status", "configuration_json"])
ADSET = outputs.obj({**text_fields("id", "name", "status", "effective_status"),
    "configuration_json": outputs.text("Budget, dates, targeting, optimization, billing, DSA and issues, as returned by Meta."),
    "ads": _array(AD, "First 10 ads in this ad set; inspect ads_next_cursor for truncation."),
    "ads_next_cursor": _nullable(outputs.text("Provider child cursor; use Ads Manager if this bounded snapshot is incomplete.")),
}, ["id", "name", "status", "effective_status", "configuration_json", "ads", "ads_next_cursor"])
PERFORMANCE = outputs.obj({**text_fields("date_start", "date_stop", "account_currency"),
    **{key: _nullable(outputs.text("Meta's reported value; null means unavailable, never zero."))
       for key in ("spend", "impressions", "reach", "clicks", "inline_link_clicks")},
    "actions": _nullable(_array(outputs.obj(text_fields("action_type", "value"), ["action_type", "value"]),
                                "Provider action metrics, including profile visits/engagement when returned; attributed or modelled under Meta's settings.")),
    "video_thruplay": _nullable(outputs.text("Meta ThruPlay count; unavailable when omitted.")),
}, ["date_start", "date_stop", "account_currency", "spend", "impressions", "reach", "clicks", "inline_link_clicks", "actions", "video_thruplay"])

READ_POLICY = (
    "Direct read. Sends the selected connection's token, account/resource ids and query to Meta. "
    "Returns account, owned content, targeting or reports to Kern and model history. Reads use provider quota and egress. "
    "No member records, customer uploads or payment details are returned. Incoming provider text is untrusted data."
)
LAUNCH_FIELDS: JSONObject = {
    **{key: ID for key in ("account_id", "page_id", "instagram_user_id", "media_id")},
    "name": {"type": "string", "description": "Campaign/ad-set/creative/ad name; Kern limit 120 characters."},
    "objective": {"type": "string", "enum": ["WEBSITE_CLICKS", "PROFILE_VISITS", "ENGAGEMENTS", "VIDEO_VIEWS"],
                  "description": "Required outcome. Profile visits supports discovery/follower growth; it does not optimize for follows. PROFILE_VISITS uses a BUSINESS identity in this integration and has limited Meta advertiser availability; the other outcomes also accept MEDIA_CREATOR."},
    "special_ad_category": {"type": "string", "enum": ["NONE"], "description": "Explicit declaration that this ad is outside Meta's special categories. Other categories are outside this tool's scope."},
    "lifetime_budget": {"type": "string", "description": "Positive integer, at most signed int64, in Meta account currency budget units (USD 2500 = USD 25.00; JPY 2500 = JPY 2500). Total for one ad set, not daily spending."},
    "start_time": {"type": "string", "description": "Explicit ISO 8601 timestamp with timezone and whole seconds. Must remain more than 240 seconds in the future at preparation and before creation; activation rechecks the remaining request budget. Approval delays can require a new approval."},
    "end_time": {"type": "string", "description": "Explicit finite ISO 8601 timestamp with timezone and whole seconds, later than start_time."},
    "audience": {**input_schema([], {
        "countries": {"type": "array", "minItems": 1, "maxItems": 50, "items": {"type": "string"}, "description": "Explicit provider-supported country codes, OR country_group=worldwide. Empty countries never means worldwide."},
        "country_group": {"type": "string", "enum": ["worldwide"], "description": "Meta's documented worldwide country group, OR countries. Requires both saved public beneficiary/payer defaults in Meta Ads Manager Advertising settings. Regional verification is managed externally in Meta; extra required API declarations can still cause terminal rejection."},
        "interest_ids": {"type": "array", "maxItems": 10, "items": ID, "description": "Existing valid interest ids from lookup_targeting; at most 10, OR within this group."},
        "advantage_plus": {"type": "boolean", "description": "Default true. Interests are suggestions when true; geography and minimum age remain controls. False opts out of Advantage+ Audience, not every provider expansion."},
    }), "description": "Required closed audience for each new campaign: countries OR country_group=worldwide; optional interest_ids (up to 10); advantage_plus defaults true. Fresh ad-set targeting, without saved custom/lookalike audience objects."},
    "destination_url": {"type": "string", "description": "Required only for WEBSITE_CLICKS. Exact HTTPS destination with a fixed Learn More CTA; no Instagram/Facebook profile URLs or credentials."},
}
LAUNCH_REQUIRED = [key for key in LAUNCH_FIELDS if key != "destination_url"]

_ACTIONS = (
    ActionSpec("list_accounts", "List one page of ad accounts accessible to this Facebook user. Select account_id explicitly for every account action.", READ_POLICY,
               input_schema([], {}, page=True), rows_schema(ACCOUNT)),
    ActionSpec("get_account", "Read account currency, timezone, status, granted tasks and available billing/default DSA facts. Does not configure billing.", READ_POLICY,
               input_schema(["account_id"], {"account_id": ID}), ACCOUNT),
    ActionSpec("diagnose_account", "Compare previously promoted advertiser Pages with the Facebook user's Pages and Page tasks, alongside live identity/scopes, account task shape and connected advertising Instagram accounts. Each edge is independently bounded; safe numeric failures remain separate from empty results. No ad creation or spend test.", READ_POLICY,
               input_schema(["account_id"], {"account_id": ID, "limit": PAGE["limit"],
                   "pages_after": PAGE["after"], "instagram_after": PAGE["after"], "user_pages_after": PAGE["after"]}), DIAGNOSTIC),
    ActionSpec("list_identities", "List one page of user Pages with advertising tasks and linked professional Instagram accounts connected to the selected advertiser. Launch rechecks Page tasks, linkage and advertiser access.", READ_POLICY,
               input_schema(["account_id"], {"account_id": ID}, page=True), rows_schema(IDENTITY)),
    ActionSpec("list_posts", "List owned video Reels from one bounded media page. Non-Reel entries are filtered; an empty page may still have a next cursor. Readability does not prove ad review acceptance.", READ_POLICY,
               input_schema(["account_id", "page_id", "instagram_user_id"], {key: ID for key in ("account_id", "page_id", "instagram_user_id")}, page=True), rows_schema(POST)),
    ActionSpec("lookup_targeting", "Find provider country, worldwide country-group or interest ids. Query is 1-100 ASCII characters and uses Kern's parameter guard.", READ_POLICY,
               input_schema(["account_id", "type", "query"], {"account_id": ID,
                   "type": {"type": "string", "enum": ["COUNTRY", "COUNTRY_GROUP", "INTEREST"], "description": "Required provider lookup category."},
                   "query": {"type": "string", "description": "Required guarded 1-100 character ASCII search term."}}, page=True), rows_schema(TARGET)),
    ActionSpec("list_campaigns", "List one page of campaigns and configured/effective delivery status.", READ_POLICY,
               input_schema(["account_id"], {"account_id": ID}, page=True), rows_schema(CAMPAIGN)),
    ActionSpec("get_campaign", "Read campaign and one page of 1-20 ad sets, with first 10 ads/creatives per set. Child cursors explicitly mark incomplete snapshots; use Ads Manager for remaining ads.", READ_POLICY,
               input_schema(["account_id", "campaign_id"], {"account_id": ID, "campaign_id": ID}, page=True),
               outputs.obj({"campaign": CAMPAIGN, "adsets": _array(ADSET),
                            "next_cursor": _nullable(outputs.text("Next ad-set cursor."))}, ["campaign", "adsets", "next_cursor"])),
    ActionSpec("get_performance", "Read one page of campaign daily Insights for a 1-90 day range, in the account timezone. Missing metrics stay unavailable; reach and actions may be estimated/modelled/attributed. Never infer attributed follows from follower changes.", READ_POLICY,
               input_schema(["account_id", "campaign_id", "start_date", "end_date"], {"account_id": ID, "campaign_id": ID,
                   **{key: {"type": "string", "description": "Required YYYY-MM-DD date; range must be 1-90 inclusive days."} for key in ("start_date", "end_date")}}, page=True), rows_schema(PERFORMANCE)),
    ActionSpec("launch_campaign", "Queue one exact approval to create a NEW campaign, ONE ad set, ONE existing owned Reel creative and ONE ad, then verify and activate. Instagram Reels only. Automatic bidding, impression billing, finite lifetime budget and flight. No existing-campaign resume.",
               "Before approval only bounded reads. Approval binds credential/access, account/currency, Page/Instagram, source Reel/caption/media/permalink, CTA/destination, outcome, audience, budget, flight and DSA. After revalidation creates paused, verifies, activates children under paused parent, revalidates and activates parent last. Review may delay and later release delivery within the approved flight. ACTIVE is not delivery. Failed/ambiguous writes stop with confirmed ids and uncertain stage; inspect Ads Manager before another launch. Meta bills ad spend separately from Kern Tools cost.",
               input_schema(LAUNCH_REQUIRED, LAUNCH_FIELDS), approval="operator"),
    ActionSpec("end_campaign", "Queue approval to pause the campaign parent and retain history/reports. No Kern resume, update or delete. Ads Manager can resume it; stopping can be delayed and past delivery remains billable.",
               "Approval binds the selected credential and stable ad account/campaign identity. Rechecks credential/access and ownership before one parent pause and readback. Does not bind volatile status. Meta retains reports and billing history; past delivery remains billable.",
               input_schema(["account_id", "campaign_id"], {"account_id": ID, "campaign_id": ID}), approval="operator"),
)
_PROTECTIONS: dict[str, dict[str, InputProtection]] = {}
for _action in _ACTIONS:
    if _action.approval == "direct":
        _PROTECTIONS[_action.id] = {
            key: guarded_input(allow_machine_tokens=True) if key in {"after", "pages_after", "instagram_after", "user_pages_after"} else
                 guarded_input() if key == "query" else
                 validated_input("Closed enum, bounded integer/date, or numeric provider id; validated before requests.")
            for key in cast(JSONObject, _action.input_schema["properties"])
        }

LIFECYCLE = "One launch approval covers creation, readback verification and activation. Creates a new campaign, ad set and ad PAUSED. The full campaign/ad-set/creative/ad hierarchy must be complete and associated, with exact identity, source Reel, goal, destination, audience, budget, flight and provider configuration read back before children become ACTIVE under the paused parent. Revalidates resources, verifies that complete configuration again, and activates parent last. Missing setup, incomplete resources, provider errors or unavailable required verification readbacks stop before any parent ACTIVE write. This is a complete configured hierarchy; Meta review and delivery remain asynchronous. Additional landing-page optimization and high-demand budget scheduling are excluded. Creates the ad set with is_budget_schedule_enabled=false and requires explicit false at both paused verification barriers. Budget-flag and existing-Reel destination_spec serialization have not been verified live: Meta may omit disabled or unset optional fields. Missing/null budget scheduling state or omitted destination configuration stops activation with confirmed hierarchy IDs under the paused parent. These conservative checks may reject an otherwise valid provider state. Flight start must remain more than 240 seconds away before creation, and cover the remaining request budget before parent activation. Provider review may later release delivery within the approved flight; configured ACTIVE does not prove accepted review or delivery."
BUDGET_AUDIENCE = "All outcomes bill impressions with LOWEST_COST_WITHOUT_CAP; clicks, profile visits, interactions and ThruPlay are optimization outcomes, not charged events. Lifetime budget uses Meta's documented currency offset. Provider minimums and regional rules apply; no fixed daily-spend promise. Geography includes people living in or recently in the selected countries, Meta's documented home/recent location types. Numeric age bounds are Meta's documented defaults 18/65; with Advantage+ the same age range is a suggestion, while geography and minimum age remain controls."

MANIFEST = ToolManifest(
    tool_id="instagram_ads", display_name="Instagram Ads", connection="oauth",
    description="Promote an existing owned professional Instagram Reel with an exact approved total budget and flight. Four outcomes; Instagram Reels only. Separate Meta Marketing API login.",
    actions=protect_inputs(_ACTIONS, _PROTECTIONS),
    config=(ConfigRequirement("INSTAGRAM_ADS_APP_ID", "Meta Business app ID with Marketing API and Facebook Login enabled."),
            ConfigRequirement("INSTAGRAM_ADS_APP_SECRET", "That Meta app's secret; separate from organic Instagram Login.")),
    technical_details=(PARAM_GUARD_TECHNICAL_DETAIL, LIFECYCLE, BUDGET_AUDIENCE,
        "list_identities makes five fixed GETs: live Facebook identity, permissions, selected account, one /me/accounts page and up to 100 connected Instagram accounts. The returned cursor continues /me/accounts. No Page access_token is requested or returned. diagnose_account makes up to six fixed GETs: live Facebook identity, permissions, selected account, promote_pages, connected_instagram_accounts and /me/accounts. Each edge has an independent limit of 1-20 (default 10) and optional guarded pages_after/instagram_after/user_pages_after cursor. User Pages request only id, name, tasks and linked Instagram id/username/account_type, never a Page access_token. Only numeric error metadata enters results; provider error detail stays in authenticated Host diagnostics. Auth expiry/revocation aborts the action rather than continuing. This diagnostic uses the same six required permissions as the other ads actions; it adds no separate scopes or app configuration.",
        "Contained failures record the action, failed phase, HTTP status/request operation when available and confirmed IDs in Host diagnostics. Bounded provider error responses appear only in the authenticated diagnostic detail, never Chat results. Logging does not retry or recover prior discarded errors."),
    protections=(PARAM_GUARD_PROTECTION,
        "Exactly two approval writes: new launch and parent pause. Unknown fields are rejected at every input object. No generic provider JSON/endpoints, uploads, payment setup, updates, resume, deletion, automatic retries or recovery actions.",
        LIFECYCLE,
        "Owned video Reels only. Fixed publisher_platforms=instagram and instagram_positions=reels. The original Reel stays unchanged. Readback rejects opted-in creative enhancements. Meta boost eligibility and ad review still apply: eligible_to_boost and configured ACTIVE do not prove impressions or acceptance.",
        BUDGET_AUDIENCE,
        "Each launch supplies fresh targeting for its new ad set: countries OR Meta's worldwide country_group and optional interest_ids, OR within the interest group. Advantage+ Audience defaults true, making interests suggestions; geography and minimum age 18 are controls. Meta may apply additional expansion. No saved custom/lookalike audience creation, reuse or storage, customer uploads or Pixel setup.",
        "Only caller-declared special_ad_category=NONE. Choose explicit countries or country_group=worldwide. EU/associated territories and worldwide require both actual default_dsa_beneficiary and default_dsa_payor saved on the selected Meta ad account. No DSA caller inputs. Exact resolved public names are visible and approval-bound, rechecked before creation and parent activation, and sent only for required geography. Missing defaults stop before creation with setup guidance; changed defaults invalidate the old approval. The operator manages regional verification/setup externally in Meta. That does not guarantee required per-request API declarations are supplied. Additional regional identity declarations are not sent by this tool; Meta can reject creation. The failure is terminal with provider codes when supplied, confirmed partial IDs and the stage; the parent remains paused when rejection precedes its activation. No retry, fallback or geographic exclusions.",
        "Each launch returns confirmed ids even when later work fails. A timeout or unclear response is terminal; inspect Meta before another launch. No rollback or background activation. Another Ads Manager user can alter or resume delivery.",
    ),
    setup_steps=(
        SetupStep("Prepare the advertiser", "Use a real Business or Creator Instagram profile linked to a Facebook Page. In Meta Ads Manager assign the Facebook user advertising access to the intended ad account, Page and Instagram identity, configure billing, and complete regional verification/setup there. Both explicit countries and worldwide targeting remain available. External verification does not ensure additional required API declarations are supplied by this tool; a provider rejection stops with confirmed partial IDs under the paused parent. PROFILE_VISITS is scoped to Business identities here, following the dedicated guide; Creator identities can use the other three outcomes. Meta also limits advertiser access to profile visits. No Page-backed, anonymous or partnership identity.", link_url="https://business.facebook.com/adsmanager/", link_label="Meta Ads Manager"),
        SetupStep("Save public beneficiary and payer defaults", "For EU countries, associated territories or worldwide targeting, set both accurate public beneficiary and payer defaults on the selected ad account in Meta Ads Manager > Advertising settings. Use the real person or organization that benefits from the ad and the one paying for it; these names may be public. Kern reads default_dsa_beneficiary and default_dsa_payor separately, shows the exact resolved names in approval and sends them only for this required geography. Missing defaults stop before creation; a later default change requires a new approval. This is account setup, with no launch caller fields or setup writes from Kern.", link_url="https://business.facebook.com/adsmanager/", link_label="Meta Ads Manager"),
        SetupStep("Create the Meta app", "Create a Meta Business app, enable Marketing API and Facebook Login, and request ads_management, ads_read, pages_show_list, pages_read_engagement, instagram_basic and business_management. The business_management permission supplies business portfolio asset access for Page discovery; it permits broader business asset reads and writes at Meta, while Kern exposes only the fixed actions listed here. User access tokens serve these fixed account, Page, media and Insights reads; read_insights is not requested. App-role/own-advertiser use can use Standard permission access. Other advertisers require applicable Advanced permissions, App Review and Business Verification. Marketing API rate-limit tiers are separately called Limited/Full Access.", link_url=DOCS + "marketing-api/overview/authorization", link_label="Meta authorization requirements"),
        SetupStep("Register this callback", "Add the exact callback shown below to Facebook Login's Valid OAuth Redirect URIs. Save the app ID and secret below in Home > Integrations > Instagram Ads. These are separate from the working organic Instagram connection.", show_callback=True, show_config=True),
        SetupStep("Allow and connect", "Enable Instagram Ads, choose Connect and authorize the intended Facebook user and exact Page/Instagram assets. Kern exchanges the user token for a long-lived token and verifies all six required scopes. Existing ads connections created without business_management must Connect again after that permission is enabled on the Meta app; the organic Instagram connection stays separate. Every direct read also checks the current connected Facebook identity and required grants; remote scope revocation requires Connect again before account data is read. Select connection_id when several users are connected; select account_id on every account action. Expired, revoked or insufficient grants require Connect again. Both approved writes require more than 240 seconds of remaining token lifetime at execution. A reconnect or app credential change invalidates existing approvals.", link_url="https://developers.facebook.com/documentation/facebook-login/guides/access-tokens/get-long-lived", link_label="Long-lived user tokens"),
        SetupStep("Diagnose empty access results", "If tasks or Instagram identities are empty, use diagnose_account for the selected advertiser. It distinguishes missing, null, empty and invalid task fields and shows previously promoted advertiser Pages before filtering, with the reason each Page is excluded. Compare pages (promotion history, which may be empty before a first ad) with user_pages (the connected Facebook user's Pages, including Page tasks and linked Instagram identities). User Page visibility does not establish permission to advertise through the selected account. Connected advertising Instagram accounts are a separate result. A failed lookup is unavailable, not an empty list. Each edge returns its own next_cursor; continue with pages_after, user_pages_after or instagram_after and an optional limit of 1-20 (default 10). Only the selected edge advances. Missing fields alone do not prove missing Meta permissions, so compare this evidence with Meta's asset access screen before changing setup. This read does not create ads, incur ad spend or establish delivery."),
        SetupStep("Verify the proposed promotion", "Use account, identity, owned Reel and targeting reads, then choose one outcome, fresh geography/optional interests and a finite budget/flight for the new campaign. WEBSITE_CLICKS adds an exact HTTPS Learn More destination. PROFILE_VISITS uses View Instagram Profile for the selected identity, not follow optimization, and is in Meta's limited API rollout. Additional landing-page optimization is excluded. Existing-Reel destination configuration and disabled-budget-scheduling readback have not been verified live; Meta may omit disabled or unset optional fields, causing this conservative tool to stop with a paused hierarchy even for an otherwise valid provider state. Review the full approval payload and visible source Reel before approving. Identity discovery pages through the Facebook user's Pages, requiring advertising tasks and a professional linked Instagram identity present in the advertiser's first 100 connected Instagram accounts. Empty filtered results may still have a user-Page cursor. Launch rechecks Page ADVERTISE/MANAGE (including corresponding PROFILE_PLUS tasks), exact Page-to-Instagram linkage, connected Instagram membership and ad-account ADVERTISE/MANAGE before creation and parent activation. promote_pages is historical evidence, never launch authorization. Account/identity membership checks read at most 100 rows and do not silently page beyond that bound.", link_url=DOCS + "instagram/ads-api/guides/use-posts-as-ads", link_label="Existing-post ads and eligibility"),
    ),
    data_summary=DataSummary(cards=(
        DataSummaryCard("What leaves this host", points=(DataSummaryPoint("Reads", "Facebook token, advertiser/Page/Instagram/media ids, bounded lookup terms and report dates go to Meta; Facebook identity and granted scopes, user Page identities/tasks, account/content/targeting/report results enter Kern and model history. User Pages may include Pages outside the selected advertiser."), DataSummaryPoint("Approved launch", "Exact source Reel, Instagram/Page identity, CTA/destination, campaign hierarchy, audience, budget, flight and public DSA names go to Meta only after approval."))),
        DataSummaryCard("Where it can go", description="Meta Graph/Marketing API and the selected advertiser. Approved content may be shown to the approved audience, including provider-expanded suggestions. Reads consume quota and egress. Meta ad spend is separate from Kern Tools cost accounting; this tool does not configure payment."),
        DataSummaryCard("What Meta can do with it", description="Operate advertising, policy review, audience selection, performance attribution and advertiser billing, under Meta's terms.", links=(DataSummaryLink("Meta Privacy Policy", "https://www.facebook.com/privacy/policy/"), DataSummaryLink("Meta Platform Terms", "https://developers.facebook.com/terms/"))),
        DataSummaryCard("How long it is retained", description="Meta retains account, advertising, reporting and billing records under its policies. Pausing retains history. Kern retains tool results and approvals in its own history; disconnecting removes Kern's saved credential and does not erase Meta ads or records.", links=(DataSummaryLink("Meta Privacy Policy", "https://www.facebook.com/privacy/policy/"),)),
    )),
    agent_notes="Use the dedicated ads login and select the advertiser and real Page-linked Instagram identity explicitly. Use diagnose_account for empty task/identity results: omitted fields and filtered Pages do not prove the operator lacks Meta access. Compare pages (advertiser) with user_pages (Facebook user); user Page visibility/tasks do not authorize a launch through the selected advertiser. Inspect each edge status and next_cursor before drawing conclusions; pagination is independent. Organic Instagram Login cannot access ads and remains separate. All four outcomes use owned video Reels and Reels-only placement; profile visits requires a Business identity here and has limited advertiser availability and is not follows. Missing report metrics are unavailable, never zero. Preapproval is read-only. A partial/uncertain launch is terminal: send confirmed ids/stage to the operator and inspect Ads Manager, never retry blindly or resume. ACTIVE does not prove delivery. Only launch_campaign and end_campaign write, with approval.",
)
