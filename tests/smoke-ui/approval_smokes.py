"""Exercise visible-page scope, concurrency, failures, and responsive review."""
import time
import base64
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import expect


def whatsapp_media_smoke(browser, url, screenshot_dir=None):
    context = browser.new_context(viewport={"width": 390, "height": 900})
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.add_init_script("window.mediaCspViolations = []; document.addEventListener('securitypolicyviolation', e => window.mediaCspViolations.push(e.violatedDirective));")
    rows = [{"id": "wa-image", "kind": "tool", "source": "WhatsApp", "tool_id": "whatsapp",
             "status": "pending", "summary": "Send frame.png to +447700900123 from Infiverse",
             "action_id": "send_message", "account_label": "Infiverse", "created_at": 1788960000,
             "updated_at": 1788960000}]
    page.route("**/v1/approvals?*", lambda route: route.fulfill(json={
        "items": rows, "page": 1, "pages": 1, "page_size": 10, "total": len(rows),
        "pending_count": len(rows), "history_count": 0}))
    mime = ["image/png"]
    held_details = []
    hold_details = [False]
    def detail(route):
        if hold_details[0]:
            held_details.append(route)
            return
        finish_detail(route)
    def finish_detail(route):
        route.fulfill(json={"approval": {"payload": {
            "recipient": "+447700900123", "account_label": "Infiverse", "text": "Exact 📷 caption <script>",
            "media_asset": {"filename": "frame.png", "media_type": mime[0], "size_bytes": 512,
                            "sha256": "a" * 64, "asset_id": "opaque"}}}})
    page.route("**/v1/tools/whatsapp/approvals/wa-image", detail)
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aLc0AAAAASUVORK5CYII=")
    missing_video = [True]
    media_requests = []
    def media_response(route):
        media_requests.append(route.request.url)
        assert route.request.headers.get("x-kern-csrf") == "1", "preview must use authenticated apiBlob"
        if mime[0] == "image/png":
            route.fulfill(content_type="image/png", body=png)
        elif missing_video[0]:
            route.fulfill(status=404)
        else:
            # An undecodable MP4 tests local blob loading and decode feedback;
            # this fixture does not claim to verify playback or a real send.
            route.fulfill(content_type="video/mp4", body=b"\x00\x00\x00\x18ftypmp42mock-video")
    page.route("**/v1/tools/whatsapp/approvals/wa-image/media", media_response)
    page.goto(url + "#approvals")
    page.locator("#password").fill("dev")
    page.locator('[data-action="login"]').click()
    page.locator("[data-approval-details] summary").click()
    expect(page.locator(".approval-payload")).to_contain_text("Exact 📷 caption <script>")
    expect(page.locator("img.approval-media-preview")).to_be_visible()
    page.wait_for_function("() => document.querySelector('img.approval-media-preview')?.naturalWidth > 0")
    assert page.locator("img.approval-media-preview").get_attribute("src").startswith("blob:")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    if screenshot_dir:
        page.screenshot(path=str(screenshot_dir / "whatsapp-media-mobile.png"), full_page=True)
    # Closing/reopening while detail requests are held must discard the old load,
    # even if both responses arrive after the details element is open again.
    page.locator("[data-approval-details] summary").click()
    expect(page.locator(".approval-media-review")).to_have_count(0)
    hold_details[0] = True
    page.locator("[data-approval-details] summary").click()
    expect(page.locator(".approval-payload")).to_have_text("Loading request...")
    page.locator("[data-approval-details] summary").click()
    page.locator("[data-approval-details] summary").click()
    def wait_for_routes(routes, count):
        deadline = time.monotonic() + 5
        while len(routes) < count and time.monotonic() < deadline:
            page.wait_for_timeout(10)
        assert len(routes) == count
    wait_for_routes(held_details, 2)
    before = len(media_requests)
    finish_detail(held_details[0])
    finish_detail(held_details[1])
    page.wait_for_function("() => document.querySelector('img.approval-media-preview')?.naturalWidth > 0")
    expect(page.locator(".approval-media-review")).to_have_count(1)
    assert len(media_requests) == before + 1, "stale detail loads must not fetch another preview"
    hold_details[0] = False
    mime[0] = "video/mp4"
    page.locator('[data-action="approval-refresh"]').click()
    page.locator("[data-approval-details] summary").click()
    expect(page.locator("video.approval-media-preview")).to_have_attribute("controls", "")
    expect(page.locator("[data-approval-details]")).to_contain_text("Attachment preview unavailable")
    missing_video[0] = False
    page.locator("[data-approval-details] summary").click()
    expect(page.locator(".approval-media-review")).to_have_count(0)
    page.locator("[data-approval-details] summary").click()
    page.wait_for_function("() => document.querySelector('video.approval-media-preview')?.src.startsWith('blob:')")
    # Slow WhatsApp approvals are submitted one at a time; unrelated tools still
    # start immediately. Releasing each response must start only its successor.
    rows.extend([{**rows[0], "id": "wa-second"}, {**rows[0], "id": "wa-third"},
                 {**rows[0], "id": "mail", "tool_id": "gmail", "source": "Gmail", "action_id": "send_email"}])
    held_decisions = []
    page.route("**/v1/tools/whatsapp/approvals/*/approve", lambda route: held_decisions.append(route))
    page.route("**/v1/tools/gmail/approvals/*/approve", lambda route: held_decisions.append(route))
    page.locator('[data-action="approval-refresh"]').click()
    expect(page.locator(".approval-card")).to_have_count(4)
    page.once("dialog", lambda dialog: dialog.accept())
    page.locator('[data-action="approval-bulk"][data-decision="approve"]').click()
    expect(page.locator(".approval-progress", has_text="Approving...")).to_have_count(2)
    expect(page.locator(".approval-progress", has_text="Queued")).to_have_count(2)
    wait_for_routes(held_decisions, 2)
    first = {urlparse(route.request.url).path.split("/")[-2]: route for route in held_decisions}
    assert set(first) == {"wa-image", "mail"}
    first["mail"].fulfill(json={"result": {"status": "executed"}})
    first["wa-image"].fulfill(json={"result": {"status": "executed"}})
    wait_for_routes(held_decisions, 3)
    assert held_decisions[-1].request.url.endswith("/wa-second/approve")
    expect(page.locator('[data-approval-key="tool:wa-third"] .approval-progress')).to_have_text("Queued")
    held_decisions[-1].fulfill(json={"result": {"status": "executed"}})
    wait_for_routes(held_decisions, 4)
    assert held_decisions[-1].request.url.endswith("/wa-third/approve")
    held_decisions[-1].fulfill(json={"result": {"status": "executed"}})
    expect(page.locator("#approval-feedback")).to_contain_text("4 approved")
    assert page.evaluate("window.mediaCspViolations") == [], "preview must obey the admin CSP"
    assert not errors, errors
    context.close()


