"""Read-only structural evidence; never return page text, URLs or error messages."""
from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit
from host.runtime.browser.actions.x_composer import MATCHES_TEXT


COMPOSER_STATE = """() => {
    const visible = element => {
        const style = getComputedStyle(element), rect = element.getBoundingClientRect();
        return style.visibility !== 'hidden' && style.visibility !== 'collapse'
            && rect.width > 0 && rect.height > 0;
    };
    const editors = Array.from(document.querySelectorAll('[role="dialog"] [data-testid="tweetTextarea_0"]'));
    if (editors.length > 200) return {page_state_scan_truncated: true};
    const active = editors.filter(editor => visible(editor) && visible(editor.closest('[role="dialog"]')));
    if (active.length !== 1) return {};
    const editor = active[0];
    const enabled = !editor.closest('[aria-disabled="true"]') && !editor.hasAttribute('disabled');
    return {
        composer_attached: editor.isConnected,
        composer_visible: true,
        composer_enabled: enabled,
        composer_editable: enabled && editor.isContentEditable && !editor.closest('[aria-readonly="true"]'),
        composer_focused: document.activeElement === editor,
        composer_empty: (__MATCHES_TEXT__)([editor, '']),
    };
}""".replace("__MATCHES_TEXT__", MATCHES_TEXT)


# These scripts return only counts, booleans, bounded numbers and fixed labels.
# Never return hrefs, text, arbitrary attributes or Playwright error messages.
PAGE_FACTS = """replyId => {
    const visible = element => {
        const style = getComputedStyle(element);
        const rect = element.getBoundingClientRect();
        return style.visibility !== 'hidden' && style.visibility !== 'collapse'
            && rect.width > 0 && rect.height > 0;
    };
    const dialogs = Array.from(document.querySelectorAll('[role="dialog"]'));
    const editors = Array.from(document.querySelectorAll('[data-testid="tweetTextarea_0"]'));
    const loginInputs = Array.from(document.querySelectorAll('input[autocomplete="username"]'));
    const facts = {
        visible_dialog_count: dialogs.slice(0, 200).filter(visible).length,
        inline_editor_count: editors.slice(0, 200).filter(e => !e.closest('[role="dialog"]')).length,
        popup_editor_count: editors.slice(0, 200).filter(e => e.closest('[role="dialog"]')).length,
        visible_inline_editor_count: editors.slice(0, 200).filter(e => !e.closest('[role="dialog"]') && visible(e)).length,
        visible_popup_editor_count: editors.slice(0, 200).filter(e => e.closest('[role="dialog"]') && visible(e)).length,
        login_input_visible: loginInputs.slice(0, 200).some(visible),
        page_state_scan_truncated: dialogs.length > 200 || editors.length > 200 || loginInputs.length > 200,
    };
    Object.assign(facts, (__COMPOSER_STATE__)());
    if (!replyId) return facts;
    const links = document.querySelectorAll('a[href]');
    const articles = new Set(), timestampArticles = new Set();
    Object.assign(facts, {
        target_id_link_count: 0,
        target_id_article_count: 0,
        target_id_timestamp_article_count: 0,
        target_id_query_link_count: 0,
        target_id_extra_path_link_count: 0,
        target_id_exact_suffix_link_count: 0,
        target_link_scan_truncated: links.length > 2000,
    });
    const pattern = new RegExp('^/[^/]+/status/' + replyId + '(/|$)');
    for (const link of Array.from(links).slice(0, 2000)) {
        let url;
        try { url = new URL(link.getAttribute('href'), document.baseURI); }
        catch { continue; }
        if (url.protocol !== 'https:' || !['x.com', 'www.x.com', 'twitter.com', 'www.twitter.com'].includes(url.hostname)
            || !pattern.test(url.pathname)) continue;
        facts.target_id_link_count++;
        if (url.search) facts.target_id_query_link_count++;
        if (!url.pathname.endsWith('/status/' + replyId)) facts.target_id_extra_path_link_count++;
        if (link.getAttribute('href').endsWith('/status/' + replyId)) facts.target_id_exact_suffix_link_count++;
        const article = link.closest('article[data-testid="tweet"]');
        if (article) {
            articles.add(article);
            if (link.querySelector('time')) timestampArticles.add(article);
        }
    }
    facts.target_id_article_count = articles.size;
    facts.target_id_timestamp_article_count = timestampArticles.size;
    return facts;
}""".replace("__COMPOSER_STATE__", COMPOSER_STATE)

