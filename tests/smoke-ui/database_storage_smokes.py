"""Storage panel: on-demand reads, retention copy, safe rendering and recovery."""
from playwright.sync_api import expect


def run(page, url, log_in):
    requests = []
    page.on("request", lambda request: requests.append(request) if "/v1/database-storage" in request.url else None)
    log_in(page, url)
    assert not requests, "Home should not fetch database sizes"
    page.locator('[data-view="database-storage"]').click()
    panel = page.locator("#panel-database-storage")
    table = page.locator("#database-storage-tables")
    expect(panel).to_be_visible()
    expect(page.locator("#database-storage-total")).to_contain_text("GiB")
    expect(table.locator("tbody tr").first).to_contain_text("agent_events")
    # The mock has 7.5 million events against the 100-million-event target.
    expect(table).to_contain_text("7.5% of history target")
    expect(table).to_contain_text("all conversations")
    expect(table).to_contain_text("Creation blocked")
    expect(table).to_contain_text("90 days")
    expect(table).to_contain_text("No automatic pruning")
    expect(table).to_contain_text("Unavailable")
    if page.viewport_size["width"] < 700:
        assert table.evaluate("el => el.scrollWidth <= el.clientWidth"), "Retention must fit without sideways scrolling"
    page.get_by_text("How to read this view", exact=True).click()
    expect(panel).to_contain_text("may be stale or unavailable")
    page.get_by_text("How to read this view", exact=True).click()
    assert len(requests) == 1
    page.wait_for_timeout(5500)  # One normal app refresh tick must not poll this endpoint.
    assert len(requests) == 1
    page.locator("#database-storage-refresh").click()
    expect(page.locator("#database-storage-status")).to_contain_text("Updated")
    assert len(requests) == 2

    page.route("**/v1/database-storage", lambda route: route.fulfill(status=503, json={"error": "Database temporarily unavailable"}))
    page.locator("#database-storage-refresh").click()
    expect(page.locator("#database-storage-status")).to_contain_text("previous successful load")
    expect(table).to_contain_text("agent_events")
    expect(page.locator("#database-storage-refresh")).to_be_enabled()
    page.unroute("**/v1/database-storage")
    page.route("**/v1/database-storage", lambda route: route.fulfill(json={
        "database_bytes": 0,
        "tables": [{"name": "<img src=x onerror=alert(1)>", "description": "<script>bad()</script>",
                    "estimated_rows": 0, "total_bytes": 0, "retention": "Unknown <b>policy</b>", "row_target": 10}],
    }))
    page.locator("#database-storage-refresh").click()
    expect(table).to_contain_text("<img src=x onerror=alert(1)>")
    expect(table.locator("img, script, b")).to_have_count(0)
    expect(table).to_contain_text("0% of history target")
    expect(page.locator("#database-storage-total")).to_have_text("0 B database storage")
    page.unroute("**/v1/database-storage")
    panel.get_by_role("button", name="Back to Home").click()
    expect(page.locator("#panel-home")).to_be_visible()
    page.go_back()
    expect(panel).to_be_visible()
    expect(table).to_contain_text("agent_events")
    assert all(request.method == "GET" for request in requests)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Page must fit a phone"
