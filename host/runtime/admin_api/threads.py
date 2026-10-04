"""Thread routes, lifecycle actions, and provider-history handoffs."""

from __future__ import annotations

import base64
from http import HTTPStatus
import json
import re
import threading
import time
from typing import Any, Callable

from host import agent_messages as message_templates
from host.config import AGENT_RUNTIMES
from host.runtime.memory_context import load_query
from host.memory_recall_rules import RELEVANT_PAGE_LIMIT
from host.runtime.agent_runtime import agent_activity, codex_app_server, orchestrator
from host.runtime.admin_api import workspace_proxy
from host.runtime.admin_api.errors import ApiError
from host.runtime.admin_api.request_params import clip_json_encoded_text as _clip_json_encoded_text, one as _one
from host.runtime.core import host_errors, state
from host.runtime.core.state import utc_now
from host.session_options import SCRIPT_RUNTIME, session_config_error

PRODUCT_THREAD_ID_RE = re.compile(
    r"(?=^[a-z0-9-]{1,64}$)^(?:(?:app|thread)-[a-z0-9-]+|schedule-[1-9][0-9]*)$"
)
PRODUCT_THREAD_PREFIX_RE = re.compile(
    r"(?=^[a-z0-9-]{1,64}$)^(?:(?:app|thread)-[a-z0-9-]*|schedule-(?:[1-9][0-9]*)?)$"
)
SCHEDULE_AGENT_THREAD_ID_RE = re.compile(r"schedule-[1-9][0-9]*")
MESSAGE_LIMIT = 50_000
RECALLED_MEMORY_PAGE_LIMIT = RELEVANT_PAGE_LIMIT + 1
THREAD_HANDOFF_MESSAGE_CHARACTER_LIMIT = 100_000
THREAD_HANDOFF_ACTIVITY_CHARACTER_LIMIT = 150_000
THREAD_HANDOFF_CHARACTER_LIMIT = (
    THREAD_HANDOFF_MESSAGE_CHARACTER_LIMIT + THREAD_HANDOFF_ACTIVITY_CHARACTER_LIMIT
)
THREAD_HANDOFF_ACTIVITY_DETAIL_LIMIT = 1_000
THREAD_HANDOFF_ACTIVITY_OUTPUT_LIMIT = 8_000
THREAD_HANDOFF_ACTIVITY_EVENT_CHARACTER_LIMIT = 8_000
THREAD_EVENT_MESSAGE_BYTES_LIMIT = 200_000
HISTORICAL_CONTEXT_PREVIEW_BYTES = 24 * 1024
WORKING_MEMORY_CLEARED_NOTICE = (
    "Working memory cleared. The agent starts fresh from here. Earlier "
    "messages are hidden and are no longer sent to it."
)
THREAD_DISPLAY_EVENT_TYPES = frozenset({
    "thread.message",
    "thread.activity",
    "thread.error",
    "thread.stopped",
    "thread.memory_cleared",
    "thread.notice",
})
_RUNTIME_USAGE_KEYS = {
    "codex": "codex_usage",
    "codex-2": "codex_usage",
    "codex-3": "codex_usage",
    "claude_code": "claude_usage",
    "grok": "grok_usage",
    "grok-2": "grok_usage",
    "hermes": "bedrock_usage",
}
_THREAD_SEND_LOCKS = tuple(threading.Lock() for _ in range(64))


def _optional_non_negative_int(query: dict[str, list[str]], key: str) -> int | None:
    value = _one(query, key)
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"{key} must be an integer") from exc
    if parsed < 0:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"{key} must be non-negative")
    return parsed


def _optional_bounded_positive_query_int(
    query: dict[str, list[str]], key: str, maximum: int
) -> int | None:
    value = _one(query, key)
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"{key} must be an integer") from exc
    if parsed < 1:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"{key} must be positive")
    if parsed > maximum:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"{key} must be at most {maximum}")
    return parsed


def _event_page_limit(query: dict[str, list[str]]) -> int:
    value = _one(query, "limit")
    if value is None:
        return state.EVENT_PAGE_LIMIT
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ApiError(HTTPStatus.BAD_REQUEST, "limit must be an integer") from exc
    if parsed < 1:
        raise ApiError(HTTPStatus.BAD_REQUEST, "limit must be positive")
    if parsed > state.EVENT_PAGE_LIMIT:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"limit must be at most {state.EVENT_PAGE_LIMIT}")
    return parsed


