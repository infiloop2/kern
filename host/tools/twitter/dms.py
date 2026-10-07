"""Bounded official X v2 DM reads and exact text-send proposals."""
from __future__ import annotations

import re
import json
from typing import cast

from host.tools.host_api import HostAPI
from host.tools.json_types import JSONObject, JSONValue
from host.tools.shared import outputs
from host.tools.shared.inputs import ToolInputValidationError, clip_text, int_field
from host.tools.twitter import costs

USER_ID = re.compile(r"^[0-9]{1,19}$")
CONVERSATION_ID = re.compile(r"^(?:[0-9]{15,19}|[0-9]{1,19}-[0-9]{1,19})$")
PAGE_TOKEN = re.compile(r"^[0-9A-Va-v]{16,2048}$")
MAX_TEXT = 10_000
COVERAGE = "X returns available DM events from up to the last 30 days; this is not exhaustive inbox or historical coverage."
EVENT_SCHEMA = outputs.obj({
    "id": outputs.text("Provider DM event id."),
    "dm_conversation_id": outputs.text("Provider conversation id; empty if unavailable."),
    "event_type": outputs.text("Provider event type."),
    "text": outputs.text("Message text, bounded to 10,000 characters."),
    "text_truncated": outputs.boolean("Whether message text was truncated."),
    "sender_id": outputs.text("Sender's numeric id; empty if unavailable."),
    "participant_ids_truncated": outputs.boolean("Whether more than 256 participant ids were returned."),
    "participant_ids": outputs.array_of(outputs.text("Provider participant id."), "Participants returned for this event; not a complete conversation roster."),
    "created_at": outputs.text("Provider timestamp; empty if unavailable."),
    "direction": {"type": "string", "enum": ["incoming", "outgoing", "unknown"], "description": "Relative to authenticated account, from sender_id."},
    "media_keys": outputs.array_of(outputs.text("Provider media key."), "Attachment identifiers only; no media downloads."),
}, ["id", "dm_conversation_id", "event_type", "text", "text_truncated", "sender_id", "participant_ids", "participant_ids_truncated", "created_at", "direction", "media_keys"])
OUTPUT_SCHEMA = outputs.obj({
    "message": outputs.text("Returned event count."),
    "account_id": outputs.text("Authenticated connected account's numeric id."),
    "coverage": outputs.text("Provider retention/coverage limitation."),
    "events": outputs.array_of(EVENT_SCHEMA, "One page, at most max_results events."),
    "next_token": outputs.nullable(outputs.text("Opaque provider pagination token; pass unchanged to pagination_token."), "Null when no next page was returned."),
}, ["message", "account_id", "coverage", "events", "next_token"])
PAGE_PROPERTIES: JSONObject = {
    "max_results": {"type": "integer", "description": "One page of 1–100 events (default 20)."},
    "pagination_token": {"type": "string", "description": "Exact next_token from a previous call for this account and target; 16–2048 base32hex characters."},
}
TARGET_PROPERTIES: JSONObject = {
    "recipient_user_id": {"type": "string", "description": "Numeric user id resolved with lookup_user; mutually exclusive with dm_conversation_id."},
    "dm_conversation_id": {"type": "string", "description": "Existing provider conversation id from DM reads; mutually exclusive with recipient_user_id."},
}


def target(tool_input: JSONObject, *, required: bool) -> JSONObject:
    fields: JSONObject = {}
    for key, pattern in (("recipient_user_id", USER_ID), ("dm_conversation_id", CONVERSATION_ID)):
        if key in tool_input:
            value = tool_input[key]
            if not isinstance(value, str) or not pattern.fullmatch(value):
                raise ToolInputValidationError(f"X {key} is invalid.")
            fields[key] = value
    if len(fields) > 1 or (required and len(fields) != 1):
        raise ToolInputValidationError("X DM requires exactly one recipient_user_id or dm_conversation_id.")
    return fields