REPLY_FACTS = """replyId => {
    const targets = Array.from(document.querySelectorAll('article[data-testid="tweet"]'))
        .filter(article => article.querySelector('a[href$="/status/' + replyId + '"], a[href$="/status/' + replyId + '/history"]'));
    if (targets.length !== 1) return {};
    const replies = targets[0].querySelectorAll('[data-testid="reply"]');
    if (replies.length !== 1) return {};
    const element = replies[0];
    const rect = element.getBoundingClientRect();
    const bounded = value => Number.isFinite(value) ? Math.max(-100000, Math.min(100000, Math.round(value))) : 0;
    const left = Math.max(0, rect.left), right = Math.min(innerWidth, rect.right);
    const top = Math.max(0, rect.top), bottom = Math.min(innerHeight, rect.bottom);
    const inViewport = right > left && bottom > top;
    // Use the visible portion's center, matching the previous hit-test snapshot.
    const hit = inViewport ? document.elementFromPoint((left + right) / 2, (top + bottom) / 2) : null;
    const tags = ['a', 'button', 'div', 'span', 'svg', 'path', 'input', 'textarea', 'main', 'section', 'body', 'html', 'form', 'label', 'iframe'];
    const roles = ['dialog', 'button', 'menu', 'menuitem', 'alert', 'presentation', 'tooltip', 'link'];
    const testIds = ['reply', 'tweet', 'mask', 'sheetDialog', 'Dropdown', 'toast', 'tweetTextarea_0', 'tweetButton'];
    let role = '', testId = '';
    // Page-controlled strings are emitted only if they are known fixed labels.
    for (let node = hit, depth = 0; node && depth < 5; node = node.parentElement, depth++) {
        if (!role && node.hasAttribute('role')) {
            const value = node.getAttribute('role');
            role = roles.includes(value) ? value : 'other';
        }
        if (!testId && node.hasAttribute('data-testid')) {
            const value = node.getAttribute('data-testid');
            testId = testIds.includes(value) ? value : 'other';
        }
    }
    const tag = hit ? hit.tagName.toLowerCase() : '';
    return {
        reply_rect_x: bounded(rect.x), reply_rect_y: bounded(rect.y),
        reply_rect_width: bounded(rect.width), reply_rect_height: bounded(rect.height),
        viewport_width: bounded(innerWidth), viewport_height: bounded(innerHeight),
        reply_in_viewport: inViewport,
        reply_fully_in_viewport: inViewport && rect.left >= 0 && rect.top >= 0 && rect.right <= innerWidth && rect.bottom <= innerHeight,
        reply_center_unobstructed: hit !== null && element.contains(hit),
        reply_center_hit_tag: tags.includes(tag) ? tag : (hit ? 'other' : ''),
        reply_center_hit_role: role, reply_center_hit_test_id: testId,
    };
}"""


