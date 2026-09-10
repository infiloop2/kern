"""Read-only, Hobby-compatible Vercel Web Analytics and project discovery."""
from __future__ import annotations

import re
from datetime import date
from typing import cast

from host.param_guard import PARAM_GUARD_PROTECTION, PARAM_GUARD_TECHNICAL_DETAIL, ParamGuardDenied
from host.tools.host_api import HostAPI
from host.tools.json_types import JSONObject, JSONValue
from host.tools.manifest import ActionSpec, ConfigRequirement, DataSummary, DataSummaryCard, DataSummaryLink, SetupStep, ToolManifest
from host.tools.results import ActionExecuted, ActionFailed, ActionResult
from host.tools.shared import outputs
from host.tools.shared.inputs import ToolInputValidationError, int_field, schema, decoded_url_component_values
from host.tools.shared.web import json_request, encode_query, WebRequestError, transport_or_unmapped_provider_error
from host.tools.tool import Tool

DOCS = "https://vercel.com/docs/analytics/web-analytics-api"
PRIVACY = "https://vercel.com/legal/privacy-notice"
PRICING = "https://vercel.com/docs/analytics/limits-and-pricing"
ENDPOINTS = {
    "list_teams": "https://api.vercel.com/v2/teams",
    "list_projects": "https://api.vercel.com/v10/projects",
    "query_visits": "https://api.vercel.com/v1/query/web-analytics/visits/aggregate",
}
DIMENSIONS = ("day", "requestPath", "route", "country", "referrerHostname", "deviceType", "browserName", "osName")
ID_RE = re.compile(r"prj_[A-Za-z0-9]{1,64}\Z")
TEAM_RE = re.compile(r"team_[A-Za-z0-9]{1,64}\Z")
# Vercel project pagination returns millisecond timestamps or Base32 continuation tokens.
CURSOR_RE = re.compile(r"[A-Za-z0-9=]{1,1024}\Z")
MAX_ROWS = 100
TEAM_FIELD = outputs.text("Optional team ID from list_teams or list_projects. Omit for personal projects. Vercel enforces token access.")
QUERY_SCHEMA = schema({
    "project_id": outputs.text("Project ID from list_projects, in prj_ format. Vercel checks the token's access on every request."),
    "team_id": TEAM_FIELD,
    "start_date": outputs.text("First UTC date, YYYY-MM-DD, inclusive. Hobby retains one month of reports."),
    "end_date": outputs.text("Last UTC date, YYYY-MM-DD, inclusive; at most 31 dates per query."),
    "group_by": {"type": "string", "enum": list(DIMENSIONS), "description": "One Hobby-compatible breakdown; default day. Use requestPath for paths such as /snowbid."},
    "limit": outputs.text("1-50 top dimension values, default 10. For day grouping, Kern raises this to cover every requested date. Vercel may add an Others group."),
    "path": outputs.text("Optional exact page path, up to 256 characters, without query or fragment; checked by the parameter guard."),
}, ["project_id", "start_date", "end_date"])
DISCOVERY_SCHEMAS = {
    "list_teams": schema({"cursor": outputs.text("Optional next_cursor from list_teams, a millisecond timestamp. One page of up to 20 teams per call.")}),
    "list_projects": schema({"team_id": TEAM_FIELD, "cursor": outputs.text("Optional next_cursor from list_projects for the same team. One page of up to 20 projects per call.")}),
}


def _discovery_output(projects: bool) -> JSONObject:
    key = "projects" if projects else "teams"
    props: JSONObject = {"id": outputs.text("Vercel resource ID."), "name": outputs.text("Resource name.")}
    if projects:
        props["team_id"] = outputs.nullable(outputs.text("Owning team ID; pass with project_id when querying."), "Null for personal projects.")
    return outputs.obj({
        key: outputs.array_of(outputs.obj(props, list(props)), "Up to 20 accessible resources; other provider fields are discarded."),
        "next_cursor": outputs.nullable(outputs.text("Pass unchanged for the next page in the same scope."), "Null when no further page exists."),
    }, [key, "next_cursor"])