def read_request(tool_input: JSONObject, *, history: bool) -> tuple[str, int]:
    from host.tools.shared.web import encode_query
    allowed = {"max_results", "pagination_token"} | (set(TARGET_PROPERTIES) if history else set())
    if set(tool_input) - allowed:
        raise ToolInputValidationError("Unsupported X DM read field.")
    resolved = target(tool_input, required=history)
    maximum = int_field(tool_input, "max_results", provider="X", default=20, low=1, high=100)
    params: dict[str, str] = {"max_results": str(maximum),
        "dm_event.fields": "id,text,event_type,created_at,dm_conversation_id,attachments", "expansions": "sender_id,participant_ids"}
    if "pagination_token" in tool_input:
        token = tool_input["pagination_token"]
        if not isinstance(token, str) or not PAGE_TOKEN.fullmatch(token):
            raise ToolInputValidationError("X pagination_token must be the bounded base32hex token from the previous page.")
        params["pagination_token"] = token
    return route(resolved, "dm_events") + "?" + encode_query(params), maximum


def route(resolved: JSONObject, suffix: str) -> str:
    if "recipient_user_id" in resolved:
        return f"/dm_conversations/with/{resolved['recipient_user_id']}/{suffix}"
    if "dm_conversation_id" in resolved:
        return f"/dm_conversations/{resolved['dm_conversation_id']}/{suffix}"
    return "/dm_events"


def read_result(response: JSONObject, account_id: str, maximum: int, api: HostAPI) -> JSONObject:
    costs.record_response(api, response, "dm_event")
    if response.get("errors"):
        # Caller maps diagnostic data to a bounded provider warning; never echo it.
        from host.tools.shared.web import WebRequestError, provider_warning
        message = "X could not return the complete DM page."
        raise provider_warning("X", "DM lookup", WebRequestError(message, status=200, body=json.dumps(response).encode()[:4096]), message)
    meta = response.get("meta")
    data = response.get("data", [] if isinstance(meta, dict) and meta.get("result_count") == 0 else None)
    if not isinstance(data, list) or len(data) > maximum:
        raise RuntimeError("X returned an invalid DM page.")
    events: list[JSONValue] = []
    for row in data:
        if not isinstance(row, dict):
            raise RuntimeError("X returned an invalid DM event.")
        event_id = row.get("id")
        if not isinstance(event_id, str) or not USER_ID.fullmatch(event_id):
            raise RuntimeError("X returned an invalid DM event.")
        sender = row.get("sender_id")
        sender = sender if isinstance(sender, str) and USER_ID.fullmatch(sender) else ""
        text = row.get("text")
        text = text if isinstance(text, str) else ""
        participants = row.get("participant_ids", [])
        attachments = row.get("attachments")
        media = attachments.get("media_keys", []) if isinstance(attachments, dict) else []
        events.append({
            "id": row["id"], "dm_conversation_id": clip_text(str(row.get("dm_conversation_id") or ""), 40),
            "event_type": clip_text(str(row.get("event_type") or ""), 40),
            "text": clip_text(text, MAX_TEXT), "text_truncated": len(text) > MAX_TEXT,
            "sender_id": clip_text(sender, 19),
            "participant_ids": cast(list[JSONValue], [p for p in participants[:256] if isinstance(p, str) and USER_ID.fullmatch(p)]) if isinstance(participants, list) else [],
            "participant_ids_truncated": isinstance(participants, list) and len(participants) > 256,
            "created_at": clip_text(str(row.get("created_at") or ""), 40),
            "direction": "outgoing" if sender == account_id else "incoming" if sender else "unknown",
            "media_keys": cast(list[JSONValue], [clip_text(p, 80) for p in media[:4] if isinstance(p, str)]) if isinstance(media, list) else [],
        })
    meta = response.get("meta")
    token = meta.get("next_token") if isinstance(meta, dict) else None
    if token is not None and (not isinstance(token, str) or not PAGE_TOKEN.fullmatch(token)):
        raise RuntimeError("X returned an invalid DM pagination token.")
    return {"message": f"Loaded {len(events)} X DM events.", "account_id": account_id,
            "coverage": COVERAGE, "events": events, "next_token": token}


def proposal(tool_input: JSONObject) -> JSONObject:
    if set(tool_input) - {"text", *TARGET_PROPERTIES}:
        raise ToolInputValidationError("X send_dm supports only text and one recipient/conversation id.")
    resolved = target(tool_input, required=True)
    text = tool_input.get("text")
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
        raise ToolInputValidationError("X DM text must be nonblank and at most 10,000 characters.")
    return {**resolved, "text": text}