def assert_review_layout(page, width):
    """Catch a styled queue falling back to the global headings and flow."""
    page.set_viewport_size({"width": width, "height": 1000})
    expect(page.locator(".approval-card").first).to_be_visible()
    layout = page.evaluate("""() => {
        const rect = selector => document.querySelector(selector).getBoundingClientRect().toJSON();
        const card = document.querySelector('.approval-card');
        const title = card.querySelector('h2');
        const tab = document.querySelector('#approval-tabs button');
        return {
            card: card.getBoundingClientRect().toJSON(),
            title: title.getBoundingClientRect().toJSON(),
            titleCase: getComputedStyle(title).textTransform,
            refresh: rect('[data-action="approval-refresh"]'),
            heading: rect('.approvals-heading h1'),
            tabs: rect('#approval-tabs'),
            toolbar: rect('#approval-toolbar'),
            countGap: parseFloat(getComputedStyle(tab.querySelector('span')).marginLeft),
            decisions: rect('.approval-decisions'),
            buttons: [...document.querySelectorAll('.approvals-page button')].map(el => el.getBoundingClientRect().height),
        };
    }""")
    assert layout["titleCase"] == "none", "request summaries must keep their natural case"
    assert layout["title"]["x"] > layout["card"]["x"] + 10, "requests need card padding"
    assert layout["refresh"]["x"] >= layout["heading"]["right"], "Refresh belongs beside the heading"
    assert layout["countGap"] >= 6, "tab labels and counts must stay separated"
    if width > 600:
        assert layout["toolbar"]["x"] >= layout["tabs"]["right"], "bulk actions belong beside the tabs"
    else:
        assert layout["toolbar"]["y"] >= layout["tabs"]["bottom"], "mobile bulk actions need their own row"
        assert min(layout["buttons"]) >= 44, "mobile controls need full touch targets"
    assert layout["decisions"]["right"] < layout["card"]["right"], "decisions must stay inside the card"
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), width


