"""Host AI month-to-date usage cards in the shared usage panel."""

from __future__ import annotations

import re


def run(page, url: str, log_in, *, mobile: bool = False) -> None:
    from playwright.sync_api import expect

    log_in(page, url)
    panel = page.locator("#runtime-overview-host-ai-panel")
    cards = page.locator("#runtime-overview .runtime-summary-host-inference")
    expect(cards).to_have_count(2)
    toggle = page.locator('[data-overview-group="host-ai"] .runtime-overview-toggle')
    runtime_toggle = page.locator('[data-overview-group="runtimes"] .runtime-overview-toggle')
    expect(page.locator(".runtime-overview-toggle")).to_have_count(3)
    expect(toggle).to_be_visible()
    expect(runtime_toggle).to_be_visible()
    expect(cards.first).to_be_hidden()
    with page.expect_request(lambda request: "/v1/host-inference/providers" in request.url):
        toggle.click()
    expect(toggle).to_have_attribute("aria-expanded", "true")
    expect(runtime_toggle).to_have_attribute("aria-expanded", "false")

    openai = cards.filter(has_text="OpenAI host")
    typesafe = cards.filter(has_text="TypeSafe")
    expect(openai).to_be_visible()
    expect(typesafe).to_be_visible()
    expect(openai).to_contain_text("$0.0012")
    expect(typesafe).to_contain_text("$0.0001")
    expect(openai).to_have_attribute(
        "aria-label",
        re.compile(r"estimated month-to-date.*including 1.0k cached.*11 of 12 responses priced"),
    )
    expect(typesafe).to_have_attribute("aria-label", re.compile(r"estimated month-to-date"))
    expect(toggle).to_contain_text("$0.0013 MTD")

    widths = panel.evaluate("element => ({client: element.clientWidth, scroll: element.scrollWidth})")
    if widths["scroll"] > widths["client"] + 1:
        raise AssertionError(f"host inference usage panel overflows horizontally: {widths}")

    # A broken Host AI aggregate leaves its last good figures in place.
    page.route(
        "**/v1/host-inference/providers",
        lambda route: route.fulfill(status=500, json={"error": "usage unavailable"}),
    )
    with page.expect_request(lambda request: "/v1/agent-runtime/refresh" in request.url):
        runtime_toggle.click()
    expect(runtime_toggle).to_have_attribute("aria-expanded", "true")
    expect(page.locator("#runtime-overview-runtimes-panel .runtime-summary").first).to_be_visible()
    with page.expect_request(lambda request: "/v1/host-inference/providers" in request.url):
        toggle.click()
    expect(toggle).to_contain_text("$0.0013 MTD")
    page.unroute("**/v1/host-inference/providers")
    expect(cards.first).to_be_visible()

    openai.click()
    expect(page.locator("#panel-network")).to_be_visible()
    expect(page.locator('.integration-row[data-integration="host_openai"] .integration-details')).to_be_visible()
