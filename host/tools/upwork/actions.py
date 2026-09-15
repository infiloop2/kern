"""Named operations from Upwork's authenticated MCP reference (2026-09-09)."""

from dataclasses import dataclass
from typing import cast

from host.tools.json_types import JSONObject, JSONValue
from host.tools.manifest import ActionSpec
from host.tools.shared import outputs
from host.tools.shared.inputs import schema
from host.tools.upwork.responses import output_schema
from host.tools.upwork.validation import OPAQUE_FIELDS

ORG = {**outputs.text("Your org_uid from list_accounts; never a team id."), "minLength": 1, "maxLength": 128}
ID = {**outputs.text("Exact identifier returned by the related Upwork read."), "minLength": 1, "maxLength": 256}
LIMIT = {**outputs.integer("Maximum results, 1-10; defaults to 10."), "minimum": 1, "maximum": 10}
CURSOR = outputs.text("Cursor from the preceding response; omit for the first page.")
PAGING: JSONObject = {"limit": LIMIT, "cursor": CURSOR}


@dataclass(frozen=True)
class Operation:
    tool: str
    action: str
    spec: ActionSpec
    input_schema: JSONObject


def operation(action_id: str, tool: str, action: str, description: str,
              properties: JSONObject, required: tuple[str, ...] = (), *,
              approval: bool = False, organization: bool = True) -> Operation:
    fields = {"org_uid": ORG, **properties} if organization else properties
    required_fields = ["org_uid", *required] if organization else list(required)
    policy = (
        "After your approval, sends the exact displayed parameters to Upwork. No automatic retries. "
        "Results enter approval history and model context."
        if approval else
        "Sends these parameters to Upwork using your connected account. Structured response data enters model context."
    )
    if action_id == "submit_proposal":
        policy = "Reads job details to show the Connects cost before approval. After approval, creates a private preview from the exact approved proposal, checks its content and cost, then submits. Changed terms or cost require a new approval."
    constraints = schema(fields, required_fields)
    # Kern's advertised schema vocabulary is structural; enforce these extra
    # provider limits in the adapter without widening the host-wide contract.
    def public_schema(value: JSONValue) -> JSONValue:
        if isinstance(value, dict):
            result = {key: public_schema(child) for key, child in value.items()
                      if key not in ("minimum", "maximum", "minLength", "maxLength")}
            return result
        if isinstance(value, list):
            return [public_schema(child) for child in value]
        return value
    return Operation("upwork__" + tool, action, ActionSpec(
        id=action_id, description=description, data_policy=policy,
        input_schema=cast(JSONObject, public_schema(constraints)),
        approval="operator" if approval else "direct",
        output_schema={} if approval else output_schema(action_id),
    ), constraints)


