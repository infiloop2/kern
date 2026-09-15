"""Compact context notices in the real Chat and App renderers."""

import re
from typing import Any


def run(page: Any, url: str, log_in: Any) -> None:
    from playwright.sync_api import expect

    events = [
        {"seq": 1, "event_type": "thread.message", "payload": {"source": "user", "message": "Continue"}},
        {"seq": 2, "event_type": "thread.context_added", "payload": {
            "message": "Historical context transferred.",
        }},
        {"seq": 3, "event_type": "thread.context_added", "payload": {
            "message": "Self identity and 2 memories injected.",
        }},
        {"seq": 4, "event_type": "thread.context_added", "payload": {
            "message": "Self identity and 2 memories injected.",
        }},
        {"seq": 5, "event_type": "thread.message", "payload": {"source": "agent", "message": "Ready"}},
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
    expect(chat.locator(".thread-activity")).to_have_count(0)
    activity = chat.get_by_role("switch", name="Activity", exact=True)
    expect(activity).to_have_attribute("aria-checked", "false")
    activity.click()
    expect(notices).to_have_count(2)
    activity.click()
    expect(notices.first).to_be_visible()
    page.reload()
    expect(notices).to_have_count(2)

    # Clearing the transcript hides older context notices with the old history.
    events.append({"seq": 6, "event_type": "thread.memory_cleared", "payload": {
        "message": "Working memory cleared.",
    }})
    page.reload()
    expect(chat.locator(".thread-stopped")).to_have_text("Working memory cleared.")
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
        expect(app.locator("#chat-history-list details")).to_have_count(0)
        expect(app.locator(".chat-history-entry.user")).to_have_text("You:Continue")
    finally:
        page.evaluate("appId => window.KernHost.api('POST', `/v1/workspace/web-apps/apps/${appId}/archive`, {})", app_id)
