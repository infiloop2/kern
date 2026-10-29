"""Bounded, direct reads from the TrustMRR developer API."""

from datetime import datetime, timezone
import json
import math
import re
from urllib.parse import quote
from typing import cast

from host.param_guard import ParamGuardDenied
from host.tools.host_api import HostAPI
from host.tools.json_types import JSONObject, JSONValue
from host.tools.manifest import ToolManifest
from host.tools.results import ActionExecuted, ActionFailed, ActionResult
from host.tools.shared.inputs import ToolInputValidationError
from host.tools.shared.web import (
    UnmappedProviderError, WebRequestError, encode_query, _request_bytes_and_headers,
    known_provider_transport_error, unmapped_provider_error,
)
from host.tools.tool import Tool
from .manifest import ENUMS, LIST_INPUT, MANIFEST, MAX_INTEGER, NUMERIC_FILTERS

ENDPOINT = "https://trustmrr.com/api/v1/startups"
MAX_BYTES = 1024 * 1024
INVALID_RESPONSE = "TrustMRR returned an invalid or oversized response."
SLUG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,199}")
HANDLE_RE = re.compile(r"[A-Za-z0-9_]{1,200}")
SECRET_TEXT = re.compile(r"tmrr_[A-Za-z0-9_-]+")
BEARER_TEXT = re.compile(r"(?i:bearer)\s+([A-Za-z0-9._~+/=-]+)")
SECRET_FIELDS = {"authorization", "api_key", "apikey", "access_token", "refresh_token", "token", "secret", "password"}


class _JSONDecimal(str):
    """A validated JSON numeric token, never a string in the wire result."""


def _parse(raw: bytes) -> JSONObject:
    def reject_constant(_: str) -> None:
        raise ValueError(INVALID_RESPONSE)

    def unique_pairs(pairs: list[tuple[str, JSONValue]]) -> JSONObject:
        result: JSONObject = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(INVALID_RESPONSE)
            result[key] = value
        return result

    parsed = json.loads(raw.decode("utf-8"), parse_float=_JSONDecimal,
                        parse_constant=reject_constant, object_pairs_hook=unique_pairs)
    if not isinstance(parsed, dict):
        raise ValueError(INVALID_RESPONSE)
    return cast(JSONObject, parsed)


