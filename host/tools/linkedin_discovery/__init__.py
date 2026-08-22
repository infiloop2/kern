"""Public LinkedIn-post discovery through Serper's Google Search API."""

from __future__ import annotations

import re
import urllib.parse
from typing import cast

from host.param_guard import PARAM_GUARD_PROTECTION, PARAM_GUARD_TECHNICAL_DETAIL
from host.tools.host_api import HostAPI
from host.tools.json_types import JSONObject, JSONValue
from host.tools.manifest import ActionSpec, ConfigRequirement, DataSummary, DataSummaryCard, DataSummaryLink, DataSummaryPoint, SetupStep, ToolManifest
from host.tools.results import ActionExecuted, ActionFailed, ActionResult
from host.tools.shared import outputs
from host.tools.shared.inputs import bounded_int as _bounded_int, clip as _text
from host.tools.shared.web import UnmappedProviderError, WebRequestError, json_request, known_provider_transport_error, unmapped_provider_error
from host.tools.tool import Tool

SERPER_SEARCH_URL = "https://google.serper.dev/search"
MAX_QUERY_CHARS = 300
MAX_RESULTS = 10
MAX_PAGE = 10

RESULT_SCHEMA: JSONObject = outputs.obj(
    {
        "message": outputs.text("How many indexed public post results were found."),
        "query": outputs.text("The topic searched, without the site: restriction Kern adds."),
        "page": outputs.text("The result page returned, as a decimal string."),
        "results": outputs.array_of(
            outputs.obj(
                {
                    "position": outputs.text("Rank on this page as a decimal string, starting at 1."),
                    "title": outputs.text("Google's title for the post page."),
                    "url": outputs.text("Public linkedin.com/posts URL."),
                    "snippet": outputs.text("Google's extract of the post text, up to 1500 characters."),
                    "date": outputs.text("Recency string Google reports, e.g. \"2 days ago\"; empty when absent."),
                    "source": outputs.text("Source label Google reports, empty when absent."),
                },
                ["position", "title", "url", "snippet", "date", "source"],
            ),
            f"Up to {MAX_RESULTS} deduplicated post results, in Google's ranking order.",
        ),
    },
    ["message", "query", "page", "results"],
)

