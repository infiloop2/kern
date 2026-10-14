"""Compact native Prolific research workflow: 17 actions, four approvals.

Open upstream dictionaries also accept JSON text: Kern requires closed object
schemas. Decoded values receive the same validation and parameter guard.
"""
from dataclasses import dataclass
from typing import Any

from host.tools.manifest import ActionSpec, guarded_input, validated_input
from host.tools.shared import outputs as o


def obj(fields, required=()):
    return o.obj(fields, list(required))


def text(description):
    return o.text(description)


def enum(*values):
    return {"type": "string", "enum": list(values), "description": "One of the listed provider values."}


def array(items, description="Provider values.", maximum=1000):
    return {"type": "array", "items": items, "maxItems": maximum, "description": description}


def open_value(schema, description):
    return {"oneOf": [schema, text(description + " Alternatively supply complete JSON text; decoded keys and values are checked before sending.")], "description": description}


ID = text("Provider object ID: 24 hexadecimal characters or canonical UUID.")
INT = o.integer("JSON integer; validated against the action's documented bounds.")
PAGE = {"limit": o.integer("Page size 1–500; default 200."), "offset": o.integer("Offset 0 or greater; default 0.")}
SCALAR = {"oneOf": [text("Filter value."), {"type": "number"}, {"type": "null"}]}
FILTER = obj({"filter_id": text("Filter identifier from get_filters."),
              "selected_values": array(text("Choice ID returned by get_filters.")),
              "selected_range": obj({"lower": SCALAR, "upper": SCALAR})}, ["filter_id"])
FILTERS = open_value(array(FILTER), "Eligibility filters; default []. JSON text supports composites, weightings and provider extensions.")
STUDY_FIELDS = {
    "name": text("Public study name."), "internal_name": text("Optional internal reference name."),
    "description": text("Participant instructions; supported HTML is accepted. Up to 5120 UTF-8 bytes per text value."),
    "data_collection_method": enum("AI_TASK_BUILDER_BATCH", "AI_TASK_BUILDER_COLLECTION"),
    "data_collection_id": ID,
    "total_available_places": o.integer("Places >= 1; required except AI_TASK_BUILDER_BATCH, which calculates places when omitted."),
    "estimated_completion_time": INT, "reward": o.integer("Reward per participant in cents/pence; integer >= 0."),
    "device_compatibility": array(enum("desktop", "tablet", "mobile")),
    "peripheral_requirements": array(enum("audio", "microphone", "camera", "download")),
    "filters": FILTERS, "workspace_id": ID, "project": ID,
}
STUDY_REQUIRED = ["name", "description", "estimated_completion_time", "reward", "data_collection_method", "data_collection_id"]
STUDY = open_value(obj(STUDY_FIELDS, STUDY_REQUIRED), "Hosted AI Task Builder draft only: data_collection_method and data_collection_id are required. external_study_url, access_details and completion_codes are rejected even through JSON text; Prolific generates hosted completion defaults. Kern sets prolific_id_option=not_required internally; caller overrides are rejected. Batch studies may omit total_available_places; collections require it. Omitted devices/peripherals stay absent for project inheritance. Filters default to [] unless filter_set_id requires absent/null filters. Other provider extensions can use JSON text.")
TASK = obj({k: text("Participant task instructions.") for k in ("task_name", "task_introduction", "task_steps")}, ["task_name", "task_introduction", "task_steps"])
PAGE_ITEM = text("JSON object for one provider content block; decoded recursively before sending.")
COLLECTION_ITEM = obj({"id": ID, "order": INT, "page_items": array(PAGE_ITEM)}, ["order", "page_items"])
COLLECTION_FIELDS = {"name": text("Collection name."), "task_details": TASK, "collection_items": array(COLLECTION_ITEM)}
COLLECTION = open_value(obj({**COLLECTION_FIELDS, "workspace_id": ID}, ["name", "collection_items"]), "Full collection content; task_details is optional, but when present requires all three instruction fields; workspace_id required only when creating. Retain existing content IDs when replacing. JSON text supports provider content blocks and extensions.")
APPROVED = frozenset(("start_study", "approve_submissions", "halt_study", "save_collection"))
TRANSITIONS = {"UNPUBLISHED": "PUBLISH", "PAUSED": "START"}



@dataclass(frozen=True)
class Operation:
    method: str
    path: str
    fields: dict[str, Any]
    required: tuple[str, ...] = ()
    defaults: dict[str, Any] | None = None
    description: str = ""