OPERATIONS = {row.spec.id: row for row in (
    operation("list_accounts", "list_accounts", "", "List your available Upwork accounts and organization IDs.", {}, organization=False),
    operation("get_profile", "get_profile", "get", "Read your freelancer profile, or a profile returned by Upwork.", {"profile_key": ID}),
    operation("get_profile_highlights", "get_profile", "list_highlights", "List your portfolio projects and certificates available for proposal highlights.", {}),
    operation("get_connects_balance", "get_profile", "connects_balance", "Read your Connects balance and recent usage. Product-specific ad credits are separate.", PAGING),
    operation("get_dashboard", "get_freelancer_dashboard", "check", "Read your freelancer dashboard: invitations, offers, messages, matches and Connects.", {}),
    operation("search_jobs", "find_jobs", "search", "Search marketplace jobs. Use title for title-only matching, or query for broad matching; do not combine them. Results have no date filter.", {
        "query": outputs.text("Broad search across titles and descriptions."),
        "title": outputs.text("One to three title words; cannot combine with query."),
        "job_type": {"description": "Fixed-price or hourly jobs.", "type": "string", "enum": ["fixed", "hourly"]},
        "experience_level": {"description": "Experience level requested by the client.", "type": "string", "enum": ["entry_level", "intermediate", "expert"]},
        "budget_min": outputs.number("Minimum fixed-price project budget, greater than zero; requires job_type=fixed."),
        "budget_max": outputs.number("Maximum fixed-price project budget, greater than zero; requires job_type=fixed."),
        "rate_min": outputs.number("Minimum hourly rate, greater than zero; pair with job_type=hourly."),
        "rate_max": outputs.number("Maximum hourly rate, greater than zero; pair with job_type=hourly."),
        "skills": {"description": "Up to five exact Upwork skill names.", "type": "array", "items": outputs.text("Exact Upwork skill name."), "maxItems": 5},
        "verified_payment_only": outputs.boolean("Only clients with verified payment methods."),
        "sort": {"description": "Result ordering; relevance cannot combine with title or skills.", "type": "string", "enum": ["recency", "relevance", "client_total_charge", "client_rating"]},
        **PAGING,
    }),
    operation("get_job", "find_jobs", "get", "Read full job details, client history, qualifications, application eligibility and Connects cost.", {"job_id": ID}, ("job_id",)),
    operation("get_recommended_jobs", "find_jobs", "smart_search", "Read personalized job recommendations. Date filters apply only to most_recent.", {
        "mode": {"description": "Personalized best matches or most recent jobs.", "type": "string", "enum": ["best_match", "most_recent"]},
        "days_posted": {**outputs.integer("Posted within this many days; use most_recent."), "minimum": 1},
        "from_date": outputs.text("Start date, YYYY-MM-DD or RFC3339; cannot combine with days_posted."),
        "to_date": outputs.text("Inclusive end date, YYYY-MM-DD or RFC3339; cannot combine with days_posted."),
        **PAGING,
    }),
    operation("list_proposals", "list_freelancer_proposals", "list", "List your proposals. Accepted means submitted, not that the client hired you.", {
        "status": {"description": "Proposal status; Accepted means submitted, not hired.", "type": "string", "enum": ["Accepted", "Offered", "Hired", "Activated", "Pending", "Declined", "Withdrawn", "Archived"]}, **PAGING,
    }),
    operation("get_proposal", "list_freelancer_proposals", "get", "Read one proposal, its terms, stored Connects bid and available insights.", {"proposal_id": ID}, ("proposal_id",)),
    operation("list_invitations", "list_freelancer_proposals", "invitations", "List client invitations, optionally for one job. Check before preparing a marketplace proposal.", {"job_posting_id": ID, "status": outputs.text("Invitation status; defaults to pending."), **PAGING}),
    operation("list_conversations", "get_messages", "list_rooms", "List message rooms in recent-activity order. Follow the cursor before claiming complete coverage.", {"unread_only": outputs.boolean("Only rooms with unread messages."), "room_type": {"description": "Conversation category to include.", "type": "string", "enum": ["ALL", "GROUP", "ONE_ON_ONE", "INTERVIEW"]}, **PAGING}),
    operation("read_messages", "get_messages", "list_messages", "Read a conversation newest first. Follow the cursor for older messages.", {"room_id": ID, **PAGING}, ("room_id",)),
    operation("submit_proposal", "manage_proposals", "create", "Queue approval for the complete proposal, terms and Connects cost. First check job requirements, invitations and existing proposals. After approval, prepares and submits internally.", {
        "job_reference": {**ID, "description": "Numeric job id from search_jobs, not the ~02 ciphertext."},
        "cover_letter": {**outputs.text("Exact cover letter, at most 5,000 characters; shown in full for approval."), "minLength": 1, "maxLength": 5000},
        "charged_amount": {**outputs.number("Proposed rate or project amount."), "minimum": 0.01},
        "answers": {"description": "Answers to every required screening question.", "type": "array", "maxItems": 20, "items": schema({"question": outputs.text("Screening question."), "answer": outputs.text("Your answer.")}, ["question", "answer"])},
        "boost_connects": {**outputs.integer("Optional additional Connects bid. Omit for no boost; never invent a bid."), "minimum": 1},
        "team_org_id": ID,
        "attachments": {"description": "Existing proposal upload file_uid values; Kern does not upload files.", "type": "array", "items": ID, "maxItems": 10},
        "certificate_ids": {"description": "Selected certificate IDs from get_account section=highlights.", "type": "array", "items": ID, "maxItems": 10},
        "portfolio_project_ids": {"description": "Selected project IDs from get_account section=highlights.", "type": "array", "items": ID, "maxItems": 10},
    }, ("job_reference", "cover_letter", "charged_amount"), approval=True),
    operation("send_message", "send_message", "send", "Queue approval to send the exact message to an existing conversation. Freelancers cannot initiate a proposal conversation before the client contacts them.", {"room_id": ID, "message": {**outputs.text("Exact message, at most 10,240 characters; shown in full for approval."), "minLength": 1, "maxLength": 10240}}, ("room_id", "message"), approval=True),
)}

# Only these closely related account reads share an agent-facing action.
# Each section retains its own closed input contract in OPERATIONS.
ACCOUNT_READS = {
    "profile": "get_profile",
    "dashboard": "get_dashboard",
    "highlights": "get_profile_highlights",
    "connects": "get_connects_balance",
}
_account_fields: JSONObject = {"section": {"type": "string", "enum": list(ACCOUNT_READS),
    "description": "Account section to read: profile, dashboard, portfolio/certificates, or Connects balance and usage."}}
for _name in ACCOUNT_READS.values():
    _account_fields.update(cast(JSONObject, OPERATIONS[_name].spec.input_schema["properties"]))
GET_ACCOUNT = ActionSpec(
    id="get_account",
    description="Read your profile, dashboard, portfolio/certificates, or Connects balance. Choose section. profile_key is only for profile; limit/cursor are only for connects.",
    data_policy="Sends the selected section's explicit parameters to Upwork. Bounded account data enters model context. Runs without approval.",
    input_schema=schema(_account_fields, ["section", "org_uid"]),
    output_schema=outputs.obj({
        section: output_schema(name) for section, name in ACCOUNT_READS.items()
    }),
)
ACTIONS = tuple(
    GET_ACCOUNT if name == "get_profile" else row.spec
    for name, row in OPERATIONS.items()
    if name == "get_profile" or name not in ACCOUNT_READS.values()
)
ACTION_IDS = frozenset(row.id for row in ACTIONS)
