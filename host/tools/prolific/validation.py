"""Local semantic validation and recursive free-text guards before egress."""
import copy
import math
import re

from host.tools.shared.inputs import decoded_url_component_values
from .actions import OPERATIONS, APPROVED, STUDY_REQUIRED, GUARDED
from .responses import dumps, loads, ID_RE, CREDENTIAL_POOL_RE

IDS = frozenset(("study_id", "workspace_id", "collection_id", "export_id", "submission_ids", "project", "id", "filter_set_id", "data_collection_id", "credential_pool_id", "participant_group"))
STATUSES = frozenset(("UNPUBLISHED", "SCHEDULED", "PUBLISHING", "ACTIVE", "AWAITING REVIEW", "PAUSED", "COMPLETED", "UNKNOWN"))
MAX_INPUT_BYTES = 65536


def identifier(value):
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise ValueError("Prolific IDs must be 24 hexadecimal characters or canonical UUIDs.")
    return value.lower()


def integer(value, low, high=2147483647):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"Prolific integer must be between {low} and {high}.")
    return value


def guard_text(value, api):
    value = api.outbound.guard_request_parameter_string(value, allow_longer_text=True)
    # Inspect semantic URL interpretations, including nested percent encodings.
    for decoded in decoded_url_component_values(value, plus=True):
        api.outbound.guard_request_parameter_string(decoded, allow_longer_text=True)
    return value


def tree(value, api, *, guarded=True, depth=0, field="", counter=None, free_context=False):
    counter = [0] if counter is None else counter
    counter[0] += 1
    if depth > 16 or counter[0] > 10000:
        raise ValueError("Prolific input exceeds nesting/item bounds.")
    if field == "credential_pool_id" and isinstance(value, str) and CREDENTIAL_POOL_RE.fullmatch(value):
        return value  # Opaque provider-prefixed ID; preserve suffix casing.
    if field in IDS:
        if field == "submission_ids":
            if not isinstance(value, list) or not 1 <= len(value) <= 1000:
                raise ValueError("Supply 1–1000 IDs.")
            return [identifier(item) for item in value]
        return identifier(value)
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,127}|[0-9]{1,9}", key):
                raise ValueError("Prolific JSON keys must be bounded provider field names.")
            if guarded:
                guard_text(key, api)
            result[key] = tree(child, api, guarded=guarded, depth=depth + 1, field=key, counter=counter, free_context=free_context or depth == 0 and key in GUARDED)
        return result
    if isinstance(value, list):
        if len(value) > 1000:
            raise ValueError("Prolific lists may contain at most 1000 items.")
        return [tree(v, api, guarded=guarded, depth=depth + 1, field=field, counter=counter, free_context=free_context) for v in value]
    if isinstance(value, str):
        if "\x00" in value or len(value.encode()) > MAX_INPUT_BYTES:
            raise ValueError("Prolific text exceeds its bound or contains NUL.")
        # Filter choices can refer to previously returned study/participant IDs.
        if field == "selected_values" and ID_RE.fullmatch(value):
            return identifier(value)
        return guard_text(value, api) if guarded else value
    if type(value) in (int, float):
        if abs(value) > 9007199254740991 or not math.isfinite(value):
            raise ValueError("Prolific numbers must be finite safe JSON numbers.")
        if guarded and free_context:
            guard_text(str(value), api)
        return value
    if value is None or type(value) is bool:
        return value
    raise ValueError("Unsupported Prolific input value.")


