"""All four agent categories on a map, weighted links, and actionable attention."""
from __future__ import annotations

import math
import re


def run(page, url: str, log_in, *, mobile: bool = False) -> None:
    from playwright.sync_api import expect

    def refresh():
        page.evaluate("() => import('/admin_ui/swarm.js').then(m => m.refreshSwarm())")

    def apply_snapshot(payload):
        page.route('**/v1/swarm', lambda route: route.fulfill(json=payload))
        with page.expect_response(lambda response: response.url.endswith('/v1/swarm')
                                  and response.status == 200 and response.json() == payload):
            refresh()

    def settle():
        # A camera write is batched into the next frame before its glide starts.
        page.evaluate('() => new Promise(requestAnimationFrame)')
        page.evaluate('''() => Promise.allSettled(document.querySelector('#swarm-canvas')
          .getAnimations({subtree: true}).filter(a => a instanceof CSSTransition).map(a => a.finished))''')

    def tap(locator):
        # Agents may sit beyond the camera; keyboard focus glides them into view.
        locator.focus()
        settle()
        locator.click()

    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    log_in(page, url)
    page.goto(url + "#home")
    expect(page.locator("#panel-home")).to_be_visible()
    if mobile:
        page.locator('#mobile-nav-toggle').click()
    page.route('**/v1/swarm/interactions', lambda route: route.fulfill(status=503, json={'error': 'unavailable'}))
    page.get_by_role('button', name='Swarm view', exact=True).click()
    expect(page.locator('.swarm-card')).to_have_count(102)
    expect(page.locator('.kind-operator')).to_have_count(1)
    expect(page.locator('.kind-host')).to_have_count(1)
    expect(page.locator('.kind-app')).to_have_count(30)
    expect(page.locator('.kind-standing')).to_have_count(25)
    expect(page.locator('.kind-spawned')).to_have_count(10)
    expect(page.locator('.kind-on-demand')).to_have_count(35)
    # Every agent type is a visually distinct robot, also shown in the legend.
    for kind, count in [('app', 30), ('standing', 25), ('spawned', 10), ('on-demand', 35)]:
        expect(page.locator(f'.swarm-card.kind-{kind} .swarm-bot.bot-{kind}')).to_have_count(count)
        expect(page.locator(f'.swarm-legend .bot-{kind}')).to_have_count(1)
    expect(page.locator('.swarm-edge')).to_have_count(0)
    expect(page.locator('#swarm-error')).to_contain_text('Could not refresh communication counts')
    page.unroute('**/v1/swarm/interactions')
    refresh()
    expect(page.locator('.swarm-edge')).to_have_count(9)
    page.route('**/v1/swarm/interactions', lambda route: route.fulfill(status=503, json={'error': 'unavailable'}))
    refresh()
    expect(page.locator('.swarm-edge')).to_have_count(9)
    expect(page.locator('.swarm-card')).to_have_count(102)
    page.unroute('**/v1/swarm/interactions')
    refresh()
    # Recovered metrics rank on Arrange, not during a routine refresh.
    page.locator('#swarm-arrange').click()
    positions = page.locator('.swarm-card').evaluate_all(
        'nodes => Object.fromEntries(nodes.map(e => [e.dataset.threadId, [parseFloat(e.style.left), parseFloat(e.style.top)]]))')

    def distance(points, thread_id):
        return math.dist(points[thread_id], points['operator'])

    # You sit at the centre; involvement pulls active agents inward on average.
    assert positions['operator'][1] == positions['kern-host'][1]
    assert positions['kern-host'][0] >= positions['operator'][0] + 160
    assert sum(distance(positions, id) for id in ['app-1', 'app-2', 'thread-66']) / 3 < sum(distance(positions, f'app-{i}') for i in range(10, 31)) / 21
    sizes = page.locator('.swarm-card').evaluate_all(
        "nodes => Object.fromEntries(nodes.map(e => [e.dataset.threadId, parseFloat(e.style.getPropertyValue('--orb'))]))")
    assert sizes['app-1'] > sizes['app-2'] > sizes['app-30']
    expect(page.locator('.swarm-ring, .swarm-ring-label')).to_have_count(0)
    original_counts = page.request.get(url + 'v1/swarm/interactions', headers={'X-Kern-Csrf': '1'}).json()
    changed_counts = {**original_counts, 'metrics': {**original_counts['metrics'],
        'app-30': {'operator_messages': 10000, 'agent_peers': 100, 'total_tokens': 10000000, 'tokens_partial': False}}}
    page.route('**/v1/swarm/interactions', lambda route: route.fulfill(json=changed_counts))
    refresh()
    assert positions == page.locator('.swarm-card').evaluate_all(
        'nodes => Object.fromEntries(nodes.map(e => [e.dataset.threadId, [parseFloat(e.style.left), parseFloat(e.style.top)]]))')
    page.locator('#swarm-arrange').click()
    rearranged = page.locator('.swarm-card').evaluate_all(
        'nodes => Object.fromEntries(nodes.map(e => [e.dataset.threadId, [parseFloat(e.style.left), parseFloat(e.style.top)]]))')
    assert distance(rearranged, 'app-30') < distance(rearranged, 'app-1')
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
    expect(page.locator('.swarm-card')).to_have_count(102)
    # Even the thickest line leaves a visible direction tip.
    max_width = page.locator('.swarm-edge').evaluate_all(
        "edges => Math.max(...edges.map(e => +e.getAttribute('stroke-width')))")
    assert max_width < float(page.locator('#swarm-arrow').get_attribute('markerHeight')) <= 12
    page.unroute('**/v1/swarm/interactions')
    refresh()
    expect(page.locator('.swarm-edge')).to_have_count(9)
    # Arrowheads stay subtle even on high-volume connections.
    marker = page.locator('#swarm-arrow')
    expect(marker).to_have_attribute('markerUnits', 'userSpaceOnUse')
    expect(marker).to_have_attribute('markerWidth', '12')
    expect(marker).to_have_attribute('markerHeight', '12')
    thick = page.get_by_role('button', name='Operator to Release notes: 24 messages', exact=True)
    thin = page.get_by_role('button', name='Kern host to On-Demand agent 66: 1 messages', exact=True)
    assert float(thick.get_attribute('stroke-width')) > float(thin.get_attribute('stroke-width'))
    # Reciprocal curves preserve their separate counts and keyboard selection.
    page.get_by_role('button', name='Billing desk to Release notes: 5 messages', exact=True).focus()
    page.keyboard.press('Enter')
    expect(page.locator('#swarm-detail')).to_contain_text('Billing desk → Release notes')
    expect(page.locator('#swarm-detail')).to_contain_text('5 accepted messages')
    page.get_by_role('button', name='Close agent details', exact=True).click()
    expect(page.locator('#swarm-attention-count')).to_have_text('8')
    panel = page.locator('#panel-swarm')
    assert panel.evaluate('e => { const r=e.getBoundingClientRect(); return r.x === 0 && r.y === 0 && r.width === innerWidth && r.height === innerHeight; }')
    assert page.locator('#sidebar').evaluate('e => e.inert')
    assert page.locator('#tab-swarm').evaluate('e => e.previousElementSibling.id === "tab-approvals"')
    viewport = page.locator('#swarm-viewport')
    canvas = page.locator('#swarm-canvas')

    def camera():
        page.evaluate('() => new Promise(requestAnimationFrame)')
        return canvas.evaluate('e => { const m = new DOMMatrix(e.style.transform); return [m.e, m.f, m.a]; }')

    def wait_for_camera(expected):
        page.wait_for_function("""expected => {
          const m = new DOMMatrix(document.querySelector('#swarm-canvas').style.transform);
          return [m.e, m.f, m.a].every((value, i) => Math.abs(value - expected[i]) < .01);
        }""", arg=expected)

    def cards_inside():
        return page.evaluate('''() => {
          const view = document.querySelector('#swarm-viewport').getBoundingClientRect();
          return [...document.querySelectorAll('.swarm-avatar')].filter(e => {
            const r = e.getBoundingClientRect();
            return r.left >= view.left - 1 && r.top >= view.top - 1 && r.right <= view.right + 1 && r.bottom <= view.bottom + 1;
          }).length;
        }''')

    def expect_cards_inside(count):
        # Camera moves glide briefly; settle, then poll for the final frame.
        settle()
        for _ in range(30):
            if cards_inside() == count:
                return
            page.wait_for_timeout(100)
        assert cards_inside() == count, (cards_inside(), camera())
    # Fit includes all nodes, including idle and disconnected ones.
    page.locator('#swarm-fit').click()
    expect(page.locator('#swarm-zoom-level')).not_to_have_text('100%')
    expect_cards_inside(102)
    page.locator('#swarm-actual').click()
    expect(page.locator('#swarm-zoom-level')).to_have_text('100%')
    box = viewport.bounding_box()
    if not mobile:
        # Drag in both axes and release outside the map. The camera is
        # unbounded: dragging well past the swarm leaves open space in view.
        start = camera()
        page.mouse.move(box['x'] + 80, box['y'] + 80)
        page.mouse.down()
        page.mouse.move(box['x'] + 20, box['y'] + 20, steps=4)
        expect(viewport).to_have_class('is-panning')
        wait_for_camera([start[0] - 60, start[1] - 60, start[2]])
        assert math.dist(camera()[:2], [start[0] - 60, start[1] - 60]) < .01, (start, camera())
        page.mouse.move(box['x'] - 10, box['y'] + 20)
        page.mouse.up()
        expect(viewport).not_to_have_class('is-panning')
        after_drag = camera()
        page.mouse.move(box['x'] + 100, box['y'] + 100)
        assert camera() == after_drag
        for _ in range(3):
            page.mouse.move(box['x'] + 40, box['y'] + 40)
            page.mouse.down()
            page.mouse.move(box['x'] + box['width'] - 40, box['y'] + box['height'] - 40, steps=6)
            page.mouse.up()
        expect_cards_inside(0)
        # Two-finger scrolling zooms around the pointer in both directions.
        before = camera()
        pointer_x, pointer_y = round(box['x'] + 200), round(box['y'] + 200)
        page.mouse.move(pointer_x, pointer_y)
        x, y = pointer_x - box['x'], pointer_y - box['y']
        anchor = [(x - before[0]) / before[2], (y - before[1]) / before[2]]
        for delta in (200, -200):
            start = camera()
            page.mouse.wheel(30, delta)
            page.wait_for_function("k => Math.abs(new DOMMatrix(document.querySelector('#swarm-canvas').style.transform).a - k) > .01", arg=start[2])
            after = camera()
            assert (after[2] < start[2]) if delta > 0 else (after[2] > start[2])
            assert math.dist([(x - after[0]) / after[2], (y - after[1]) / after[2]], anchor) < .01, (anchor, before, after, x, y)
        # Trackpad pinch (Ctrl + wheel) still zooms; 0 fits everything.
        page.keyboard.down('Control')
        page.mouse.wheel(0, -200)
        page.keyboard.up('Control')
        page.wait_for_function("k => new DOMMatrix(document.querySelector('#swarm-canvas').style.transform).a > k", arg=before[2])
        assert camera()[2] > before[2]
        viewport.focus()
        page.keyboard.press('0')
        expect_cards_inside(102)
        # A press that becomes a drag on an agent pans instead of selecting it.
        operator_box = page.locator('.kind-operator .swarm-avatar').bounding_box()
        page.mouse.move(operator_box['x'] + 10, operator_box['y'] + 10)
        page.mouse.down()
        # Pressing an agent keeps it in place (no global button-press transform).
        assert page.locator('.kind-operator .swarm-avatar').bounding_box() == operator_box
        page.mouse.move(operator_box['x'] + 90, operator_box['y'] + 60, steps=5)
        page.mouse.up()
        expect(page.locator('#swarm-detail')).not_to_have_class('has-selection')
        # A press released outside the map before it travelled far enough to
        # pan must not leave a stale pointer that turns the next drag into a pinch.
        page.mouse.move(box['x'] + 3, box['y'] + 200)
        page.mouse.down()
        page.mouse.move(box['x'] - 40, box['y'] + 200)
        page.mouse.up()
        start = camera()
        page.mouse.move(box['x'] + 300, box['y'] + 300)
        page.mouse.down()
        page.mouse.move(box['x'] + 340, box['y'] + 330, steps=4)
        page.mouse.up()
        wait_for_camera([start[0] + 40, start[1] + 30, start[2]])
        assert math.dist(camera(), [start[0] + 40, start[1] + 30, start[2]]) < .01, (start, camera())
        # Pressing open canvas space (not just the grid) focuses the map for keys.
        page.locator('#swarm-close').focus()
        ring = page.locator('#swarm-grid')
        for name in ('pointerdown', 'pointerup'):
            ring.dispatch_event(name, {'pointerId': 7, 'isPrimary': True, 'button': 0, 'pointerType': 'mouse', 'bubbles': True})
        expect(viewport).to_be_focused()
    else:
        # One finger drags the map; native page scroll stays outside it.
        assert viewport.evaluate("e => getComputedStyle(e).touchAction") == 'none'
    tap(page.locator('.kind-operator'))
    expect(page.locator('#swarm-detail')).to_contain_text('→ Release notes · 24')
    expect(page.locator('#swarm-detail')).to_contain_text('Automated triggers and agent replies are excluded')
    expect(page.locator('#swarm-detail button').filter(has_text='Open conversation')).to_have_count(0)
    expect(page.locator('.swarm-edge-count').filter(has_text='24')).to_have_count(1)
    page.get_by_role('button', name='Close agent details', exact=True).click()
    # Host connections show automated deliveries to different agent categories.
    host = page.locator('.kind-host')
    tap(host)
    expect(host.locator('.swarm-agent-name')).to_have_text('Kern host')
    expect(host.locator('.host-server')).to_have_count(1)
    expect(page.locator('#swarm-detail')).to_contain_text('→ Standing agent 31 · 14')
    expect(page.locator('#swarm-detail')).to_contain_text('→ Release notes · 3')
    expect(page.locator('#swarm-detail')).to_contain_text('→ On-Demand agent 66 · 1')
    expect(page.locator('#swarm-detail')).to_contain_text('scheduled triggers, approval outcomes and restart notices')
    expect(page.locator('#swarm-detail')).not_to_contain_text('Involvement ·')
    expect(page.locator('#swarm-detail button').filter(has_text='Open conversation')).to_have_count(0)
    page.locator('.swarm-connection').filter(has_text='→ Release notes').click()
    expect(page.locator('#swarm-detail')).to_contain_text('Kern host → Release notes')
    expect(page.locator('#swarm-detail')).to_contain_text('3 accepted messages')
    page.get_by_role('button', name='Close agent details', exact=True).click()
    first = page.locator('.swarm-card[data-thread-id="app-1"]')
    expect(first.locator('.swarm-type')).to_have_text('App')
    expect(first.locator('.swarm-agent-description')).to_have_text('Keep the project moving')
    expect(first).not_to_contain_text('Prepare the next release')
    tap(first)
    expect(page.locator('#swarm-detail')).to_contain_text('Keep the project moving')
    expect(page.locator('#swarm-detail')).to_contain_text('Direct operator messages: 24')
    expect(page.locator('#swarm-detail')).to_contain_text('Other agents interacted with: 2')
    expect(page.locator('#swarm-detail')).to_contain_text('Tokens processed: 123,456')
    expect(page.locator('#swarm-detail')).to_contain_text('→ Billing desk · 18')
    expect(page.locator('#swarm-detail')).to_contain_text('← Kern host · 3')
    expect(first).to_have_css('background-color', 'rgba(0, 0, 0, 0)')
    expect(first.locator('.swarm-avatar > svg')).to_have_count(1)
    expect(first.locator('.bot-motion-code')).to_have_count(1)
    expect(first.locator('.bot-busy')).to_have_count(1)
    # Selecting an agent fades agents outside its neighbourhood.
    expect(page.locator('[data-thread-id="app-2"]')).not_to_have_class(re.compile('is-faded'))
    expect(page.locator('[data-thread-id="app-30"]')).to_have_class(re.compile('is-faded'))
    # Missing token telemetry is not displayed as a measured zero.
    page.get_by_role('button', name='Close agent details', exact=True).click()
    tap(page.locator('[data-thread-id="thread-66"]'))
    expect(page.locator('#swarm-detail')).to_contain_text('Tokens processed: Unavailable')
    page.get_by_role('button', name='Close agent details', exact=True).click()
    tap(page.locator('[data-thread-id="app-2"]'))
    expect(page.locator('#swarm-detail')).to_contain_text('Tokens processed: 500,000 (partial)')
    page.get_by_role('button', name='Close agent details', exact=True).click()
    tap(first)
    # Directional counts can be inspected from the accessible detail list.
    page.locator('.swarm-connection').filter(has_text='→ Billing desk').click()
    expect(page.locator('#swarm-detail')).to_contain_text('18 accepted messages')
    tap(first)
    first.focus()
    page.evaluate('window.__swarmCard = document.activeElement')
    refresh()
    expect(first).to_be_focused()
    assert page.evaluate('window.__swarmCard === document.activeElement')
    # Reparenting a chat changes its type without changing runtime state.
    original = page.request.get(url + 'v1/swarm', headers={'X-Kern-Csrf': '1'}).json()
    reparented = {**original, 'agents': [
        {**agent, 'kind': 'spawned', 'spawned_by_thread_id': 'app-1'}
        if agent['thread_id'] == 'thread-66' else agent for agent in original['agents']
    ]}
    converted = page.locator('[data-thread-id="thread-66"]')
    apply_snapshot(reparented)
    expect(converted.locator('.swarm-type')).to_have_text('Spawned')
    expect(converted.locator('.swarm-bot.bot-spawned')).to_have_count(1)
    expect(converted.locator('.swarm-bot.bot-on-demand')).to_have_count(0)
    apply_snapshot(original)
    expect(converted.locator('.swarm-bot.bot-on-demand')).to_have_count(1)
    # A selected agent that leaves the roster clears its neighbourhood focus.
    tap(converted)
    expect(page.locator('.swarm-card.is-faded').first).to_be_attached()
    apply_snapshot({**original, 'agents': [agent for agent in original['agents'] if agent['thread_id'] != 'thread-66']})
    expect(converted).to_have_count(0)
    expect(page.locator('.swarm-card.is-faded')).to_have_count(0)
    apply_snapshot(original)
    expect(converted).to_have_count(1)
    tap(first)
    # Highlighting preserves every agent on the map.
    page.locator('[data-swarm-filter="needs-human"]').click()
    expect(page.locator('.swarm-card:not(.is-dimmed)')).to_have_count(5)
    expect(page.locator('.swarm-card:visible')).to_have_count(102)
    page.locator('[data-swarm-filter="needs-human"]').click()
    page.locator('#swarm-search').fill('On-demand agent 100')
    expect(page.locator('.swarm-card:not(.is-dimmed)')).to_have_count(1)
    page.locator('#swarm-search').fill('')
    page.get_by_role('button', name='Close agent details', exact=True).click()
    # Sidebar is a duplicate attention view, not a separate home for failed nodes.
    page.locator('.swarm-attention-item').first.click()
    expect(page.locator('#swarm-detail')).to_contain_text('1 pending Kern approval.')
    expect(page.locator('#swarm-detail')).to_contain_text('View approvals')
    expect(page.locator('.swarm-card:visible')).to_have_count(102)
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
    expect(first.locator('.swarm-bot.bot-app')).to_have_count(1)
    expect(first.locator('.badge-failed')).to_have_count(1)
    assert position == first.evaluate('e => [e.style.left, e.style.top]')
    tap(first)
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
    expect(page.locator('.swarm-card')).to_have_count(1003)
    # Fit all keeps its promise for catalogs that need less than the usual 8% minimum.
    page.evaluate("() => { const v = document.querySelector('#swarm-viewport'); v.style.height = '200px'; }")
    page.locator('#swarm-fit').click()
    expect_cards_inside(1003)
    assert camera()[2] < .08
    page.evaluate("() => { document.querySelector('#swarm-viewport').style.height = ''; }")
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
    # Halos, pulses and in-flight layout transitions all stop under reduced motion.
    page.wait_for_function("() => document.querySelector('#swarm-canvas').getAnimations({subtree: true}).length === 0")
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