MANIFEST = ToolManifest(
    tool_id="linkedin_discovery",
    display_name="LinkedIn Discovery",
    description=(
        "Lets your agent search Google-indexed public LinkedIn posts without connecting "
        "a LinkedIn account."
    ),
    connection="enable_only",
    actions=(
        ActionSpec(
            id="search_posts",
            description=(
                "Search Google-indexed public LinkedIn post pages for a topic and return bounded titles, "
                "LinkedIn URLs, dates, sources, and search snippets. This is discovery and snippet reading, "
                "not a LinkedIn feed or full-post reader: unindexed, private, deleted, login-gated, or text "
                "omitted from Google's snippet is unavailable. The tool adds site:linkedin.com/posts itself, "
                "runs immediately, and spends one Serper search credit when the request succeeds."
            ),
            data_policy=(
                "Sends the topic inside a site:linkedin.com/posts Google query, page number, language, country, "
                "and the deployment API key to Serper. Returns only bounded organic-result "
                "metadata for LinkedIn post URLs; the result enters active model context. LinkedIn is not contacted "
                "by this host. This read-only action runs directly with no approval."
            ),
            input_schema={
                "type": "object",
                "required": ["query"],
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Topic, phrase, company, or person to find in public LinkedIn posts, up to 300 characters; do not include site: because the tool scopes it automatically.",
                    },
                    "limit": {
                        "type": "string",
                        "description": "Maximum matching LinkedIn results returned, 1-10 (default 10). One successful API request is charged regardless of this value.",
                    },
                    "page": {
                        "type": "string",
                        "description": "Google results page, 1-10 (default 1). Use a later page only after the prior page; each page is a separate Serper request.",
                    },
                },
                "additionalProperties": False,
            },
            output_schema=RESULT_SCHEMA,
        ),
    ),
    config=(
        ConfigRequirement(
            key="SERPERAPI_API_KEY",
            description="Serper account API key used only for this LinkedIn public-post search tool.",
        ),
    ),
    protections=(
        "The host never logs in to or sends a request to LinkedIn. The only provider request is a bounded site-restricted Google search through Serper.",
        "Only public LinkedIn post URLs are returned. The API key stays in write-only tool config, queries are capped at 300 characters, and results are capped at 10.",
        PARAM_GUARD_PROTECTION,
    ),
    technical_details=(PARAM_GUARD_TECHNICAL_DETAIL,),
    setup_steps=(
        SetupStep(
            title="Create a Serper account",
            description="Create a Serper account and obtain enough Google Search credits for the expected use. One successful search_posts page consumes one credit even when Google returns no matching LinkedIn post.",
            link_url="https://serper.dev",
            link_label="Open Serper",
        ),
        SetupStep(
            title="Copy the API key",
            description="Open the Serper dashboard and copy the private API key. Kern sends synchronous Google Search requests; Serper states that it queries Google in real time and does not cache results.",
            link_url="https://serper.dev/dashboard",
            link_label="Open the Serper dashboard",
        ),
        SetupStep(
            title="Configure and enable LinkedIn Discovery",
            show_config=True,
            description="Open LinkedIn Discovery under Home > Integrations, save the key as SERPERAPI_API_KEY, then enable the tool. There is no LinkedIn app, login, OAuth consent, cookie, or session to configure; never paste LinkedIn credentials into this field.",
        ),
    ),
    data_summary=DataSummary(
        cards=(
            DataSummaryCard(
                title="What leaves this host",
                description=(
                    "Only the topic text, sent as a site:linkedin.com/posts Google query with fixed English and United States "
                    "locale settings plus paging options. This tool holds no LinkedIn credential; besides the Serper key that "
                    "authenticates each request, nothing else on this host is sent. What the agent searches for is data sent "
                    "to Serper and Google. The topic text "
                    "first passes the host parameter guard (see Technical notes), which denies secret- or credential-shaped values before it is sent."
                ),
            ),
            DataSummaryCard(
                title="Where it can go",
                points=(
                    DataSummaryPoint(label="Serper", text="The query goes to Serper, which states that it runs the Google search in real time without caching results."),
                    DataSummaryPoint(label="Google", text="Serper submits the query to Google Search, so Google also sees the topic text along with Serper's request metadata rather than this host's."),
                ),
            ),
            DataSummaryCard(
                title="What Serper can do with it",
                description=(
                    "Serper's policy permits processing personal data to operate, secure, monitor, and improve the service and "
                    "for analytics, including AI or machine learning. It does not say whether query text is included in activity "
                    "logs or analytics. Serper may use contracted service providers and says it acts as processor when providing "
                    "the service involves personal data. Google processes the search under its own Privacy Policy."
                ),
                links=(
                    DataSummaryLink(label="Serper Privacy Policy", url="https://serper.dev/privacy"),
                    DataSummaryLink(label="Serper Terms", url="https://serper.dev/terms"),
                    DataSummaryLink(label="Google Privacy Policy", url="https://policies.google.com/privacy"),
                ),
            ),
            DataSummaryCard(
                title="How long Serper retains it",
                description="Serper says search results are not cached, but its public policy gives no separate retention period for search queries or activity logs. It generally retains personal data while the account exists and longer when required for legal, tax, contractual, or litigation purposes. Google's retention follows its own policy.",
                links=(
                    DataSummaryLink(label="Serper Privacy Policy", url="https://serper.dev/privacy"),
                ),
            ),
        ),
    ),
    # Nothing to add beyond the description: it drives this tool on its own.
    agent_notes="",
)


def _query(tool_input: JSONObject) -> str:
    value = tool_input.get("query")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("LinkedIn discovery query is required.")
    query = value.strip()
    if len(query) > MAX_QUERY_CHARS:
        raise ValueError(
            f"LinkedIn discovery query must be at most {MAX_QUERY_CHARS} characters."
        )
    if "site:" in query.lower():
        raise ValueError("Do not include a site: operator; LinkedIn Discovery adds its fixed scope.")
    if re.search(r"(?:^|\s)OR(?:\s|$)", query, flags=re.IGNORECASE):
        raise ValueError("Do not include an OR operator; LinkedIn Discovery keeps every result scoped to LinkedIn posts.")
    return query