def decode_model(value, kind):
    if isinstance(value, str):
        value = loads(value)
    if not isinstance(value, dict):
        raise ValueError(f"{kind} must be an object or JSON object text.")
    value = copy.deepcopy(value)
    if kind == "study":
        if set(STUDY_REQUIRED) - value.keys():
            raise ValueError("study is missing required draft fields.")
        if {"external_study_url", "access_details", "completion_codes"} & value.keys():
            raise ValueError("Hosted studies do not accept external_study_url, access_details or completion_codes; Prolific supplies completion defaults.")
        if "prolific_id_option" in value:
            raise ValueError("prolific_id_option is fixed to not_required for hosted studies; omit it.")
        value["prolific_id_option"] = "not_required"
        method = value["data_collection_method"]
        if method not in ("AI_TASK_BUILDER_BATCH", "AI_TASK_BUILDER_COLLECTION"):
            raise ValueError("Use AI_TASK_BUILDER_BATCH or AI_TASK_BUILDER_COLLECTION.")
        identifier(value["data_collection_id"])
        for k in ("name", "description"):
            if not isinstance(value[k], str) or not value[k]:
                raise ValueError(f"study.{k} must be nonempty text.")
        integer(value["reward"], 0)
        if method != "AI_TASK_BUILDER_BATCH" or "total_available_places" in value:
            integer(value.get("total_available_places"), 1)
        integer(value["estimated_completion_time"], 1)
        for k, choices in (("device_compatibility", ("desktop", "tablet", "mobile")), ("peripheral_requirements", ("audio", "microphone", "camera", "download"))):
            if k in value and (not isinstance(value[k], list) or any(v not in choices for v in value[k])):
                raise ValueError(f"Invalid study.{k}.")
        saved_filters = value.get("filter_set_id") is not None
        if saved_filters:
            if value.get("filters") is not None:
                raise ValueError("Use filter_set_id or inline filters, never both.")
        else:
            value["filters"] = filters(value.get("filters", []))
        value = {k: v for k, v in value.items() if v is not None or saved_filters and k == "filters"}
    else:
        if not isinstance(value.get("name"), str) or not value["name"]:
            raise ValueError("collection.name must be nonempty text.")
        task = value.get("task_details")
        if "task_details" in value and (not isinstance(task, dict) or any(not isinstance(task.get(k), str) for k in ("task_name", "task_introduction", "task_steps"))):
            raise ValueError("collection.task_details requires task_name, task_introduction and task_steps.")
        items = value.get("collection_items")
        if not isinstance(items, list) or not items:
            raise ValueError("collection_items must be a nonempty list.")
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("Collection pages must be objects.")
            integer(item.get("order"), 0)
            pages = item.get("page_items")
            if not isinstance(pages, list):
                raise ValueError("page_items must be a list.")
            item["page_items"] = [loads(p) if isinstance(p, str) else p for p in pages]
            if any(not isinstance(p, dict) for p in item["page_items"]):
                raise ValueError("Page content blocks must be objects.")
    return value


def filters(value):
    value = loads(value) if isinstance(value, str) else value
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(v, dict) for v in value):
        raise ValueError("filters must be an array of objects or JSON array text.")
    return value


def parameters(action, value, api):
    op = OPERATIONS[action]
    if not isinstance(value, dict) or set(value) - op.fields.keys() or set(op.required) - value.keys():
        raise ValueError("Missing required or unknown Prolific parameters.")
    if len(dumps(value).encode()) > MAX_INPUT_BYTES:
        raise ValueError("Prolific input exceeds 64 KiB.")
    p = copy.deepcopy(op.defaults or {})
    p.update({k: v for k, v in value.items() if v is not None or k in op.required})
    for k, v in p.items():
        if k in IDS:
            if k == "submission_ids":
                if not isinstance(v, list) or not 1 <= len(v) <= 1000:
                    raise ValueError("Supply 1–1000 IDs.")
                for item in v:
                    identifier(item)
            else:
                identifier(v)
        elif k in ("page", "page_size", "limit", "offset"):
            integer(v, 0 if k == "offset" else 1, 100 if action == "get_filters" else 200 if k == "page_size" else 500 if k == "limit" else 2147483647)
        elif k == "state":
            if not isinstance(v, str) or not (v in STATUSES or v.startswith("(") and v.endswith(")") and all(x in STATUSES for x in v[1:-1].split("|"))):
                raise ValueError("Invalid study state.")
        elif k == "study_type":
            if v not in ("STUDY_TYPE_SINGLE", "STUDY_TYPE_REP_SAMPLE"):
                raise ValueError("Invalid study_type.")
        elif k not in ("filters", "study", "collection") and not isinstance(v, str):
            raise ValueError(f"{k} must be text.")
    if "filters" in p:
        p["filters"] = filters(p["filters"])
        if action == "export_demographics" and len(p["filters"]) > 15:
            raise ValueError("Demographic exports permit at most 15 filters.")
    for model in ("study", "collection"):
        if model in p:
            p[model] = decode_model(p[model], model)
    if action == "save_collection" and "collection_id" not in p:
        identifier(p["collection"].get("workspace_id"))
    if action == "save_collection" and "collection_id" in p and "workspace_id" in p["collection"]:
        raise ValueError("A collection's workspace cannot be changed.")
    p = tree(p, api, guarded=action not in APPROVED)
    if action == "get_filters" and not p["search"].strip():
        raise ValueError("Filter search must contain non-whitespace text.")
    return p