def _reject_query_keys(query: dict[str, list[str]], allowed: frozenset[str], label: str) -> None:
    unexpected = sorted(set(query) - allowed)
    if unexpected:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"unsupported {label} query parameter: {unexpected[0]}")


def thread_route(
    method: str,
    path: str,
    query: dict[str, list[str]],
    body: Any,
    peer_sender_thread_id: str | None,
    operator_sent_message: bool,
) -> Any:
    parts = path.strip("/").split("/")
    if len(parts) < 3 or not PRODUCT_THREAD_ID_RE.fullmatch(parts[2]):
        raise ApiError(HTTPStatus.NOT_FOUND, "thread route not found")
    thread_id = parts[2]
    if len(parts) == 3 and method == "GET":
        if query:
            raise ApiError(HTTPStatus.BAD_REQUEST, "thread detail does not accept query parameters")
        return {"thread": get_thread(thread_id)}
    if len(parts) == 4 and parts[3] == "messages" and method == "POST":
        return send_thread_message(thread_id, body, peer_sender_thread_id, operator_sent_message=operator_sent_message)
    if len(parts) == 4 and parts[3] == "notices" and method == "POST":
        if query or not isinstance(body, dict) or set(body) != {"kind", "summary", "details"}:
            raise ApiError(HTTPStatus.BAD_REQUEST, "notice requires kind, summary, and details")
        if (not isinstance(body["kind"], str) or body["kind"] not in message_templates.ACTION_KINDS or not isinstance(body["summary"], str)
                or not 1 <= len(body["summary"]) <= 100 or not isinstance(body["details"], str)
                or len(json.dumps(body).encode()) > 24 * 1024):
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid Kern notice")
        with state.mutation() as cur:
            if state.thread_session_config(thread_id, cur) is None:
                raise ApiError(HTTPStatus.NOT_FOUND, "thread not found")
            state.append_agent_event(cur, "thread.notice", thread_id, {"notice": body})
        return {"status": "recorded"}
    if len(parts) == 4 and parts[3] == "stop" and method == "POST":
        return stop_thread(thread_id)
    if len(parts) == 4 and parts[3] == "clear-memory" and method == "POST":
        return clear_thread_memory(thread_id)
    if len(parts) == 4 and parts[3] == "events" and method == "GET":
        _reject_query_keys(
            query,
            frozenset({"since", "before", "limit", "message_bytes", "event_type"}),
            "thread event",
        )
        message_bytes = _optional_bounded_positive_query_int(
            query, "message_bytes", THREAD_EVENT_MESSAGE_BYTES_LIMIT
        )
        requested_event_types = query.get("event_type")
        event_types: tuple[str, ...] | None = None
        if requested_event_types:
            unknown_event_types = sorted(
                set(requested_event_types) - THREAD_DISPLAY_EVENT_TYPES
            )
            if unknown_event_types:
                raise ApiError(
                    HTTPStatus.BAD_REQUEST,
                    f"unsupported thread event type: {unknown_event_types[0]}",
                )
            event_types = tuple(dict.fromkeys(requested_event_types))
        since = _optional_non_negative_int(query, "since")
        before = _optional_non_negative_int(query, "before")
        if since is not None and before is not None:
            raise ApiError(HTTPStatus.BAD_REQUEST, "since and before cannot be combined")
        page_kwargs: dict[str, Any] = {"before": before}
        if event_types is not None:
            page_kwargs["event_types"] = event_types
        events = state.page_thread_events(
            thread_id, since, _event_page_limit(query), notice_kinds=None, **page_kwargs, message_sources=None
        )
        if message_bytes is not None:
            for event in events:
                payload = event.get("payload")
                if not isinstance(payload, dict):
                    continue
                for field in ("message", "error_message"):
                    value = payload.get(field)
                    if isinstance(value, str):
                        payload[field] = _clip_json_encoded_text(value, message_bytes)
                activity = payload.get("activity")
                if isinstance(activity, dict):
                    # A single event must stay below the Workspace proxy's 1 MiB
                    # response cap. Keep the useful command/tool output large,
                    # but bound both rich text fields and mark every clip.
                    detail_budget = min(message_bytes, 24 * 1024)
                    output_budget = max(1, message_bytes - detail_budget)
                    for field, budget in (("detail", detail_budget), ("output", output_budget)):
                        value = activity.get(field)
                        if isinstance(value, str):
                            activity[field] = _clip_json_encoded_text(value, budget)
        return {"events": events}
    raise ApiError(HTTPStatus.NOT_FOUND, "thread route not found")

