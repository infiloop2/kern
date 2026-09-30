"""Recover pending logins without polling hidden cards or absent sessions."""


def run(page, url, log_in, runtime="grok-2", provider="xai"):
    from datetime import datetime, timezone

    from playwright.sync_api import expect

    # The app has a five-second background health tick. Freeze it between the
    # explicit clock advances below so real elapsed time cannot add a GET to a
    # request-count assertion while Playwright waits for a card or response.
    clock_time = datetime(2026, 9, 28, tzinfo=timezone.utc)
    page.clock.install(time=clock_time)
    page.clock.pause_at(clock_time)

    def pending_runtime(route):
        response = route.fetch()
        data = response.json()
        if response.ok and "agent_runtime" in data:
            for record in data["agent_runtime"]["runtimes"]:
                if record["type"] == runtime:
                    record["status"] = "awaiting_login"
        route.fulfill(response=response, json=data)

    methods = []
    login = {"device_code": "MOCK-CODE", "login_url": "https://example.invalid/login",
             "expires_at": "2026-10-01T00:00:00Z"}
    started = False

    def oauth(route):
        nonlocal started
        methods.append(route.request.method)
        if route.request.method == "POST":
            started = True
        if started:
            route.fulfill(status=200, json=login)
        else:
            route.fulfill(status=404, json={"error": {"message": "login has not been started"}})

    route_name = "claude" if runtime == "claude_code" else runtime
    page.route("**/v1/health", pending_runtime)
    page.route(f"**/v1/agent-runtime/{route_name}-oauth-login", oauth)
    log_in(page, url)

    refresh = "() => import('/admin_ui/health.js').then(module => module.refreshHealth())"
    page.evaluate(refresh)
    assert methods == [], methods
    page.locator(f'#panel-home .home-card[data-action="open-home-integration"][data-guide="{provider}"]').click()
    expect(page.locator(f'.integration-details[data-integration-details="{provider}"]')).to_be_visible()
    page.evaluate(refresh)
    assert methods == ["GET"], methods
    page.clock.run_for(20000)
    page.wait_for_load_state("networkidle")
    page.evaluate(refresh)
    assert methods == ["GET"], methods
    page.clock.run_for(11000)
    page.wait_for_load_state("networkidle")
    page.evaluate(refresh)
    assert methods.count("GET") == 2, methods

    # An explicit start works immediately even just after an absent-session read.
    # Read the card in the same browser task as the completed start. A pending
    # health tick can replace the card before a separate Playwright assertion.
    started_text = page.evaluate("""async runtime => {
      const { startLogin } = await import('/admin_ui/health.js');
      await startLogin(runtime);
      return document.querySelector(`[data-provider-oauth="${runtime}"]`)?.textContent || '';
    }""", runtime)
    assert methods.count("POST") == 1, methods
    assert login["login_url"] in started_text, started_text
    # A refresh must recover the started login after the card is re-rendered.
    reads_before_recovery = methods.count("GET")
    page.evaluate(refresh)
    target = page.locator(f'[data-provider-oauth="{runtime}"]')
    expect(target).to_contain_text(login["login_url"])
    if runtime == "claude_code":
        expect(target.get_by_role("button", name="Submit code")).to_be_visible()
    else:
        expect(target).to_contain_text(login["device_code"])
    assert methods.count("GET") > reads_before_recovery, methods

    # Successful recovery clears the pause, so the next visible refresh can
    # read again. Await that refresh directly: advancing the fake clock does
    # not await tick()'s async work, and a busy tick can skip interval callbacks.
    reads_before_refresh = methods.count("GET")
    page.evaluate(refresh)
    assert methods.count("GET") > reads_before_refresh, methods

    # Leaving the integration suppresses reads even when the code exists.
    reads_before_hiding = methods.count("GET")
    page.locator("#panel-network .home-back").click()
    page.clock.run_for(31000)
    page.wait_for_load_state("networkidle")
    page.evaluate(refresh)
    assert methods.count("GET") == reads_before_hiding, methods

    # A reload still recovers a login started earlier (or in another tab).
    page.reload()
    expect(page.locator("#app")).to_be_visible()
    # The policy render replaces integration cards. Wait for the actual card
    # instead of networkidle, which can hang behind the app's recurring polls.
    expect(page.locator(f'#agent-runtime-integrations > section[data-integration="{provider}"]')).to_be_attached()
    page.locator(f'#panel-home .home-card[data-action="open-home-integration"][data-guide="{provider}"]').click()
    expect(page.locator(f'.integration-details[data-integration-details="{provider}"]')).to_be_visible()
    page.evaluate(refresh)
    expect(target).to_contain_text(login["login_url"])
    assert methods.count("POST") == 1, methods
    page.unroute_all(behavior="wait")
