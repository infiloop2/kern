"""Recover pending logins without polling hidden cards or absent sessions."""


def run(page, url, log_in, open_home_integration, runtime="grok-2", provider="xai"):
    from datetime import datetime, timedelta, timezone

    from playwright.sync_api import expect

    # Keep recurring health ticks paused. Move wall time without firing timers,
    # then await one real refresh at a time: run_for() fires async intervals but
    # does not join their fetch/render work.
    clock_time = datetime(2026, 9, 28, tzinfo=timezone.utc)
    # install() starts a running clock. Pausing at its exact start races the
    # next protocol call on slow runners; pause ahead before app timers exist.
    page.clock.install(time=clock_time - timedelta(days=1))
    page.clock.pause_at(clock_time)

    def pending_runtime(route):
        response = route.fetch()
        data = response.json()
        if response.ok and "agent_runtime" in data:
            for record in data["agent_runtime"]["runtimes"]:
                if record["type"] == runtime:
                    record["status"] = "awaiting_login"
        route.fulfill(response=response, json=data)

    def enabled_policy(route):
        response = route.fetch()
        data = response.json()
        if response.ok:
            data["network_controls"]["network_integrations"][provider] = {"enabled": True}
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
    page.route("**/v1/network/policy", enabled_policy)
    page.route("**/v1/health", pending_runtime)
    page.route(f"**/v1/agent-runtime/{route_name}-oauth-login", oauth)
    log_in(page, url)

    refresh = "() => import('/admin_ui/health.js').then(module => module.refreshHealth())"
    page.evaluate(refresh)
    assert methods == [], methods
    open_home_integration(page, provider)
    page.evaluate(refresh)
    assert methods == ["GET"], methods
    page.clock.set_system_time(clock_time + timedelta(seconds=20))
    page.evaluate(refresh)
    assert methods == ["GET"], methods
    page.clock.set_system_time(clock_time + timedelta(seconds=31))
    page.evaluate(refresh)
    assert methods.count("GET") == 2, methods

    # Exercise the operator's actual control, including its event wiring.
    page.locator(f'[data-action="start-login"][data-runtime="{runtime}"]').click()
    target = page.locator(f'[data-provider-oauth="{runtime}"]')
    expect(target).to_contain_text(login["login_url"])
    assert methods.count("POST") == 1, methods
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

    # Successful recovery clears the pause, so the next visible refresh reads
    # again. Every count follows a completed refresh, never an elapsed sleep.
    reads_before_refresh = methods.count("GET")
    page.evaluate(refresh)
    assert methods.count("GET") > reads_before_refresh, methods

    # Leaving the integration suppresses reads even when the code exists.
    reads_before_hiding = methods.count("GET")
    page.locator("#panel-network .home-back").click()
    page.clock.set_system_time(clock_time + timedelta(seconds=62))
    page.evaluate(refresh)
    assert methods.count("GET") == reads_before_hiding, methods

    # A reload still recovers a login started earlier (or in another tab).
    page.reload()
    expect(page.locator("#app")).to_be_visible()
    # The policy render replaces integration cards. Wait for the actual card
    # instead of networkidle, which can hang behind the app's recurring polls.
    expect(page.locator(f'#agent-runtime-integrations > section[data-integration="{provider}"]')).to_be_attached()
    open_home_integration(page, provider)
    page.evaluate(refresh)
    expect(target).to_contain_text(login["login_url"])
    assert methods.count("POST") == 1, methods
    page.unroute_all(behavior="wait")
    print(f"OAuth recovery smoke: {runtime}", flush=True)
