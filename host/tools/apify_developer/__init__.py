"""Fixed-endpoint Apify development with reviewed writes and bounded test runs."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import io
import json
import math
import re
import urllib.parse
import uuid
from typing import Any, cast

from host.tools.host_api import ApprovalRecord, HostAPI
from host.tools.json_types import JSONObject
from host.tools.results import (
    ActionExecuted, ActionFailed, ActionPendingApproval, ActionResult,
    ApprovalExecuted, ApprovalResult, OpenedStreamingAsset, StreamingAsset,
)
from host.tools.shared.inputs import clip_text
from host.tools.shared.web import WebRequestError, request_bytes
from host.tools.tool import Tool
from .manifest import MANIFEST

ORIGIN = "https://api.apify.com/v2"
MAX_RESPONSE = 2 * 1024 * 1024
ID_RE = re.compile(r"[A-Za-z0-9]{17}\Z")
VERSION_RE = re.compile(r"(?:0|[1-9][0-9]?)\.(?:0|[1-9][0-9]?)\Z")
BUILD_RE = re.compile(r"[0-9]{1,2}\.[0-9]{1,2}\.[0-9]{1,10}\Z")
SECRET_RE = re.compile(r"(?i)(?<![A-Za-z0-9])(?:apify_api_|sk-(?:proj-)?|gh[pousr]_)[A-Za-z0-9_-]+|\bbearer\s+[A-Za-z0-9._-]+")
SCOPE = "Actor aggregate counters include owner/free usage; they do not identify paying customers or retention."
RUN_SCOPE = "Only runs owned by the configured account, not a complete seller/customer usage feed."
BUILD_SCOPE = "Only builds of an Actor owned by the configured account; these are build records, not customer runs."


def _string(value: Any, name: str, maximum: int, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()) or len(value) > maximum:
        raise ValueError(f"{name} must be text within its documented length limit.")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise ValueError(f"{name} must be valid UTF-8 text.") from None
    if "\x00" in value:
        raise ValueError(f"{name} must not contain NUL characters.")
    return value


def _id(value: Any) -> str:
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise ValueError("Expected an opaque 17-character Apify id.")
    return value


def _version(value: Any) -> str:
    if not isinstance(value, str) or not VERSION_RE.fullmatch(value):
        raise ValueError("version must be MAJOR.MINOR, with components from 0 to 99.")
    return value


def _int(value: Any, name: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name} must be an integer from {low} to {high}.")
    return value


def _number(value: Any) -> int | float | None:
    if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1e15:
        return value
    return None


def _text(value: Any, maximum: int = 300) -> str:
    return SECRET_RE.sub("[redacted]", value)[:maximum] if isinstance(value, str) else ""


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("Apify returned an invalid object.")
    return value


def _json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON must not contain duplicate keys.")
        result[key] = value
    return result


def _parse(raw: str | bytes, *, preserve_numbers: bool = False) -> Any:
    def reject(_):
        raise ValueError("JSON must contain only finite numbers.")
    def parse_float(value):
        number = float(value)
        if not math.isfinite(number):
            reject(value)
        return number
    try:
        return json.loads(raw, object_pairs_hook=_pairs, parse_constant=reject,
                          parse_float=str if preserve_numbers else parse_float,
                          parse_int=str if preserve_numbers else int)
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise ValueError("Invalid JSON document.") from None


def _request(api: HostAPI, method: str, path: str, *, params=None, body=None, raw=False) -> Any:
    # Every path is constructed here from fixed segments and validated opaque ids.
    token = api.config["APIFY_API_TOKEN"]
    url = ORIGIN + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    response = request_bytes(method, url, headers={"Authorization": "Bearer " + token,
        "Content-Type": "application/json", "Accept": "application/json"},
        data=body if isinstance(body, bytes) else _json(body) if body is not None else None,
        timeout=30, max_bytes=MAX_RESPONSE, failure_message="Apify Developer request failed.")
    if len(response) > MAX_RESPONSE:
        raise ValueError("Apify response exceeds the 2 MiB limit.")
    if raw:
        return response
    return _object(_parse(response)).get("data")


def _redact_result(value: Any, api: HostAPI, depth: int = 0) -> Any:
    """Redact after JSON decoding, including escaped credentials in dataset cells.

    Keep raw provider source untouched for approval fingerprints and validation.
    """
    if depth > 50:
        raise ValueError("Apify result exceeds the supported nesting depth.")
    if isinstance(value, str):
        token = api.config["APIFY_API_TOKEN"]
        return SECRET_RE.sub("[redacted]", value.replace(token, "[redacted]") if token else value)
    if isinstance(value, list):
        return [_redact_result(item, api, depth + 1) for item in value]
    if isinstance(value, dict):
        return {_redact_result(k, api, depth + 1): _redact_result(v, api, depth + 1) for k, v in value.items()}
    return value


def _executed(value: JSONObject, api: HostAPI) -> ActionExecuted:
    return ActionExecuted(_redact_result(value, api))


def _account(api: HostAPI) -> str:
    return _id(_object(_request(api, "GET", "/users/me")).get("id"))


def _actor(api: HostAPI, actor_id: str, account: str | None = None) -> dict[str, Any]:
    # Current canonical resource: https://docs.apify.com/api/v2/actor-get
    actor = _object(_request(api, "GET", "/actors/" + _id(actor_id)))
    if actor.get("id") != actor_id or (account is not None and actor.get("userId") != account):
        raise ValueError("Actor must be owned by the configured Apify account.")
    return actor


def _job(api: HostAPI, kind: str, job_id: str, account: str, *, succeeded=False) -> dict[str, Any]:
    path = "/actor-builds/" if kind == "build" else "/actor-runs/"
    job = _object(_request(api, "GET", path + _id(job_id)))
    if job.get("id") != job_id or job.get("userId") != account:
        raise ValueError("Job must be owned by the configured Apify account.")
    _id(job.get("actId"))
    if succeeded and job.get("status") != "SUCCEEDED":
        raise ValueError("A successful completed job is required.")
    return job


def _actor_result(actor: dict[str, Any]) -> JSONObject:
    stats = actor.get("stats")
    stats = stats if isinstance(stats, dict) else {}
    versions = actor.get("versions")
    versions = versions if isinstance(versions, list) else []
    public_runs = stats.get("publicActorRunStats30Days")
    public_runs = public_runs if isinstance(public_runs, dict) else {}
    pricing = actor.get("currentPricingInfo")
    pricing = pricing if isinstance(pricing, dict) else {}
    return {
        "id": _text(actor.get("id"), 17), "name": _text(actor.get("name"), 64),
        "username": _text(actor.get("username"), 64), "title": _text(actor.get("title"), 100),
        "description": _text(actor.get("description")),
        "is_public": actor.get("isPublic") if type(actor.get("isPublic")) is bool else None,
        "created_at": _text(actor.get("createdAt"), 40), "modified_at": _text(actor.get("modifiedAt"), 40),
        "stats": {name: _number(stats.get(source)) for name, source in (
            ("total_runs", "totalRuns"), ("total_users", "totalUsers"), ("users_7_days", "totalUsers7Days"),
            ("users_30_days", "totalUsers30Days"), ("users_90_days", "totalUsers90Days"),
            ("rating", "actorReviewRating"), ("reviews", "actorReviewCount"))},
        "public_runs_30_days": {name: _number(public_runs.get(source)) for name, source in (
            ("total", "TOTAL"), ("succeeded", "SUCCEEDED"), ("failed", "FAILED"), ("aborted", "ABORTED"), ("timed_out", "TIMED-OUT"))},
        "pricing_model": _text(pricing.get("pricingModel"), 40),
        "versions": [_text(v.get("versionNumber"), 5) for v in versions[:100] if isinstance(v, dict)],
    }


def _job_result(job: dict[str, Any]) -> JSONObject:
    stats = job.get("stats")
    stats = stats if isinstance(stats, dict) else {}
    events = job.get("chargedEventCounts")
    events = events if isinstance(events, dict) else {}
    return {
        "id": _text(job.get("id"), 17), "actor_id": _text(job.get("actId"), 17),
        "build_id": _text(job.get("buildId"), 17), "build_number": _text(job.get("buildNumber"), 30),
        "status": _text(job.get("status"), 30), "started_at": _text(job.get("startedAt"), 40),
        "finished_at": _text(job.get("finishedAt"), 40),
        "duration_seconds": _number(stats.get("runTimeSecs")), "compute_units": _number(stats.get("computeUnits")),
        "usage_usd": _number(job.get("usageTotalUsd")), "dataset_id": _text(job.get("defaultDatasetId"), 17),
        "charged_events": [{"event": _text(k, 80), "count": _number(v)} for k, v in list(events.items())[:50]],
    }


def _page(data: Any, values: dict[str, Any], normalizer, scope: str, account=None) -> JSONObject:
    data = _object(data)
    items = data.get("items")
    if not isinstance(items, list):
        raise ValueError("Apify returned an invalid page.")
    limit, offset = values["limit"], values["offset"]
    selected: list[Any] = []
    consumed = 0
    for item in items[:limit]:
        item = _object(item)
        # Never leak another user's run details, even if the provider broadens its endpoint.
        if account and item.get("userId") != account:
            consumed += 1
            continue
        normalized = normalizer(item)
        if len(_json(selected + [normalized])) > 48 * 1024:
            break
        selected.append(normalized)
        consumed += 1
    total = _number(data.get("total"))
    next_offset = offset + consumed
    if next_offset > 10000 or (consumed == len(items) and (not items or len(items) < limit or (total is not None and next_offset >= total))):
        next_offset = None
    return {"items": selected, "total": total,
            "next_offset": next_offset, "metrics_scope": scope}


def _guard_text(text: str, api: HostAPI, *, key=False) -> None:
    # Decoding is inspection only. The original value is sent unchanged.
    for _ in range(7):
        if SECRET_RE.search(text) or api.config["APIFY_API_TOKEN"] in text:
            raise ValueError("Request parameter cannot contain API credentials.")
        api.outbound.guard_request_parameter_string(text)
        if key and re.search(r"(?i)password|secret|token|cookie|authorization|headers|webhook|proxy|^(?:code|script|function)$|pageFunction|requestHandler|NavigationHooks|customJs", text):
            raise ValueError("Actor input cannot include credentials, code, proxy or webhook configuration.")
        if re.search(r"%(?![0-9a-fA-F]{2})", text):
            raise ValueError("Actor input contains malformed percent encoding.")
        try:
            decoded = urllib.parse.unquote_plus(text, errors="strict")
        except UnicodeError:
            raise ValueError("Actor input contains invalid encoded UTF-8.") from None
        if decoded == text:
            return
        text = decoded
    raise ValueError("Actor input has excessive nested encoding.")


def _guard_input(value: str, api: HostAPI) -> JSONObject:
    if len(value.encode("utf-8")) > 8192:
        raise ValueError("Actor input must be at most 8 KiB.")
    parsed = _object(_parse(value))
    nodes = 0
    def walk(item, depth):
        nonlocal nodes
        nodes += 1
        if nodes > 200 or depth > 6:
            raise ValueError("Actor input exceeds its depth or node limit.")
        if isinstance(item, dict):
            for key, child in item.items():
                _guard_text(key, api, key=True)
                walk(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                walk(child, depth + 1)
        elif isinstance(item, str):
            _guard_text(item, api)
        elif type(item) in (int, float):
            if not math.isfinite(item) or abs(item) > 1e9:
                raise ValueError("Actor input numbers must be finite and at most one billion in magnitude.")
        elif item is not None and type(item) is not bool:
            raise ValueError("Unsupported Actor input value.")

    walk(parsed, 0)
    return parsed


def _validate(action: str, values: dict[str, Any], api: HostAPI) -> dict[str, Any]:
    spec = MANIFEST.action(action)
    if spec is None:
        raise ValueError("Unsupported Apify Developer action.")
    properties = _object(spec.input_schema["properties"])
    required = cast(list[str], spec.input_schema.get("required", []))
    if not isinstance(values, dict) or set(values) - set(properties) or any(k not in values for k in required):
        raise ValueError("Apify Developer input has missing or unsupported fields.")
    values = dict(values)
    for key in ("actor_id", "build_id", "test_run_id", "run_id", "job_id"):
        if key in values:
            values[key] = _id(values[key])
    if "limit" in properties:
        values["limit"] = _int(values.get("limit", 20), "limit", 1, 100 if action == "export_results" else 50)
        values["offset"] = _int(values.get("offset", 0), "offset", 0, 10000)
    if "version" in values:
        values["version"] = _version(values["version"])
    for key, maximum in (("title", 100), ("description", 300)):
        if key in values:
            values[key] = _string(values[key], key, maximum)
            if SECRET_RE.search(values[key]) or api.config["APIFY_API_TOKEN"] in values[key]:
                raise ValueError("Listing text must not contain API credentials.")
    if action == "search_store":
        values["query"] = _string(values.get("query", ""), "query", 160, empty=True)
        _guard_text(values["query"], api)
        values["sort"] = values.get("sort", "relevance")
        if values["sort"] not in ("newest", "popularity", "relevance", "lastUpdate"):
            raise ValueError("Unsupported Store sort.")
    if action == "read_log" and values["kind"] not in ("run", "build"):
        raise ValueError("kind must be run or build.")
    if action == "create_actor":
        if not isinstance(values["name"], str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,62}[a-z0-9]", values["name"]):
            raise ValueError("Actor name must be 3–64 lowercase letters, digits and internal hyphens.")
    if action == "create_version":
        files = values["files"]
        if not isinstance(files, list) or not 1 <= len(files) <= 30:
            raise ValueError("Provide 1–30 source files.")
        seen = set()
        for file in files:
            if not isinstance(file, dict) or set(file) != {"path", "content"}:
                raise ValueError("Each source file needs exactly path and content.")
            path = _string(file["path"], "path", 160)
            if not re.fullmatch(r"[A-Za-z0-9_./-]+", path) or any(p in ("", ".", "..") for p in path.split("/")) or path in seen:
                raise ValueError("Source file paths must be unique relative paths without traversal.")
            seen.add(path)
            content = _string(file["content"], "content", 48000, empty=True)
            if SECRET_RE.search(content) or api.config["APIFY_API_TOKEN"] in content:
                raise ValueError("Source must not contain API credentials.")
        if len(_json(values)) > 48 * 1024:
            raise ValueError("Source request exceeds 48 KiB.")
    if action == "run_actor":
        _guard_input(_string(values["input_json"], "input_json", 8192), api)
        values["timeout_seconds"] = _int(values.get("timeout_seconds", 60), "timeout_seconds", 1, 120)
        values["memory_mb"] = _int(values.get("memory_mb", 512), "memory_mb", 128, 1024)
        if values["memory_mb"] not in (128, 256, 512, 1024):
            raise ValueError("memory_mb must be 128, 256, 512 or 1024.")
        charge = values.get("max_charge_usd", 0.25)
        if type(charge) not in (float, int) or not math.isfinite(charge) or not 0 < charge <= 0.5:
            raise ValueError("max_charge_usd must be greater than zero and at most 0.50.")
        values["max_charge_usd"] = charge
    if action == "publish_actor":
        categories = values["categories"]
        if not isinstance(categories, list) or not 1 <= len(categories) <= 3 or any(not isinstance(c, str) or not re.fullmatch(r"[A-Z][A-Z_]{1,39}", c) for c in categories):
            raise ValueError("Provide 1–3 uppercase Apify category identifiers.")
    return values


def _digest(value: Any) -> str:
    canonical = json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(canonical).hexdigest()


def _run_target(api, values, account):
    build = _job(api, "build", values["build_id"], account, succeeded=True)
    actor_id = _id(build["actId"])
    _actor(api, actor_id, account)
    number = build.get("buildNumber")
    if not isinstance(number, str) or not BUILD_RE.fullmatch(number):
        raise ValueError("Build lacks an immutable build number.")
    return actor_id, number


def _listing_state(actor):
    # Mutable settings that publication must not silently replace after review.
    return {k: actor.get(k) for k in ("name", "title", "description", "categories", "isPublic", "taggedBuilds", "actorPermissionLevel", "pricingInfos", "defaultRunOptions")}


def _source(api, actor_id, version):
    source = _object(_request(api, "GET", f"/actors/{actor_id}/versions/{version}"))
    if source.get("sourceType") != "SOURCE_FILES" or source.get("envVars") or source.get("applyEnvVarsToBuild"):
        raise ValueError("Builds require source files without injected environment variables.")
    files = source.get("sourceFiles")
    if not isinstance(files, list) or any(not isinstance(f, dict) or f.get("format") != "TEXT" for f in files):
        raise ValueError("Build approval requires reviewable text source files.")
    _validate("create_version", {"actor_id": actor_id, "version": version,
        "files": [{"path": f.get("name"), "content": f.get("content")} for f in files]}, api)
    return source


def _release(api, values, account):
    build = _job(api, "build", values["build_id"], account, succeeded=True)
    run = _job(api, "run", values["test_run_id"], account, succeeded=True)
    if build["actId"] != values["actor_id"] or run["actId"] != values["actor_id"] or run.get("buildId") != values["build_id"]:
        raise ValueError("Publication requires a successful test run of this exact Actor build.")
    # The legacy README content is deprecated. Modern builds include an immutable
    # actVersion snapshot; never inspect a mutable current version for this gate.
    readme = build.get("readme")
    snapshot = build.get("actVersion")
    files = snapshot.get("sourceFiles", []) if isinstance(snapshot, dict) else []
    definition = build.get("actorDefinition")
    path = definition.get("readme") if isinstance(definition, dict) else None
    paths = {"README.md", ".actor/README.md"}
    if isinstance(path, str):
        paths.update((path, ".actor/" + path.removeprefix("./")))
    has_file = isinstance(files, list) and any(
        isinstance(f, dict) and f.get("name") in paths and f.get("format", "TEXT") == "TEXT"
        and isinstance(f.get("content"), str) and f["content"].strip() for f in files)
    if not (isinstance(readme, str) and readme.strip()) and not has_file:
        raise ValueError("Publication requires a build with a README.")


def _failure(exc: Exception) -> ActionFailed:
    if isinstance(exc, WebRequestError):
        if exc.status in (401, 403):
            return ActionFailed("Apify rejected the token or resource access. Check Apify Developer configuration and permissions.")
        if exc.status == 429:
            return ActionFailed("Apify rate or account limits reached. No request was retried.")
        if exc.status == 402:
            return ActionFailed("Apify requires sufficient account credit or billing setup.")
        return ActionFailed("Apify request failed; a mutation may already have executed. Reconcile Actors/builds/runs before retrying.")
    if isinstance(exc, (ValueError, RuntimeError)):
        return ActionFailed(str(exc))
    return ActionFailed("Apify Developer request failed.")


class ApifyDeveloperTool(Tool):
    @property
    def manifest(self):
        return MANIFEST

    @property
    def credentials(self):
        return None

    def execute(self, action: str, tool_input: JSONObject, api: HostAPI) -> ActionResult:
        try:
            values = _validate(action, tool_input, api)
            if action == "search_store":
                data = _request(api, "GET", "/store", params={"search": values["query"], "sortBy": values["sort"], "limit": values["limit"], "offset": values["offset"]})
                return _executed(_page(data, values, _actor_result, SCOPE), api)
            if action == "get_actor":
                return _executed({"actor": _actor_result(_actor(api, values["actor_id"])), "metrics_scope": SCOPE}, api)
            account = _account(api)
            if action == "get_account_usage":
                usage = _object(_request(api, "GET", "/users/me/usage/monthly"))
                limits = _object(_request(api, "GET", "/users/me/limits"))
                cycle = _object(usage.get("usageCycle", {}))
                ceiling = _object(limits.get("limits", {}))
                current = _object(limits.get("current", {}))
                daily = usage.get("dailyServiceUsages", [])
                if not isinstance(daily, list):
                    raise ValueError("Apify returned an invalid daily usage list.")
                services = _object(usage.get("monthlyServiceUsage", {}))
                return _executed({"account_id": account, "cycle_start": _text(cycle.get("startAt"), 40),
                    "cycle_end": _text(cycle.get("endAt"), 40), "usage_usd": _number(usage.get("totalUsageCreditsUsdAfterVolumeDiscount")),
                    "monthly_limit_usd": _number(ceiling.get("maxMonthlyUsageUsd")), "active_jobs": _number(current.get("activeActorJobCount")),
                    "retention_days": _number(ceiling.get("dataRetentionDays")),
                    "daily_usage": [{"date": _text(d.get("date"), 40), "usage_usd": _number(d.get("totalUsageCreditsUsd"))} for d in daily[:32] if isinstance(d, dict)],
                    "services": [{"service": _text(k, 80), "quantity": _number(v.get("quantity")), "cost_usd": _number(v.get("amountAfterVolumeDiscountUsd"))} for k, v in list(services.items())[:50] if isinstance(v, dict)]}, api)
            if action == "list_actors":
                data = _request(api, "GET", "/actors", params={"my": "true", "limit": values["limit"], "offset": values["offset"]})
                return _executed(_page(data, values, _actor_result, SCOPE, account), api)
            if action in ("get_build", "get_run"):
                kind = "build" if action == "get_build" else "run"
                return _executed({kind: _job_result(_job(api, kind, values[kind + "_id"], account))}, api)
            if action in ("list_builds", "list_runs"):
                _actor(api, values["actor_id"], account)
                suffix = "builds" if action == "list_builds" else "runs"
                data = _request(api, "GET", f"/actors/{values['actor_id']}/{suffix}", params={"limit": values["limit"], "offset": values["offset"], "desc": "true"})
                scope = BUILD_SCOPE if action == "list_builds" else RUN_SCOPE
                return _executed(_page(data, values, _job_result, scope, account), api)
            if action == "read_log":
                _job(api, values["kind"], values["job_id"], account)
                raw = _request(api, "GET", "/logs/" + values["job_id"], raw=True)
                text = raw.decode("utf-8", "replace")
                safe_text = _redact_result(text, api)
                clipped = safe_text[:16000]
                while len(_json(clipped)) > 48 * 1024:
                    clipped = clipped[:len(clipped) // 2]
                return _executed({"text": clipped, "truncated": len(clipped) < len(safe_text)}, api)
            if action == "export_results":
                job = _job(api, "run", values["run_id"], account)
                dataset_id = _id(job.get("defaultDatasetId"))
                raw = _request(api, "GET", f"/datasets/{dataset_id}/items", params={"format": "json", "limit": values["limit"], "offset": values["offset"]}, raw=True)
                text = raw.decode("utf-8")
                rows = _parse(text, preserve_numbers=True)
                if not isinstance(rows, list) or len(rows) > values["limit"]:
                    raise ValueError("Apify returned an invalid dataset page.")
                # Validate depth with the normal decoded-data redactor, but edit
                # only string tokens in the original JSON. Numbers (including
                # -0, long decimals and large exponents) remain byte-for-byte.
                _redact_result(rows, api)
                def redact_string(match):
                    original = match.group()
                    decoded = json.loads(original)
                    redacted = _redact_result(decoded, api)
                    return original if redacted == decoded else _json(redacted).decode("ascii")
                content = re.sub(r'"(?:[^"\\]|\\.)*"', redact_string, text).encode("utf-8")
                # Redaction of object keys must not introduce ambiguous duplicates.
                _parse(content, preserve_numbers=True)
                if len(content) > MAX_RESPONSE:
                    raise ValueError("Dataset page exceeds the export byte limit; request fewer rows.")

                @contextmanager
                def stream():
                    with io.BytesIO(content) as source:
                        yield OpenedStreamingAsset("apify-results.json", "application/json", len(content), source)

                return StreamingAsset(stream)
            payload: JSONObject = {"action": action, "account_id": account, "input": values}
            target = "new private Actor"
            if action == "run_actor":
                target, number = _run_target(api, values, account)
                payload["actor_id"] = target
                payload["build_number"] = number
            elif action != "create_actor":
                actor = _actor(api, values["actor_id"], account)
                target = values["actor_id"]
                if action == "create_version":
                    versions = actor.get("versions", [])
                    if not isinstance(versions, list) or any(isinstance(v, dict) and v.get("versionNumber") == values["version"] for v in versions):
                        raise ValueError("Version already exists; choose a new version number.")
                if action == "build_actor":
                    source = _source(api, target, values["version"])
                    if len(_json(source)) > 48 * 1024:
                        raise ValueError("Source exceeds the approval review size limit.")
                    payload["source_for_review"] = _redact_result(source, api)
                    payload["source_digest"] = _digest(source)
                    payload["tag"] = "kern-candidate-" + uuid.uuid4().hex[:15]
                if action == "publish_actor":
                    _release(api, values, account)
                    payload["listing_digest"] = _digest(_listing_state(actor))
            summary = f"Apify {action} on {target} in account {account}."
            if action == "create_actor":
                summary += f" Name: {values['name']}. Private, limited permissions."
            if action == "create_version":
                summary += f" Version {values['version']}: {len(values['files'])} source files; review exact code in payload."
            if action == "build_actor":
                summary += f" Version {values['version']}; executes build code and incurs charges without a per-build dollar cap."
            if action == "run_actor":
                summary += (f" Build {values['build_id']}; maximum ${values['max_charge_usd']:.2f}, "
                    f"{values['timeout_seconds']} seconds, {values['memory_mb']} MiB, limited permissions. "
                    "Review the exact JSON input in the payload; code executes on Apify and may contact upstream services.")
            if action == "publish_actor":
                summary += f" Public listing and latest build {values['build_id']}; default runs select latest with limited permissions; title: {clip_text(values['title'], 100)}."
            approval = api.approvals.request(action_id=action, summary=summary, payload=payload)
            return ActionPendingApproval(approval.approval_id, approval.summary)
        except Exception as exc:
            return _failure(exc)

    def execute_approved(self, approval: ApprovalRecord, api: HostAPI) -> ApprovalResult:
        try:
            payload = _object(approval.payload)
            action = payload.get("action")
            spec = MANIFEST.action(action) if isinstance(action, str) else None
            if not isinstance(action, str) or spec is None or spec.approval != "operator" or approval.action_id != action:
                raise ValueError("Invalid Apify Developer approval.")
            values = _validate(action, _object(payload.get("input")), api)
            account = _account(api)
            if payload.get("account_id") != account:
                raise ValueError("Apify account changed after approval; queue a new approval.")
            if action == "create_actor":
                actor = _object(_request(api, "POST", "/actors", body={**values, "isPublic": False, "actorPermissionLevel": "LIMITED_PERMISSIONS"}))
                return ApprovalExecuted("Created private Apify Actor " + _id(actor.get("id")) + ".")
            if action == "run_actor":
                actor_id, number = _run_target(api, values, account)
                if payload.get("actor_id") != actor_id or payload.get("build_number") != number:
                    raise ValueError("Actor or build changed after approval; queue a new approval.")
                # Validation above parses the document, but its approved number
                # lexemes (including -0) must reach JavaScript Actors unchanged.
                run = _object(_request(api, "POST", f"/actors/{actor_id}/runs", body=values["input_json"].encode("utf-8"), params={
                    "build": number, "timeout": values["timeout_seconds"], "memory": values["memory_mb"],
                    "maxTotalChargeUsd": values["max_charge_usd"], "restartOnError": "false",
                    "forcePermissionLevel": "LIMITED_PERMISSIONS", "waitForFinish": 0}))
                return ApprovalExecuted("Started Apify run " + _id(run.get("id")) + f" for Actor {actor_id}. Inspect its final status before publishing.")
            actor_id = values["actor_id"]
            actor = _actor(api, actor_id, account)
            if action == "create_version":
                # POST is create-only; an existing version must fail at the provider as well.
                versions = actor.get("versions", [])
                if not isinstance(versions, list) or any(isinstance(v, dict) and v.get("versionNumber") == values["version"] for v in versions):
                    raise ValueError("Version was created after approval; queue a new version.")
                _request(api, "POST", f"/actors/{actor_id}/versions", body={"versionNumber": values["version"], "sourceType": "SOURCE_FILES", "buildTag": None, "applyEnvVarsToBuild": False,
                    "sourceFiles": [{"name": f["path"], "content": f["content"], "format": "TEXT"} for f in values["files"]]})
                return ApprovalExecuted(f"Created Apify Actor {actor_id} version {values['version']}; no build started.")
            if action == "build_actor":
                if payload.get("source_digest") != _digest(_source(api, actor_id, values["version"])):
                    raise ValueError("Actor source or build settings changed after approval; queue a new approval.")
                tag = payload.get("tag")
                if not isinstance(tag, str) or not re.fullmatch(r"kern-candidate-[a-f0-9]{15}", tag):
                    raise ValueError("Invalid candidate build tag.")
                build = _object(_request(api, "POST", f"/actors/{actor_id}/builds", params={"version": values["version"], "tag": tag, "useCache": "false", "waitForFinish": 0}))
                return ApprovalExecuted("Started Apify build " + _id(build.get("id")) + f" for Actor {actor_id}. Inspect its final status before running.")
            if action == "publish_actor":
                if payload.get("listing_digest") != _digest(_listing_state(actor)):
                    raise ValueError("Actor listing, pricing, build tags or run defaults changed after approval; queue a new approval.")
                _release(api, values, account)
                # Preserve provider defaults, including charge limits and future
                # resource settings, while changing only the reviewed selectors.
                defaults = dict(_object(actor.get("defaultRunOptions") or {}))
                defaults.update({"build": "latest", "forcePermissionLevel": "LIMITED_PERMISSIONS"})
                # actor-put documents taggedBuilds as a per-tag patch; omitted
                # beta/stable/candidate tags remain assigned to their builds.
                _request(api, "PUT", f"/actors/{actor_id}", body={"isPublic": True, "title": values["title"], "description": values["description"], "categories": values["categories"],
                    "actorPermissionLevel": "LIMITED_PERMISSIONS", "defaultRunOptions": defaults,
                    "taggedBuilds": {"latest": {"buildId": values["build_id"]}}})
                return ApprovalExecuted(f"Published Apify Actor {actor_id} with latest build {values['build_id']}.")
            raise ValueError("Unsupported Apify Developer approval.")
        except Exception as exc:
            return _failure(exc)


BUNDLED_TOOL = ApifyDeveloperTool()
