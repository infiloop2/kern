"""Fixed browser actions; the private service never gives agents browser control."""
from __future__ import annotations
from typing import cast
from host.runtime.browser import client
from host.runtime.browser.actions.x_post_tweet import validate_post, validate_post_request
from host.tools import (ActionExecuted, ActionFailed, ActionPendingApproval, ActionResult,
                        ActionSpec, ApprovalExecuted, ApprovalRecord, ApprovalResult,
                        DataSummary, DataSummaryCard, HostAPI, JSONObject, SetupStep, Tool, ToolManifest)
from host.tools.manifest import protect_inputs
from host.tools.shared import outputs
from host.tools.shared.inputs import clip_text

ACCOUNT = outputs.obj({
    "account_id": outputs.text("Stable Kern connected-account identifier."),
    "provider": {"type": "string", "enum": ["x"]},
    "provider_identifier": outputs.text("Provider-defined account identifier. For X, the lowercase handle without @; verified again before submission."),
    "state": {"type": "string", "enum": ["connected", "needs_attention"]},
    "checked_at": outputs.text("UTC time of the last login check."),
}, ["account_id", "provider", "provider_identifier", "state", "checked_at"])
MANIFEST = ToolManifest(
    tool_id="browser", display_name="Browser", connection="enable_only",
    host_service_dependency="kern-browser.service",
    description="Connect accounts in isolated browser profiles to enable direct browser-based actions. Operators manage logins; agents use the connected accounts through fixed actions.",
    actions=protect_inputs((
        ActionSpec(id="x_connection_status", description="List connected X accounts and their last checked login states. No cookies, page content or screenshots are returned.",
                   data_policy="Reads local non-secret account metadata into model context; no external request.",
                   input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                   output_schema=outputs.obj({"accounts": outputs.array_of(ACCOUNT, "Connected X accounts.")}, ["accounts"])),
        ActionSpec(id="x_post_tweet", description="Request approval for one original X post or reply from a connected browser account. Set in_reply_to_tweet_id to reply. Each approval authorizes one submission attempt.",
                   data_policy="Queues the exact text, selected account and optional reply target for Kern approval. Only after approval, and only while its login is verified, sends the text to X through its website.",
                   input_schema={"type": "object", "properties": {
                       "account_id": {"type": "string", "description": "Stable Kern account ID returned by x_connection_status. The service resolves its verified X handle."},
                       "text": {"type": "string", "description": "Exact post or reply text, 1–280 characters; X also checks weighted length."},
                       "in_reply_to_tweet_id": {"type": "string", "description": "Optional numeric ID of the X post to reply to. Omit for an original post."}},
                       "required": ["account_id", "text"], "additionalProperties": False},
                   approval="operator"),
    ), {}),
    setup_steps=(SetupStep(title="Connect X account", description="Enable Browser, choose Connect X account and sign in in the popup. Save and close creates the connected account. Check login verifies it later."),
                 SetupStep(title="Approve posts and replies", description="Every post or reply requires approval for its exact account, text and reply target. The action allows up to 50 submission attempts per connected account per UTC day.")),
    data_summary=DataSummary(cards=(
        DataSummaryCard(title="What leaves this host", description="Login and browser traffic go to X and any login providers you choose. Approved post or reply text goes to X. Cookies, passwords and screenshots never enter model context."),
        DataSummaryCard(title="Where it can go", description="The popup starts at x.com and has no address bar. X may direct login through its verification providers. The coded posting action targets X only."),
        DataSummaryCard(title="Provider use", description="X handles login and activity under its policies. X prohibits website automation and may restrict the account."),
        DataSummaryCard(title="Retention", description="Saved X profiles survive redeploys until disconnected. Daily action counts stay with the account until disconnect. Login sessions may expire independently."),
    )),
    protections=("Each X post or reply requires Kern approval, given by you or a configured auto-approval policy. Agents cannot open the browser, read cookies, run scripts, or manage connected accounts.",
                 "Profiles live under a separate service account. Website browsing cannot reach private networks or Kern services.",
                 "X prohibits website automation and may restrict or suspend the account. This integration does not bypass login challenges."),
    technical_details=("Saved login state persists on the private admin volume; Chromium runs in a temporary context. Disconnect deletes the account, saved login and usage data.",
                       "Original text posts and replies to numeric X post IDs are supported. Quotes, DMs and arbitrary browsing are not agent actions."),
    agent_notes="Use x_connection_status to select account_id. The X handle is account metadata, not a tool input. Login and Check login are operator-only. x_post_tweet queues a per-post approval; it does not publish until approved. The recorded state is from the last check; approved posting verifies the live account again. A failed call is finished. Check X if publication could not be confirmed; another attempt requires a new approval. No action accepts arbitrary code or URLs.",
)