def _thread_send_lock(thread_id: str) -> threading.Lock:
    return _THREAD_SEND_LOCKS[hash(thread_id) % len(_THREAD_SEND_LOCKS)]

def _account_response_metadata(account: dict[str, Any], runtime_type: str) -> dict[str, Any]:
    # Provider capture sanitizes metadata before storage; this selects only the
    # public fields without re-normalizing provider-owned usage shapes.
    response: dict[str, Any] = {}
    for key in ("account_id", "email", "plan_type", "arn"):
        value = account.get(key)
        if isinstance(value, str) and value:
            response[key] = value
    if runtime_type in ("grok", "grok-2"):
        opt_out = account.get("coding_data_retention_opt_out")
        if isinstance(opt_out, bool):
            response["coding_data_retention_opt_out"] = opt_out
    usage_key = _RUNTIME_USAGE_KEYS.get(runtime_type)
    if usage_key is None:
        return response
    usage = account.get(usage_key)
    if isinstance(usage, dict) and usage:
        response[usage_key] = usage
    return response

def send_thread_message(
    thread_id: str,
    body: Any,
    peer_sender_thread_id: str | None,
    operator_sent_message: bool,
) -> dict[str, Any]:
    """Start or steer one turn through the ordinary thread path."""
    if PRODUCT_THREAD_ID_RE.fullmatch(thread_id) is None:
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            "thread_id must start with app-, thread-, or schedule-",
        )
    if (
        SCHEDULE_AGENT_THREAD_ID_RE.fullmatch(thread_id) is not None
        and not state.schedule_thread_is_owned(thread_id)
    ):
        raise ApiError(HTTPStatus.NOT_FOUND, "schedule thread is not reserved")
    message = _message(body)
    notice = {"kind": "operator"} if operator_sent_message else body.get("kern_notice")
    if not isinstance(notice, dict):
        raise ApiError(HTTPStatus.BAD_REQUEST, "Kern input requires a specific notice kind and summary")
    with _thread_send_lock(thread_id):
        session_config = state.thread_session_config(thread_id)
        agent_runtime, model, effort = _resolve_session_config(body, session_config, thread_id)
        # Follow-ups belong to the live turn; supplied settings apply on admission.
        steering_runtime = session_config["agent_runtime"] if session_config else agent_runtime
        if orchestrator.steer_live_turn(
            thread_id, steering_runtime, message, peer_sender_thread_id=peer_sender_thread_id,
            operator_sent_message=operator_sent_message, input_notice=notice,
        ):
            assert session_config is not None
            agent_runtime, model, effort = (
                session_config["agent_runtime"], session_config["model"], session_config["effort"]
            )
            turn = None
            provider_session_id = None
        else:
            recalled_pages: list[dict[str, Any]] = []
            recall_details = ""
            task_context = ""
            if agent_runtime != SCRIPT_RUNTIME:
                task_context = _memory_task_query(thread_id, message, notice)
                recalled_pages, recall_details = _recalled_memory_pages(thread_id, task_context)
            after_commit: list[Callable[[], None]] = []
            with state.mutation(after_commit=after_commit) as cur:
                # Re-read inside the admission transaction. The send lock keeps
                # same-thread messages ordered, while this snapshot keeps the
                # initial session row and turn events in one commit.
                session_config = state.thread_session_config(thread_id, cur)
                agent_runtime, model, effort = _resolve_session_config(body, session_config, thread_id)
                switching_session = _session_configuration_changed(
                    session_config, agent_runtime, model, effort
                )
                launch_message = message
                handoff_message = ""
                session_change_activity = None
                handoff_events: list[dict[str, Any]] = []
                missing_provider_context = (
                    agent_runtime != SCRIPT_RUNTIME
                    and session_config is not None
                    and not session_config.get("provider_session_id")
                )
                if agent_runtime != SCRIPT_RUNTIME and (
                    switching_session or missing_provider_context
                ):
                    handoff_events = state.recent_thread_handoff_events(
                        cur,
                        thread_id,
                        message_character_limit=THREAD_HANDOFF_MESSAGE_CHARACTER_LIMIT,
                        activity_character_limit=THREAD_HANDOFF_ACTIVITY_CHARACTER_LIMIT,
                        activity_event_character_limit=THREAD_HANDOFF_ACTIVITY_EVENT_CHARACTER_LIMIT,
                        # A cleared thread has no provider session, so it takes
                        # the handoff path; the floor is what keeps that path
                        # from handing back the context that was cleared.
                        after_seq=int((session_config or {}).get("context_cleared_seq") or 0),
                    )
                if switching_session:
                    assert session_config is not None
                    if session_config["status"] != "idle":
                        raise ApiError(
                            HTTPStatus.CONFLICT,
                            "thread runtime, model, and effort can change only while the thread is idle",
                        )
                    try:
                        state.rotate_thread_session(
                            cur,
                            thread_id,
                            agent_runtime,
                            model,
                            effort,
                            utc_now(),
                        )
                    except ValueError as exc:
                        raise ApiError(
                            HTTPStatus.CONFLICT,
                            "thread runtime, model, and effort can change only while the thread is idle",
                        ) from exc
                    provider_session_id = None
                    session_change_activity = _session_change_activity(
                        session_config,
                        agent_runtime,
                        model,
                        effort,
                    )
                else:
                    provider_session_id = (
                        session_config.get("provider_session_id") if session_config else None
                    )
                    state.save_thread_session(
                        cur,
                        agent_runtime,
                        thread_id,
                        provider_session_id,
                        utc_now(),
                        model,
                        effort,
                    )
                # Only when there is history to hand over. Both paths above can
                # produce none — a cleared thread by its floor, a first-message
                # switch by having no events — and the prompt tells the new
                # session it is continuing a thread, which is exactly wrong for
                # a run that starts fresh.
                if handoff_events:
                    handoff_message = _session_handoff_message(handoff_events, message)
                    launch_message = handoff_message
                if agent_runtime != SCRIPT_RUNTIME:
                    memory_context_message = message_templates.memory_context_message(thread_id, recalled_pages)
                    launch_message = f"{memory_context_message}\n\n{launch_message}"
                turn = orchestrator.admit_turn(
                    cur,
                    after_commit,
                    thread_id,
                    agent_runtime,
                    model,
                    effort,
                    message,
                    pre_message_activity=session_change_activity,
                    peer_sender_thread_id=peer_sender_thread_id,
                    operator_sent_message=operator_sent_message, input_notice=notice,
                )
                # Persist alongside admission: rejected turns leave no notices.
                # These are display events, excluded from future history handoffs.
                if handoff_events:
                    state.append_agent_event(
                        cur,
                        "thread.notice",
                        thread_id,
                        message_templates.history_notice(_historical_context_preview(handoff_message)),
                        run_number=turn.run_number,
                    )
                if agent_runtime != SCRIPT_RUNTIME:
                    state.append_agent_event(
                        cur,
                        "thread.notice",
                        thread_id,
                        message_templates.memory_notice(recalled_pages, recall_details),
                        run_number=turn.run_number,
                    )
            orchestrator.launch_turn(turn, launch_message, provider_session_id,
                                     task_context=task_context, recalled_pages=recalled_pages)
    return {
        "status": "accepted",
        "thread": _public_thread(thread_id, agent_runtime, model, effort),
    }


