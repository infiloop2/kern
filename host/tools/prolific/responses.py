"""Closed documented result fields with complete, redacted JSON fallback."""
import json
import re
from urllib.parse import quote
from typing import Any, cast

from host.tools.json_types import JSONObject

from host.tools.shared import outputs as o

MAX_BYTES = 1024 * 1024
MAX_FILTER_BYTES = 8 * MAX_BYTES
MAX_FILTER_RESULT_BYTES = 256 * 1024
MAX_RESULT_BYTES = 2 * MAX_BYTES + 65536
ID_RE = re.compile(r"(?:[a-fA-F0-9]{24}|[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12})\Z")
CREDENTIAL_POOL_RE = re.compile(r"cred_pool_[A-Za-z0-9_-]{1,64}\Z")
SECRET_KEY = re.compile(r"(?:token|secret|password|authorization|api[_-]?key|cookie|credential)", re.I)
SECRET_TEXT = re.compile(r"(?i)(?:Bearer|Token)\s+[A-Za-z0-9_.~+/=-]+|(?:secret|password|api[_-]?key|access[_-]?token)\s*[:=]\s*[^\s,;]+")


def dumps(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True)


def loads(value):
    def pairs(rows):
        result = {}
        for k, v in rows:
            if k in result:
                raise ValueError("Duplicate JSON fields are not accepted.")
            result[k] = v
        return result
    try:
        result = json.loads(value, object_pairs_hook=pairs)
        dumps(result)
        return result
    except (ValueError, RecursionError, UnicodeError):
        raise ValueError("Prolific returned invalid JSON; check the provider before retrying a write.") from None


def redact_text(value, secrets=()):
    original = value
    for _ in range(20):
        for secret in secrets:
            if secret:
                for variant in (secret, quote(secret, safe=""), json.dumps(secret)[1:-1]):
                    value = value.replace(variant, "[REDACTED]")
        value = SECRET_TEXT.sub("[REDACTED]", value)
        decoded = re.sub(r'\\(?:u[0-9a-fA-F]{4}|["\\/bfnrt])',
                         lambda match: json.loads('"' + match[0] + '"'), value)
        if decoded == value:
            break
        value = decoded
    else:
        raise ValueError("Prolific response exceeds the escape nesting limit.")
    if "[REDACTED]" not in value:
        return original
    return value.encode("utf-8", errors="backslashreplace").decode("utf-8").replace("\x00", "\\u0000")



def redact(value, secrets=(), depth=0, *, members=False):
    if depth > 32:
        raise ValueError("Prolific response exceeds the nesting limit.")
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            safe_key = redact_text(key, secrets)
            if safe_key in result:
                raise ValueError("Prolific response keys collide after redaction.")
            # These documented metadata fields contain no credential values.
            metadata = (key == "has_credentials" and type(child) is bool) or (
                key == "credential_pool_id" and (child is None or isinstance(child, str)
                    and (ID_RE.fullmatch(child) is not None or CREDENTIAL_POOL_RE.fullmatch(child) is not None)))
            result[safe_key] = "[REDACTED]" if (members and key == "users") or (SECRET_KEY.search(key) and not metadata) else redact(child, secrets, depth + 1, members=members)
        return result
    if isinstance(value, list):
        return [redact(child, secrets, depth + 1, members=members) for child in value]
    if isinstance(value, str):
        return redact_text(value, secrets)
    return value


TEXT = o.text("Provider value.")
NUMBER = o.number("Provider numeric value.")
BOOLEAN = o.boolean("Provider boolean value.")
STUDY = o.obj({**{k: TEXT for k in ("id", "name", "description", "status", "external_study_url", "prolific_id_option", "project", "currency")},
    "internal_name": o.nullable(TEXT, "Internal study name."),
    "filter_set_id": o.nullable(TEXT, "Saved targeting filter set, when present."),
    "credential_pool_id": o.nullable(TEXT, "Credential pool identifier; never credential values."),
    "has_credentials": BOOLEAN,
    **{k: NUMBER for k in ("reward", "total_available_places", "estimated_completion_time", "total_cost", "places_taken", "number_of_submissions")}})
