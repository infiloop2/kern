"""Compact context notices in the real Chat and App renderers."""

import re
from typing import Any


def run(page: Any, url: str, log_in: Any) -> None:
    from playwright.sync_api import expect

    page_ids = ["thread-1", 'memory-"quoted"<page>&']
    tooltip = "Recall: 12 ms; 1200 memory-content bytes.\nCurrent query: <img src=x onerror=alert(1)>\nSelected thread-1 r2: self memory\n" + "\n".join(page_ids)
    tooltip += "\n" + "\n".join(f"Candidate {n}: useful-guide-{n} r2 — semantic rank {n} cosine 0.610" for n in range(1, 13))

    historical_context = (
        "--- RETAINED CONVERSATION ---\n"
        'User:\n  Keep <img src=x onerror=alert(1)> & "quotes" literal.\n'
        + "Agent:\nEarlier retained answer.\n" * 350
        + "\n\n… [middle omitted from preview] …\n\n"
        + "Agent:\nLater retained answer.\n" * 350
        + "--- END RETAINED CONVERSATION ---\n\n"
        "--- CURRENT USER MESSAGE ---\nContinue\n--- END CURRENT USER MESSAGE ---"
    )

    def check_memory_panel(
        notice: Any, dismiss: Any, *,
        label: str = "Self identity and 2 memories injected.", details: str = tooltip,
    ) -> None:
        trigger = notice.get_by_role("button", name=label, exact=True)
        panel = notice.locator('[role="tooltip"]')
        page.mouse.move(0, 0)
        expect(panel).to_be_hidden()
        trigger.hover()
        expect(panel).to_be_hidden()
        expect(trigger).to_have_attribute("aria-controls", panel.get_attribute("id"))
        assert trigger.get_attribute("aria-describedby") is None
        trigger.click()
        expect(panel).to_be_visible()
        expect(panel).to_have_text(details)
        assert panel.text_content() == details
        expect(trigger).to_have_attribute("aria-expanded", "true")
        expect(trigger).to_have_attribute("aria-describedby", panel.get_attribute("id"))
        panel_bounds = panel.bounding_box()
        scroll_bounds = notice.locator("xpath=ancestor::*[@id='chat-scroll' or @id='chat-history-scroll']").bounding_box()
        assert panel_bounds and scroll_bounds
        assert panel_bounds["y"] >= scroll_bounds["y"]
        assert panel_bounds["y"] + panel_bounds["height"] <= scroll_bounds["y"] + scroll_bounds["height"]
        panel.click(force=True)
        expect(panel).to_be_visible()
        expect(panel.locator("*")).to_have_count(0)
        dismiss.click()
        expect(panel).to_be_hidden()
        trigger.focus()
        expect(panel).to_be_hidden()
        trigger.press("Enter")
        expect(panel).to_be_visible()
        expect(trigger).to_have_attribute("aria-describedby", panel.get_attribute("id"))
        trigger.press("Escape")
        expect(panel).to_be_hidden()
        expect(trigger).to_have_attribute("aria-expanded", "false")
        assert trigger.get_attribute("aria-describedby") is None
        trigger.click()
        expect(panel).to_be_visible()
        page.evaluate(
            "() => document.documentElement.dispatchEvent("
            "new MouseEvent('click', { bubbles: true, composed: true }))"
        )
        expect(panel).to_be_hidden()
        if page.evaluate("matchMedia('(pointer: coarse)').matches"):
            assert trigger.bounding_box()["height"] >= 44
            trigger.tap()
            expect(panel).to_be_visible()
            page.touchscreen.tap(1, 1)
            expect(panel).to_be_hidden()

    events = [
        {"seq": 1, "event_type": "thread.message", "payload": {"source": "user", "message": "Continue"}},
        {"seq": 2, "event_type": "thread.notice", "payload": {
            "message": "Historical context transferred.", "historical_context": historical_context,
        }},
        {"seq": 3, "event_type": "thread.notice", "payload": {
            "message": "Self identity and 2 memories injected.",
            "memory_page_ids": page_ids, "memory_recall_details": tooltip,
        }},
        {"seq": 4, "event_type": "thread.notice", "payload": {
            "message": "Self identity and 2 memories injected.",
        }},
        {"seq": 5, "event_type": "thread.notice", "payload": {
            "message": "Self identity and 0 memories injected.", "memory_page_ids": [],
            "memory_recall_details": "Recall: 3 ms; 0 candidates. Current query: hello",
        }},
        {"seq": 6, "event_type": "thread.notice", "payload": {
            "message": "Historical context transferred.",
        }},
        {"seq": 7, "event_type": "thread.message", "payload": {"source": "agent", "message": "Ready"}},
    ]
    for event in events:
        if event["event_type"] == "thread.notice":
            event["payload"]["notice"] = {"kind": "history_transfer" if event["payload"]["message"] == "Historical context transferred." else "memory_injection", "summary": event["payload"]["message"]}

    # Repeated notice text must retain separate rows, including after polling
    # and reloading. Context notices are not collapsible activity cards.
    page.route("**/v1/workspace/chat/threads/thread-1/events*",
               lambda route: route.fulfill(json={"events": events}))
    page.route("**/v1/workspace/web-apps/apps/*/conversation/events*",
               lambda route: route.fulfill(json={"events": events}))
    log_in(page, url)
    page.goto(url + "#chat/thread-1")
    chat = page.locator("#panel-workspace-chat")
    history_notices = chat.locator(".thread-stopped", has_text="Historical context transferred.")
    expect(history_notices).to_have_count(2)
    check_memory_panel(history_notices.first, chat.locator("#thread-title"),
                       label="Historical context transferred.", details=historical_context)
    expect(history_notices.nth(1).get_by_role("button")).to_have_count(0)
    notices = chat.locator(".thread-stopped", has_text="Self identity and 2 memories injected.")
    expect(notices).to_have_count(2)
    expect(notices.first).to_be_visible()
    check_memory_panel(notices.first, chat.locator("#thread-title"))
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

    check_memory_panel(notices.first, chat.locator("#thread-title"))
    check_memory_panel(history_notices.first, chat.locator("#thread-title"),
                       label="Historical context transferred.", details=historical_context)

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
    check_memory_panel(bottom_notice, chat.locator("#thread-title"))
    assert chat.locator("#chat-scroll").evaluate("element => element.scrollTop") == before_scroll
    events[:] = original_events

    # Clearing the transcript hides older context notices with the old history.
    events.append({"seq": 8, "event_type": "thread.memory_cleared", "payload": {
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
        app_history = app.locator(".chat-history-message", has_text="Historical context transferred.")
        expect(app_history).to_have_count(2)
        check_memory_panel(app_history.first, app.locator("#app-title"),
                           label="Historical context transferred.", details=historical_context)
        expect(app_history.nth(1).get_by_role("button")).to_have_count(0)
        expect(app.locator(".chat-history-entry.stopped", has_text="Self identity and 2 memories injected.")).to_have_count(2)
        app_notices = app.locator(".chat-history-entry.stopped .chat-history-message", has_text="Self identity and 2 memories injected.")
        check_memory_panel(app_notices.first, app.locator("#app-title"))
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
        check_memory_panel(bottom_app_notice, app.locator("#app-title"))
        assert app.locator("#chat-history-scroll").evaluate("element => element.scrollTop") == before_scroll

    finally:
        page.evaluate("appId => window.KernHost.api('POST', `/v1/workspace/web-apps/apps/${appId}/archive`, {})", app_id)


def run_kern_notices(page: Any, url: str, log_in: Any) -> None:
    """Incoming messages and action outcomes stay visible with Activity off."""
    from urllib.parse import parse_qs, urlparse
    from playwright.sync_api import expect

    literal = 'This is an automated message from Kern.\n\n---\n\n<img src=x onerror=alert(1)> & "literal"'
    labels = ["Scheduled trigger for Release checker: Summarize open work", "Message from Portfolio: Review the release",
              "Published Portfolio UI", "Saved release guidance memory", "Failed: Sent message to Reviewer. Agent unavailable",
              "1 additional memory suggested."]
    events = [
        {"seq": 1, "event_type": "thread.message", "payload": {"source": "user", "message": literal, "notice": {"kind": "operator"}}},
        {"seq": 2, "event_type": "thread.notice", "payload": {"source": "user", "message": literal, "notice": {"kind": "scheduled_trigger", "summary": labels[0]}}},
        {"seq": 3, "event_type": "thread.notice", "payload": {"source": "user", "message": literal, "notice": {"kind": "agent_message", "summary": labels[1]}}},
        *[{"seq": index + 4, "event_type": "thread.notice", "payload": {"notice": {"kind": "app_ui_published", "summary": label, "details": literal}}}
          for index, label in enumerate(labels[2:5])],
        {"seq": 7, "event_type": "thread.notice", "payload": {"message": labels[5], "notice": {"kind": "memory_suggestion", "summary": labels[5]}, "memory_page_ids": ["guide"], "memory_recall_details": literal}},
        {"seq": 8, "event_type": "thread.activity", "payload": {"activity": {"provider": "codex", "activity_id": "tool-1", "kind": "tool", "phase": "completed", "title": "General tool", "detail": "Only Activity"}}},
        {"seq": 9, "event_type": "thread.message", "payload": {"source": "agent", "message": "Done"}},
    ]

    def route_events(route: Any) -> None:
        query = parse_qs(urlparse(route.request.url).query)
        visible = events
        if query.get("include_activity") == ["false"]:
            visible = [event for event in visible if event["event_type"] != "thread.activity"]
        route.fulfill(json={"events": visible})

    page.route("**/v1/workspace/chat/threads/thread-1/events*", route_events)
    page.route("**/v1/workspace/web-apps/apps/*/conversation/events*", route_events)
    log_in(page, url)
    page.goto(url + "#chat/thread-1")
    chat = page.locator("#panel-workspace-chat")

    def check(surface: Any, activity_selector: str) -> None:
        for label in labels:
            expect(surface.get_by_role("button", name=label, exact=True)).to_be_visible()
        expect(surface.locator(activity_selector)).not_to_be_visible()
        trigger = surface.get_by_role("button", name=labels[2], exact=True)
        trigger.click()
        panel = surface.locator(".memory-notice-open .memory-pages")
        expect(panel).to_have_text(literal)
        expect(panel.locator("img")).to_have_count(0)
        trigger.press("Escape")
        expect(panel).to_have_count(0)
        toggle = surface.get_by_role("switch", name="Activity", exact=True)
        toggle.click()
        expect(surface.locator(activity_selector)).to_be_visible()
        expect(surface.get_by_role("button", name=labels[2], exact=True)).to_have_count(1)
        toggle.click()
        expect(surface.locator(activity_selector)).not_to_be_visible()
        trigger.click()
        # A new poll patches the conversation while preserving the opened row.
        events.append({"seq": 10, "event_type": "thread.message", "payload": {"source": "agent", "message": "Polled"}})
        expect(surface.get_by_text("Polled", exact=True)).to_be_visible(timeout=12_000)
        expect(surface.locator(".memory-notice-open .memory-pages")).to_have_text(literal)
        events.pop()
        trigger.press("Escape")

    check(chat, ".thread-activity")
    expect(chat.locator(".thread-user")).to_have_text(literal)
    page.reload()
    expect(chat.get_by_role("button", name=labels[0], exact=True)).to_be_visible()
    if page.get_by_role("button", name="Open navigation", exact=True).is_visible():
        page.get_by_role("button", name="Open navigation", exact=True).click()
    page.get_by_role("button", name="New app", exact=True).click()
    app = page.locator("#panel-workspace-web-apps")
    expect(app.locator("#app-title")).to_have_text(re.compile(r"app-\d+"))
    app_id = app.locator("#app-title").inner_text()
    try:
        app.locator("#history-toggle").click()
        check(app, ".chat-history-entry.activity")
        expect(app.locator(".chat-history-entry.user .chat-history-message")).to_have_text(literal)
        events.append({"seq": 20, "event_type": "thread.memory_cleared", "payload": {"message": "Working memory cleared."}})
        page.reload()
        app.locator("#history-toggle").click()
        expect(app.get_by_role("button", name=labels[0], exact=True)).to_have_count(0)
    finally:
        page.evaluate("appId => window.KernHost.api('POST', `/v1/workspace/web-apps/apps/${appId}/archive`, {})", app_id)
