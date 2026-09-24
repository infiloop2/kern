"""Recover pending logins without polling hidden cards or absent sessions."""


def run(page, url, log_in, runtime="grok-2", provider="xai"):
    from playwright.sync_api import expect

    page.clock.install()

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
    page.evaluate(refresh)
    assert methods == ["GET"], methods
    page.clock.run_for(11000)
    page.evaluate(refresh)
    assert methods.count("GET") == 2, methods

    # An explicit start works immediately even just after an absent-session read.
    page.evaluate("runtime => import('/admin_ui/health.js').then(module => module.startLogin(runtime))", runtime)
    assert methods.count("POST") == 1, methods
    target = page.locator(f'[data-provider-oauth="{runtime}"]')
    expect(target).to_contain_text(login["login_url"])
    if runtime == "claude_code":
        expect(target.get_by_role("button", name="Submit code")).to_be_visible()
    else:
        expect(target).to_contain_text(login["device_code"])
    page.evaluate(refresh)
    assert methods.count("GET") == 3, methods

    # Leaving the integration suppresses reads even when the code exists.
    page.locator("#panel-network .home-back").click()
    page.clock.run_for(31000)
    page.evaluate(refresh)
    assert methods.count("GET") == 3, methods

    # A reload still recovers a login started earlier (or in another tab).
    page.reload()
    expect(page.locator("#app")).to_be_visible()
    page.locator(f'#panel-home .home-card[data-action="open-home-integration"][data-guide="{provider}"]').click()
    expect(page.locator(f'.integration-details[data-integration-details="{provider}"]')).to_be_visible()
    page.evaluate(refresh)
    expect(target).to_contain_text(login["login_url"])
    assert methods.count("POST") == 1, methods
    page.unroute_all(behavior="wait")