BALANCE = o.obj({**{k: TEXT for k in ("currency", "currency_code")}, **{k: NUMBER for k in ("balance", "total_balance", "available_balance")}})
PROJECT = o.obj({k: TEXT for k in ("id", "title", "description", "owner", "type", "workspace")})
COLLECTION = o.obj({k: TEXT for k in ("id", "name", "workspace_id", "created_at", "created_by", "last_modified_at", "last_modified_by")})
SUBMISSION = o.obj({**{k: TEXT for k in ("id", "study_id", "participant_id", "status", "started_at", "completed_at", "entered_code")}, "time_taken": NUMBER})
COUNTS = o.obj({k: NUMBER for k in ("ACTIVE", "APPROVED", "AWAITING REVIEW", "REJECTED", "RESERVED", "RETURNED", "TIMED-OUT", "PARTIALLY APPROVED", "SCREENED OUT", "TOTAL")})
EXPORT = o.obj({"status": TEXT, "export_id": TEXT, "expires_at": o.nullable(TEXT, "Link expiry.")})
COST = o.obj({"currency": TEXT, "reward": NUMBER, "places": NUMBER,
              "projected_total_minor_units": o.text("Estimated reservation in cents/pence, applying the platform-fee floor."),
              "estimated_usd": o.text("Decimal USD reservation estimate, rounded up to cents."),
              "usd_per_currency_unit": o.text("Planning FX rate, not a live quote.")},
             ["currency", "reward", "places", "projected_total_minor_units", "estimated_usd", "usd_per_currency_unit"])


def listing(item):
    return o.obj({"results": o.array_of(item, "Provider records."), "meta": o.obj({"count": NUMBER, "next": o.nullable(TEXT, "Next page."), "previous": o.nullable(TEXT, "Previous page.")})})


def with_details(schema):
    return o.obj({**cast(JSONObject, schema["properties"]), "provider_details_json": o.text("Complete redacted provider JSON when any field/type is not modeled; never clipped.")})


WORKSPACE = o.obj({**{k: TEXT for k in ("id", "title", "description", "owner", "currency", "currency_code")},
                   **{k: NUMBER for k in ("balance", "total_balance", "available_balance")},
                   "wallet": BALANCE, "projects": listing(PROJECT)})
BASE = {
    "get_account": o.obj({"workspaces": o.array_of(WORKSPACE, "Accessible workspaces with their projects.")}),
    "get_eligibility_count": o.obj({"count": o.number("Privacy-safe count; zero means fewer than five, including zero."),
        "count_note": o.text("Provider privacy-floor semantics."),
        "workspace_id": o.nullable(TEXT, "Requested workspace ID; null means provider current workspace identity was not returned."),
        "workspace_scope": o.text("requested_workspace or provider_current_workspace_unverified; supply an explicit workspace for study planning.")}),
    "list_studies": listing(STUDY), "create_study": STUDY,
    "get_study": o.obj({"study": STUDY, "cost": o.nullable(COST, "Projected cost for UNPUBLISHED or PAUSED studies; otherwise null.")}),
    "list_submissions": o.obj({"counts": COUNTS, **cast(JSONObject, listing(SUBMISSION)["properties"])}),
    "list_collections": listing(COLLECTION), "get_collection": o.obj({**cast(JSONObject, COLLECTION["properties"]), "preview_url": TEXT}),
    "export_collection": EXPORT, "get_collection_export_status": EXPORT,
}
RANGE: JSONObject = {"oneOf": [TEXT, NUMBER, {"type": "null"}]}
FILTER = o.obj({**{k: TEXT for k in ("id", "title", "type", "question", "data_type")},
                **{k: RANGE for k in ("min", "max", "min_max", "max_min")},
                "choices": o.array_of(o.obj({"id": TEXT, "label": TEXT}, ["id", "label"]), "At most 50 choices; matching labels first."),
                "choice_count": NUMBER, "truncated": BOOLEAN}, ["id", "title", "type", "question", "choices", "choice_count", "truncated"])
LOCAL = {
    "get_filters": o.obj({"results": o.array_of(FILTER, "Matching filters only."), "match_count": NUMBER, "truncated": BOOLEAN}, ["results", "match_count", "truncated"]),
}