SNAPSHOT_TIMEOUT_MS = 500
_COUNT_FIELDS = frozenset({
    "visible_dialog_count", "inline_editor_count", "popup_editor_count",
    "visible_inline_editor_count", "visible_popup_editor_count", "target_id_link_count",
    "target_id_article_count", "target_id_timestamp_article_count", "target_id_query_link_count",
    "target_id_extra_path_link_count", "target_id_exact_suffix_link_count",
})
_BOOL_FIELDS = frozenset({
    "composer_attached", "composer_visible", "composer_enabled", "composer_editable",
    "composer_focused", "composer_empty",
    "login_input_visible", "page_state_scan_truncated", "target_link_scan_truncated",
    "reply_in_viewport", "reply_fully_in_viewport", "reply_center_unobstructed",
})
_GEOMETRY_FIELDS = frozenset({
    "reply_rect_x", "reply_rect_y", "reply_rect_width", "reply_rect_height",
    "viewport_width", "viewport_height",
})
_LABEL_FIELDS = {
    "reply_center_hit_tag": frozenset({"", "other", "a", "button", "div", "span", "svg", "path",
        "input", "textarea", "main", "section", "body", "html", "form", "label", "iframe"}),
    "reply_center_hit_role": frozenset({"", "other", "dialog", "button", "menu", "menuitem",
        "alert", "presentation", "tooltip", "link"}),
    "reply_center_hit_test_id": frozenset({"", "other", "reply", "tweet", "mask", "sheetDialog",
        "Dropdown", "toast", "tweetTextarea_0", "tweetButton"}),
}


def _validated_facts(value: Any) -> dict[str, Any]:
    """Page globals can be overridden: validate every returned field in Python."""
    if not isinstance(value, dict):
        return {"snapshot_incomplete": True}
    facts: dict[str, Any] = {}
    for key, item in value.items():
        if key in _COUNT_FIELDS and type(item) is int and 0 <= item <= 2000:
            facts[key] = item
        elif key in _BOOL_FIELDS and type(item) is bool:
            facts[key] = item
        elif key in _GEOMETRY_FIELDS and type(item) is int and -100000 <= item <= 100000:
            facts[key] = item
        elif key in _LABEL_FIELDS and isinstance(item, str) and item in _LABEL_FIELDS[key]:
            facts[key] = item
        else:
            facts["snapshot_incomplete"] = True
    return facts


def _evaluate_facts(page: Any, script: str, reply_id: str) -> dict[str, Any]:
    # Page.evaluate has no execution deadline; locator timeout only bounds the
    # element lookup. Chromium's Runtime.evaluate timeout bounds the JS itself.
    session = page.context.new_cdp_session(page)
    try:
        response = session.send("Runtime.evaluate", {
            "expression": f"({script})({json.dumps(reply_id)})",
            "timeout": SNAPSHOT_TIMEOUT_MS,
            "returnByValue": True,
        })
        if "exceptionDetails" in response:
            return {"snapshot_incomplete": True}  # Never retain provider details.
        return _validated_facts(response.get("result", {}).get("value"))
    finally:
        try:
            session.detach()
        except Exception:
            pass  # A closed page must not mask the original failure.


def reply_target(page: Any, reply_id: str) -> Any:
    # Edited posts can link their timestamp to history. Keep the ID and both
    # allowed endings exact; preparation still requires one unique article.
    return page.locator('article[data-testid="tweet"]').filter(
        has=page.locator(f'a[href$="/status/{reply_id}"], a[href$="/status/{reply_id}/history"]'))


def composer_state(page: Any) -> dict[str, Any]:
    # Same bounded evaluation and Python allowlist as failure snapshots. Only
    # an explicit boolean true permits skipping the clear operation.
    try:
        return _evaluate_facts(page, COMPOSER_STATE, "")
    except Exception:
        return {"snapshot_incomplete": True}


def preparation_facts(page: Any, reply_id: str) -> dict[str, Any]:
    facts: dict[str, Any] = {"snapshot_phase": "after_failure"}
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
        facts["account_access_route"] = url.path.startswith("/account/access")
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
                    # Snapshot only: never click, scroll, retry or force an action.
                    facts.update(_evaluate_facts(page, REPLY_FACTS, reply_id))
    except Exception:
        # A closed/navigating page or failed diagnostic must never replace the
        # original error or cause another click. Retain any facts already read.
        facts["snapshot_incomplete"] = True
    # The page-wide scan is independent: a detached reply control must not hide
    # link/editor evidence, and a failed scan must not erase locator facts.
    if facts.get("page_closed") is False:
        try:
            facts.update(_evaluate_facts(page, PAGE_FACTS, reply_id))
        except Exception:
            facts["snapshot_incomplete"] = True
    return facts