def approval_smoke(browser, url, screenshot_dir=None):
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
    rows[2].update({
        "summary": (
            "Reply to https://x.com/i/status/2105531308642590814 as @infiloop2: "
            "Tracking brand visibility across AI answers is useful feedback. "
            "Let's connect. I'm building https://kernai.cloud"
        ),
        "risk_scores": {
            "commits_money_or_obligation": 0.92,
            "sensitive_data": 0.51,
            "summary_mismatch": 0.2,
        },
    })
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
            row = next(row for row in rows if row["id"] == parts[-1])
            route.fulfill(json={"approval": {**row, "payload": {"to": "recipient@example.test", "body": "Exact reviewed message"}}})
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
    assessed = page.locator('[data-approval-key="tool:2"]')
    scores = assessed.locator(".approval-risk-scores")
    expect(scores).to_have_attribute("aria-label", "TypeSafe Jev risk scores")
    expect(scores.locator(".approval-risk-score")).to_have_count(3)
    expect(scores.locator(".approval-risk-score").nth(0)).to_have_attribute(
        "aria-label", "Commits money or obligation risk 92%"
    )
    expect(scores.locator(".approval-risk-score").nth(1)).to_have_attribute(
        "aria-label", "Sensitive data risk 51%"
    )
    expect(scores.locator(".approval-risk-score").nth(2)).to_have_attribute(
        "aria-label", "Summary mismatch risk 20%"
    )
    expect(scores).to_contain_text("92%")
    expect(scores).to_contain_text("51%")
    expect(scores).to_contain_text("20%")
    for width in (1440, 768, 390, 320):
        assert_review_layout(page, width)
        tracks = scores.locator("progress").evaluate_all("elements => elements.map(el => el.getBoundingClientRect().x)")
        assert max(tracks) - min(tracks) < 1, "risk tracks must align across rows"
        if screenshot_dir:
            page.screenshot(path=str(screenshot_dir / f"pending-{width}.png"), full_page=True, animations="disabled")
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.locator('[data-action="approval-page"][data-page="2"]').click()
    expect(page.locator(".approval-card")).to_have_count(3)
    page.locator('[data-action="approval-page"][data-page="1"]').click()
    expect(page.locator(".approval-card")).to_have_count(10)
    page.locator('[data-approval-key="tool:2"] summary').click()
    expect(page.locator('[data-approval-key="tool:2"]')).to_contain_text("gmail-2")
    expect(page.locator('[data-approval-key="tool:2"] pre')).to_contain_text("Exact reviewed message")
    if screenshot_dir:
        page.screenshot(path=str(screenshot_dir / "pending-exact-request.png"), full_page=True, animations="disabled")
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
        expect(page.locator(".approval-card h2").first).to_have_css("text-transform", "none")
        if screenshot_dir:
            page.screenshot(path=str(screenshot_dir / f"history-{width}.png"), full_page=True, animations="disabled")

    # The write can finish while its response is lost. Reconcile from the
    # stored approval without submitting the publishing action again.
    page.locator('[data-action="approval-view"][data-view="pending"]').click()
    for item_id, decision, recorded_status, expected in (
        ("lost-executed", "approve", "executed", "1 approved"),
        ("lost-failed", "approve", "failed", "0 approved, 1 failed"),
        ("lost-pending", "approve", "pending", "0 approved, 1 unconfirmed"),
        ("lost-denied", "approve", "denied", "0 approved, 1 resolved differently"),
        ("lost-approved", "deny", "executed", "0 denied, 1 resolved differently"),
    ):
        row = {**next(row for row in rows if row["id"] == "2"),
               "id": item_id, "status": "pending", "summary": item_id}
        if recorded_status == "failed":
            row["result"] = "Instagram declined request"
        rows.insert(0, row)
        page.locator('[data-action="approval-refresh"]').click()
        before = len(held)
        page.locator(f'[data-approval-key="tool:{item_id}"] [data-decision="{decision}"]').click()
        deadline = time.monotonic() + 5
        while len(held) == before and time.monotonic() < deadline:
            page.wait_for_timeout(10)
        assert len(held) == before + 1
        row["status"] = recorded_status
        held[-1].abort("failed")
        expect(page.locator("#approval-feedback")).to_contain_text(expected)
        if recorded_status == "failed":
            expect(page.locator("#approval-feedback")).to_contain_text("Instagram declined request")
        if recorded_status == "pending":
            expect(page.locator("#approval-feedback")).to_contain_text("Check History before taking further action")
        if item_id in {"lost-denied", "lost-approved"}:
            expect(page.locator("#approval-feedback")).to_contain_text(
                "Already denied in another session" if decision == "approve" else "Already approved in another session"
            )
        assert len(held) == before + 1, "decision must not be replayed"
        if recorded_status == "pending":
            rows.remove(row)
            page.locator('[data-action="approval-refresh"]').click()
    page.route("**/v1/network-tools/github-pending-pushes", lambda route: route.fulfill(
        json={"pending_pushes": [row for row in rows if row["kind"] == "github_push"]}
    ))
    for item_id, recorded_status, expected in (
        ("lost-github", "approved", "1 approved"),
        ("lost-github-rejected", "rejected", "0 approved, 1 resolved differently"),
    ):
        github = {**next(row for row in rows if row["id"] == "0"),
                  "id": item_id, "status": "pending", "summary": item_id}
        rows.insert(0, github)
        page.locator('[data-action="approval-refresh"]').click()
        before = len(held)
        page.locator(f'[data-approval-key="github_push:{item_id}"] [data-decision="approve"]').click()
        deadline = time.monotonic() + 5
        while len(held) == before and time.monotonic() < deadline:
            page.wait_for_timeout(10)
        assert len(held) == before + 1
        github["status"] = recorded_status
        held[-1].abort("failed")
        expect(page.locator("#approval-feedback")).to_contain_text(expected)
        if recorded_status == "rejected":
            expect(page.locator("#approval-feedback")).to_contain_text("Already denied in another session")
        assert len(held) == before + 1
    rows.clear()
    page.locator('[data-action="approval-refresh"]').click()
    expect(page.locator(".approval-empty h2")).to_have_text("All caught up")
    for width in (1440, 390, 320):
        page.set_viewport_size({"width": width, "height": 900})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), width
        expect(page.locator(".approval-empty h2")).to_have_css("text-transform", "none")
        if screenshot_dir:
            page.screenshot(path=str(screenshot_dir / f"empty-{width}.png"), full_page=True, animations="disabled")
    assert not errors, errors
    context.close()