def output_schema(action):
    return LOCAL[action] if action in LOCAL else with_details(BASE[action])


def search_filters(catalogue, search, limit, secrets=()) -> JSONObject:
    if not isinstance(catalogue, dict) or not isinstance(catalogue.get("results"), list):
        raise ValueError("Prolific returned an invalid filter catalogue.")
    needle = search.casefold().strip()
    rows: list[dict[str, Any]] = []
    count = 0
    for item in catalogue["results"]:
        item = redact(item, secrets)
        if not isinstance(item, dict) or not isinstance(item.get("filter_id"), str):
            raise ValueError("Prolific returned an invalid filter record.")
        choices = item.get("choices", {})
        if not isinstance(choices, dict):
            raise ValueError("Prolific returned invalid filter choices.")
        labels = [(key, value if isinstance(value, str) else dumps(value)) for key, value in choices.items()]
        matching = [(key, label) for key, label in labels if needle in label.casefold()]
        if not matching and not any(needle in str(item.get(key, "")).casefold() for key in ("filter_id", "title", "question")):
            continue
        count += 1
        if len(rows) == limit:
            continue
        # An exact choice-label search must remain useful even when its match
        # occurs beyond the first fifty choices in the provider catalogue.
        selected = (matching + [(key, label) for key, label in labels if needle not in label.casefold()])[:50]
        row = {"id": item["filter_id"], **{key: str(item.get(key, "")) for key in ("title", "type", "question")},
               "choices": [{"id": key, "label": label} for key, label in selected],
               "choice_count": len(labels), "truncated": len(labels) > 50}
        for key in ("data_type", "min", "max", "min_max", "max_min"):
            if key in item:
                value = item[key]
                if value is not None and type(value) not in (str, int, float):
                    raise ValueError("Prolific returned invalid filter range metadata.")
                if key == "data_type" and not isinstance(value, str):
                    raise ValueError("Prolific returned an invalid filter data type.")
                row[key] = value
        rows.append(row)
    result = {"results": rows, "match_count": count, "truncated": count > limit}
    if len(dumps(result).encode()) > MAX_FILTER_RESULT_BYTES:
        raise ValueError("Matching filters exceed 256 KiB; narrow search or reduce limit.")
    return cast(JSONObject, result)


_MISSING = object()


def project(value, schema):
    if "oneOf" in schema:
        for alternative in schema["oneOf"]:
            parsed, complete = project(value, alternative)
            if parsed is not _MISSING:
                return parsed, complete
        return _MISSING, False
    kind = schema["type"]
    if kind == "object" and isinstance(value, dict):
        result, complete = {}, True
        for key, child in value.items():
            if key not in schema["properties"]:
                complete = False
                continue
            parsed, covered = project(child, schema["properties"][key])
            if parsed is not _MISSING:
                result[key] = parsed
            complete &= covered
        return result, complete
    if kind == "array" and isinstance(value, list):
        result, complete = [], True
        for child in value:
            parsed, covered = project(child, schema["items"])
            if parsed is _MISSING:
                return _MISSING, False
            result.append(parsed)
            complete &= covered
        return result, complete
    valid = ((kind == "string" and isinstance(value, str)) or
             (kind == "number" and type(value) in (int, float)) or
             (kind == "boolean" and type(value) is bool) or
             (kind == "null" and value is None))
    return (value, True) if valid else (_MISSING, False)


def normalize(action, value, secrets=()) -> JSONObject:
    if action == "list_submissions" and isinstance(value, dict) and isinstance(value.get("results"), list):
        # Provider submission metadata includes IP and birth date in strata.
        # Demographic exports are explicit; ordinary listings omit this block.
        value = {**value, "results": [
            {key: "[REDACTED]" if key in ("ip", "strata") else child for key, child in row.items()}
            if isinstance(row, dict) else row for row in value["results"]]}
    clean = redact(value, secrets, members=action == "get_account")
    result, complete = project(clean, BASE[action])
    if not isinstance(result, dict):
        result = {}
    if not complete:
        result["provider_details_json"] = dumps(clean)
    if len(dumps(result).encode()) > MAX_RESULT_BYTES:
        raise ValueError("Prolific result exceeds the output limit; request a smaller page.")
    return result