def _memory_task_query(thread_id: str, message: str, notice: dict[str, str] | None = None) -> str:
    if not message.strip():
        return ""
    return load_query(thread_id, {
        "event_type": "thread.notice" if notice and notice["kind"] != "operator" else "thread.message",
        "payload": {"source": "user", "message": message, "notice": notice},
    })


def _recalled_memory_pages(
    thread_id: str,
    query: str,
) -> tuple[list[dict[str, Any]], str]:
    started = time.monotonic()
    try:
        response = workspace_proxy.recall_memory(thread_id, query)
    except ApiError as exc:
        _report_degraded_recall(thread_id, exc)
        return [], "Recall unavailable; see Host diagnostics."
    if not isinstance(response, dict):
        _report_degraded_recall(
            thread_id,
            "Workspace returned a non-object recall response",
        )
        return [], "Recall unavailable; see Host diagnostics."
    pages = response.get("pages")
    if not isinstance(pages, list):
        _report_degraded_recall(
            thread_id,
            "Workspace returned an invalid recall page list",
        )
        return [], "Recall unavailable; see Host diagnostics."
    recalled = [
        page for page in pages[:RECALLED_MEMORY_PAGE_LIMIT]
        if isinstance(page, dict)
    ]
    if len(recalled) != min(len(pages), RECALLED_MEMORY_PAGE_LIMIT):
        _report_degraded_recall(
            thread_id,
            "Workspace returned a malformed recall page",
        )
        return [], "Recall unavailable; see Host diagnostics."
    normalized: list[dict[str, Any]] = []
    for page in recalled:
        if not (
            isinstance(page.get("page_id"), str)
            and page.get("scope") in {"self", "swarm"}
            and isinstance(page.get("description"), str)
            and isinstance(page.get("content"), str)
            and isinstance(page.get("revision"), int)
        ):
            continue
        normalized.append(
            {
                "content": page["content"],
                "description": page["description"],
                "page_id": page["page_id"],
                "revision": page["revision"],
                "scope": page["scope"],
                "selection": (
                    "self" if page["scope"] == "self"
                    else "popular" if page.get("selection") == "popular"
                    else "relevant"
                ),
            }
        )
    # Keep relevance order within the middle group; popular context goes last.
    normalized.sort(
        key=lambda page: 0 if page["selection"] == "self"
        else 2 if page["selection"] == "popular" else 1
    )
    details = response.get("diagnostics")
    details = details[:12000] if isinstance(details, str) else "No retrieval details recorded."
    details = (
        f"Admission recall: {round((time.monotonic() - started) * 1000)} ms.\n"
        f"{details}"
    )
    return normalized, details


