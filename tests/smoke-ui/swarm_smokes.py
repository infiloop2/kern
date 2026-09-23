"""100 selectable characters, live state changes, and phone/reduced-motion UX."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


def run(page, url: str, log_in, *, mobile: bool = False) -> None:
    from playwright.sync_api import expect

    def apply_snapshot(payload):
        page.route('**/v1/swarm', lambda route: route.fulfill(json=payload))
        # An existing five-second poll may still own the in-flight request.
        # Wait for this exact replacement, rather than assuming refresh started it.
        with page.expect_response(lambda response: response.url.endswith('/v1/swarm')
                                  and response.status == 200 and response.json() == payload):
            page.evaluate("() => import('/admin_ui/swarm.js').then(m => m.refreshSwarm())")

    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    log_in(page, url)
    if mobile:
        page.locator('#mobile-nav-toggle').click()
    page.get_by_role('button', name='Swarm view', exact=True).click()
    panel = page.locator('#panel-swarm')
    assert panel.evaluate('e => { const r=e.getBoundingClientRect(); return r.x === 0 && r.y === 0 && r.width === innerWidth && r.height === innerHeight; }')
    assert page.locator('#sidebar').evaluate('e => e.inert')
    assert page.locator('#swarm-close').evaluate('e => { const r=e.getBoundingClientRect(); return document.elementFromPoint(r.x+22,r.y+22) === e; }')
    assert page.locator('#tab-swarm').evaluate('e => e.previousElementSibling.id === "tab-approvals"')
    page.get_by_role('button', name='Close Swarm view', exact=True).click()
    expect(page.locator('#panel-home')).to_be_visible()
    assert not page.locator('#sidebar').evaluate('e => e.inert')
    if mobile:
        page.locator('#mobile-nav-toggle').click()
    page.get_by_role('button', name='Swarm view', exact=True).click()
    figures = page.locator('.swarm-figure')
    expect(figures).to_have_count(100)
    expect(page.locator('.pose-busy')).to_have_count(8)
    expect(page.locator('.pose-needs-human')).to_have_count(5)
    expect(page.locator('.pose-idle')).to_have_count(84)
    expect(page.locator('.pose-failed')).to_have_count(3)
    first = page.locator('.swarm-figure[data-thread-id="app-1"]')
    expect(first.locator('.swarm-agent-task')).to_have_text('Prepare the next release')
    expect(first.locator('.swarm-agent-purpose')).to_have_text('Keep the project moving')
    first.click()
    expect(page.locator('#swarm-detail')).to_contain_text('Prepare the next release')
    expect(page.locator('#swarm-bubbles')).to_contain_text('Prepare the next release')

    if mobile:
        expect(page.locator('#swarm-detail')).to_have_css('position', 'fixed')
    page.get_by_role('button', name='Close agent details', exact=True).click()

    # Stable snapshots preserve DOM identity, focus, and ongoing animation.
    first.focus()
    page.evaluate("window.__swarmFigure = document.activeElement")
    page.evaluate("() => import('/admin_ui/swarm.js').then(m => m.refreshSwarm())")
    expect(first).to_be_focused()
    assert page.evaluate('window.__swarmFigure === document.activeElement')
    first.click()

    page.get_by_role('button', name='Close agent details', exact=True).click()
    idle = page.locator('[data-swarm-filter="idle"]')
    assert page.locator('#swarm-agents-idle .swarm-figure').first.evaluate('e => e.offsetWidth < 40')
    idle.click()
    expect(page.locator('.swarm-figure:visible')).to_have_count(84)
    assert page.locator('#swarm-agents-idle .swarm-figure').first.evaluate('e => e.offsetWidth >= 100')
    idle.click()
    needs = page.locator('[data-swarm-filter="needs-human"]')
    needs.click()
    expect(page.locator('.swarm-figure:visible')).to_have_count(5)
    needs.click()
    expect(page.locator('.swarm-figure:visible')).to_have_count(100)
    with page.expect_response(lambda response: response.url.endswith('/v1/swarm?q=chat%20agent%20100')
                              and response.status == 200):
        page.locator('#swarm-search').fill('Chat agent 100')
    expect(page.locator('.swarm-figure:visible')).to_have_count(1)
    page.locator('.swarm-figure:visible').click()
    expect(page.locator('#swarm-detail')).to_contain_text('Human input: not assessed')
    page.get_by_role('button', name='Close agent details', exact=True).click()
    page.locator('#swarm-search').fill('')

    # Recent peer messages produce finite walks and text-free dialog icons.
    payload = page.request.get(url + 'v1/swarm', headers={'X-Kern-Csrf': '1'}).json()
    now = datetime.now(timezone.utc)
    peer_feed = {'messages': [
        {'seq': 99999, 'timestamp': now.isoformat(),
         'sender_thread_id': 'app-1', 'target_thread_id': 'app-2'},
        {'seq': 99998, 'timestamp': now.isoformat(),
         'sender_thread_id': 'app-3', 'target_thread_id': 'app-4'},
        {'seq': 99997, 'timestamp': (now - timedelta(seconds=60)).isoformat(),
         'sender_thread_id': 'app-5', 'target_thread_id': 'app-6'},
    ]}
    page.route('**/v1/swarm/peer-messages*', lambda route: route.fulfill(json=peer_feed))
    with page.expect_response(lambda response: response.url.endswith('/v1/swarm/peer-messages')
                              and response.status == 200):
        page.evaluate("() => import('/admin_ui/swarm.js').then(m => m.refreshSwarm())")
    expect(page.locator('.swarm-message-icon')).to_have_count(2)
    assert page.locator('.swarm-message-icon').all_inner_texts() == ['', '']
    assert page.locator('.is-walking').count() <= 2
    page.unroute('**/v1/swarm/peer-messages*')
    page.unroute('**/v1/swarm')

    # A failed runtime remains distinct from an affirmative human assessment.
    failed = page.locator('[data-swarm-filter="failed"]')
    failed.click()
    expect(page.locator('.swarm-figure:visible')).to_have_count(3)
    page.locator('.swarm-figure:visible').first.click()
    expect(page.locator('#swarm-detail .swarm-state')).to_have_text('Failed')
    page.get_by_role('button', name='Close agent details', exact=True).click()
    failed.click()
    payload['agents'][0]['state'] = 'failed'
    payload['agents'][0]['needs_human'] = True
    apply_snapshot(payload)
    expect(first).to_have_attribute('data-pose', 'failed')
    expect(page.locator('#swarm-count-failed')).to_have_text('4')
    payload['agents'][0]['state'] = 'busy'
    apply_snapshot(payload)
    expect(first).to_have_attribute('data-pose', 'busy')
    page.unroute('**/v1/swarm')

    # Change the server snapshot: busy has priority, then return to idle.
    payload = page.request.get(url + 'v1/swarm', headers={'X-Kern-Csrf': '1'}).json()
    payload['agents'][0]['state'] = 'idle'
    payload['agents'][0]['needs_human'] = True
    payload['agents'][0]['task'] = '<img src=x onerror=alert(1)>'
    apply_snapshot(payload)
    expect(first).to_have_attribute('data-pose', 'needs-human')
    first.click()
    expect(page.locator('#swarm-detail')).to_contain_text('<img src=x onerror=alert(1)>')
    expect(page.locator('#swarm-detail img')).to_have_count(0)
    expect(page.locator('#swarm-agents-needs-human .swarm-figure')).to_have_count(6)
    page.unroute('**/v1/swarm')

    # Navigation failures and failed polls retain the last usable scene.
    page.evaluate("window.__swarmOpen = window.KernHost.openWorkspace; window.KernHost.openWorkspace = () => Promise.resolve(false)")
    page.get_by_role('button', name='Open conversation', exact=True).click()
    expect(page.locator('#swarm-error')).to_contain_text('thread is no longer available')
    page.evaluate("() => { window.KernHost.openWorkspace = window.__swarmOpen; }")
    page.route('**/v1/swarm', lambda route: route.abort())
    # A normal poll may already be in flight, in which case refreshSwarm
    # intentionally skips this call. Retry until the intercepted failure runs.
    for _ in range(30):
        page.evaluate("() => import('/admin_ui/swarm.js').then(m => m.refreshSwarm()).catch(() => {})")
        if 'Showing the snapshot from' in page.locator('#swarm-error').inner_text():
            break
        page.wait_for_timeout(100)
    expect(page.locator('#swarm-error')).to_contain_text('Showing the snapshot from')
    expect(figures).to_have_count(100)
    page.unroute('**/v1/swarm')
    # The shared API pauses reads after the deliberately aborted request.
    expect(page.locator('#overload-status')).to_be_hidden(timeout=15000)

    page.emulate_media(reduced_motion='reduce')
    expect(first.locator('.critter-body')).to_have_css('animation-name', 'none')
    # An unconfigured inference provider leaves absent annotations unknown.
    for agent in payload['agents']:
        agent.pop('task', None)
        agent.pop('needs_human', None)
    apply_snapshot(payload)
    expect(page.locator('#swarm-detail')).to_contain_text('No task title yet')
    expect(page.locator('#swarm-detail')).to_contain_text('Human input: not assessed')
    expect(page.locator('#swarm-agents-needs-human .swarm-figure')).to_have_count(0)
    expect(page.locator('#swarm-messages')).to_have_count(0)
    expect(page.locator('#swarm-bubbles')).not_to_contain_text('undefined')
    assert page.locator('.is-walking').count() == 0
    page.unroute('**/v1/swarm')
    page.keyboard.press('Escape')
    expect(page.locator('#panel-home')).to_be_visible()
    expect(page.locator('#swarm-bubbles')).to_be_empty()
    assert page.evaluate('document.documentElement.scrollWidth - innerWidth') <= 1
    # Close returns to the actual entry view; a direct link falls back to Home.
    if mobile:
        page.locator('#mobile-nav-toggle').click()
    page.locator('#tab-approvals').click()
    if mobile:
        page.locator('#mobile-nav-toggle').click()
    page.get_by_role('button', name='Swarm view', exact=True).click()
    page.get_by_role('button', name='Close Swarm view', exact=True).click()
    expect(page.locator('#panel-approvals')).to_be_visible()
    page.goto(url + '#swarm')
    page.reload()
    expect(page.locator('#panel-swarm')).to_be_visible()
    page.get_by_role('button', name='Close Swarm view', exact=True).click()
    expect(page.locator('#panel-home')).to_be_visible()
    assert not errors, errors
