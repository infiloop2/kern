"""Compact context notices in the real Chat and App renderers."""

import re
from typing import Any


def run(page: Any, url: str, log_in: Any) -> None:
    from playwright.sync_api import expect

    page_ids = ["thread-1", 'memory-"quoted"<page>&']
    tooltip = "Recall: 12 ms; 1200 memory-content bytes.\nCurrent query: <img src=x onerror=alert(1)>\nSelected thread-1 r2: self memory\n" + "\n".join(page_ids)
    tooltip += "\n" + "\n".join(f"Candidate {n}: useful-guide-{n} r2 — semantic rank {n} cosine 0.610" for n in range(1, 13))

    def check_memory_panel(notice: Any) -> None:
        trigger = notice.get_by_role("button", name="Self identity and 2 memories injected.", exact=True)
        panel = notice.locator('[role="tooltip"]')
        page.mouse.move(0, 0)
        expect(panel).to_be_hidden()
        trigger.hover()
        expect(panel).to_be_visible()
        expect(panel).to_have_text(tooltip)
        panel_bounds = panel.bounding_box()
        scroll_bounds = notice.locator("xpath=ancestor::*[@id='chat-scroll' or @id='chat-history-scroll']").bounding_box()
        assert panel_bounds and scroll_bounds
        assert panel_bounds["y"] >= scroll_bounds["y"]
        assert panel_bounds["y"] + panel_bounds["height"] <= scroll_bounds["y"] + scroll_bounds["height"]
        panel.hover()
        expect(panel).to_be_visible()
        expect(panel.locator("*")).to_have_count(0)
        page.mouse.move(0, 0)
        expect(panel).to_be_hidden()
        trigger.focus()
        expect(panel).to_be_visible()
        expect(trigger).to_have_attribute("aria-describedby", panel.get_attribute("id"))
        trigger.evaluate("element => element.blur()")
        expect(panel).to_be_hidden()
        if page.evaluate("matchMedia('(pointer: coarse)').matches"):
            assert trigger.bounding_box()["height"] >= 44
            trigger.tap()
            expect(panel).to_be_visible()
            page.touchscreen.tap(1, 1)
            expect(panel).to_be_hidden()

    events = [
        {"seq": 1, "event_type": "thread.message", "payload": {"source": "user", "message": "Continue"}},
        {"seq": 2, "event_type": "thread.context_added", "payload": {
            "message": "Historical context transferred.",
        }},
        {"seq": 3, "event_type": "thread.context_added", "payload": {
            "message": "Self identity and 2 memories injected.",
            "memory_page_ids": page_ids, "memory_recall_details": tooltip,
        }},
        {"seq": 4, "event_type": "thread.context_added", "payload": {
            "message": "Self identity and 2 memories injected.",
        }},
        {"seq": 5, "event_type": "thread.context_added", "payload": {
            "message": "Self identity and 0 memories injected.", "memory_page_ids": [],
            "memory_recall_details": "Recall: 3 ms; 0 candidates. Current query: hello",
        }},
        {"seq": 6, "event_type": "thread.message", "payload": {"source": "agent", "message": "Ready"}},
    ]
    # Repeated notice text must retain separate rows, including after polling
    # and reloading. Context notices are not collapsible activity cards.
    page.route("**/v1/workspace/chat/threads/thread-1/events*",
               lambda route: route.fulfill(json={"events": events}))
    page.route("**/v1/workspace/web-apps/apps/*/conversation/events*",
               lambda route: route.fulfill(json={"events": events}))
    log_in(page, url)
    page.goto(url + "#chat/thread-1")
    chat = page.locator("#panel-workspace-chat")
    expect(chat.locator(".thread-stopped", has_text="Historical context transferred.")).to_be_visible()
    notices = chat.locator(".thread-stopped", has_text="Self identity and 2 memories injected.")
    expect(notices).to_have_count(2)
    expect(notices.first).to_be_visible()
    check_memory_panel(notices.first)
    expect(notices.nth(1).get_by_role("button")).to_have_count(0)
    expect(chat.locator(".thread-stopped", has_text="Self identity and 0 memories injected.").get_by_role("button")).to_have_count(1)
    expect(chat.locator(".thread-activity")).to_have_count(0)
    activity = chat.get_by_role("switch", name="Activity", exact=True)
    expect(activity).to_have_attribute("aria-checked", "false")
    activity.click()
    expect(notices).to_have_count(2)
    activity.click()
    expect(notices.first).to_be_visible()
    page.reload()
    expect(notices).to_have_count(2)

    check_memory_panel(notices.first)

    # A fresh notice can be the last row while the agent has not replied yet.
    original_events = events[:]
    events[:] = [
        {"seq": 1, "event_type": "thread.message", "payload": {
            "source": "user", "message": "Earlier conversation\n" * 80,
        }},
        original_events[2],
    ]
    page.reload()
    bottom_notice = chat.locator(".memory-notice")
    expect(bottom_notice).to_be_visible()
    before_scroll = chat.locator("#chat-scroll").evaluate("element => element.scrollTop")
    check_memory_panel(bottom_notice)
    assert chat.locator("#chat-scroll").evaluate("element => element.scrollTop") == before_scroll
    events[:] = original_events

    # Clearing the transcript hides older context notices with the old history.
    events.append({"seq": 7, "event_type": "thread.memory_cleared", "payload": {
        "message": "Working memory cleared.",
    }})
    page.reload()
    expect(chat.locator(".thread-stopped")).to_have_text("Working memory cleared.")
    notice_style = """element => {
        const style = getComputedStyle(element);
        return { color: style.color, fontSize: style.fontSize,
                 textAlign: style.textAlign };
    }"""
    cleared_style = chat.locator(".thread-stopped").evaluate(notice_style)
    events.pop()

    if page.get_by_role("button", name="Open navigation", exact=True).is_visible():
        page.get_by_role("button", name="Open navigation", exact=True).click()
    page.get_by_role("button", name="New app", exact=True).click()
    app = page.locator("#panel-workspace-web-apps")
    expect(app.locator("#app-title")).to_have_text(re.compile(r"app-\d+"))
    app_id = app.locator("#app-title").inner_text()
    try:
        app.locator("#history-toggle").click()
        expect(app.locator(".chat-history-entry.stopped", has_text="Historical context transferred.")).to_be_visible()
        expect(app.locator(".chat-history-entry.stopped", has_text="Self identity and 2 memories injected.")).to_have_count(2)
        app_notices = app.locator(".chat-history-entry.stopped .chat-history-message", has_text="Self identity and 2 memories injected.")
        check_memory_panel(app_notices.first)
        expect(app_notices.nth(1).get_by_role("button")).to_have_count(0)
        expect(app.locator(".chat-history-message", has_text="Self identity and 0 memories injected.").get_by_role("button")).to_have_count(1)
        context_message = app.locator(".chat-history-entry.stopped .chat-history-message").first
        assert context_message.evaluate(notice_style) == cleared_style
        expect(app.locator("#chat-history-list details")).to_have_count(0)
        expect(app.locator(".chat-history-entry.user")).to_have_text("You:Continue")
        events[:] = [
            {"seq": 1, "event_type": "thread.message", "payload": {
                "source": "user", "message": "Earlier conversation\n" * 80,
            }},
            original_events[2],
        ]
        page.reload()
        app.locator("#history-toggle").click()
        bottom_app_notice = app.locator(".memory-notice")
        expect(bottom_app_notice).to_be_visible()
        before_scroll = app.locator("#chat-history-scroll").evaluate("element => element.scrollTop")
        check_memory_panel(bottom_app_notice)
        assert app.locator("#chat-history-scroll").evaluate("element => element.scrollTop") == before_scroll

    finally:
        page.evaluate("appId => window.KernHost.api('POST', `/v1/workspace/web-apps/apps/${appId}/archive`, {})", app_id)
