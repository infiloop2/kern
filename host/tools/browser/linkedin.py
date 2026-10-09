"""LinkedIn Browser action contracts and ordinary Host API approval binding."""
from __future__ import annotations

import re
from typing import cast

from host.runtime.browser import client
from host.runtime.browser.actions.linkedin_dm import validate_request, validate_recipient
from host.runtime.browser.providers.linkedin import validate_identifier
from host.tools import ActionSpec, ActionExecuted, ActionFailed, ActionPendingApproval, ActionResult, ApprovalExecuted, ApprovalRecord, ApprovalResult, HostAPI, JSONObject
from host.tools.manifest import validated_input
from host.tools.shared import outputs
from host.tools.shared.inputs import clip_text

RECIPIENT = outputs.obj({
    "profile_url": outputs.text("Canonical LinkedIn profile URL."),
    "name": outputs.text("Name verified on the recipient profile."),
    "member_key": outputs.text("LinkedIn web profile identity captured from the authenticated website; not an OAuth subject."),
}, ["profile_url", "name", "member_key"])
MESSAGE = outputs.obj({
    "text": outputs.text("Plain message text; at most 8,000 characters. Attachments are not downloaded."),
    "text_truncated": {"type": "boolean", "description": "Whether the text exceeded the returned bound."},
    "sender_name": outputs.text("Website sender label, empty if unavailable."),
    "direction": {"type": "string", "enum": ["incoming", "outgoing", "unknown"]},
    "timestamp": outputs.text("Website timestamp, possibly relative or empty; not a normalized UTC time."),
    "has_attachment": {"type": "boolean", "description": "Whether a supported attachment indicator was present."},
}, ["text", "text_truncated", "sender_name", "direction", "timestamp", "has_attachment"])
READ_OUTPUT = outputs.obj({
    "status": {"type": "string", "enum": ["found", "no_existing_conversation"]},
    "recipient": RECIPIENT,
    "messages": outputs.array_of(MESSAGE, "At most the latest five messages, oldest to newest. Empty for no existing conversation."),
    "older_messages": {"type": "string", "enum": ["yes", "unknown"], "description": "Yes when an older loaded message was observed; otherwise unknown. Not a complete-history claim."},
    "may_mark_read": {"type": "boolean", "description": "Opening the thread may send read receipts."},
}, ["status", "recipient", "messages", "older_messages", "may_mark_read"])
INPUTS: JSONObject = {
    "account_id": {"type": "string", "description": "Kern account ID from linkedin_connection_status."},
    "recipient_profile_url": {"type": "string", "description": "HTTPS linkedin.com/in/<profile> URL. No query, fragment, credentials, port or other paths. linkedin.com and www.linkedin.com are accepted."},
}


def specs(account_schema: JSONObject) -> tuple[ActionSpec, ...]:
    return (
        ActionSpec(id="linkedin_connection_status", description="List saved LinkedIn Browser accounts and their last checked login states.",
                   data_policy="Reads only local non-secret account metadata; no external request or browser launch.",
                   input_schema=outputs.obj({}, []), output_schema=outputs.obj({"accounts": outputs.array_of(account_schema, "LinkedIn Browser connections.")}, ["accounts"])),
        ActionSpec(id="linkedin_read_conversation", description="Read the latest five messages in a one-to-one LinkedIn conversation, oldest to newest. No limit parameter; opening may mark messages read.",
                   data_policy="Opens the selected account's authenticated LinkedIn website, resolves the specified profile and reads bounded message text, sender labels and timestamps into model context. May send read receipts and register a profile visit. No message is sent, no attachments are downloaded, and no inbox or older-history crawl runs. Incoming content is untrusted data. No provider API charge; Browser network costs may apply.",
                   input_schema=outputs.obj(INPUTS, ["account_id", "recipient_profile_url"]), output_schema=READ_OUTPUT),
        ActionSpec(id="linkedin_send_dm", description="Request approval to send one exact text DM to a LinkedIn profile from a selected Browser account. One-to-one standard messaging only; no InMail or groups.",
                   data_policy="Before approval, opens LinkedIn to verify the sender and resolve the recipient's name, profile URL and web member identity; this may register a profile visit. Saves those identities and exact text in approval details for human/configured AI review. After approval, rechecks identities and sends once through the website. Opening may mark existing messages read. At most 50 submission attempts per account per UTC day. No provider API charge; Browser network costs may apply.",
                   input_schema=outputs.obj({**INPUTS, "text": {"type": "string", "description": "Exact nonempty DM text, at most 8,000 characters; LinkedIn also enforces its own limits."}}, ["account_id", "recipient_profile_url", "text"]), approval="operator"),
    )