OPERATIONS = {
    "get_account": Operation("GET", "/workspaces/", {}, description="Discover workspaces with nested projects and wallet/currency data when present."),
    "get_filters": Operation("GET", "/filters/", {"workspace_id": ID, "search": text("Required nonempty substring, matched case-insensitively against filter IDs, titles, questions and choice labels in Kern."), "limit": o.integer("Maximum matching filters, 1–100; default 50. Each filter returns at most 50 choices with explicit truncation flags.")}, ("search",), {"limit": 50}, description='Search the targeting catalogue locally by text, optionally scoped to a workspace; returns bounded choices and truncation flags.'),
    "get_eligibility_count": Operation("POST", "/eligibility-count/", {"filters": FILTERS, "workspace_id": text("Target workspace ID. Supply the study workspace: its country affects eligibility under US tax rules. Omission uses the provider account current workspace, whose ID is not returned."), "study_type": enum("STUDY_TYPE_SINGLE", "STUDY_TYPE_REP_SAMPLE")}, (), {"filters": [], "study_type": "STUDY_TYPE_SINGLE"}, description='Estimate eligibility for the target workspace and filters. Count 0 means fewer than 5, not necessarily none. Supply workspace_id for study planning; omission uses the account current workspace.'),
    "list_studies": Operation("GET", "/studies/", {"state": text("Study status, or (ACTIVE|PAUSED) style group. Omit for every state."), "page": o.integer("Page >= 1; default 1."), "page_size": o.integer("Page size 1–200; default 50."), "search": text("Search study name/internal_name.")}, (), {"page": 1, "page_size": 50}, description='List studies with optional state or name filtering and pagination.'),
    "get_study": Operation("GET", "/studies/{study_id}/", {"study_id": ID}, ("study_id",), description="Read study and projected cost for UNPUBLISHED/PAUSED studies. Show the estimated USD cost before requesting start_study approval."),
    "list_submissions": Operation("GET", "/studies/{study_id}/submissions/", {"study_id": ID, **PAGE}, ("study_id",), {"limit": 200, "offset": 0}, "Read submissions and current counts together."),
    "export_demographics": Operation("POST", "/studies/{study_id}/demographic-export/", {"study_id": ID, "filters": FILTERS}, ("study_id",), {"filters": []}, description='Save study submission demographics as a redacted CSV workspace file, with up to 15 applied filters. Plan first: only two different filter export requests per study; identical requests can be repeated.'),
    "list_collections": Operation("GET", "/data-collection/collections", {"workspace_id": ID, **PAGE}, ("workspace_id",), {"limit": 200, "offset": 0}, description="List a workspace's data collections with pagination."),
    "get_collection": Operation("GET", "/data-collection/collections/{collection_id}", {"collection_id": ID}, ("collection_id",), description="Read collection and its deterministic preview URL."),
    "export_collection": Operation("POST", "/data-collection/collections/{collection_id}/export", {"collection_id": ID}, ("collection_id",), description="Start a collection export and return its export_id. Poll get_collection_export_status, then save_collection_export when complete. Never automatically retry creation."),
    "get_collection_export_status": Operation("GET", "/data-collection/collections/{collection_id}/export/{export_id}", {"collection_id": ID, "export_id": ID}, ("collection_id", "export_id"), description="Read an existing collection export status. Poll every 10–30 seconds; when complete use save_collection_export for the ZIP. Signed URLs stay internal."),
    "save_collection_export": Operation("GET", "/data-collection/collections/{collection_id}/export/{export_id}", {"collection_id": ID, "export_id": ID}, ("collection_id", "export_id"), description="Save a completed export as an unchanged ZIP workspace file (up to 200 MB). Rechecks status and refreshes the private download URL. Does not create or extract exports."),
    "create_study": Operation("POST", "/studies/", {"study": STUDY}, ("study",), description='Create an unpublished hosted AI Task Builder study draft; use get_study to inspect cost before requesting start_study approval.'),
    "halt_study": Operation("POST", "/studies/{study_id}/transition/", {"study_id": ID}, ("study_id",), description="Request operator approval to permanently stop recruitment for a study. Accepts only study_id; no pause option."),
    "save_collection": Operation("POST", "/data-collection/collections", {"collection": COLLECTION, "collection_id": ID}, ("collection",), description="Request approval to create without collection_id or replace the complete existing collection. Shows exact proposed content; retain entity IDs, since omitted content is deleted on replacement."),
    "start_study": Operation("POST", "/studies/{study_id}/transition/", {"study_id": ID}, ("study_id",), description="Request approval to publish an UNPUBLISHED study or resume a PAUSED study, binding the current definition and cost."),
    "approve_submissions": Operation("POST", "/submissions/bulk-approve/", {"submission_ids": array(ID)}, ("submission_ids",), description="Request approval for 1–1000 submission_ids from list_submissions results[].id. Queues payment from reserved funds; confirm outcomes with list_submissions."),
}
GUARDED = {"search", "filters", "study", "collection"}


def specs(output_schema):
    result = []
    for name, op in OPERATIONS.items():
        approved = name in APPROVED
        cost = ("Estimated USD reservation for the whole study, deduplicated by study ID across publish/resume; not a settled invoice."
                if name == "start_study" else
                "Reports $0 additional reservation. Submission approval pays previously reserved participant rewards." if name == "approve_submissions" else
                "Reports $0; no participant reward reservation from this action.")
        result.append(ActionSpec(id=name, description=op.description or name.replace("_", " ").capitalize() + ".",
            input_schema=obj({k: field if k in op.required else o.nullable(field, field["description"]) for k, field in op.fields.items()}, op.required), output_schema={} if approved or name in ("save_collection_export", "export_demographics") else output_schema(name),
            returns_asset=name in ("save_collection_export", "export_demographics"),
            approval="operator" if approved else "direct", cost_description=cost,
            data_policy="Sends the explicit " + (", ".join(op.fields) or "authenticated request") + " to api.prolific.com. " +
                ("Downloads the provider-returned signed HTTPS URL without API credentials and saves the original ZIP through the host file pipeline." if name == "save_collection_export" else
                 "Writes only after approval; start_study reads study and projected cost before approval and again before execution." if approved else
                 "Filter search and limit are applied locally; only the authenticated catalogue request leaves the host." if name == "get_filters" else "Runs directly without approval."),
            input_protections={} if approved else {k: guarded_input(allow_longer_text=True) if k in GUARDED else validated_input("Strict provider ID, status grammar, enum or integer range.") for k in op.fields}))
    return tuple(result)