class BrowserTool(Tool):
    @property
    def manifest(self) -> ToolManifest:
        return MANIFEST

    @property
    def credentials(self) -> None:
        return None

    def execute(self, action: str, tool_input: JSONObject, api: HostAPI) -> ActionResult:
        try:
            if action == "x_connection_status":
                return ActionExecuted(cast(JSONObject, client.request("/actions/list")))
            if action == "x_post_tweet":
                payload = dict(tool_input)
                account_id = payload.pop("account_id", None)
                if not isinstance(account_id, str):
                    return ActionFailed("Select a connected browser account.")
                text, reply_id = validate_post_request(payload)
                accounts = client.request("/actions/list").get("accounts")
                selected = next((item for item in accounts if isinstance(item, dict) and item.get("account_id") == account_id), None) if isinstance(accounts, list) else None
                if not selected or selected.get("provider") != "x" or selected.get("state") != "connected":
                    return ActionFailed("Browser account is not connected or needs attention. Reconnect in Browser settings.")
                account, _, _ = validate_post({"provider_identifier": selected.get("provider_identifier"), **payload})
                proposal: JSONObject = {"action": action, "account_id": account_id, "provider_identifier": account,
                                        "text": text}
                if reply_id:
                    proposal["in_reply_to_tweet_id"] = reply_id
                summary = clip_text(f"{'Reply to https://x.com/i/status/' + reply_id if reply_id else 'Post on X'} as @{account}: {text}", 500)
                approval = api.approvals.request(action_id=action, summary=summary, payload=proposal)
                return ActionPendingApproval(approval.approval_id, approval.summary)
            return ActionFailed("Unknown browser action.")
        except client.BrowserError as exc:
            return ActionFailed(str(exc))

    def execute_approved(self, approval: ApprovalRecord, api: HostAPI) -> ApprovalResult:
        del api
        payload = approval.payload
        if approval.action_id != "x_post_tweet" or set(payload) not in ({"action", "account_id", "provider_identifier", "text"}, {"action", "account_id", "provider_identifier", "text", "in_reply_to_tweet_id"}) or payload.get("action") != "x_post_tweet":
            return ActionFailed("Browser approval payload is invalid.")
        account_id = payload.get("account_id")
        if not isinstance(account_id, str):
            return ActionFailed("Browser approval account is invalid.")
        proposal = {key: payload[key] for key in ("provider_identifier", "text")}
        if "in_reply_to_tweet_id" in payload:
            proposal["in_reply_to_tweet_id"] = payload["in_reply_to_tweet_id"]
        try:
            account, _, reply_id = validate_post(proposal)
            accounts = client.request("/actions/list").get("accounts")
            selected = next((item for item in accounts if isinstance(item, dict) and item.get("account_id") == account_id), None) if isinstance(accounts, list) else None
            if not selected or selected.get("provider") != "x" or selected.get("provider_identifier") != account or selected.get("state") != "connected":
                return ActionFailed("X account or login state changed after approval. Recheck the connection before requesting another approval.")
            result = client.request("/actions/post_tweet", {"account_id": account_id, **proposal})
            if result.get("status") == "posted" and isinstance(result.get("url"), str):
                return ApprovalExecuted(f"{'Replied to ' + reply_id if reply_id else 'Posted on X'} as @{account}: {result['url']}")
            return ActionFailed("Could not confirm publication. Check X before approving another attempt.")
        except client.BrowserError as exc:
            return ActionFailed(str(exc))


BUNDLED_TOOL = BrowserTool()