def _report_degraded_recall(
    thread_id: str,
    issue: BaseException | str,
) -> None:
    kind = (
        "memory_recall_timeout"
        if isinstance(issue, ApiError) and issue.status == HTTPStatus.GATEWAY_TIMEOUT
        else "memory_recall_degraded"
    )
    host_errors.report_warning(
        "admin_api.memory_recall",
        issue,
        context={"thread_id": thread_id},
        kind=kind,
    )


def get_thread(thread_id: str) -> dict[str, Any]:
    config = state.thread_session_config(thread_id)
    if config is None:
        raise ApiError(HTTPStatus.NOT_FOUND, "thread not found")
    return _public_thread(
        thread_id,
        config["agent_runtime"],
        config["model"],
        config["effort"],
        last_used_at=config.get("last_used_at"),
    )

def stop_thread(thread_id: str) -> dict[str, str]:
    if state.thread_session_config(thread_id) is None:
        raise ApiError(HTTPStatus.NOT_FOUND, "thread not found")
    if not orchestrator.stop_thread_turn(thread_id):
        raise ApiError(HTTPStatus.CONFLICT, "the thread has no running work")
    return {"status": "accepted"}

def sweep_archived_codex_sessions() -> int:
    """Delete provider sessions for archived Chats; retain the Chats themselves.

    No age or size cutoff. Each runtime uses one short-lived, agent-owned
    app-server without resuming a thread or invoking a model. Restored chats
    use the ordinary history handoff on their next message.
    """
    deleted = 0
    for runtime in codex_app_server.CODEX_RUNTIME_TYPES:
        candidates = state.archived_thread_session_ids(runtime)
        if not candidates:
            continue
        server = codex_app_server.CodexAppServer(runtime_type=runtime)
        try:
            server.start(init_timeout=5)
            for thread_id in candidates:
                # Serialize the snapshot/detach with sends. The live fence
                # also covers idle-in-the-database turns still tearing down.
                with _thread_send_lock(thread_id):
                    if thread_id in orchestrator.live_thread_ids():
                        continue
                    with state.mutation() as cur:
                        session_id = state.detach_archived_thread_session(cur, thread_id, runtime)
                if session_id is not None:
                    # Detach commits first. A restore/send can now safely start
                    # a different session even while this deletion is pending.
                    codex_app_server.delete_session(server, session_id)
                    deleted += 1
        except Exception as exc:
            # Stop this runtime's pass on a transport/delete failure so a dead
            # app-server cannot detach all the remaining candidates needlessly.
            host_errors.report_unexpected(
                "admin_api.archived_codex_sweep", exc, context={"runtime": runtime},
            )
        finally:
            try:
                server.close()
            except Exception as exc:
                host_errors.report_unexpected(
                    "admin_api.archived_codex_sweep.close", exc, context={"runtime": runtime},
                )
    return deleted


