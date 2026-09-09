"""Operator-only combined approval summaries; decisions use existing routes."""

from http import HTTPStatus
import re
from typing import Any

from host.runtime.admin_api.errors import ApiError
from host.runtime.core import state
from host.runtime.tools import tools_host


def list_approvals(query: dict[str, list[str]]) -> dict[str, Any]:
    views = query.get("view", ["pending"])
    pages = query.get("page", ["1"])
    if len(views) != 1 or views[0] not in {"pending", "history"}:
        raise ApiError(HTTPStatus.BAD_REQUEST, "view must be pending or history")
    if len(pages) != 1 or not re.fullmatch(r"[1-9][0-9]{0,5}", pages[0]):
        raise ApiError(HTTPStatus.BAD_REQUEST, "page must be a positive integer")
    result = state.page_approvals(views[0], int(pages[0]))
    for item in result["items"]:
        if item["kind"] == "tool":
            tool = tools_host.BUNDLED_TOOLS.get(item["tool_id"])
            item["source"] = tool.manifest.display_name if tool else item["tool_id"]
        else:
            item["source"] = "GitHub"
            item["summary"] = f"Push to {item['owner']}/{item['repo']}"
    return result
