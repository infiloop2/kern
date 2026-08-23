"""Retry the host's explicitly marked transient thread conflicts."""

from __future__ import annotations

from http import HTTPStatus
import time
from typing import Any, Callable

from host.runtime.workspace.host_api import WorkspaceError, call_admin_api


RETRY_MARKER = "retry shortly"
RETRY_DELAY_SECONDS = 0.5


def post_with_busy_retry(
    path: str,
    body: Any,
    *,
    attempts: int,
    exhausted_message: str,
    post: Callable[[str, str, Any], dict[str, Any]] = call_admin_api,
) -> dict[str, Any]:
    for attempt in range(attempts):
        try:
            return post("POST", path, body)
        except WorkspaceError as exc:
            transient = exc.status == HTTPStatus.CONFLICT and RETRY_MARKER in exc.message
            if not transient or attempt == attempts - 1:
                raise
            time.sleep(RETRY_DELAY_SECONDS)
    raise WorkspaceError(HTTPStatus.CONFLICT, exhausted_message)
