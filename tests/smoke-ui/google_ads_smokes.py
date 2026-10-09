"""Google Ads connection guide and exact write approval on desktop and mobile."""
import json

from playwright.sync_api import expect


def run(page, url, log_in, open_home_integration):
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    log_in(page, url)
    open_home_integration(page, "tool:google_ads")
    guide = page.locator("[data-guide-section='tool:google_ads']")
    expect(guide).to_contain_text("Explorer")
    expect(guide).to_contain_text("2,880")
    expect(guide).to_contain_text("total budget")
    for key in ("GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET"):
        expect(page.locator(f"#tool-config-google_ads-{key}")).to_have_attribute("type", "password")
    expect(guide.locator(".guide-capability")).to_have_count(7)
    locations = guide.locator(".guide-capability").filter(has=page.locator("h4 code", has_text="list_locations"))
    expect(locations).to_contain_text("219 active countries")
    locations.locator(".guide-action-contract > summary").click()
    expect(locations).to_contain_text("country_code")
    expect(locations).to_contain_text("snapshot_date")
    create = guide.locator(".guide-capability").filter(has=page.locator("h4 code", has_text="launch_campaign"))
    create.locator(".guide-action-contract > summary").click()
    expect(create).to_contain_text("no EU political advertising")
    expect(create).to_contain_text("geo_target_ids")
    expect(create).to_contain_text("1,000,000 micros")
    report = guide.locator(".guide-capability").filter(has=page.locator("h4 code", has_text="report"))
    report.locator(".guide-action-contract > summary").click()
    expect(report).to_contain_text("search_terms")
    expect(report).to_contain_text("cost_micros")
    expect(report).to_contain_text("time zone")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "guide overflow"

    payload = {"tool_id": "google_ads", "action": "launch_campaign",
        "google_account": {"id": "google-sub-1", "email": "user@example.com"},
        "account": {"customer_id": "1234567890", "name": "Example Ads", "currency_code": "INR", "time_zone": "Asia/Kolkata"},
        "input": {"customer_id": "1234567890", "name": "Kern Search",
            "total_budget_micros": 10000000, "start_time": "2026-10-14T18:30:00Z", "end_time": "2026-10-22T18:29:59Z", "geo_target_ids": [],
            "keywords": [{"text": "AI agent host", "match_type": "EXACT"}],
            "final_url": "https://example.com/pricing?source=ads&version=1",
            "headlines": ["Your AI Team", "Keep Work Moving", "Try Kern Today"],
            "descriptions": ["A permanent home for your AI team.", "Manage agents from one command center."]}, "current_target": {}, "flight": {"time_zone": "Asia/Kolkata", "start_date_time": "2026-10-15 00:00:00", "end_date_time": "2026-10-22 23:59:59"}}
    row = {"id": "google-ads-review", "kind": "tool", "source": "Google Ads", "tool_id": "google_ads", "action_id": "launch_campaign", "status": "pending", "summary": "Launch new Search campaign, total budget 10 INR, Maximize Clicks. One approval permits paid delivery.", "created_at": 1791417600, "updated_at": 1791417600}
    page.route("**/v1/approvals?*", lambda route: route.fulfill(json={"items": [row], "page": 1, "pages": 1, "page_size": 25, "total": 1, "pending_count": 1, "history_count": 0}))
    page.route("**/v1/tools/google_ads/approvals/google-ads-review", lambda route: route.fulfill(json={"approval": {"payload": payload}}))
    if page.locator("#mobile-nav-toggle").is_visible():
        page.locator("#mobile-nav-toggle").click()
    page.get_by_role("button", name="Approvals", exact=False).click()
    card = page.locator("[data-approval-key='tool:google-ads-review']")
    card.get_by_text("View exact request", exact=True).click()
    preview = card.locator(".approval-google-ads-review")
    expect(preview).to_contain_text("Total campaign budget: 10 INR")
    expect(preview).to_contain_text("automatic Maximize Clicks bidding")
    expect(preview).to_contain_text("Worldwide, no location restriction")
    expect(preview).to_contain_text("2026-10-15 00:00:00")
    expect(preview).to_contain_text("without another Kern approval")
    expect(preview).to_contain_text("during or after Google review")
    expect(preview).to_contain_text("Contains EU political advertising: No")
    expect(preview).to_contain_text("EXACT: AI agent host")
    expect(preview).to_contain_text("Try Kern Today")
    pre = card.locator("details > pre.approval-payload")
    expect(pre).to_contain_text(payload["input"]["final_url"])
    expect(pre).not_to_contain_text("contains_eu_political_advertising")
    assert json.loads(pre.inner_text()) == payload
    card.get_by_text("View exact request", exact=True).click()
    expect(preview).to_have_count(0)
    card.get_by_text("View exact request", exact=True).click()
    expect(preview).to_have_count(1)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "approval overflow"
    end = {**payload, "action": "end_campaign", "input": {"customer_id": "1234567890", "campaign_id": "77"}, "current_target": {"campaign": {"campaign_id": "77", "name": "Kern Search", "status": "ENABLED"}}}
    row["action_id"] = "end_campaign"
    row["summary"] = "End Kern Search (77). Stop delivery and preserve reporting."
    page.unroute("**/v1/tools/google_ads/approvals/google-ads-review")
    page.route("**/v1/tools/google_ads/approvals/google-ads-review", lambda route: route.fulfill(json={"approval": {"payload": end}}))
    card.get_by_text("View exact request", exact=True).click()
    card.get_by_text("View exact request", exact=True).click()
    expect(card.locator(".approval-google-ads-review")).to_contain_text("Set PAUSED")
    expect(card.locator(".approval-google-ads-review")).to_contain_text("past delivery remains billable")
    assert json.loads(card.locator("details > pre.approval-payload").inner_text()) == end
    assert not errors, errors
    print("Google Ads setup, action contracts and exact approval are reviewable", flush=True)
