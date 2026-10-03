"""All four agent categories on a map, weighted links, and actionable attention."""
from __future__ import annotations


def run(page, url: str, log_in, *, mobile: bool = False) -> None:
    from playwright.sync_api import expect

    def refresh():
        page.evaluate("() => import('/admin_ui/swarm.js').then(m => m.refreshSwarm())")

    def apply_snapshot(payload):
        page.route('**/v1/swarm', lambda route: route.fulfill(json=payload))
        with page.expect_response(lambda response: response.url.endswith('/v1/swarm')
                                  and response.status == 200 and response.json() == payload):
            refresh()

    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    log_in(page, url)
    page.goto(url + "#home")
    expect(page.locator("#panel-home")).to_be_visible()
    if mobile:
        page.locator('#mobile-nav-toggle').click()
    page.route('**/v1/swarm/interactions', lambda route: route.fulfill(status=503, json={'error': 'unavailable'}))
    page.get_by_role('button', name='Swarm view', exact=True).click()
    expect(page.locator('.swarm-card')).to_have_count(101)
    expect(page.locator('.kind-operator')).to_have_count(1)
    expect(page.locator('.kind-app')).to_have_count(30)
    expect(page.locator('.kind-standing')).to_have_count(25)
    expect(page.locator('.kind-spawned')).to_have_count(10)
    expect(page.locator('.kind-on-demand')).to_have_count(35)
    expect(page.locator('.swarm-edge')).to_have_count(0)
    expect(page.locator('#swarm-error')).to_contain_text('Could not refresh communication counts')
    page.unroute('**/v1/swarm/interactions')
    refresh()
    expect(page.locator('.swarm-edge')).to_have_count(6)
    page.route('**/v1/swarm/interactions', lambda route: route.fulfill(status=503, json={'error': 'unavailable'}))
    refresh()
    expect(page.locator('.swarm-edge')).to_have_count(6)
    expect(page.locator('.swarm-card')).to_have_count(101)
    page.unroute('**/v1/swarm/interactions')
    refresh()
    # Recovered metrics rank on Arrange, not during a routine refresh.
    page.locator('#swarm-arrange').click()
    positions = page.locator('.swarm-card').evaluate_all(
        'nodes => Object.fromEntries(nodes.map(e => [e.dataset.threadId, [parseFloat(e.style.left), parseFloat(e.style.top)]]))')
    assert all(point[1] > positions['operator'][1] for key, point in positions.items() if key != 'operator')
    assert positions['app-1'][1] < positions['app-2'][1] < positions['app-30'][1]
    original_counts = page.request.get(url + 'v1/swarm/interactions', headers={'X-Kern-Csrf': '1'}).json()
    changed_counts = {**original_counts, 'metrics': {**original_counts['metrics'],
        'app-30': {'operator_messages': 10000, 'agent_peers': 100, 'total_tokens': 10000000, 'tokens_partial': False}}}
    page.route('**/v1/swarm/interactions', lambda route: route.fulfill(json=changed_counts))
    refresh()
    assert positions == page.locator('.swarm-card').evaluate_all(
        'nodes => Object.fromEntries(nodes.map(e => [e.dataset.threadId, [parseFloat(e.style.left), parseFloat(e.style.top)]]))')
    page.locator('#swarm-arrange').click()
    assert page.locator('[data-thread-id="app-30"]').evaluate('e => parseFloat(e.style.top)') < page.locator('[data-thread-id="app-1"]').evaluate('e => parseFloat(e.style.top)')
    page.unroute('**/v1/swarm/interactions')
    refresh()
    page.locator('#swarm-arrange').click()
    # A capped dense graph still retains every node and exposes all returned links.
    agents = page.request.get(url + 'v1/swarm', headers={'X-Kern-Csrf': '1'}).json()['agents']
    ids = [agent['thread_id'] for agent in agents]
    dense = [{'sender_thread_id': ids[i % 100], 'target_thread_id': ids[(i % 100 + 1 + i // 100) % 100], 'count': i + 1} for i in range(500)]
    page.route('**/v1/swarm/interactions', lambda route: route.fulfill(json={'interactions': dense}))
    refresh()
    expect(page.locator('.swarm-edge')).to_have_count(500)
    expect(page.locator('.swarm-card')).to_have_count(101)
    page.unroute('**/v1/swarm/interactions')
    refresh()
    expect(page.locator('.swarm-edge')).to_have_count(6)
    expect(page.locator('#swarm-attention-count')).to_have_text('8')
    panel = page.locator('#panel-swarm')
    assert panel.evaluate('e => { const r=e.getBoundingClientRect(); return r.x === 0 && r.y === 0 && r.width === innerWidth && r.height === innerHeight; }')
    assert page.locator('#sidebar').evaluate('e => e.inert')
    assert page.locator('#tab-swarm').evaluate('e => e.previousElementSibling.id === "tab-approvals"')
    # Fit includes all nodes, including idle and disconnected ones.
    page.locator('#swarm-fit').click()
    assert page.locator('#swarm-stage').evaluate('e => e.clientWidth <= e.parentElement.clientWidth && e.clientHeight <= e.parentElement.clientHeight')
    page.locator('#swarm-actual').click()
    page.locator('.kind-operator').click()
    expect(page.locator('#swarm-detail')).to_contain_text('→ Release notes · 24')
    expect(page.locator('#swarm-detail')).to_contain_text('Automated triggers and agent replies are excluded')
    expect(page.locator('#swarm-detail button').filter(has_text='Open conversation')).to_have_count(0)
    expect(page.locator('.swarm-edge-count').filter(has_text='24')).to_have_count(1)
    page.get_by_role('button', name='Close agent details', exact=True).click()
    first = page.locator('.swarm-card[data-thread-id="app-1"]')
    expect(first.locator('.swarm-type')).to_have_text('App')
    expect(first.locator('.swarm-agent-description')).to_have_text('Keep the project moving')
    expect(first).not_to_contain_text('Prepare the next release')
    first.click()
    expect(page.locator('#swarm-detail')).to_contain_text('Keep the project moving')
    expect(page.locator('#swarm-detail')).to_contain_text('Direct operator messages: 24')
    expect(page.locator('#swarm-detail')).to_contain_text('Other agents interacted with: 2')
    expect(page.locator('#swarm-detail')).to_contain_text('Tokens processed: 123,456')
    expect(page.locator('#swarm-detail')).to_contain_text('→ Billing desk · 18')
    expect(first).to_have_css('background-color', 'rgba(0, 0, 0, 0)')
    expect(first.locator('.swarm-avatar svg')).to_have_count(1)
    expect(first.locator('.critter-laptop')).to_have_count(1)
    # Missing token telemetry is not displayed as a measured zero.
    page.get_by_role('button', name='Close agent details', exact=True).click()
    page.locator('[data-thread-id="thread-66"]').click()
    expect(page.locator('#swarm-detail')).to_contain_text('Tokens processed: Unavailable')
    page.get_by_role('button', name='Close agent details', exact=True).click()
    page.locator('[data-thread-id="app-2"]').click()
    expect(page.locator('#swarm-detail')).to_contain_text('Tokens processed: 500,000 (partial)')
    page.get_by_role('button', name='Close agent details', exact=True).click()
    first.click()
    # Directional counts can be inspected from the accessible detail list.
    page.locator('.swarm-connection').filter(has_text='→ Billing desk').click()
    expect(page.locator('#swarm-detail')).to_contain_text('18 accepted messages')
    first.click()
    first.focus()
    page.evaluate('window.__swarmCard = document.activeElement')
    refresh()
    expect(first).to_be_focused()
    assert page.evaluate('window.__swarmCard === document.activeElement')
    # Highlighting preserves every agent on the map.
    page.locator('[data-swarm-filter="needs-human"]').click()
    expect(page.locator('.swarm-card:not(.is-dimmed)')).to_have_count(5)
    expect(page.locator('.swarm-card:visible')).to_have_count(101)
    page.locator('[data-swarm-filter="needs-human"]').click()
    page.locator('#swarm-search').fill('On-demand agent 100')
    expect(page.locator('.swarm-card:not(.is-dimmed)')).to_have_count(1)
    page.locator('#swarm-search').fill('')
    page.get_by_role('button', name='Close agent details', exact=True).click()
    # Sidebar is a duplicate attention view, not a separate home for failed nodes.
    page.locator('.swarm-attention-item').first.click()
    expect(page.locator('#swarm-detail')).to_contain_text('1 pending Kern approval.')
    expect(page.locator('#swarm-detail')).to_contain_text('View approvals')
    expect(page.locator('.swarm-card:visible')).to_have_count(101)
    page.get_by_role('button', name='Close agent details', exact=True).click()
    # Same node position survives changes in status/counts, and untrusted text is inert.
    position = first.evaluate('e => [e.style.left, e.style.top]')
    payload = page.request.get(url + 'v1/swarm', headers={'X-Kern-Csrf': '1'}).json()
    payload['agents'][0]['purpose'] = '<img src=x onerror=alert(1)>'
    payload['agents'][0]['state'] = 'failed'
    payload['agents'][0]['pending_approval_count'] = 1
    apply_snapshot(payload)
    expect(first.locator('.swarm-state')).to_have_text('Error')
    expect(first.locator('.swarm-approval')).to_have_text('1 approval')
    assert position == first.evaluate('e => [e.style.left, e.style.top]')
    first.click()
    expect(page.locator('#swarm-detail')).to_contain_text('<img src=x onerror=alert(1)>')
    expect(page.locator('#swarm-detail img')).to_have_count(0)
    expect(page.locator('#swarm-attention-count')).to_have_text('9')
    page.evaluate("window.__swarmOpen = window.KernHost.openWorkspace; window.KernHost.openWorkspace = () => Promise.resolve(false)")
    page.get_by_role('button', name='Open conversation', exact=True).click()
    expect(page.locator('#swarm-error')).to_contain_text('thread is no longer available')
    page.evaluate('() => { window.KernHost.openWorkspace = window.__swarmOpen; }')
    # Spawned catalogs resolve parent names without repeated full-list scans.
    large = {**payload, 'agents': [
        {**payload['agents'][0], 'thread_id': f'thread-{10000 + i}', 'kind': 'spawned',
         'name': f'Delegate {i}', 'spawned_by_thread_id': 'thread-parent', 'state': 'idle',
         'pending_approval_count': 0} for i in range(1000)
    ]}
    large['agents'].append({**payload['agents'][0], 'thread_id': 'thread-parent',
                            'kind': 'on-demand', 'name': 'Team lead', 'state': 'idle',
                            'pending_approval_count': 0})
    apply_snapshot(large)
    expect(page.locator('.swarm-card')).to_have_count(1002)
    expect(page.locator('.kind-spawned .swarm-agent-description').first).to_have_text('Delegated by Team lead')
    page.evaluate("""() => {
      window.__swarmContentMutations = 0;
      window.__swarmContentObserver = new MutationObserver(records => { window.__swarmContentMutations += records.length; });
      window.__swarmContentObserver.observe(document.querySelector('#swarm-nodes'), {childList: true, subtree: true});
    }""")
    page.locator('#swarm-search').fill('Team lead')
    expect(page.locator('.swarm-card:not(.is-dimmed)')).to_have_count(1001)
    assert page.evaluate("""() => {
      const count = window.__swarmContentMutations + window.__swarmContentObserver.takeRecords().length;
      window.__swarmContentObserver.disconnect();
      return count;
    }""") == 0, 'search rewrote unchanged agent content'
    # Index refreshes when a parent is renamed, and keeps missing-parent fallback.
    large['agents'][-1]['name'] = 'Renamed lead'
    large['agents'][0]['spawned_by_thread_id'] = 'thread-missing'
    apply_snapshot(large)
    expect(page.locator('.kind-spawned .swarm-agent-description').nth(1)).to_have_text('Delegated by Renamed lead')
    expect(page.locator('.kind-spawned .swarm-agent-description').first).to_have_text('Delegated by thread-missing')
    page.locator('#swarm-search').fill('')
    page.unroute('**/v1/swarm')
    page.emulate_media(reduced_motion='reduce')
    assert page.locator('#swarm-canvas').evaluate('e => e.getAnimations({subtree:true}).length') == 0
    page.keyboard.press('Escape')
    expect(page.locator('#panel-home')).to_be_visible()
    assert not page.locator('#sidebar').evaluate('e => e.inert')
    assert page.evaluate('document.documentElement.scrollWidth - innerWidth') <= 1
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
