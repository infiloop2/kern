"""Third top-bar menu, including small costs and failed refreshes."""
import re


def run(page, url, log_in, *, mobile=False):
    from playwright.sync_api import expect
    log_in(page, url)
    toggle = page.locator('[data-overview-group="tools"] .runtime-overview-toggle')
    panel = page.locator('#runtime-overview-tools-panel')
    expect(page.locator('.runtime-overview-toggle')).to_have_count(3)
    expect(toggle).to_contain_text('$1.2052 MTD')
    expect(toggle.locator('.tool-cost-indicator')).to_have_count(0)
    expect(toggle).not_to_have_attribute('aria-label', re.compile('partial'))
    toggle.click()
    expect(panel).to_be_visible()
    expect(panel.locator('.tool-spend-card')).to_have_count(3)
    cards = panel.locator('.tool-spend-card')
    for index in range(3):
        expect(cards.nth(index)).to_be_visible()
    # Read all boxes in one browser evaluation: the usage poll can replace the
    # panel between separate bounding_box() calls.
    page.wait_for_function("""() => {
        const cards = [...document.querySelectorAll('#runtime-overview-tools-panel .tool-spend-card')];
        if (cards.length !== 3) return false;
        const boxes = cards.map(card => card.getBoundingClientRect());
        if (boxes.some(box => !box.width || !box.height)) return false;
        const rows = boxes.map(box => box.y);
        if (innerWidth > 860) return Math.max(...rows) - Math.min(...rows) < 2;
        if (innerWidth > 340) return Math.abs(rows[0] - rows[1]) < 2 && rows[2] > rows[0];
        return rows[1] > rows[0];
    }""")
    expect(panel.locator('.tool-spend-card').filter(has_text='retired_tool')).to_have_count(0)
    expect(panel.get_by_role('button', name=re.compile('TwitterAPI.io:'))).to_contain_text('$0.0002')
    expect(panel.get_by_role('button', name=re.compile('Gmail:'))).to_have_count(0)
    expect(panel.locator('.tool-spend-card').filter(has_text='Disabled reporter')).to_have_count(0)
    expect(panel.get_by_role('button', name=re.compile('Runway:'))).to_contain_text('$1.20')
    expect(panel.get_by_role('button', name=re.compile('Reddit ScrapeCreators Search:'))).to_contain_text('$0.0056')
    expect(panel).not_to_contain_text('unmeasured')
    # A long tool name wraps within its column instead of running under the cost.
    overlaps = panel.evaluate('''panel => [...panel.querySelectorAll('.tool-spend-card')].filter(card => {
        const text = document.createRange();
        text.selectNodeContents(card.querySelector('.runtime-summary-copy > span'));
        return text.getBoundingClientRect().right > card.querySelector('.runtime-usage').getBoundingClientRect().left;
    }).map(card => card.textContent.trim())''')
    assert not overlaps, overlaps
    for target in (panel, page.locator('#runtime-overview')):
        bounds = target.evaluate('e => ({left:e.getBoundingClientRect().left,right:e.getBoundingClientRect().right,scroll:e.scrollWidth,client:e.clientWidth,width:innerWidth})')
        assert bounds['left'] >= 0 and bounds['right'] <= bounds['width'] + 1, bounds
        assert bounds['scroll'] <= bounds['client'] + 1, bounds
    tiny = {"month_to_date": "0.000000001", "tools": [
        {"tool_id": "runway", "display_name": "Runway", "enabled": True, "month_to_date": "0.000000001"}]}
    page.route('**/v1/tools/usage', lambda route: route.fulfill(json=tiny))
    toggle.click()
    toggle.click()
    expect(toggle).to_contain_text('<$0.0001 MTD')
    expect(panel.get_by_role('button', name=re.compile('Runway:'))).to_contain_text('<$0.0001')
    page.unroute('**/v1/tools/usage')
    toggle.click()
    toggle.click()
    expect(toggle).to_contain_text('$1.2052 MTD')
    page.route('**/v1/tools/usage', lambda route: route.fulfill(status=500, json={"error": "unavailable"}))
    toggle.click()
    toggle.click()
    expect(toggle).to_have_attribute('aria-label', re.compile('stale'))
    expect(toggle).to_contain_text('$1.2052')
    expect(panel).to_contain_text('Refresh failed')
    page.unroute('**/v1/tools/usage')
    panel.get_by_role('button', name=re.compile('TwitterAPI.io:')).click()
    expect(page.get_by_role("heading", name="TwitterAPI.io", exact=True).first).to_be_visible()
    expect(page.locator('.guide-action-cost').filter(has_text='$0.00015')).to_be_visible()
