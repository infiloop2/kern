"""Policy editing, historical reasons and mobile layout."""
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import expect


def auto_approval_smoke(browser, url, screenshot_dir=None):
    context = browser.new_context(viewport={"width": 1440, "height": 1100})
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    policies = [{"tool_id": "gmail", "action_id": "send_email", "instructions": "Approve routine follow-ups from the support account. Leave discounts, attachments and new recipients for my review."}]
    checks = [{"id": 1, "tool_id": "gmail", "action_id": "send_email", "summary": "Follow up on a delivery date", "checked_at": 1788960000,
               "policy": policies[0]["instructions"], "reason": "The message follows up on an existing delivery without changing terms.", "model": "gpt-6.1-sol",
               "outcome": "approved", "status": "executed", "result": "Message sent.", "approval_id": "approval_1.token"},
              {"id": 2, "tool_id": "gmail", "action_id": "send_email", "summary": "Offer a renewal discount", "checked_at": 1788960000,
               "policy": policies[0]["instructions"], "reason": "This message offers a 15% discount, which your policy excludes.", "model": "gpt-6.1-sol",
               "outcome": "left_pending", "status": "pending", "result": "", "approval_id": "approval_2.token"}]
    pending = {"id": "approval_2.token", "kind": "tool", "tool_id": "gmail", "source": "Gmail", "action_id": "send_email", "summary": "Offer a renewal discount",
               "status": "pending", "created_at": 1788960000, "updated_at": 1788960000, "has_auto_policy": True,
               "auto_review": {"outcome": "left_pending", "reason": checks[1]["reason"], "checked_at": 1788960000}}
    availability = [True]
    executed = []
    settings = {"sleep_start_minute": 0, "sleep_end_minute": 480}
    settings_saves = []

    def auto_route(route):
        path = urlparse(route.request.url).path
        body = route.request.post_data_json if route.request.method != "GET" else None
        if path.endswith("/settings"):
            settings.update(body)
            settings_saves.append(body)
            route.fulfill(json={"saved": True})
        elif path.endswith("/policy"):
            if route.request.method == "DELETE":
                policies.clear()
            else:
                policies[:] = [body]
            route.fulfill(json={"saved": True})
        else:
            selected = parse_qs(urlparse(route.request.url).query).get("outcome", [""])[0]
            rows = [row for row in checks if not selected or row["outcome"] == selected]
            route.fulfill(json={"settings": settings, "policies": policies, "available": availability[0], "quiet": False, "next_review_at": 1788961800,
                                "catalog": [{"tool_id": "gmail", "name": "Gmail", "actions": [{"id": "send_email", "description": "Send an email from a connected account."}]}],
                                "history": {"items": rows, "total": len(rows), "page": 1, "pages": 1}})

    def tool_route(route):
        if route.request.method != "GET":
            executed.append(route.request.url)
        route.fulfill(json={"approval": {**pending, "payload": {"body": "Here is a 15% discount."}}})

    page.route("**/v1/auto-approvals**", auto_route)
    page.route("**/v1/tools/gmail/approvals/**", tool_route)
    page.route("**/v1/approvals?*", lambda route: route.fulfill(json={"items": [pending], "page": 1, "pages": 1, "page_size": 10, "total": 1, "pending_count": 1, "history_count": 0}))
    page.goto(url + "#approvals")
    page.locator("#password").fill("dev")
    page.locator('[data-action="login"]').click()
    expect(page.locator(".auto-review-note")).to_contain_text("15% discount")
    page.locator('[data-action="auto-policy"]').click()
    expect(page.locator("#auto-policy-dialog")).to_be_visible()
    expect(page.locator("#auto-policy-tool")).to_be_disabled()
    if screenshot_dir:
        page.screenshot(path=str(screenshot_dir / "desktop-policy-editor.png"))
    page.locator("#auto-policy-instructions").fill("Only routine customer follow-ups. No discounts.")
    page.get_by_role("button", name="Save policy", exact=True).click()
    expect(page.locator("#auto-policy-dialog")).not_to_be_visible()
    assert policies[0]["instructions"] == "Only routine customer follow-ups. No discounts."
    assert not executed
    page.locator('[data-action="approval-view"][data-view="auto"]').click()
    expect(page.locator(".auto-policy-row")).to_have_count(1)
    expect(page.locator(".auto-history-row")).to_have_count(2)
    expect(page.locator("#auto-sleep-start")).to_have_value("00:00")
    expect(page.locator(".auto-schedule-details")).to_contain_text("GPT-6.1 Sol")
    expect(page.locator("#auto-sleep-end")).to_have_value("08:00")
    page.locator("#auto-sleep-start").fill("22:00")
    page.locator("#auto-sleep-end").fill("03:59")
    page.get_by_role("button", name="Save sleep times", exact=True).click()
    expect(page.locator("#auto-sleep-error")).to_contain_text("at least 6 hours")
    assert not settings_saves
    page.locator("#auto-sleep-end").fill("04:00")
    page.get_by_role("button", name="Save sleep times", exact=True).click()
    expect(page.locator(".auto-schedule-details")).to_contain_text("Quiet hours 22:00 to 04:00 UTC")
    assert settings_saves == [{"sleep_start_minute": 1320, "sleep_end_minute": 240}]
    page.locator('[data-action="approval-refresh"]').click()
    expect(page.locator("#auto-sleep-start")).to_have_value("22:00")
    expect(page.locator("#auto-sleep-end")).to_have_value("04:00")

    page.locator(".auto-history-row").first.locator("summary").click()
    expect(page.locator(".auto-history-detail").first).to_contain_text("Message sent.")
    expect(page.locator(".auto-history-detail").first).to_contain_text("Approve routine follow-ups from the support account")
    checks[0].update(status="pending", result="", approval_error="tools service unavailable")
    page.locator('[data-action="approval-refresh"]').click()
    page.locator(".auto-history-row").first.locator("summary").click()
    expect(page.locator(".auto-history-row").first).to_contain_text("AI decision: approve")
    expect(page.locator(".auto-history-detail").first).to_contain_text("Pending")
    expect(page.locator(".auto-history-detail").first).to_contain_text("tools service unavailable")
    expect(page.locator(".auto-history-detail").first).not_to_contain_text("Succeeded")
    if screenshot_dir:
        page.screenshot(path=str(screenshot_dir / "auto-decision-pending.png"), full_page=True)
    checks[0].update(status="executed", result="Message sent.", approval_error="")
    page.locator("#auto-history-filter").select_option("left_pending")
    expect(page.locator(".auto-history-row")).to_have_count(1)
    page.locator("#auto-history-filter").select_option("")
    expect(page.locator(".auto-history-row")).to_have_count(2)
    if screenshot_dir:
        page.screenshot(path=str(screenshot_dir / "auto-approval-desktop.png"), full_page=True)
    availability[0] = False
    page.locator('[data-action="approval-refresh"]').click()
    expect(page.locator(".auto-schedule")).to_contain_text("Auto-approval is off")
    expect(page.locator(".auto-schedule")).to_contain_text("Enable OpenAI under Home > Host AI inference")
    expect(page.locator(".auto-policy-row")).to_have_count(1)
    expect(page.locator(".auto-history-row")).to_have_count(2)
    if screenshot_dir:
        page.screenshot(path=str(screenshot_dir / "auto-approval-off.png"), full_page=True)
    availability[0] = True
    page.locator('[data-action="approval-refresh"]').click()
    expect(page.locator(".auto-schedule")).to_contain_text("Scheduled reviews")
    # Existing policy selected through Add opens its text instead of creating duplicates.
    page.get_by_role("button", name="Add policy", exact=True).click()
    page.locator("#auto-policy-tool").select_option("gmail")
    page.locator("#auto-policy-action").select_option("send_email")
    expect(page.locator("#auto-policy-instructions")).to_have_value(policies[0]["instructions"])
    expect(page.locator("#auto-policy-delete")).to_be_visible()
    page.locator('[data-auto-action="close"]').first.click()
    for width in (390, 320):
        page.set_viewport_size({"width": width, "height": 900})
        expect(page.locator(".auto-policy-row")).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), f"page overflow at {width}"
        if screenshot_dir:
            page.screenshot(path=str(screenshot_dir / f"auto-approval-{width}.png"), full_page=True)
        page.locator(".auto-policy-row").click()
        expect(page.locator("#auto-policy-dialog")).to_be_visible()
        assert page.locator("#auto-policy-dialog").evaluate("el => el.scrollWidth <= el.clientWidth"), "editor overflow"
        if screenshot_dir:
            page.screenshot(path=str(screenshot_dir / f"auto-approval-editor-{width}.png"))
        page.locator('[data-auto-action="close"]').first.click()
    page.locator(".auto-policy-row").click()
    page.once("dialog", lambda dialog: dialog.accept())
    page.locator("#auto-policy-delete").click()
    expect(page.locator(".auto-policy-row")).to_have_count(0)
    expect(page.get_by_role("button", name="Create your first policy")).to_be_visible()
    assert not executed and not errors, errors
    context.close()