def _linkedin_post_url(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 2_048:
        return ""
    url = value.strip()
    if any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in url):
        return ""
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError:
        return ""
    host = (parsed.hostname or "").lower()
    segments = [urllib.parse.unquote(segment) for segment in parsed.path.split("/") if segment]
    # Accept regional LinkedIn hosts (uk.linkedin.com, de.linkedin.com, ...) that
    # Google commonly returns, not just the bare/www host; normalize to www.
    if (
        parsed.scheme != "https"
        or (host != "linkedin.com" and not host.endswith(".linkedin.com"))
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or len(segments) < 2
        or segments[0] != "posts"
        or any(segment in {".", ".."} for segment in segments)
    ):
        return ""
    return urllib.parse.urlunsplit(("https", "www.linkedin.com", parsed.path, "", ""))


def _organic_results(response: JSONObject, *, limit: int) -> list[JSONObject]:
    raw_results = response.get("organic")
    if not isinstance(raw_results, list):
        return []
    results: list[JSONObject] = []
    seen: set[str] = set()
    for raw in raw_results:
        if not isinstance(raw, dict):
            continue
        url = _linkedin_post_url(raw.get("link"))
        if not url or url in seen:
            continue
        seen.add(url)
        position = raw.get("position")
        item: JSONObject = {
            "position": str(position if isinstance(position, int) and not isinstance(position, bool) else len(results) + 1),
            "title": _text(raw.get("title"), limit=500),
            "url": url,
            "snippet": _text(raw.get("snippet"), limit=1_500),
            "date": _text(raw.get("date"), limit=100),
            "source": _text(raw.get("source"), limit=200),
        }
        results.append(item)
        if len(results) >= limit:
            break
    return results


def _search(api_key: str, query: str, page: int) -> JSONObject:
    body: JSONObject = {
        "q": f"site:linkedin.com/posts {query}",
        "hl": "en",
        "gl": "us",
        "num": MAX_RESULTS,
        "page": page,
        "autocorrect": False,
    }
    return json_request(
        "POST",
        SERPER_SEARCH_URL,
        headers={"X-API-KEY": api_key},
        body=body,
        failure_message="Serper LinkedIn search failed.",
        invalid_response_message="Serper returned an invalid LinkedIn search response.",
    )


class LinkedInDiscoveryTool(Tool):
    @property
    def manifest(self) -> ToolManifest:
        return MANIFEST

    @property
    def credentials(self) -> None:
        return None

    def execute(self, action: str, tool_input: JSONObject, api: HostAPI) -> ActionResult:
        if action != "search_posts":
            return ActionFailed("Unsupported LinkedIn Discovery action.")
        try:
            query = api.outbound.guard_request_parameter_string(_query(tool_input))
            limit = _bounded_int(tool_input.get("limit"), name="limit", default=MAX_RESULTS, minimum=1, maximum=MAX_RESULTS)
            page = _bounded_int(tool_input.get("page"), name="page", default=1, minimum=1, maximum=MAX_PAGE)
            response = _search(api.config["SERPERAPI_API_KEY"], query, page)
            results = _organic_results(response, limit=limit)
            return ActionExecuted(
                {
                                        "message": f"Found {len(results)} indexed public LinkedIn post result(s).",
                    "query": query,
                    "page": str(page),
                    "results": cast(list[JSONValue], results),
                }
            )
        except WebRequestError as exc:
            if exc.status in {401, 403}:
                message = "Serper rejected the configured API key."
            elif exc.status == 429:
                message = "Serper search capacity or account credits were exhausted."
            elif exc.status:
                message = f"Serper returned HTTP {exc.status}."
            else:
                message = known_provider_transport_error(exc)
                if not message:
                    raise unmapped_provider_error("Serper", "LinkedIn discovery", exc) from None
            return ActionFailed(message)
        except UnmappedProviderError:
            raise
        except (ValueError, RuntimeError) as exc:
            # Input validation and config-unset carry curated messages.
            return ActionFailed(str(exc) or "LinkedIn Discovery request failed.")
        except Exception:
            return ActionFailed("LinkedIn Discovery request failed.")


BUNDLED_TOOL = LinkedInDiscoveryTool()