def x_exact_request_smoke(browser, url):
    """Exact X sender, target, text and ordered digests render safely on mobile."""
    context = browser.new_context(viewport={"width": 390, "height": 900})
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    rows = [{"id": "x-request", "kind": "tool", "source": "X", "tool_id": "twitter",
             "status": "pending", "summary": "Publish exact request", "action_id": "post_tweet",
             "account_label": "@claw", "created_at": 1788960000, "updated_at": 1788960000}]
    page.route("**/v1/approvals?*", lambda route: route.fulfill(json={
        "items": rows, "page": 1, "pages": 1, "page_size": 10, "total": 1,
        "pending_count": 1, "history_count": 0}))
    payload = {"x_account": {"id": "111", "label": "@claw"}, "proposal": {
        "text": " Exact 📷 <script> text\n", "in_reply_to_tweet_id": "55",
        "image_assets": [{"filename": f"frame-{i}.png", "media_type": "image/png",
                          "size_bytes": 512, "sha256": str(i) * 64} for i in (1, 2)]}}
    page.route("**/v1/tools/twitter/approvals/x-request", lambda route: route.fulfill(json={"approval": {"payload": payload}}))
    page.goto(url + "#approvals")
    page.locator("#password").fill("dev")
    page.locator('[data-action="login"]').click()
    page.locator("[data-approval-details] summary").click()
    review = page.locator(".approval-x-review")
    expect(review).to_contain_text("@claw (111) to reply to post 55")
    expect(review.locator("pre")).to_have_text(payload["proposal"]["text"])
    expect(review).to_contain_text("Attachment 1: frame-1.png")
    expect(review).to_contain_text("SHA256 " + "2" * 64)
    assert review.locator("script").count() == 0
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.locator("[data-approval-details] summary").click()
    expect(review).to_have_count(0)
    rows[0]["action_id"] = "send_dm"
    payload["proposal"] = {"text": "Exact private <script> text", "dm_conversation_id": "111-222"}
    page.locator('[data-action="approval-refresh"]').click()
    page.locator("[data-approval-details] summary").click()
    expect(review).to_contain_text("Direct message from @claw (111) to conversation 111-222")
    expect(review.locator("pre")).to_have_text("Exact private <script> text")
    expect(review).not_to_contain_text("Attachment")
    assert not errors, errors
    context.close()