def clear_thread_memory(thread_id: str) -> dict[str, str]:
    """Drop the thread's provider session so its next run starts fresh.

    This deletes nothing. Retained events stay readable in the thread and in
    conversation history; they simply stop being replayed into the provider.
    The visible marker is committed with the state change, so a thread can
    never show a clear that did not take effect.
    """
    # Take the same lock a send does: the clear must land either wholly before
    # or wholly after a send, never between that send's session snapshot and
    # its launch, which would strip context the run was admitted with. Holding
    # it also means no new turn can be admitted between the live check below
    # and the write.
    with _thread_send_lock(thread_id):
        session_config = state.thread_session_config(thread_id)
        if session_config is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "thread not found")
        if session_config["status"] != "idle":
            raise ApiError(
                HTTPStatus.CONFLICT,
                "working memory can be cleared only while the thread is idle",
            )
        # A stopped turn returns the thread to durable idle while its process
        # is still closing, and that finishing worker may still report a
        # provider session for its run number. Clearing on the persisted status
        # alone would let that late write restore the session just cleared, so
        # the fence is the live set: once a thread leaves it, no worker can
        # still write for it.
        if thread_id in orchestrator.live_thread_ids():
            raise ApiError(
                HTTPStatus.CONFLICT,
                "the thread is still finishing; retry shortly",
            )
        with state.mutation() as cur:
            session_config = state.thread_session_config(thread_id, cur)
            if session_config is None:
                raise ApiError(HTTPStatus.NOT_FOUND, "thread not found")
            if session_config["status"] != "idle":
                raise ApiError(
                    HTTPStatus.CONFLICT,
                    "working memory can be cleared only while the thread is idle",
                )
            cleared_seq = state.append_agent_event(
                cur,
                # Its own display type, not thread.activity: the Chat UI can
                # hide activity, and the boundary must stay visible when it is.
                "thread.memory_cleared",
                thread_id,
                {"message": WORKING_MEMORY_CLEARED_NOTICE},
                run_number=session_config["run_number"],
            )
            try:
                state.clear_thread_context(cur, thread_id, cleared_seq, utc_now())
            except ValueError as exc:
                raise ApiError(
                    HTTPStatus.CONFLICT,
                    "working memory can be cleared only while the thread is idle",
                ) from exc
    return {"status": "cleared"}

def list_threads(
    query: dict[str, list[str]],
) -> dict[str, Any]:
    limit = _event_page_limit(query)
    before = _thread_list_cursor(query)
    prefix = _thread_list_prefix(query)
    summaries = state.page_thread_summaries(
        before,
        limit + 1,
        thread_prefix=prefix,
    )
    page = summaries[:limit]
    live = orchestrator.live_thread_ids()
    for thread in page:
        if thread["thread_id"] in live:
            thread["status"] = "running"
    response: dict[str, Any] = {"threads": page}
    if len(summaries) > limit and page:
        response["next_before"] = _encode_thread_list_cursor(page[-1])
    return response


def _public_thread(
    thread_id: str,
    agent_runtime: str,
    model: str,
    effort: str,
    *,
    last_used_at: str | None = None,
) -> dict[str, Any]:
    config = state.thread_session_config(thread_id)
    latest_event_seq, latest_message_seq = state.latest_thread_event_seqs(thread_id)
    if last_used_at is None:
        last_used_at = config.get("last_used_at") if config else None
    status = str(config.get("status") if config else "idle")
    if thread_id in orchestrator.live_thread_ids():
        status = "running"
    return {
        "thread_id": thread_id,
        "agent_runtime": agent_runtime,
        "model": model,
        "effort": effort,
        "last_used_at": str(last_used_at or ""),
        "status": status or "idle",
        "latest_event_seq": latest_event_seq,
        "latest_message_seq": latest_message_seq,
    }

def _message(body: Any) -> str:
    """The one request-body validation for a thread message send; the session
    configuration readers below trust the dict this establishes."""
    if not isinstance(body, dict):
        raise ApiError(HTTPStatus.BAD_REQUEST, "request body must be a JSON object")
    value = body.get("message")
    if not isinstance(value, str) or not value:
        raise ApiError(HTTPStatus.BAD_REQUEST, "message must be a non-empty string")
    if len(value) > MESSAGE_LIMIT:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"message must be at most {MESSAGE_LIMIT} characters")
    return value


def _agent_runtime(body: dict[str, Any]) -> str:
    value = body.get("agent_runtime")
    if not isinstance(value, str) or value not in AGENT_RUNTIMES:
        raise ApiError(HTTPStatus.BAD_REQUEST, "agent_runtime must be one of " + ", ".join(sorted(AGENT_RUNTIMES)))
    return value

