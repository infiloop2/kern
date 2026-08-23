"""Shared validation for Workspace query-string mappings."""

from http import HTTPStatus

from host.runtime.workspace.host_api import WorkspaceError


def one(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    if not values:
        return None
    if len(values) != 1:
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, f"{key} must be provided once")
    return values[0]
