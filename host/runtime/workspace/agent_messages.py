"""One-shot messages between existing host threads.

Workspace owns destination eligibility; Admin starts or steers the thread.
Sender identity comes from the peer's host-created process scope, never from
the caller's JSON. Messages carry an explicit host-authored header.
"""

from http import HTTPStatus
import re
from typing import Any

from host.runtime.core import db
from host.session_options import SCRIPT_RUNTIME
from host.runtime.workspace.host_api import WorkspaceError, call_admin_api

THREAD_ID_RE = re.compile(r"(?:app|thread|schedule)-[1-9][0-9]*")
MESSAGE_HEADER = (
    "This is a message from another agent, not the operator.\n"
    "Sender thread: {sender}\n"
    'To reply, use send_agent_message with thread_id: "{sender}". '
    "Reply only when needed.\n\n---\n\n"
)
MAX_MESSAGE_CHARS = 10_000
MAX_MESSAGE_BYTES = 50_000


def send_agent_message(body: Any, sender_thread_id: str | None) -> dict[str, Any]:
    if sender_thread_id is None or not THREAD_ID_RE.fullmatch(sender_thread_id):
        raise WorkspaceError(HTTPStatus.CONFLICT, "agent thread identity is unavailable")
    if not isinstance(body, dict) or set(body) != {"thread_id", "message"}:
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, "send_agent_message requires exactly thread_id and message")
    target = body["thread_id"]
    message = body["message"]
    if not isinstance(target, str) or not THREAD_ID_RE.fullmatch(target):
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, "thread_id must identify an existing App, Chat, or Schedule")
    if target == sender_thread_id:
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, "You cannot message your own thread; continue working in the current turn.")
    if not isinstance(message, str) or not message.strip():
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, "message must be a non-empty string")
    if len(message) > MAX_MESSAGE_CHARS:
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, "message must be at most 10000 characters")
    content = MESSAGE_HEADER.format(sender=sender_thread_id) + message
    return deliver_message(target, {"message": content})


def deliver_message(thread_id: str, body: Any) -> dict[str, Any]:
    """Send agent correspondence or a Kern notice to an eligible existing thread."""
    if not isinstance(thread_id, str) or not THREAD_ID_RE.fullmatch(thread_id):
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, "thread_id must identify an existing App, Chat, or Schedule")
    if not isinstance(body, dict) or set(body) != {"message"}:
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, "agent message requires exactly message")
    message = body["message"]
    if not isinstance(message, str):
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, "message must be a string")
    if not message.strip() or len(message.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, "message must be non-empty and at most 50000 bytes")
    # Keep the destination eligible through admission/steering. The row
    # lock also covers concurrent archive and settings changes.
    with db.transaction() as cur:
        settings: dict[str, str] = {}
        if thread_id.startswith("app-"):
            cur.execute(
                "SELECT agent_runtime, agent_model, agent_effort, archived, agent_updates_locked"
                " FROM web_apps WHERE app_id = %s FOR SHARE", (thread_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise WorkspaceError(HTTPStatus.NOT_FOUND, "app not found")
            if row[3]:
                raise WorkspaceError(HTTPStatus.CONFLICT, "archived apps cannot receive agent messages")
            if row[4]:
                raise WorkspaceError(HTTPStatus.LOCKED, "the operator has locked agent updates for this app")
            settings = {"agent_runtime": row[0], "model": row[1], "effort": row[2]}
        elif thread_id.startswith("schedule-"):
            cur.execute(
                "SELECT agent_runtime, model, effort FROM schedules"
                " WHERE thread_id = %s AND deleted_at IS NULL FOR SHARE", (thread_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise WorkspaceError(HTTPStatus.NOT_FOUND, "active schedule not found")
            if row[0] == SCRIPT_RUNTIME:
                raise WorkspaceError(HTTPStatus.CONFLICT, "Bash schedules cannot receive agent messages")
            settings = {"agent_runtime": row[0], "model": row[1], "effort": row[2]}
        else:
            cur.execute("SELECT archived FROM chat_threads WHERE thread_id = %s FOR SHARE", (thread_id,))
            row = cur.fetchone()
            if row is None:
                raise WorkspaceError(HTTPStatus.NOT_FOUND, "chat thread not found")
            if row[0]:
                raise WorkspaceError(HTTPStatus.CONFLICT, "archived chats cannot receive agent messages")
        response = call_admin_api("POST", f"/v1/threads/{thread_id}/messages",
                                  {"message": message, **settings})
        if response.get("status") != "accepted":
            raise WorkspaceError(HTTPStatus.BAD_GATEWAY, "host admin returned invalid send status")
    return {"status": "accepted", "thread_id": thread_id}
