"""Thread routes, lifecycle actions, and provider-history handoffs."""

from __future__ import annotations

import base64
from http import HTTPStatus
import json
import re
import threading
from typing import Any, Callable

from host.config import AGENT_RUNTIMES
from host.runtime.agent_runtime import agent_activity, orchestrator
from host.runtime.admin_api.errors import ApiError
from host.runtime.admin_api.request_params import clip_json_encoded_text as _clip_json_encoded_text, one as _one
from host.runtime.core import state
from host.runtime.core.state import utc_now
from host.session_options import session_config_error

PRODUCT_THREAD_ID_RE = re.compile(
    r"(?=^[a-z0-9-]{1,64}$)^(?:app|thread|schedule)-[a-z0-9-]+$"
)
PRODUCT_THREAD_PREFIX_RE = re.compile(
    r"(?=^[a-z0-9-]{1,64}$)^(?:app|thread|schedule)-[a-z0-9-]*$"
)
SCHEDULE_THREAD_PREFIX = "schedule-"
MESSAGE_LIMIT = 50_000
THREAD_HANDOFF_MESSAGE_CHARACTER_LIMIT = 100_000
THREAD_HANDOFF_ACTIVITY_CHARACTER_LIMIT = 150_000
THREAD_HANDOFF_CHARACTER_LIMIT = (
    THREAD_HANDOFF_MESSAGE_CHARACTER_LIMIT + THREAD_HANDOFF_ACTIVITY_CHARACTER_LIMIT
)
THREAD_HANDOFF_ACTIVITY_DETAIL_LIMIT = 1_000
THREAD_HANDOFF_ACTIVITY_OUTPUT_LIMIT = 8_000
THREAD_HANDOFF_ACTIVITY_EVENT_CHARACTER_LIMIT = 8_000
THREAD_EVENT_MESSAGE_BYTES_LIMIT = 200_000
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
})
_RUNTIME_USAGE_KEYS = {
    "codex": "codex_usage",
    "claude_code": "claude_usage",
    "grok": "grok_usage",
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
        return send_thread_message(thread_id, body)
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
            thread_id, since, _event_page_limit(query), **page_kwargs
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
    if runtime_type == "grok":
        for key in ("coding_data_retention_opt_out", "zdr_enabled"):
            if isinstance(account.get(key), bool):
                response[key] = account[key]
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
) -> dict[str, Any]:
    """The one write path for agent work: start a turn on an idle thread
    (creating the thread on its first message) or steer the thread's running
    turn. There is no queue — a message that cannot run now is rejected with
    a retry hint and the caller decides."""
    if PRODUCT_THREAD_ID_RE.fullmatch(thread_id) is None:
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            "thread_id must start with app-, thread-, or schedule-",
        )
    message = _message(body)
    with _thread_send_lock(thread_id):
        session_config = state.thread_session_config(thread_id)
        agent_runtime, model, effort = _resolve_session_config(body, session_config, thread_id)
        switching_session = _session_configuration_changed(
            session_config, agent_runtime, model, effort
        )
        if not switching_session and orchestrator.steer_live_turn(
            thread_id, agent_runtime, message
        ):
            turn = None
            provider_session_id = None
        else:
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
                session_change_activity = None
                handoff_events: list[dict[str, Any]] = []
                missing_provider_context = (
                    session_config is not None
                    and not session_config.get("provider_session_id")
                )
                if switching_session or missing_provider_context:
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
                    launch_message = _session_handoff_message(handoff_events, message)
                turn = orchestrator.admit_turn(
                    cur,
                    after_commit,
                    thread_id,
                    agent_runtime,
                    model,
                    effort,
                    message,
                    pre_message_activity=session_change_activity,
                )
            orchestrator.launch_turn(turn, launch_message, provider_session_id)
    return {
        "status": "accepted",
        "thread": _public_thread(thread_id, agent_runtime, model, effort),
    }

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
    surfaces already refuse to offer it, but this is the executor: the boundary
    is enforced here, on the one thing a direct caller cannot forge, so a send
    that bypasses those surfaces cannot rotate a product thread onto it either.
    """
    return thread_id.startswith(SCHEDULE_THREAD_PREFIX)

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
    if event_type == "thread.message":
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

def _session_handoff_message(history: list[dict[str, Any]], message: str) -> str:
    """Build independently bounded conversation and activity handoff sections."""
    conversation = _bounded_handoff_section(
        [event for event in history if event.get("event_type") == "thread.message"],
        THREAD_HANDOFF_MESSAGE_CHARACTER_LIMIT,
    )
    activity = _bounded_handoff_section(
        [event for event in history if event.get("event_type") == "thread.activity"],
        THREAD_HANDOFF_ACTIVITY_CHARACTER_LIMIT,
    )
    return (
        "You are a new agent session continuing a thread previously handled by another "
        "agent session. Your provider-side context and cache are not available. Use the "
        "retained conversation and activity below, then respond to the current "
        "user message. Do not mention this handoff unless it is relevant.\n\n"
        "--- RETAINED CONVERSATION ---\n"
        f"{conversation or '[No retained messages.]'}\n"
        "--- END RETAINED CONVERSATION ---\n\n"
        "--- RECENT AGENT ACTIVITY ---\n"
        f"{activity or '[No retained activity.]'}\n"
        "--- END RECENT AGENT ACTIVITY ---\n\n"
        "--- CURRENT USER MESSAGE ---\n"
        f"{message}\n"
        "--- END CURRENT USER MESSAGE ---"
    )

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
