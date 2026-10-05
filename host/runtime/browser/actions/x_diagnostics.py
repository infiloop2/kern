"""Read-only structural evidence; never return page text, URLs or error messages."""
from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit


def reply_target(page: Any, reply_id: str) -> Any:
    return page.locator('article[data-testid="tweet"]').filter(
        has=page.locator(f'a[href$="/status/{reply_id}"]'))


def preparation_facts(page: Any, reply_id: str) -> dict[str, Any]:
    facts: dict[str, Any] = {}
    if reply_id:
        facts["reply_target_id"] = reply_id  # Already bound to the approval.
    try:
        facts["page_closed"] = page.is_closed()
        if facts["page_closed"]:
            return facts
        url = urlsplit(page.url)
        facts["host"] = url.hostname or ""
        # Known route categories only, never arbitrary paths/query parameters.
        facts["page_route"] = (
            "target" if reply_id and url.path.endswith(f"/status/{reply_id}") else
            "login" if url.path.startswith("/i/flow/login") else
            "account_access" if url.path.startswith("/account/access") else
            "composer" if url.path == "/compose/post" else "other"
        )
        facts["article_count"] = page.locator('article[data-testid="tweet"]').count()
        facts["dialog_count"] = page.get_by_role("dialog").count()
        facts["composer_count"] = page.get_by_role("dialog").get_by_test_id("tweetTextarea_0").count()
        if reply_id:
            target = reply_target(page, reply_id)
            facts["target_count"] = target.count()
            if facts["target_count"] == 1:
                facts["target_visible"] = target.is_visible()
                reply = target.get_by_test_id("reply")
                facts["reply_control_count"] = reply.count()
                if facts["reply_control_count"] == 1:
                    facts["reply_visible"] = reply.is_visible()
                    facts["reply_enabled"] = reply.is_enabled(timeout=500)
                    # A snapshot of the center hit test, not a claim about all
                    # Playwright actionability checks or the preceding timeout.
                    facts["reply_center_unobstructed"] = reply.evaluate("""element => {
                        const rect = element.getBoundingClientRect();
                        const left = Math.max(0, rect.left), right = Math.min(innerWidth, rect.right);
                        const top = Math.max(0, rect.top), bottom = Math.min(innerHeight, rect.bottom);
                        if (right <= left || bottom <= top) return false;
                        const hit = document.elementFromPoint((left + right) / 2, (top + bottom) / 2);
                        return hit !== null && element.contains(hit);
                    }""", timeout=500)
    except Exception:
        # A closed/navigating page or failed diagnostic must never replace the
        # original error or cause another click. Retain any facts already read.
        facts["snapshot_incomplete"] = True
    return facts
