"""Render dedicated Ads setup and review the complete launch request."""

import copy
import json

from playwright.sync_api import expect


def run(page, url, log_in, open_home_integration):
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    log_in(page, url)
    logo = page.locator("[data-integration-logo='tool:x_ads']").first
    expect(logo).to_have_attribute("data-logo-source", "brand")
    open_home_integration(page, "tool:x_ads")
    guide = page.locator("[data-guide-section='tool:x_ads']")
    for key in ("CONSUMER_KEY", "CONSUMER_SECRET", "ACCESS_TOKEN", "ACCESS_TOKEN_SECRET"):
        expect(page.locator(f"#tool-config-x_ads-X_ADS_{key}")).to_have_attribute("type", "password")
    expect(guide).to_contain_text("OAuth 1.0a")
    expect(guide).to_contain_text("Standard Access")
    expect(guide).to_contain_text("ENGAGEMENTS")
    expect(guide).to_contain_text("one millionth of the funding currency")
    expect(guide).to_contain_text("No quoted/reposted/reply creative")
    expect(guide).to_contain_text("What leaves this host")
    expect(guide.locator(".guide-capability")).to_have_count(12)
    for action in ("launch_campaign", "end_campaign", "get_performance"):
        capability = guide.locator(".guide-capability").filter(has=page.locator("h4 code", has_text=action))
        capability.locator(".guide-action-contract > summary").click()
        expect(capability).to_contain_text("account_id")
    for text in ("AUTO", "REACH", "WEBSITE_CLICKS", "VIDEO_VIEWS", "SIMILAR_TO_FOLLOWERS_OF_USER", "audience_expansion", "Worldwide, no location restriction", "without another Kern write or approval", "no resume"):
        expect(guide).to_contain_text(text)
    for action in ("create_paused_campaign", "pause_campaign", "update_campaign"):
        expect(guide.locator("h4 code", has_text=action)).to_have_count(0)
    targets = [{"type": "SIMILAR_TO_FOLLOWERS_OF_USER", "value": "14230524"}]
    targets += [{"type": "PHRASE_KEYWORD", "value": f"agent workflow {i}"} for i in range(19)]
    post = {"id": "1166476031668015104", "text": "Build with Kern: https://kern.example/?source=ads&campaign=demo", "author_id": "756201191646691328", "author_username": "infiloop", "created_at": "", "url": "https://x.com/i/status/1166476031668015104", "content_sha256": "c" * 64}
    payload = {"plan": {"account_id": "advertiser1", "funding_instrument_id": "fund1", "promotable_user_id": "promoter1", "post_id": post["id"],
                "name": "Kern launch", "daily_budget_amount_local_micro": "10000000", "total_budget_amount_local_micro": "50000000",
                "start_time": "2030-01-01T00:00:00Z", "end_time": "2030-01-02T00:00:00Z", "objective": "WEBSITE_CLICKS", "audience_expansion": "EXPANDED", "targeting": targets},
               "credential_binding": "a" * 64, "reference_sha256": "b" * 64, "plan_sha256": "d" * 64, "currency": "EUR",
               "references": {"post": post, "funding": {"id": "fund1", "currency": "EUR"}},
               "delivery": {"objective": "WEBSITE_CLICKS", "bid_strategy": "AUTO", "goal": "LINK_CLICKS", "expected_pay_by": "IMPRESSION",
                    "geographic_scope": "Worldwide, no location restriction", "audience_expansion": "EXPANDED", "expansion_description": "EXPANDED",
                    "provider_review": "If X review is PENDING, delivery is blocked until X accepts within the approved flight without another Kern write or approval."}}
    row = {"id": "x-ads-review", "kind": "tool", "source": "X Ads", "tool_id": "x_ads",
           "action_id": "launch_campaign", "status": "pending", "summary": "Launch NEW Kern with AUTO, EUR daily 10, total 50. Worldwide, no location restriction. Can spend when X approves pending review without another Kern approval.",
           "created_at": 1788960000, "updated_at": 1788960000}
    empty_payload = copy.deepcopy(payload)
    empty_payload["plan"]["targeting"] = []
    empty_payload["plan"].pop("audience_expansion")
    empty_payload["delivery"]["audience_expansion"] = None
    empty_payload["delivery"]["expansion_description"] = "No expansion"
    empty_row = {**row, "id": "x-ads-empty-review", "summary": "Launch NEW Kern. Worldwide, no location restriction; no targeting criteria and no expansion."}
    page.route("**/v1/approvals?*", lambda route: route.fulfill(json={
        "items": [row, empty_row], "page": 1, "pages": 1, "page_size": 25, "total": 2, "pending_count": 2, "history_count": 0,
    }))
    page.route("**/v1/tools/x_ads/approvals/x-ads-review", lambda route: route.fulfill(json={"approval": {"payload": payload}}))
    page.route("**/v1/tools/x_ads/approvals/x-ads-empty-review", lambda route: route.fulfill(json={"approval": {"payload": empty_payload}}))
    if page.locator("#mobile-nav-toggle").is_visible():
        page.locator("#mobile-nav-toggle").click()
    page.get_by_role("button", name="Approvals", exact=False).click()
    card = page.locator("[data-approval-key='tool:x-ads-review']")
    expect(card).to_contain_text("EUR daily 10, total 50")
    card.get_by_text("View exact request", exact=True).click()
    pre = card.locator(".approval-payload")
    expect(pre).to_contain_text("agent workflow 18")
    expect(pre).to_contain_text(post["url"])
    expect(pre).to_contain_text("14230524")
    expect(pre).to_contain_text("EXPANDED")
    expect(pre).to_contain_text("WEBSITE_CLICKS")
    expect(pre).to_contain_text("Worldwide, no location restriction")
    expect(pre).to_contain_text("without another Kern write or approval")
    assert json.loads(pre.inner_text()) == payload
    empty_card = page.locator("[data-approval-key='tool:x-ads-empty-review']")
    expect(empty_card).to_contain_text("no targeting criteria and no expansion")
    empty_card.get_by_text("View exact request", exact=True).click()
    empty_pre = empty_card.locator(".approval-payload")
    expect(empty_pre).to_contain_text("No expansion")
    assert json.loads(empty_pre.inner_text()) == empty_payload
    assert not errors, errors
    print("X Ads four password fields, all 12 actions, objectives, 20-criterion and empty worldwide launch approvals are reviewable", flush=True)
