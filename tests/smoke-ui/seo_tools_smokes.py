"""Render both SEO guides and review the complete IndexNow approval batch."""

import json

from playwright.sync_api import expect


def run(page, url, log_in, open_home_integration):
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    log_in(page, url)
    for tool_id, key, action in (
        ("pagespeed_insights", "PAGESPEED_INSIGHTS_API_KEY", "analyze_page"),
        ("indexnow", None, "submit_urls"),
        ("google_search_console", "GOOGLE_OAUTH_CLIENT_ID", "inspect_urls"),
    ):
        open_home_integration(page, f"tool:{tool_id}")
        guide = page.locator(f"[data-guide-section='tool:{tool_id}']")
        expect(guide).to_contain_text("2,048")
        expect(guide.locator(".guide-technical-details")).to_contain_text("Parameter guard")
        if key:
            expect(page.locator(f"#tool-config-{tool_id}-{key}")).to_have_attribute("type", "password")
        else:
            expect(page.locator("#tool-config-indexnow-INDEXNOW_KEY")).to_have_count(0)
        capability = guide.locator(".guide-capability").filter(has=page.locator("h4 code", has_text=action))
        capability.locator(".guide-action-contract > summary").click()
        if tool_id == "pagespeed_insights":
            expect(capability.locator(".guide-input-protection").first).to_contain_text("longer text")
            expect(guide).to_contain_text("free")
        elif tool_id == "indexnow":
            expect(guide).to_contain_text("get_verification_file")
            expect(guide).to_contain_text("no manual key configuration")
            expect(guide).to_contain_text("not Google")
            expect(guide).to_contain_text("before approval and again before submission")
        else:
            expect(capability).to_contain_text("One to five")
            expect(capability).to_contain_text("Parameter guard applied")
            expect(guide).to_contain_text("180-second budget")
            expect(guide).to_contain_text("Errors and unprocessed URLs are separate")

    urls = [f"https://example.com/changed/{i}?version=2&source=public" for i in range(100)]
    payload = {"tool_id": "indexnow", "action": "submit_urls", "host": "example.com",
               "urls": urls, "key_fingerprint": "private-host-binding"}
    row = {"id": "indexnow-review", "kind": "tool", "source": "IndexNow", "tool_id": "indexnow",
           "action_id": "submit_urls", "status": "pending", "summary": "Notify IndexNow about 100 changed URLs on example.com.",
           "created_at": 1788960000, "updated_at": 1788960000}
    page.route("**/v1/approvals?*", lambda route: route.fulfill(json={
        "items": [row], "page": 1, "pages": 1, "page_size": 25, "total": 1,
        "pending_count": 1, "history_count": 0,
    }))
    page.route("**/v1/tools/indexnow/approvals/indexnow-review", lambda route: route.fulfill(json={"approval": {"payload": payload}}))
    if page.locator("#mobile-nav-toggle").is_visible():
        page.locator("#mobile-nav-toggle").click()
    page.get_by_role("button", name="Approvals", exact=False).click()
    card = page.locator("[data-approval-key='tool:indexnow-review']")
    card.get_by_text("View exact request", exact=True).click()
    pre = card.locator(".approval-payload")
    expect(pre).to_contain_text(urls[-1])
    assert json.loads(pre.inner_text())["urls"] == urls
    assert not errors, errors
    print("PageSpeed/IndexNow/Search Console guides and all 100 exact URLs with paths/queries are reviewable", flush=True)