MANIFEST = ToolManifest(
    tool_id="vercel_analytics",
    display_name="Vercel Analytics",
    description="Discover your Vercel projects and read production page views, visitors, daily trends and path breakdowns. Works with Hobby Web Analytics.",
    connection="enable_only",
    actions=(
        ActionSpec(id="list_teams", description="Discover teams accessible to the configured token, one page at a time.", data_policy="Sends the token and optional pagination timestamp to Vercel in one GET. Returns only team IDs, names and a pagination cursor to the agent. Runs directly.", input_schema=DISCOVERY_SCHEMAS["list_teams"], output_schema=_discovery_output(False)),
        ActionSpec(id="list_projects", description="Discover accessible projects in a team or personal scope, one page at a time. No manual project configuration.", data_policy="Sends the token, optional team ID and pagination cursor to Vercel in one GET. Returns only project IDs, names, owning team IDs and the next cursor; drops deployment, environment and other project settings. Runs directly.", input_schema=DISCOVERY_SCHEMAS["list_projects"], output_schema=_discovery_output(True)),
        ActionSpec(id="query_visits", description="Query production page views and visitors over up to 31 UTC dates, grouped by a Hobby-compatible dimension.", data_policy="Sends the token, project/team IDs, dates, breakdown, limit and guarded exact path filter to Vercel in one read-only request. Aggregate results become available to the agent and its model provider. Runs directly without approval; no writes or event collection.", input_schema=QUERY_SCHEMA, output_schema=outputs.obj({
            "message": outputs.text("Aggregation and reporting-window caveats."),
            "project_id": outputs.text("Queried project ID."),
            "start_date": outputs.text("First UTC date, inclusive."),
            "end_date": outputs.text("Last UTC date, inclusive."),
            "group_by": outputs.text("Requested breakdown dimension."),
            "rows": outputs.array_of(outputs.obj({
                "value": outputs.nullable(outputs.text("Dimension value, or timestamp for day."), "Null for unknown values."),
                "pageviews": outputs.integer("Page views."),
                "visitors": outputs.integer("Visitors in this group; not additive across groups."),
            }, ["value", "pageviews", "visitors"]), "At most 100 aggregate rows; never raw events."),
        }, ["message", "project_id", "start_date", "end_date", "group_by", "rows"])),
    ),
    config=(ConfigRequirement("VERCEL_ACCESS_TOKEN", "Vercel access token. Use the narrowest scope and an expiry; stored write-only by Kern. Projects are discovered automatically."),),
    protections=(
        "Read-only by construction: fixed GET endpoints for team/project discovery and visit aggregates. No deployments, settings, billing changes, raw logs, or event writes.",
        "Vercel enforces the token's project permissions on every request. IDs have strict formats; agents cannot supply URLs or raw query expressions. Access includes all projects the token permits.",
        "One request per action: up to 20 teams/projects per page, at most 31 dates, top 1-50 dimension values and at most 100 aggregate rows. No automatic retries or pagination; redirects are refused.",
        "The token stays in write-only host config and is never returned to the agent. These Kern restrictions do not reduce the token's permissions outside this tool.",
        PARAM_GUARD_PROTECTION,
    ),
    technical_details=(PARAM_GUARD_TECHNICAL_DETAIL, "GET api.vercel.com/v2/teams, /v10/projects and /v1/query/web-analytics/visits/aggregate with bearer authentication. Exact path filters are guarded, escaped and constructed by Kern; production is forced. Only discovery IDs/names/cursors and selected aggregate fields reach the agent. No custom event or UTM queries."),
    setup_steps=(
        SetupStep("Enable Web Analytics in Vercel", "Enable Web Analytics for your project and install its tracking package. Hobby supports page views, visitors and standard breakdowns with a one-month reporting window. This connection reads existing data; it does not add tracking.", DOCS, "Vercel Web Analytics API"),
        SetupStep("Create a scoped access token", "In your personal Vercel account settings, create a token with the narrowest available scope and an expiry. An ordinary team-scoped token is not an analytics-only credential. Kern enforces read-only queries, but anyone holding the token can use its underlying permissions outside Kern.", "https://vercel.com/account/tokens", "Open Vercel tokens"),
        SetupStep("Save your token", "Save the access token in Configuration above. The agent discovers accessible teams and projects through Vercel; you do not need project IDs or a JSON allowlist.", show_config=True),
        SetupStep("Enable and check a report", "Enable Vercel Analytics here, then ask the agent to find your project and report yesterday's page views by path. Compare with Vercel's production dashboard using the same UTC dates. A saved token is not proof that access works."),
    ),
    data_summary=DataSummary(cards=(
        DataSummaryCard("What leaves this host", "Your token, selected project/team IDs, pagination cursors, date range, breakdown, limit and optional guarded path filter go to Vercel. Project/team names and IDs and aggregate results become available to the agent and its selected model provider. Returned paths may contain sensitive information recorded by your site."),
        DataSummaryCard("Where it can go", "Only fixed discovery and Web Analytics endpoints on api.vercel.com receive requests. The tool does not contact your website, follow redirects, or forward the token to the agent.", links=(DataSummaryLink("Web Analytics API", DOCS),)),
        DataSummaryCard("What Vercel can do with it", "Vercel processes requests to provide and secure its services under your account agreement and privacy notice, including use of service providers and legally required disclosures. This integration does not establish a separate no-training or zero-retention agreement.", links=(DataSummaryLink("Vercel privacy notice", PRIVACY), DataSummaryLink("Vercel Web Analytics privacy", "https://vercel.com/docs/analytics/privacy-policy"))),
        DataSummaryCard("How long Vercel retains it", "Hobby provides one month of analytics reports. A reporting window is not a deletion guarantee for API request or security records; Vercel's privacy notice does not specify a separate fixed lifetime for these API queries. Disabling this tool stops future queries, not Vercel's site tracking or retained data.", links=(DataSummaryLink("Reporting windows and pricing", PRICING), DataSummaryLink("Vercel retention policy", PRIVACY))),
    )),
    agent_notes="Discover teams with list_teams, then list_projects for relevant team IDs; omit team_id to check personal projects. Follow next_cursor in the same scope until null when asked for all resources. Do not ask the operator to configure project IDs. Use returned project IDs and owning team IDs for query_visits. Vercel enforces token access; do not retry denied requests in other scopes to evade it. Use recent UTC dates within Hobby's one-month reporting window (inclusive, max 31 dates). group_by=day gives trends; requestPath gives page breakdowns; path=/snowbid filters to that page. Only production is queried. Pageviews can be summed across returned groups, including Others; visitors cannot be summed into unique visitors. Empty data may mean incomplete tracking or retention. No custom events, UTM dimensions, raw logs, writes or arbitrary OData filters. Never request tokens in chat; configuration belongs in Home > Integrations.",
)