def _runs_scripts(thread_id: str) -> bool:
    """Whether this thread may run the script runtime.

    The script runtime reads a thread's message as a path to a bash script
    rather than as conversation, so a Chat or App thread rotated onto it would
    start treating the user's next sentence as a filename. The Workspace
    surfaces already refuse to offer it, but this is the executor: the
    boundary is enforced here on the schedule namespace.
    """
    return thread_id.startswith("schedule-")

def _session_config(
    body: dict[str, Any], runtime: str, *, allow_script: bool
) -> tuple[str, str]:
    model = body.get("model")
    effort = body.get("effort")
    error = session_config_error(runtime, model, effort, allow_script=allow_script)
    if error is not None:
        raise ApiError(HTTPStatus.BAD_REQUEST, error)
    assert isinstance(model, str) and isinstance(effort, str)
    return model, effort

def _resolve_session_config(
    body: dict[str, Any],
    session_config: dict[str, Any] | None,
    thread_id: str,
) -> tuple[str, str, str]:
    allow_script = _runs_scripts(thread_id)
    stored = None
    if session_config is not None:
        stored = (
            session_config["agent_runtime"],
            session_config["model"],
            session_config["effort"],
        )

    fields = ("agent_runtime", "model", "effort")
    supplied = [field for field in fields if field in body]
    if stored is not None:
        # A superseded configuration stays readable and can be replaced, but
        # cannot start another provider session as-is.
        if session_config_error(*stored, allow_script=allow_script) is not None and (
            not supplied or tuple(body.get(field) for field in fields) == stored
        ):
            raise ApiError(
                HTTPStatus.CONFLICT,
                "this thread runs a session configuration that is no longer offered;"
                " select a currently offered model to continue",
            )
        if not supplied:
            return stored
        if len(supplied) != len(fields):
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                "agent_runtime, model, and effort must be provided together",
            )
        requested_runtime = _agent_runtime(body)
        requested_model, requested_effort = _session_config(
            body, requested_runtime, allow_script=allow_script
        )
        return requested_runtime, requested_model, requested_effort
    if not supplied:
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            "agent_runtime, model, and effort are required when starting a new thread",
        )
    if len(supplied) != len(fields):
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            "agent_runtime, model, and effort must be provided together",
        )

    agent_runtime = _agent_runtime(body)
    model, effort = _session_config(body, agent_runtime, allow_script=allow_script)
    return agent_runtime, model, effort

def _session_configuration_changed(
    session_config: dict[str, Any] | None,
    runtime: str,
    model: str,
    effort: str,
) -> bool:
    if session_config is None:
        return False
    return (
        session_config["agent_runtime"],
        session_config["model"],
        session_config["effort"],
    ) != (runtime, model, effort)

def _session_change_activity(
    previous: dict[str, Any],
    runtime: str,
    model: str,
    effort: str,
) -> dict[str, Any]:
    previous_runtime = str(previous["agent_runtime"])
    title = (
        "Agent provider changed"
        if previous_runtime != runtime
        else "Agent session changed"
    )

    def label(runtime_type: str, model_name: str, effort_name: str) -> str:
        runtime_name = orchestrator.RUNTIME_LABELS.get(runtime_type, runtime_type)
        return f"{runtime_name} · {model_name} · {effort_name}"

    detail = (
        f"{label(previous_runtime, str(previous['model']), str(previous['effort']))}"
        f" → {label(runtime, model, effort)}"
    )
    return agent_activity.activity(
        "kern",
        "session-change",
        "status",
        "completed",
        title,
        detail=detail,
        status="completed",
    )

def _handoff_event_block(event: dict[str, Any]) -> str:
    payload = event.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    event_type = event.get("event_type")
    if message_templates.is_conversation_event(event):
        label = "User" if payload.get("source") == "user" else "Agent"
        return f"{label}:\n{payload.get('message', '')}"
    if event_type == "thread.activity":
        activity = payload.get("activity")
        activity = activity if isinstance(activity, dict) else {}
        summary = {
            key: activity[key]
            for key in ("provider", "kind", "phase", "title", "status")
            if key in activity
        }
        for key, limit in (
            ("detail", THREAD_HANDOFF_ACTIVITY_DETAIL_LIMIT),
            ("output", THREAD_HANDOFF_ACTIVITY_OUTPUT_LIMIT),
            ("error", THREAD_HANDOFF_ACTIVITY_OUTPUT_LIMIT),
        ):
            value = activity.get(key)
            if isinstance(value, str) and value:
                summary[key] = agent_activity.clip_text(value, limit)
        block = "Agent activity (summary):\n" + json.dumps(
            summary, ensure_ascii=False, indent=2, default=str
        )
        return agent_activity.clip_text(
            block, THREAD_HANDOFF_ACTIVITY_EVENT_CHARACTER_LIMIT
        )
    return ""

