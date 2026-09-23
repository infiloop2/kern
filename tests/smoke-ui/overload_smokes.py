"""Real-browser coverage for shared overload cooldown and recovery."""

def run(page, url, log_in):
    from playwright.sync_api import expect

    log_in(page, url)
    # Let initial mount settle before controlling time and inducing overload.
    expect(page.locator('#app')).to_be_visible()
    page.clock.install()
    attempts = []
    blocked = True

    def health(route):
        attempts.append(route.request.method)
        if blocked:
            route.fulfill(status=503, content_type='text/html', body='<h1>Busy</h1>', headers={'Retry-After': '5'})
        else:
            route.continue_()

    page.route('**/v1/**', health)
    result = page.evaluate('''async () => {
      try { await window.KernHost.api("GET", "/v1/health"); }
      catch (error) { return {code: error.code, status: error.status, message: error.message}; }
    }''')
    assert result['code'] == 'host_unavailable' and result['status'] == 503, result
    assert 'JSON' not in result['message'], result
    expect(page.locator('#overload-status')).to_be_visible()
    expect(page.locator('#app')).to_be_visible()
    expect(page.locator('#login')).to_be_hidden()
    at_failure = len(attempts)
    page.clock.run_for(4000)
    assert len(attempts) == at_failure, attempts
    # Both background reads and operator actions fail locally during cooldown;
    # no deferred write is saved for replay when the server recovers.
    result = page.evaluate('''async () => {
      try { await window.KernHost.api("POST", "/v1/health", {test: true}); }
      catch (error) { return error.code; }
    }''')
    assert result == 'host_unavailable', result
    assert len(attempts) == at_failure, attempts
    blocked = False
    page.clock.run_for(7000)
    expect(page.locator('#overload-status')).to_be_hidden(timeout=10000)
    assert len(attempts) > at_failure, attempts
    assert 'POST' not in attempts, attempts
    expect(page.locator('#app')).to_be_visible()
    # Logout remains available during cooldown, and a rejected logout keeps
    # the authenticated view visible because its HttpOnly cookie still exists.
    blocked = True
    page.evaluate('window.KernHost.api("GET", "/v1/health").catch(() => {})')
    before_logout = len(attempts)
    page.locator('#logout-button').click()
    expect(page.locator('#app')).to_be_visible()
    expect(page.locator('#login')).to_be_hidden()
    assert len(attempts) > before_logout and attempts[-1] == 'POST', attempts
    blocked = False
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
    mount_page.wait_for_function('() => Boolean(document.querySelector("#overload-status")?.textContent)')
    assert not first_shell
    expect(mount_page.locator('#overload-status')).to_be_hidden(timeout=15000)
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
    print('Admin overload: HTML 503, cooldown, no action replay, recovery passed', flush=True)