def _id(value: JSONValue, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ToolInputValidationError(f"Vercel Analytics {label} must be a valid ID from discovery.")
    return value


def _cursor(value: JSONValue, teams: bool) -> str:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        value = str(value)
    pattern = r"[0-9]{1,16}" if teams else CURSOR_RE.pattern
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise ToolInputValidationError("Vercel Analytics cursor must be a valid next_cursor from the same discovery action.")
    return value


def _date(tool_input: JSONObject, key: str) -> date:
    raw = tool_input.get(key)
    if not isinstance(raw, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", raw):
        raise ToolInputValidationError(f"Vercel Analytics {key} must be YYYY-MM-DD.")
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise ToolInputValidationError(f"Vercel Analytics {key} must be a valid date.") from None


def _filter(tool_input: JSONObject, api: HostAPI) -> str:
    raw = tool_input.get("path")
    if not isinstance(raw, str) or not raw.startswith("/") or raw.startswith("//") or len(raw) > 256 or any(ord(c) < 32 or ord(c) == 127 or c in "?#\\" for c in raw):
        raise ToolInputValidationError("Vercel Analytics path must be a page path up to 256 characters without a query, fragment or control characters.")
    value = api.outbound.guard_request_parameter_string(raw)
    for decoded in decoded_url_component_values(value, plus=False):
        api.outbound.guard_request_parameter_string(decoded)
    return value.replace("'", "''")


def _request(action: str, tool_input: JSONObject, api: HostAPI) -> dict[str, str]:
    shape = QUERY_SCHEMA if action == "query_visits" else DISCOVERY_SCHEMAS[action]
    if set(tool_input) - set(cast(dict, shape["properties"])):
        raise ToolInputValidationError("Vercel Analytics request contains unsupported fields.")
    params: dict[str, str] = {}
    if "team_id" in tool_input:
        params["teamId"] = _id(tool_input["team_id"], TEAM_RE, "team_id")
    if action != "query_visits":
        params["limit"] = "20"
        if "cursor" in tool_input:
            cursor = _cursor(tool_input["cursor"], action == "list_teams")
            if action == "list_projects":
                cursor = api.outbound.guard_request_parameter_string(cursor, allow_identifiers=True, allow_machine_tokens=True)
            params["until" if action == "list_teams" else "from"] = cursor
        return params
    params["projectId"] = _id(tool_input.get("project_id"), ID_RE, "project_id")
    start, end = _date(tool_input, "start_date"), _date(tool_input, "end_date")
    if not 0 <= (end - start).days < 31:
        raise ToolInputValidationError("Vercel Analytics date range must cover 1-31 days in chronological order.")
    group = tool_input.get("group_by", "day")
    if not isinstance(group, str) or group not in DIMENSIONS:
        raise ToolInputValidationError("Vercel Analytics group_by is not a supported Hobby dimension.")
    limit = int_field(tool_input, "limit", provider="Vercel Analytics", default=10, low=1, high=50)
    if group == "day":
        limit = max(limit, (end - start).days + 1)
    filters = ["environment eq 'production'"]
    if "path" in tool_input:
        filters.append(f"requestPath eq '{_filter(tool_input, api)}'")
    params.update({"since": f"{start.isoformat()}T00:00:00.000Z", "until": f"{end.isoformat()}T23:59:59.999Z", "by": group, "limit": str(limit), "filter": " and ".join(filters)})
    return params


def _discovery(response: JSONObject, projects: bool, requested_team: str | None) -> JSONObject:
    key = "projects" if projects else "teams"
    raw = response.get(key)
    pagination = response.get("pagination")
    if not isinstance(raw, list) or len(raw) > 20 or not isinstance(pagination, dict) or "next" not in pagination:
        raise ToolInputValidationError("Vercel returned invalid discovery data or pagination.")
    next_value = pagination["next"]
    next_cursor = None if next_value is None else _cursor(next_value, not projects)
    rows: list[JSONValue] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ToolInputValidationError("Vercel returned an invalid discovery item.")
        resource_id = _id(item.get("id"), ID_RE if projects else TEAM_RE, "resource ID")
        name = item.get("name")
        if not isinstance(name, str) or len(name) > 256:
            raise ToolInputValidationError("Vercel returned an invalid resource name.")
        row: JSONObject = {"id": resource_id, "name": name}
        if projects:
            account = item.get("accountId")
            team = _id(account, TEAM_RE, "owner team ID") if isinstance(account, str) and account.startswith("team_") else requested_team
            if requested_team and team != requested_team:
                raise ToolInputValidationError("Vercel returned a project from an unexpected team.")
            row["team_id"] = team
        rows.append(row)
    return {key: rows, "next_cursor": next_cursor}


def _rows(response: JSONObject, group: str) -> list[JSONValue]:
    raw = response.get("data")
    if not isinstance(raw, list) or len(raw) > MAX_ROWS:
        raise ToolInputValidationError("Vercel Analytics returned invalid or excessive aggregate rows.")
    rows: list[JSONValue] = []
    for row in raw:
        if not isinstance(row, dict):
            raise ToolInputValidationError("Vercel Analytics returned an invalid aggregate row.")
        value = row.get("timestamp" if group == "day" else group)
        if value is not None and (not isinstance(value, str) or len(value) > 1024):
            raise ToolInputValidationError("Vercel Analytics returned an invalid dimension value.")
        for field in ("pageviews", "visitors"):
            number = row.get(field)
            if not isinstance(number, int) or isinstance(number, bool) or not 0 <= number <= 2**53 - 1:
                raise ToolInputValidationError("Vercel Analytics returned invalid aggregate metrics.")
        rows.append({"value": value, "pageviews": row["pageviews"], "visitors": row["visitors"]})
    return rows


class VercelAnalyticsTool(Tool):
    @property
    def manifest(self) -> ToolManifest:
        return MANIFEST

    @property
    def credentials(self) -> None:
        return None

    def execute(self, action: str, tool_input: JSONObject, api: HostAPI) -> ActionResult:
        if action not in ENDPOINTS:
            return ActionFailed("Unsupported Vercel Analytics action.")
        try:
            params = _request(action, tool_input, api)
            token = api.config.get("VERCEL_ACCESS_TOKEN", "")
            if not token:
                raise ToolInputValidationError("Vercel access token is not set. Configure it in Home > Integrations.")
            if not token.isascii() or any(ord(c) <= 32 or ord(c) >= 127 for c in token):
                raise ToolInputValidationError("Configure a valid Vercel access token in Home > Integrations.")
            response = json_request("GET", ENDPOINTS[action] + "?" + encode_query(params), headers={"Authorization": "Bearer " + token}, failure_message="Vercel Analytics request failed.", invalid_response_message="Vercel Analytics returned invalid JSON.")
            if action != "query_visits":
                return ActionExecuted(_discovery(response, action == "list_projects", params.get("teamId")))
            return ActionExecuted({"message": "Production aggregates. Pageviews can be summed across groups including Others; visitors are not additive. Hobby's reporting window is one month; empty data may reflect missing tracking or expired history.", "project_id": params["projectId"], "start_date": tool_input["start_date"], "end_date": tool_input["end_date"], "group_by": params["by"], "rows": _rows(response, params["by"])})
        except (ToolInputValidationError, ParamGuardDenied) as exc:
            return ActionFailed(str(exc))
        except WebRequestError as exc:
            messages = {400: "Vercel rejected the request; check IDs, dates and query parameters.", 401: "Vercel rejected the access token; replace it in Home > Integrations.", 403: "Vercel denied access; check the token's scope and project/team permissions.", 404: "Vercel could not find the requested resource or analytics.", 429: "Vercel API rate limit reached; try again later."}
            if exc.status in messages:
                return ActionFailed(messages[exc.status])
            raise transport_or_unmapped_provider_error("Vercel", "analytics", exc) from None


BUNDLED_TOOL = VercelAnalyticsTool()
