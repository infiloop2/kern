"""Analytics journey against the ordinary admin shell and a fixed usage report."""
from datetime import datetime, timedelta, timezone


def fixture():
    today = datetime.now(timezone.utc).date()
    days = [(today - timedelta(days=6-i)).isoformat() for i in range(7)]
    fields = ['input_tokens', 'cached_input_tokens', 'cache_write_tokens', 'output_tokens']
    groups = []
    for i, (kind, thread, name, runtime, model) in enumerate([
        ('chats', 'thread-1', 'Research <notes>', 'codex', 'gpt-6-astra'),
        ('apps', 'app-1', 'Project dashboard', 'claude_code', 'claude-fable-5-1'),
        ('schedules', 'schedule-1', 'Daily research', 'grok', 'grok-4.6'),
    ]):
        for day in days:
            tokens = dict(zip(fields, [10000*(i+1), 50000*(i+1), 1000, 2000]))
            groups.append(dict(thread_id=thread, name=name, runtime=runtime, model=model, kind=kind, day=day, turns=2, active=True,
                               tokens=tokens, measured_turns={key:2 for key in fields}))
    groups[0]['tokens']['cache_write_tokens'] = None
    groups[0]['measured_turns']['cache_write_tokens'] = 0
    return dict(since=days[0]+'T00:00:00Z', until=days[-1]+'T12:00:00Z', days=days, groups=groups)


def run(page, url, login):
    from playwright.sync_api import expect
    # The caller blocks service workers, which can bypass Playwright routes.
    # Install interception before the first request to disable browser caching.
    response = {'status': 200, 'json': fixture()}
    page.route('**/v1/analytics', lambda route: route.fulfill(**response))
    login(page, url)
    lifetime = page.locator('#health .lifetime-tokens')
    expect(lifetime.locator('.history-stat-label')).to_have_text(['Input', 'Cached input', 'Cache write', 'Output'])
    expect(lifetime.locator('.history-stat-value')).to_have_text(['1,234,567,890', '9,876,543,210,987,654', '23,456,789', '345,678,901'])
    page.set_viewport_size({'width':390,'height':844})
    expect(lifetime).to_be_visible()
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
    page.set_viewport_size({'width':1280,'height':900})
    page.locator('#tab-analytics').click()
    expect(page).to_have_url(url.rstrip('/') + '/#analytics')
    expect(page.locator('#analytics-status')).to_be_empty()
    expect(page.locator('#analytics-chart .analytics-day')).to_have_count(7)
    assert page.locator('.analytics-segment.chats').first.bounding_box()["height"] > 0
    expect(page.locator('#analytics-threads tr')).to_have_count(3)
    expect(page.locator('#analytics-cards')).to_contain_text('*')
    expect(page.locator('#analytics-threads')).to_contain_text('Research <notes>')
    page.locator('#analytics-threads a[href="#chat/thread-1"]').click()
    expect(page.locator('#panel-workspace-chat')).to_be_visible()
    expect(page.locator('#panel-analytics')).to_be_hidden()
    page.go_back()
    expect(page.locator('#panel-analytics')).to_be_visible()
    page.locator('#analytics-kind').select_option('schedules')
    expect(page.locator('#analytics-threads tr')).to_have_count(1)
    expect(page.locator('#analytics-threads a')).to_have_attribute('href', '#chat/schedule-1')
    page.locator('[data-analytics-thread="schedule-1"]').click()
    expect(page.locator('#analytics-focus')).to_be_visible()
    page.locator('#analytics-clear').click()
    page.locator('#analytics-kind').select_option('all')
    page.locator('[data-analytics-thread="schedule-1"]').click()
    remaining = fixture()
    remaining['groups'] = [row for row in remaining['groups'] if row['kind'] != 'schedules']
    response['json'] = remaining
    with page.expect_response('**/v1/analytics') as refreshed:
        page.locator('#analytics-refresh').click()
    actual = refreshed.value.json()
    assert actual == remaining, f"Unexpected Analytics fixture response: {actual!r}"
    expect(page.locator('#analytics-status')).to_be_empty()
    expect(page.locator('#analytics-focus')).to_be_hidden()
    expect(page.locator('#analytics-threads tr')).to_have_count(2)
    expect(page.locator('#analytics-empty')).to_be_hidden()
    response['json'] = fixture()
    page.reload()
    expect(page.locator('#panel-analytics')).to_be_visible()
    expect(page.locator('#tab-analytics')).to_have_class('tab-button active-tab')
    expect(page.locator('#analytics-threads tr')).to_have_count(3)
    page.set_viewport_size({'width':390,'height':844})
    expect(page.locator('#analytics-cards')).to_be_visible()
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
    page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
    page.wait_for_function('() => window.scrollY > 0')
    # Avoid Playwright scrolling to the button before dispatching the click;
    # opening the panel itself must restore the document position.
    page.locator('#tab-analytics').evaluate('button => button.click()')
    page.wait_for_function('() => window.scrollY === 0')
    expect(page.locator('#analytics-status')).to_be_empty()
    response['json'] = {**fixture(), 'groups':[]}
    page.locator('#analytics-refresh').click()
    expect(page.locator('#analytics-empty')).to_be_visible()
    expect(page.locator('#analytics-detail')).to_be_hidden()
    response.update(status=503, json={'error':{'message':'Usage temporarily unavailable'}})
    page.locator('#analytics-refresh').click()
    expect(page.locator('#analytics-status')).to_contain_text('Usage temporarily unavailable')
    response.update(status=200, json=fixture())
    for row in response['json']['groups']:
        row['active'] = False
    page.route('**/workspace/chat.html', lambda route: route.fulfill(status=503, body='Unavailable'))
    page.reload()
    expect(page.locator('#panel-analytics')).to_be_visible()
    expect(page.locator('#analytics-status')).to_be_empty()
    expect(page.locator('#analytics-threads tr')).to_have_count(3)
    expect(page.locator('#analytics-turns')).to_contain_text('42 turns')
    expect(page.locator('#analytics-threads a')).to_have_count(0)
    for thread_id, status in [('thread-1', 'Archived'), ('app-1', 'Archived'), ('schedule-1', 'Inactive')]:
        row = page.locator('#analytics-threads tr').filter(has=page.locator(f'[data-analytics-thread="{thread_id}"]'))
        expect(row).to_contain_text(status)
    assert page.locator('.analytics-segment.schedules').first.bounding_box()["height"] > 0
    page.locator('[data-analytics-thread="schedule-1"]').click()
    expect(page.locator('#analytics-focus')).to_be_visible()
    expect(page.locator('#analytics-turns')).to_contain_text('14 turns')
