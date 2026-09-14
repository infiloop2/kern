"""Analytics journey against the ordinary admin shell and a fixed usage report."""
from datetime import datetime, timedelta, timezone
import re


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
    hermes = page.locator('#runtime-overview .runtime-summary-bedrock')
    expect(hermes).to_contain_text('2.6M')
    expect(hermes).to_have_attribute('aria-label', re.compile(r'2\.6M input tokens \(of which cached: 600\.0k\)'))
    lifetime = page.locator('#health .lifetime-tokens')
    expect(lifetime.locator('.history-stat-label')).to_have_text(['Input tokens', 'Output tokens'])
    expect(lifetime.locator('.history-stat-value')).to_have_text(['9,876,544,469,012,332', '345,678,901'])
    expect(lifetime.locator('.token-cache-detail')).to_have_text('Of which cached: 9,876,543,210,987,654')
    for width in [1280, 390]:
        page.set_viewport_size({'width':width,'height':844})
        expect(lifetime).to_be_visible()
        # Home sections load independently and can move this whole grid.
        # Compare positions in one browser operation, in the same layout.
        values = lifetime.locator('.history-stat-value').evaluate_all(
            '(elements) => elements.map(el => el.getBoundingClientRect().y)')
        assert abs(values[0] - values[1]) < 1, values
        # Very large input totals wrap on phones; compare labels only when
        # both totals fit one line on desktop.
        if width == 1280:
            labels = lifetime.locator('.history-stat-label').evaluate_all(
                '(elements) => elements.map(el => el.getBoundingClientRect().y)')
            assert abs(labels[0] - labels[1]) < 1, labels
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
    for width in [1280, 390]:
        page.set_viewport_size({'width':width,'height':900})
        bounds = page.locator('#analytics-cards').evaluate('''cards => ({
            inputBottom: cards.querySelector('.stat-value > span').getBoundingClientRect().bottom,
            cachedTop: cards.querySelector('.token-cache-detail').getBoundingClientRect().top,
            valueTops: [...cards.querySelectorAll('.stat-value > span')].map(el => el.getBoundingClientRect().top),
        })''')
        assert bounds['cachedTop'] >= bounds['inputBottom'], bounds
        assert abs(bounds['valueTops'][0] - bounds['valueTops'][1]) < 1, bounds
    page.set_viewport_size({'width':1280,'height':900})
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
    # Set up a settled scroll position before testing navigation's reset;
    # a smooth scroll still in flight can otherwise fight the reset.
    page.evaluate('window.scrollTo({top: document.body.scrollHeight, behavior: "instant"})')
    page.wait_for_function('() => window.scrollY > 0')
    # Avoid Playwright scrolling to the button before dispatching the click;
    # opening the panel itself must restore the document position.
    page.locator('#tab-analytics').evaluate('button => button.click()')
    page.wait_for_function('() => window.scrollY === 0')
    expect(page.locator('#analytics-status')).to_be_empty()
    check_token_totals(page, response)
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


def check_token_totals(page, response):
    from playwright.sync_api import expect
    report = fixture()
    fields = ['input_tokens', 'cached_input_tokens', 'cache_write_tokens', 'output_tokens']
    # These are the disjoint buckets delivered by each runtime adapter.
    # All four providers must show 100 input (60 cached) and 20 output.
    report['groups'] = [
        dict(thread_id=f'thread-provider-{i}', name=runtime, runtime=runtime, model='test-model',
             kind='chats', day=report['days'][-1], turns=1, active=False,
             tokens=dict(zip(fields, [30, 60, 10, 20])), measured_turns=dict.fromkeys(fields, 1))
        for i, runtime in enumerate(['codex', 'claude_code', 'grok', 'hermes'])
    ]
    response['json'] = report
    page.locator('#analytics-refresh').click()
    expect(page.locator('#analytics-cards .stat-label')).to_have_text(['Input tokens', 'Output tokens'])
    expect(page.locator('#analytics-cards .stat-value > span')).to_have_text(['400', '80'])
    expect(page.locator('#analytics-cards .token-cache-detail')).to_have_text('Of which cached: 240')
    expect(page.locator('.analytics-table th')).to_have_text(['Thread', 'Turns', 'Input tokens', 'Output tokens'])
    expect(page.locator('#analytics-threads tr')).to_have_count(4)
    for row in page.locator('#analytics-threads tr').all():
        expect(row.locator('td').nth(2).locator(':scope > span')).to_have_text('100')
        expect(row.locator('.token-cache-detail')).to_have_text('Of which cached: 60')
        expect(row.locator('td').nth(3)).to_have_text('20')
    expect(page.locator('#analytics-providers .token-cache-detail')).to_have_text(['Of which cached: 60'] * 4)
    expect(page.locator('#analytics-providers .analytics-provider-counts > div > span')).to_have_text(['100', '20'] * 4)
    expect(page.locator('#analytics-chart')).to_have_attribute('aria-label', '; '.join(
        f'{day}: {480 if day == report["days"][-1] else 0} measured tokens' for day in report['days']))
    # A missing write bucket still contributes the known input and cache read,
    # while explicitly marking that the combined input is incomplete.
    report['groups'][0]['tokens']['cache_write_tokens'] = None
    report['groups'][0]['measured_turns']['cache_write_tokens'] = 0
    page.locator('#analytics-refresh').click()
    expect(page.locator('#analytics-cards .stat-value > span')).to_have_text(['390*', '80'])
    expect(page.locator('#analytics-cards .token-cache-detail')).to_have_text('Of which cached: 240')
    # Unknown usage must not become a measured zero when buckets are combined.
    for value, expected in [(None, '—'), (0, '0')]:
        report['groups'] = [{**report['groups'][0], 'tokens':dict.fromkeys(fields, value),
                             'measured_turns':dict.fromkeys(fields, 0 if value is None else 1)}]
        page.locator('#analytics-refresh').click()
        expect(page.locator('#analytics-cards .stat-value > span')).to_have_text([expected, expected])
        expect(page.locator('#analytics-cards .token-cache-detail')).to_have_text(f'Of which cached: {expected}')