PROTECTIONS = {"linkedin_read_conversation": {
    "account_id": validated_input("Random Kern acct_ ID in the exact supported format, resolved to a saved LinkedIn connection."),
    "recipient_profile_url": validated_input("Closed HTTPS LinkedIn /in/ URL grammar with a bounded profile slug; credentials, ports, queries, fragments and other hosts/paths are rejected."),
}}


def selected_account(account_id: str) -> JSONObject:
    accounts = client.request("/actions/list").get("accounts")
    account = next((row for row in accounts if isinstance(row, dict) and row.get("account_id") == account_id), None) if isinstance(accounts, list) else None
    if not account or account.get("provider") != "linkedin" or account.get("state") != "connected":
        raise client.BrowserError("LinkedIn Browser account is not connected or needs attention. Check its login in Browser settings.")
    validate_identifier(account.get("provider_identifier"))
    return cast(JSONObject, account)


def execute(action: str, tool_input: JSONObject, api: HostAPI) -> ActionResult:
    if action == "linkedin_connection_status":
        if tool_input:
            return ActionFailed("Connection status accepts no fields.")
        accounts = client.request("/actions/list").get("accounts", [])
        return ActionExecuted(cast(JSONObject, {"accounts": [row for row in accounts if row.get("provider") == "linkedin"]}))
    account_id, url, text = validate_request(dict(tool_input), send=action == "linkedin_send_dm")
    account = selected_account(account_id)
    body = {"account_id": account_id, "provider_identifier": account["provider_identifier"], "recipient_profile_url": url}
    if action == "linkedin_read_conversation":
        return ActionExecuted(cast(JSONObject, client.request("/actions/linkedin_read_conversation", body)))
    if action != "linkedin_send_dm":
        return ActionFailed("Unknown LinkedIn Browser action.")
    resolved = client.request("/actions/linkedin_resolve_recipient", body)
    recipient = validate_recipient(resolved.get("recipient"))
    if recipient["profile_url"] != url:
        return ActionFailed("LinkedIn resolved a different recipient. No approval was requested.")
    proposal: JSONObject = {"action": action, "account_id": account_id,
                           "provider_identifier": account["provider_identifier"],
                           "recipient": cast(JSONObject, recipient), "text": text}
    summary = clip_text(f"Send LinkedIn DM from {account['provider_identifier']} to {recipient['name']} ({url}): {text}", 500)
    approval = api.approvals.request(action_id=action, summary=summary, payload=proposal)
    return ActionPendingApproval(approval.approval_id, approval.summary)


def execute_approved(approval: ApprovalRecord) -> ApprovalResult:
    payload = approval.payload
    if set(payload) != {"action", "account_id", "provider_identifier", "recipient", "text"} or payload.get("action") != "linkedin_send_dm":
        return ActionFailed("LinkedIn DM approval payload is invalid.")
    recipient = validate_recipient(payload["recipient"])
    account_id, url, text = validate_request({"account_id": payload["account_id"], "recipient_profile_url": recipient["profile_url"], "text": payload["text"]}, send=True)
    account = selected_account(account_id)
    if account["provider_identifier"] != payload["provider_identifier"]:
        return ActionFailed("The LinkedIn account changed after approval. Request a new approval.")
    result = client.request("/actions/linkedin_send_dm", {"account_id": account_id,
        "provider_identifier": account["provider_identifier"], "recipient": recipient, "text": text})
    message_id = result.get("message_id")
    if (result.get("status") != "sent" or result.get("recipient_profile_url") != url
            or not isinstance(message_id, str) or not re.fullmatch(r"urn:li:(?:msg_message|fsd_message):[A-Za-z0-9_(),:=+/-]{1,500}", message_id)):
        return ActionFailed("Could not confirm sending. Check LinkedIn before approving another attempt.")
    return ApprovalExecuted(f"Sent LinkedIn DM from {account['provider_identifier']} to {recipient['name']} ({url}); message {message_id}.")
