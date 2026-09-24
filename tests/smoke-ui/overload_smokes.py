"""Real-browser coverage for corroborated failures and overload recovery."""

def run(page, url, log_in):
    from playwright.sync_api import expect

    log_in(page, url)
    expect(page.locator('#app')).to_be_visible()
    page.set_viewport_size({'width': 390, 'height': 844})
    page.clock.install()
    app_top = page.locator('#app').evaluate('(element) => element.getBoundingClientRect().top')

    # Slow background reads leave the host-wide status hidden. Repeated failures
    # across separate areas below show the popover without adding a layout row.
    page.evaluate('''() => {
      const originalFetch = window.fetch.bind(window);
      const held = [];
      window.__releaseSlowReads = () => {
        for (const resolve of held) resolve(new Response('{"ok":true}', {
          status: 200, headers: {'Content-Type': 'application/json'},
        }));
        window.fetch = originalFetch;
      };
      window.fetch = (path, options) => String(path).startsWith('/v1/slow-')
        ? new Promise(resolve => held.push(resolve))
        : String(path).startsWith('/v1/mixed-')
          ? Promise.resolve(new Response('<h1>Busy</h1>', {status: 503}))
          : originalFetch(path, options);
      window.__slowReads = ['a', 'b'].map(name => window.KernHost.api('GET', '/v1/slow-' + name));
    }''')
    page.clock.run_for(10001)
    expect(page.locator('#overload-status')).to_be_hidden()
    page.evaluate('''async () => {
      await Promise.all(['a', 'b'].map(name =>
        window.KernHost.api('GET', '/v1/mixed-' + name).catch(() => {})));
    }''')
    expect(page.locator('#overload-status')).to_be_hidden()
    page.evaluate('''async () => {
      window.__releaseSlowReads();
      await Promise.all(window.__slowReads);
    }''')
    expect(page.locator('#overload-status')).to_be_hidden()
    page.clock.run_for(61000)

    attempts = []
    mode = 'generic'

    def health(route):
        if mode == 'generic' and not any(route.request.url.endswith('/v1/' + name) for name in (
            'workspace/failure-a', 'workspace/failure-b', 'workspace/failure-c',
            'approvals/failure-d', 'workspace/failure-e', 'tools/failure-f', 'failure-action'
        )):
            # Background refreshes must not clear this controlled failure run.
            route.fulfill(status=503, content_type='application/json',
                          body='{"error":{"message":"Section temporarily unavailable"}}')
            return
        attempts.append(route.request.method)
        if mode == 'generic':
            route.fulfill(status=503, content_type='text/html', body='<h1>Busy</h1>', headers={'Retry-After': '5'})
        elif mode == 'busy':
            route.fulfill(status=503, content_type='application/json', body='{"error":{"code":"host_busy"}}', headers={'Retry-After': '5'})
        else:
            route.continue_()

    page.route('**/v1/**', health)
    result = page.evaluate('''async () => {
      try { await window.KernHost.api("GET", "/v1/workspace/failure-a"); }
      catch (error) { return {code: error.code, status: error.status, message: error.message}; }
    }''')
    assert result['code'] == 'host_unavailable' and result['status'] == 503, result
    assert 'JSON' not in result['message'], result
    expect(page.locator('#overload-status')).to_be_hidden()
    page.evaluate('window.KernHost.api("GET", "/v1/workspace/failure-b").catch(() => {})')
    expect(page.locator('#overload-status')).to_be_hidden()
    page.evaluate('window.KernHost.api("GET", "/v1/workspace/failure-c").catch(() => {})')
    expect(page.locator('#overload-status')).to_be_hidden()
    page.evaluate('window.KernHost.api("GET", "/v1/approvals/failure-d").catch(() => {})')
    expect(page.locator('#overload-status')).to_be_hidden()
    page.evaluate('window.KernHost.api("GET", "/v1/workspace/failure-e").catch(() => {})')
    expect(page.locator('#overload-status')).to_be_hidden()
    page.evaluate('window.KernHost.api("GET", "/v1/tools/failure-f").catch(() => {})')
    expect(page.locator('#overload-status')).to_be_visible()
    assert page.locator('#overload-status').evaluate('(element) => element.parentElement.tagName') == 'HEADER'
    assert page.locator('#overload-status').evaluate('(element) => getComputedStyle(element).position') == 'absolute'
    assert abs(page.locator('#app').evaluate('(element) => element.getBoundingClientRect().top') - app_top) < 1
    expect(page.locator('#app')).to_be_visible()
    expect(page.locator('#login')).to_be_hidden()
    before = len(attempts)
    page.evaluate('async () => { try { await window.KernHost.api("POST", "/v1/failure-action", {test: true}); } catch (_) {} }')
    assert attempts[before:].count('POST') == 1, attempts
    page.clock.run_for(59000)
    expect(page.locator('#overload-status')).to_be_visible()
    page.clock.run_for(2000)
    expect(page.locator('#overload-status')).to_be_hidden()
    mode = 'ok'
    page.evaluate('window.KernHost.api("GET", "/v1/health").catch(() => {})')
    expect(page.locator('#overload-status')).to_be_hidden(timeout=10000)
    expect(page.locator('#app')).to_be_visible()

    # An explicit host_busy response still applies a read cooldown, while a
    # deliberate operator action gets one real attempt and is never replayed.
    mode = 'busy'
    page.evaluate('window.KernHost.api("GET", "/v1/health").catch(() => {})')
    expect(page.locator('#overload-status')).to_be_visible()
    before_logout = len(attempts)
    page.locator('#logout-button').click()
    expect(page.locator('#app')).to_be_visible()
    expect(page.locator('#login')).to_be_hidden()
    assert len(attempts) > before_logout and attempts[-1] == 'POST', attempts
    mode = 'ok'
    page.clock.run_for(7000)
    page.locator('#logout-button').click()
    expect(page.locator('#login')).to_be_visible()

    # A transient 503 for the Workspace shell must not poison its mount cache.
    mount_page = page.context.new_page()
    first_shell = True

    def shell(route):
        nonlocal first_shell
        if first_shell:
            first_shell = False
            route.fulfill(status=503, content_type='text/html', body='<h1>Busy</h1>')
        else:
            route.continue_()

    mount_page.route('**/workspace/chat.html', shell)
    log_in(mount_page, url)
    expect(mount_page.locator('#app')).to_be_visible()
    assert not first_shell
    expect(mount_page.locator('#overload-status')).to_be_hidden()
    mount_page.get_by_role('button', name='New chat', exact=True).click()
    expect(mount_page.locator('#panel-workspace-chat')).to_be_visible()

    # A successful HTML shell with a failed stylesheet must also be retryable.
    first_style = True

    def style(route):
        nonlocal first_style
        if first_style:
            first_style = False
            route.fulfill(status=503, content_type='application/json', body='{"error":{"code":"host_busy"}}')
        else:
            route.continue_()

    mount_page.route('**/workspace/chat.css', style)
    mount_page.reload()
    expect(mount_page.locator('#app')).to_be_visible()
    expect(mount_page.locator('#notice')).to_contain_text('Could not load /workspace/chat.css')
    assert not first_style
    mount_page.get_by_role('button', name='New chat', exact=True).click()
    expect(mount_page.locator('#panel-workspace-chat')).to_be_visible()
    mount_page.wait_for_function('''() => {
      const shadow = document.querySelector('#panel-workspace-chat').shadowRoot;
      const links = shadow?.querySelectorAll('link[href="/workspace/chat.css"]');
      return links?.length === 1 && Boolean(links[0].sheet);
    }''')

    # A broken Global stylesheet must not prevent the already mounted Chat UI.
    mount_page.route('**/workspace/global.css', lambda route: route.fulfill(status=404, body='missing'))
    mount_page.reload()
    expect(mount_page.locator('#notice')).to_contain_text('Could not load /workspace/global.css')
    mount_page.get_by_role('button', name='New chat', exact=True).click()
    expect(mount_page.locator('#panel-workspace-chat')).to_be_visible()
    mount_page.unroute('**/workspace/global.css')

    mount_page.close()
    print('Admin overload: correlated failures, stable layout, action recovery passed', flush=True)
