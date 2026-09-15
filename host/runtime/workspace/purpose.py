"""Short discovery descriptions shared by Apps and Schedules."""

from http import HTTPStatus
from typing import Any

from host.runtime.workspace.host_api import WorkspaceError


def validate_purpose(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 100 or "\n" in value or "\r" in value:
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, "purpose must be one line of at most 100 characters")
    return value.strip()
