"""Exercise visible-page scope, concurrency, failures, and responsive review."""
import time
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import expect


def approval_smoke(browser, url):
    context = browser.new_context(viewport={"width": 1440, "height": 1000})
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    now = 1788960000
    rows = [{"id": str(index), "kind": "github_push" if index < 2 else "tool",
             "source": "GitHub" if index < 2 else "Gmail", "tool_id": "gmail",
             "status": "pending", "summary": f"Review request {index}",
             "action_id": "send_email", "account_label": "work@example.test",
             "connection_id": f"gmail-{index}",
             "created_at": now - index, "updated_at": now - index,
             "ref_updates": [{"ref": "refs/heads/change"}], "changed_paths": [".github/workflows/test.yml"]}
            for index in range(13)]
    held = []
    seen = []
    submitted_keys = []
    fail_listing = []

    def listing(route):
        if fail_listing:
            fail_listing.pop()
            route.fulfill(status=500, json={"error": "temporary failure"})
            return
        query = parse_qs(urlparse(route.request.url).query)
        pending = query.get("view", ["pending"])[0] == "pending"
        filtered = [row for row in rows if (row["status"] == "pending") == pending]
        pages = max(1, (len(filtered) + 9) // 10)
        number = min(int(query.get("page", ["1"])[0]), pages)
        route.fulfill(json={"items": filtered[(number-1)*10:number*10], "page": number,
                            "pages": pages, "page_size": 10, "total": len(filtered),
                            "pending_count": sum(row["status"] == "pending" for row in rows),
                            "history_count": sum(row["status"] != "pending" for row in rows)})

    def tool_request(route):
        parts = urlparse(route.request.url).path.split("/")
        if route.request.method == "GET":
            route.fulfill(json={"approval": {"payload": {"to": "recipient@example.test", "body": "Exact reviewed message"}}})
        else:
            held.append(route)
            submitted_keys.append(f"tool:{parts[-2]}")
            seen.append((parts[-2], parts[-1]))

    def github_request(route):
        parts = urlparse(route.request.url).path.split("/")
        held.append(route)
        submitted_keys.append(f"github_push:{parts[-2]}")
        seen.append((parts[-2], parts[-1]))

    page.route("**/v1/approvals?*", listing)
    page.route("**/v1/tools/gmail/approvals/**", tool_request)
    page.route("**/v1/network-tools/github-pending-pushes/**", github_request)
    page.goto(url + "#approvals")
    page.locator("#password").fill("dev")
    page.locator('[data-action="login"]').click()
    expect(page.locator(".approval-card")).to_have_count(10)
    expect(page.locator("#approval-nav-count")).to_have_text("13")
    page.locator('[data-action="approval-page"][data-page="2"]').click()
    expect(page.locator(".approval-card")).to_have_count(3)
    page.locator('[data-action="approval-page"][data-page="1"]').click()
    expect(page.locator(".approval-card")).to_have_count(10)
    page.locator('[data-approval-key="tool:2"] summary').click()
    expect(page.locator('[data-approval-key="tool:2"]')).to_contain_text("gmail-2")
    expect(page.locator('[data-approval-key="tool:2"] pre')).to_contain_text("Exact reviewed message")
    # Arriving work changes the count but cannot replace the reviewed page.
    rows.insert(0, {**rows[2], "id": "late", "summary": "Arrived after review"})
    page.evaluate("import('/admin_ui/approvals.js').then(m => m.pollApprovals())")
    expect(page.locator("#approval-updates")).to_be_visible()
    expect(page.locator('[data-approval-key="tool:late"]')).to_have_count(0)
    visible_keys = page.locator(".approval-card").evaluate_all("cards => cards.map(card => card.dataset.approvalKey)")
    page.once("dialog", lambda dialog: dialog.accept())
    page.locator('[data-action="approval-bulk"][data-decision="approve"]').click()
    # Eight tools and one GitHub request start before any response is released.
    expect(page.locator(".approval-progress", has_text="Approving...")).to_have_count(9)
    page.wait_for_timeout(150)
    assert len(held) == 9, seen
    assert ("1", "approve") not in seen
    assert {item_id for item_id, _ in seen} == {"0", *map(str, range(2, 10))}
    expect(page.locator('[data-action="approval-page"]').last).to_be_disabled()
    expect(page.locator('[data-action="approval-bulk"]').first).to_be_disabled()

    def finish(route, fail=False):
        item_id, decision = urlparse(route.request.url).path.split("/")[-2:]
        row = next(row for row in rows if row["id"] == item_id)
        row["status"] = "failed" if fail else ("executed" if decision == "approve" else "denied")
        route.fulfill(json={"result": {"status": "failed", "error": "Provider declined request"}} if fail else {"result": {"status": "executed"}})

    for route in list(held):
        finish(route, "/2/approve" in route.request.url)
    expect(page.locator("#approval-feedback")).to_contain_text("9 of 10")
    # Progress can render before Playwright intercepts the next GitHub call.
    deadline = time.monotonic() + 5
    while len(held) < 10 and time.monotonic() < deadline:
        page.wait_for_timeout(10)
    assert len(held) == 10, seen
    finish(held[-1])
    expect(page.locator("#approval-feedback")).to_contain_text("9 approved, 1 failed")
    expect(page.locator("#approval-feedback")).to_contain_text("Provider declined request")
    expect(page.locator(".approval-card")).to_have_count(4)
    assert {item_id for item_id, _ in seen} == set(map(str, range(10)))
    assert sorted(submitted_keys) == sorted(visible_keys)
    expect(page.locator("#approval-nav-count")).to_have_text("4")
    page.locator('[data-action="approval-view"][data-view="history"]').click()
    expect(page.locator(".approval-card")).to_have_count(10)
    expect(page.locator('[data-action="approval-bulk"]')).to_have_count(0)
    expect(page.locator('[data-action="approval-decide"]')).to_have_count(0)
    page.locator('[data-action="approval-view"][data-view="pending"]').click()
    expect(page.locator(".approval-card")).to_have_count(4)
    # Each individual decision submits on its first click and disables repeats.
    for item_id, decision in (("late", "approve"), ("10", "deny")):
        card = page.locator(f'[data-approval-key="tool:{item_id}"]')
        before = len(held)
        card.get_by_role("button", name=decision.capitalize(), exact=True).click()
        expect(card.locator(".approval-progress")).to_have_text("Approving..." if decision == "approve" else "Denying...")
        expect(card.get_by_role("button", name=decision.capitalize(), exact=True)).to_be_disabled()
        page.wait_for_timeout(100)
        assert len(held) == before + 1, seen
        assert seen[-1] == (item_id, decision)
        finish(held[-1])
        expect(card).to_have_count(0)
    before = len(held)
    page.once("dialog", lambda dialog: dialog.accept())
    page.locator('[data-action="approval-bulk"][data-decision="deny"]').click()
    expect(page.locator(".approval-progress", has_text="Denying...")).to_have_count(2)
    page.wait_for_timeout(100)
    for route in held[before:]:
        finish(route)
    expect(page.locator("#approval-list")).to_contain_text("All caught up")
    expect(page.locator("#approval-nav-count")).to_be_hidden()
    fail_listing.append(True)
    page.locator('[data-action="approval-refresh"]').click()
    expect(page.locator("#approval-feedback")).to_contain_text("Could not load approvals")
    expect(page.locator("#approval-feedback")).to_contain_text("2 denied")
    page.locator('[data-action="approval-refresh"]').click()
    expect(page.locator("#approval-feedback")).not_to_contain_text("Could not load approvals")
    expect(page.locator("#approval-feedback")).to_contain_text("2 denied")
    page.locator('[data-action="approval-view"][data-view="history"]').click()
    expect(page.locator(".approval-card")).to_have_count(10)
    page.locator('[data-action="approval-page"][data-page="2"]').click()
    expect(page.locator(".approval-card")).to_have_count(4)
    for width in (320, 390, 1440):
        page.set_viewport_size({"width": width, "height": 900})
        page.wait_for_timeout(300)
        assert not page.evaluate("document.documentElement.scrollWidth > innerWidth"), width
    assert not errors, errors
    context.close()