def _bounded_handoff_section(
    history: list[dict[str, Any]], character_limit: int
) -> str:
    """Newest event blocks within one exact model-facing character budget."""
    if character_limit <= 0:
        return ""
    blocks_reversed: list[str] = []
    remaining = character_limit
    omitted = False
    for event in reversed(history):
        separator_size = 2 if blocks_reversed else 0
        block = _handoff_event_block(event)
        if not block:
            continue
        available = remaining - separator_size
        if available <= 0:
            omitted = True
            break
        if len(block) <= available:
            blocks_reversed.append(block)
            remaining -= separator_size + len(block)
            continue
        marker = "\n[Earlier event content truncated]\n"
        content_space = available - len(marker)
        if content_space > 1:
            prefix_size = content_space // 2
            suffix_size = content_space - prefix_size
            blocks_reversed.append(
                block[:prefix_size] + marker + block[-suffix_size:]
            )
        omitted = True
        break
    if len(blocks_reversed) < len(history):
        omitted = True
    transcript = "\n\n".join(reversed(blocks_reversed))
    if omitted:
        marker = "[Older retained thread events were omitted.]"
        if len(marker) >= character_limit:
            return marker[:character_limit]
        content_limit = character_limit - len(marker) - 2
        if len(transcript) > content_limit:
            transcript = transcript[-content_limit:]
        transcript = marker + ("\n\n" + transcript if transcript else "")
    return transcript

def _historical_context_preview(text: str) -> str:
    """Keep both ends within a 24 KiB JSON budget, without changing the handoff."""
    limit = HISTORICAL_CONTEXT_PREVIEW_BYTES
    if len(text) <= limit and len(json.dumps(text).encode()) <= limit:
        return text
    marker = "\n\n… [middle omitted from preview] …\n\n"

    def preview(keep: int) -> str:
        tail = keep // 2
        return text[:keep - tail] + marker + (text[-tail:] if tail else "")

    low, high = 0, min(len(text), limit)
    while low < high:
        middle = (low + high + 1) // 2
        if len(json.dumps(preview(middle)).encode()) <= limit:
            low = middle
        else:
            high = middle - 1
    return preview(low)


def _session_handoff_message(history: list[dict[str, Any]], message: str) -> str:
    """Build independently bounded conversation and activity handoff sections."""
    conversation = _bounded_handoff_section(
        [event for event in history if message_templates.is_conversation_event(event)],
        THREAD_HANDOFF_MESSAGE_CHARACTER_LIMIT,
    )
    activity = _bounded_handoff_section(
        [event for event in history if event.get("event_type") == "thread.activity"],
        THREAD_HANDOFF_ACTIVITY_CHARACTER_LIMIT,
    )
    return message_templates.session_handoff_message(conversation, activity, message)

def _thread_list_prefix(query: dict[str, list[str]]) -> str | None:
    prefix = _one(query, "prefix")
    if prefix is None:
        return None
    if PRODUCT_THREAD_PREFIX_RE.fullmatch(prefix) is None:
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            "prefix must start with app-, thread-, or schedule-",
        )
    return prefix


def _encode_thread_list_cursor(thread: dict[str, Any]) -> str:
    raw = json.dumps(
        [
            str(thread.get("last_used_at") or ""),
            str(thread["thread_id"]),
        ],
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")

def _thread_list_cursor(
    query: dict[str, list[str]],
) -> tuple[str, str] | None:
    value = _one(query, "before")
    if value is None:
        return None
    try:
        if not value or len(value) > 512:
            raise ValueError
        padded = value + "=" * (-len(value) % 4)
        decoded = base64.b64decode(
            padded.encode(),
            altchars=b"-_",
            validate=True,
        )
        fields = json.loads(decoded)
        if (
            not isinstance(fields, list)
            or len(fields) != 2
            or not all(isinstance(field, str) for field in fields)
            or PRODUCT_THREAD_ID_RE.fullmatch(fields[1]) is None
        ):
            raise ValueError
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            "before must be a valid thread list cursor",
        ) from exc
    return fields[0], fields[1]
