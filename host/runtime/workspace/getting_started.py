"""Read-only completion signals for the operator onboarding checklist."""

from __future__ import annotations

from http import HTTPStatus
from typing import Any

from host.runtime.core import db
from host.runtime.workspace.host_api import WorkspaceError, active_agent_runtimes
from host.session_options import SCRIPT_RUNTIME


def route_browser(
    method: str,
    path: str,
    body: Any,
    query: dict[str, list[str]],
) -> dict[str, bool]:
    if query:
        raise WorkspaceError(
            HTTPStatus.BAD_REQUEST,
            "getting started does not accept query parameters",
        )
    if body is not None:
        raise WorkspaceError(
            HTTPStatus.BAD_REQUEST,
            "getting started does not accept a request body",
        )
    if method == "GET" and path == "/getting-started":
        return completion_status()
    if method == "POST" and path == "/getting-started/dismiss":
        return dismiss()
    raise WorkspaceError(HTTPStatus.NOT_FOUND, "getting started route not found")


def completion_status() -> dict[str, bool]:
    """Derive every checklist step from live host state.

    Nothing is latched. A step that stops being true goes back to incomplete,
    which keeps all four steps consistent and keeps the checklist honest: a
    host whose last provider was deactivated cannot run an agent, so claiming
    otherwise would be worse than showing the step again. An operator who does
    not want the panel dismisses it, and that decision is what we store.

    The Workspace owns the whole checklist so that `/v1/workspace/*` stays a
    plain proxy, at the cost of one cached admin read per poll.
    """
    with db.transaction() as cur:
        # Every table here is Workspace-owned. `thread_sessions` would be the
        # sharper signal for chat, since the host writes it only on acceptance,
        # but it is host-owned and this role has no grant on it. Chat writes a
        # `chat_threads` row while sending, so a rejected send can still count;
        # for a checklist that is a better trade than reaching across the
        # database permission boundary.
        cur.execute(
            "SELECT"
            " EXISTS (SELECT 1 FROM chat_threads),"
            " EXISTS (SELECT 1 FROM web_apps),"
            " EXISTS (SELECT 1 FROM schedules),"
            " EXISTS (SELECT 1 FROM workspace_onboarding_dismissal)"
        )
        row = cur.fetchone()
        assert row is not None
        chat_created, app_created, schedule_created, dismissed = row
    return {
        "provider_ready": _inference_provider_ready(),
        "chat_created": bool(chat_created),
        "app_created": bool(app_created),
        "schedule_created": bool(schedule_created),
        "dismissed": bool(dismissed),
    }


def _inference_provider_ready() -> bool:
    """Whether the operator has connected an inference provider.

    Kern runs the script runtime itself, so an active one says nothing about
    whether any provider is configured. Unknown activation reads as incomplete:
    the checklist only nudges, so re-showing a step costs less than ticking one
    we cannot confirm.
    """
    return any(
        runtime != SCRIPT_RUNTIME for runtime in (active_agent_runtimes() or ())
    )


def dismiss() -> dict[str, bool]:
    """Hide the checklist for every operator browser on this host."""
    with db.transaction() as cur:
        cur.execute(
            "INSERT INTO workspace_onboarding_dismissal (singleton) VALUES (TRUE)"
            " ON CONFLICT (singleton) DO NOTHING"
        )
    return completion_status()
