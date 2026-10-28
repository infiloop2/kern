"""Real toolbar/panel journeys for each model-agent surface, desktop and mobile."""
import re
from urllib.parse import urlparse, parse_qs
from typing import Any


def run(page: Any, url: str, log_in: Any) -> None:
    from playwright.sync_api import expect
    print(f"Reminder panel smoke: {page.viewport_size}", flush=True)
    entries: dict[str, list[dict[str, Any]]] = {}
    requests = []

    def respond(route: Any) -> None:
        parts = urlparse(route.request.url).path.split("/")
        thread = parts[4]
        rows = entries.setdefault(thread, [])
        method = route.request.method
        requests.append(method)
        assert method == "GET", method
        before = int(parse_qs(urlparse(route.request.url).query).get("before", ["999999999"])[0])
        history = sorted((r for r in rows if r["id"] < before), key=lambda r: r["id"], reverse=True)
        route.fulfill(json={"reminders": history[:2], "pending_count": sum(r["status"] == "pending" for r in rows), "pending": [r for r in rows if r["status"] == "pending"], "next_before": history[1]["id"] if len(history) > 2 else None})


    page.route(re.compile(r"/v1/workspace/reminders/"), respond)
    log_in(page, url)
    app = page.evaluate("() => window.KernHost.api('POST', '/v1/workspace/web-apps/apps', {})")
    app_id = app["app"]["app_id"] if "app" in app else app["app_id"]
    schedule = page.evaluate("""() => window.KernHost.api('POST', '/v1/workspace/schedules', {
      name: 'Reminder agent', triggers: [], agent_runtime: 'codex', model: 'gpt-6.1-sol', effort: 'high'
    })""")["schedule"]
    targets = [("thread-1", "chat"), ("thread-6", "chat"),
               (schedule["thread_id"], "chat"), (app_id, "web-apps")]
    for thread, workspace in targets:
        print(f"Reminder toolbar: {thread}", flush=True)
        page.goto(url + ("#apps/" if workspace == "web-apps" else "#chat/") + thread)
        surface = page.locator(f"#panel-workspace-{workspace}")
        button = surface.locator("#agent-reminders")
        expect(button).to_be_visible()
        expect(button).to_be_in_viewport()
        button.click()
        dialog = surface.get_by_role("dialog", name="Reminders", exact=True)
        expect(dialog).to_be_visible()
        expect(dialog.locator("[data-pending]")).to_have_text("No pending reminders.")
        expect(dialog.locator("[data-history]")).to_have_text("No completed reminders.")
        expect(dialog.get_by_role("heading", name="Completed", exact=True)).to_be_visible()
        expect(dialog.locator("form, input, textarea")).to_have_count(0)
        expect(dialog.get_by_role("button", name="New reminder", exact=True)).to_have_count(0)
        expect(dialog.get_by_text("Up to 5", exact=False)).to_have_count(0)
        entries[thread] = [{"id": 1, "message": f"Check {thread} <img src=x>", "due_at": "2026-10-29T12:00:00Z", "status": "pending", "error": None},
                           {"id": 2, "message": "Earlier check", "due_at": "2026-10-28T12:00:00Z", "status": "failed", "error": "runtime capacity reached"}]
        dialog.get_by_role("button", name="Close reminders", exact=True).click()
        button.click()
        expect(dialog.locator("[data-pending] .reminder-row")).to_have_count(1)
        expect(dialog.locator(".reminder-row img")).to_have_count(0)
        expect(button).to_have_attribute("aria-label", "Reminders, 1 pending")
        expect(dialog.locator(".reminder-row button")).to_have_count(0)
        expect(dialog.locator("[data-pending] p")).to_have_text(f"Check {thread} <img src=x>")
        expect(dialog.locator("[data-history]")).to_contain_text("failed")
        expect(dialog.locator("[data-history]")).to_contain_text("runtime capacity reached")
        entries[thread][0]["status"] = "delivered"
        dialog.get_by_role("button", name="Close reminders", exact=True).click()
        button.click()
        expect(dialog.locator("[data-pending]")).to_have_text("No pending reminders.")
        expect(dialog.locator("[data-history]")).to_contain_text("delivered")
        dialog.get_by_role("button", name="Close reminders", exact=True).click()
        expect(dialog).not_to_be_visible()
    # A previously loaded pending entry can finish while older history loads.
    entries["thread-1"] = [
        {"id": 3, "message": "Recently finished", "due_at": "2026-10-28T12:00:00Z", "status": "pending", "error": None},
        {"id": 2, "message": "Previous result", "due_at": "2026-10-28T11:00:00Z", "status": "delivered", "error": None},
        {"id": 1, "message": "Older result", "due_at": "2026-10-28T10:00:00Z", "status": "delivered", "error": None},
    ]
    page.goto(url + "#chat/thread-1")
    surface = page.locator("#panel-workspace-chat")
    surface.locator("#agent-reminders").click()
    dialog = surface.get_by_role("dialog", name="Reminders", exact=True)
    expect(dialog.locator("[data-pending]")).to_contain_text("Recently finished")
    entries["thread-1"][0].update(status="failed", error="host admin request failed")
    dialog.get_by_role("button", name="Load more", exact=True).click()
    expect(dialog.locator("[data-pending]")).to_have_text("No pending reminders.")
    expect(dialog.locator("[data-history] .reminder-row")).to_have_count(3)
    expect(dialog.locator("[data-history]")).to_contain_text("Recently finished")
    expect(dialog.locator("[data-history]")).to_contain_text("host admin request failed")
    dialog.get_by_role("button", name="Close reminders", exact=True).click()
    assert requests and set(requests) == {"GET"}, requests
    # Archived agents retain the same display-only panel.
    page.evaluate("() => window.KernHost.api('POST', '/v1/workspace/chat/threads/thread-6/archive', {})")
    page.evaluate("() => window.KernHost.refreshNavigation()")
    page.goto(url + "#chat/thread-6")
    surface = page.locator("#panel-workspace-chat")
    expect(surface.locator("#archive-thread")).to_have_text("Unarchive")
    surface.locator("#agent-reminders").click()
    dialog = surface.get_by_role("dialog", name="Reminders", exact=True)
    expect(dialog.get_by_role("button", name="New reminder", exact=True)).to_be_hidden()
    expect(dialog.locator("[data-history]")).to_contain_text("delivered")
    dialog.get_by_role("button", name="Close reminders", exact=True).click()
    page.evaluate("() => window.KernHost.api('POST', '/v1/workspace/chat/threads/thread-6/unarchive', {})")

    page.goto(url + f"#scheduled-agents/{schedule['id']}")
    settings = page.locator("#panel-workspace-global")
    expect(settings.locator("#schedule-name")).to_have_value("Reminder agent")
    expect(settings.locator("#agent-reminders")).to_be_visible()
    settings.locator("#global-new").click()
    expect(settings.locator("#schedule-name")).to_have_value("")
    expect(settings.locator("#agent-reminders")).to_be_hidden()