def _dumps(value: JSONValue) -> str:
    # Only json.loads constructs these tokens. Keep their exact decimal and
    # exponent representation rather than round through binary floating point.
    if isinstance(value, _JSONDecimal):
        return value
    if isinstance(value, dict):
        return "{" + ",".join(json.dumps(key, ensure_ascii=True) + ":" + _dumps(child) for key, child in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ",".join(_dumps(child) for child in value) + "]"
    return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"))


def _redact_bearer(match: re.Match[str]) -> str:
    token = match[1]
    # Ordinary prose such as "Bearer authentication" is not a credential.
    # Exact configured keys and tmrr_ tokens are removed independently.
    credential_shaped = len(token) >= 20 and any(c.isdigit() for c in token) and any(c.isalpha() for c in token)
    return "[redacted]" if credential_shaped else match[0]


def _identifier(value: JSONValue, pattern: re.Pattern[str], name: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ToolInputValidationError(f"TrustMRR {name} must match the documented 1–200 ASCII character grammar; URLs and encoded values are not accepted.")
    return value


def _query(tool_input: JSONObject, api: HostAPI) -> dict[str, str]:
    if set(tool_input) - LIST_INPUT.keys():
        raise ToolInputValidationError("TrustMRR list_startups received unknown parameters.")
    query: dict[str, str] = {}
    for name, default, maximum in (("page", 1, MAX_INTEGER), ("limit", 10, 10)):
        value = tool_input.get(name, default)
        if type(value) is not int or not 1 <= value <= maximum:
            raise ToolInputValidationError(f"TrustMRR {name} must be an integer from 1 to {maximum}.")
        query[name] = str(value)
    for name, options in ENUMS.items():
        if name in tool_input:
            value = tool_input[name]
            if not isinstance(value, str) or value not in options:
                raise ToolInputValidationError(f"TrustMRR {name} must be a documented string option.")
            query[name] = value
    query.setdefault("sort", "best-deal" if query.get("onSale") == "true" else "revenue-desc")
    for name in NUMERIC_FILTERS:
        if name not in tool_input:
            continue
        value = tool_input[name]
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or (isinstance(value, float) and not math.isfinite(value))
                or abs(value) > 1.7976931348623157e308
                or (not name.endswith("Growth") and value < 0)):
            raise ToolInputValidationError(f"TrustMRR {name} must be finite and monetary filters must be nonnegative.")
        query[name] = str(value)
    for metric in ("Revenue", "Mrr", "Growth", "Price"):
        low, high = tool_input.get("min" + metric), tool_input.get("max" + metric)
        if isinstance(low, (int, float)) and isinstance(high, (int, float)) and low > high:
            raise ToolInputValidationError(f"TrustMRR min{metric} must not exceed max{metric}.")
    if "xHandle" in tool_input:
        handle = _identifier(tool_input["xHandle"], HANDLE_RE, "xHandle")
        query["xHandle"] = api.outbound.guard_request_parameter_string(handle)
    return query


def _redact(value: JSONValue, api_key: str, depth: int = 0) -> JSONValue:
    if depth > 32:
        raise ValueError(INVALID_RESPONSE)
    if isinstance(value, _JSONDecimal):
        return value
    if isinstance(value, str):
        return BEARER_TEXT.sub(_redact_bearer, SECRET_TEXT.sub("[redacted]", value.replace(api_key, "[redacted]")))
    if isinstance(value, list):
        return [_redact(item, api_key, depth + 1) for item in value]
    if isinstance(value, dict):
        result: JSONObject = {}
        for key, child in value.items():
            safe_key = str(_redact(key, api_key, depth + 1))
            if safe_key in result:
                raise ValueError(INVALID_RESPONSE)
            normalized = key.lower().replace("-", "_")
            result[safe_key] = "[redacted]" if normalized in SECRET_FIELDS or normalized.endswith(("_api_key", "_token", "_secret", "_password")) else _redact(child, api_key, depth + 1)
        return result
    return value


def _response(response: JSONObject, action: str, tool_input: JSONObject, api_key: str) -> str:
    data = response.get("data")
    if action == "list_startups":
        meta = response.get("meta")
        limit = tool_input.get("limit", 10)
        if (not isinstance(limit, int) or not isinstance(data, list) or len(data) > limit
                or any(not isinstance(row, dict) for row in data) or not isinstance(meta, dict)):
            raise ValueError(INVALID_RESPONSE)
        for field in ("page", "limit", "total"):
            value = meta.get(field)
            if type(value) is not int or value < (0 if field == "total" else 1):
                raise ValueError(INVALID_RESPONSE)
        if (meta["page"] != tool_input.get("page", 1) or meta["limit"] != tool_input.get("limit", 10)
                or not isinstance(meta.get("hasMore"), bool)):
            raise ValueError(INVALID_RESPONSE)
    elif not isinstance(data, dict) or data.get("slug") != tool_input["slug"]:
        raise ValueError(INVALID_RESPONSE)
    result = _dumps(_redact(response, api_key))
    if len(result.encode("utf-8")) > MAX_BYTES:
        raise ValueError(INVALID_RESPONSE)
    return result


def _rate_header(headers: dict[str, str], name: str) -> int | None:
    value = headers.get("x-ratelimit-" + name, "")
    if re.fullmatch(r"[0-9]{1,16}", value) and int(value) <= MAX_INTEGER:
        return int(value)
    return None


class TrustMRRTool(Tool):
    @property
    def manifest(self) -> ToolManifest:
        return MANIFEST

    @property
    def credentials(self) -> None:
        return None

    def execute(self, action: str, tool_input: JSONObject, api: HostAPI) -> ActionResult:
        if action not in {"list_startups", "get_startup"}:
            return ActionFailed("Unsupported TrustMRR action.")
        try:
            if action == "list_startups":
                url = ENDPOINT + "?" + encode_query(_query(tool_input, api))
            else:
                if set(tool_input) != {"slug"}:
                    raise ToolInputValidationError("TrustMRR get_startup requires only slug.")
                slug = _identifier(tool_input["slug"], SLUG_RE, "slug")
                # No exception flags: scan the exact supplied path identifier
                # before encoding or requesting anything from the provider.
                slug = api.outbound.guard_request_parameter_string(slug)
                url = ENDPOINT + "/" + quote(slug, safe="")
            api_key = api.config.get("TRUSTMRR_API_KEY", "")
            if not api_key.strip():
                return ActionFailed("TRUSTMRR_API_KEY is not set. Set it under Home > Integrations, then enable TrustMRR.")
            raw, headers = _request_bytes_and_headers(
                "GET", url, headers={"Accept": "application/json", "Authorization": "Bearer " + api_key},
                failure_message="TrustMRR request failed.",
                max_bytes=MAX_BYTES,
            )
            response = _parse(raw)
            return ActionExecuted({
                "provider_response_json": _response(response, action, tool_input, api_key),
                "retrieved_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "rate_limit": {name: _rate_header(headers, name) for name in ("limit", "remaining", "reset")},
            })
        except (ToolInputValidationError, ParamGuardDenied) as exc:
            return ActionFailed(str(exc))
        except WebRequestError as exc:
            messages = {
                400: "TrustMRR rejected the request parameters. Check its current API documentation.",
                401: "TrustMRR authentication failed. Replace TRUSTMRR_API_KEY under Home > Integrations.",
                403: "TrustMRR API access is disabled or forbidden. Review its acceptable-use policy and contact TrustMRR; do not bypass the restriction.",
                404: "TrustMRR could not find the requested startup.",
                429: "TrustMRR rate limit reached. Wait for the next rate window before another read (10 requests/minute standard, 60 premium). This call was not retried.",
                500: "TrustMRR encountered a server error. This call was not retried.",
            }
            if exc.status in messages:
                return ActionFailed(messages[exc.status])
            known = known_provider_transport_error(exc)
            if known:
                return ActionFailed(known)
            raise unmapped_provider_error("TrustMRR", action, exc) from None
        except UnmappedProviderError:
            raise
        except (ValueError, RuntimeError, RecursionError, OverflowError):
            return ActionFailed(INVALID_RESPONSE)
        except Exception:
            # Never stringify unexpected errors: they may contain request
            # credentials or provider-echoed data.
            return ActionFailed("TrustMRR request failed.")


BUNDLED_TOOL = TrustMRRTool()
