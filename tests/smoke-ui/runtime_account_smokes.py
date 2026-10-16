"""Operator controls for additional accounts and their usage visibility."""


def run(page, url, log_in, open_home_integration):
    from playwright.sync_api import expect

    enabled = {"codex-2": False, "codex-3": False, "grok-2": False}
    providers = {"codex": "openai", "codex-2": "openai", "codex-3": "openai", "grok": "xai", "grok-2": "xai"}
    writes = []
    provider_access = {"openai": True, "xai": True}

    def accounts():
        rows = []
        for runtime, provider in providers.items():
            row = {"agent_runtime": runtime, "provider": provider, "status": "active", "email": f"{runtime}@example.com"}
            if runtime in enabled:
                row["enabled"] = enabled[runtime]
            if row.get("enabled") is False or not provider_access[provider]:
                row["status"] = "deactivated"
            if row["status"] == "active":
                row["codex_usage"] = {"rate_limits": {"primary": {"used_percent": 25, "window_duration_mins": 300}}}
                row["grok_usage"] = {"usage_percent": 25, "period_type": "weekly"}
            rows.append(row)
        return {"accounts": rows}

    def policy(route):
        response = route.fetch()
        body = response.json()
        body["network_controls"]["network_integrations"].update({provider: {"enabled": value} for provider, value in provider_access.items()})
        route.fulfill(response=response, json=body)

    def toggle(route):
        body = route.request.post_data_json
        writes.append(body)
        enabled[body["agent_runtime"]] = body["enabled"]
        route.fulfill(json=accounts())

    def health(route):
        response = route.fetch()
        body = response.json()
        body["agent_runtime"] = {"runtimes": [
            {"type": row["agent_runtime"], "status": row["status"], "active_thread_ids": []}
            for row in accounts()["accounts"]
        ]}
        route.fulfill(response=response, json=body)

    page.route("**/v1/health", health)
    page.route("**/v1/network/policy", policy)
    page.route("**/v1/agent-runtime/account", lambda route: route.fulfill(json=accounts()))
    page.route("**/v1/agent-runtime/refresh", lambda route: route.fulfill(json=accounts()))
    page.route("**/v1/agent-runtime/account-enabled", toggle)
    page.route("**/v1/agent-runtime/status", lambda route: route.fulfill(json={"runtimes": [
        {"type": row["agent_runtime"], "status": row["status"], "active_thread_ids": []}
        for row in accounts()["accounts"]
    ]}))
    log_in(page, url)
    for runtime, provider in (("codex-2", "openai"), ("codex-3", "openai"), ("grok-2", "xai")):
        open_home_integration(page, provider)
        card = page.locator(f'.integration-account[data-runtime="{runtime}"]')
        usage_card = page.locator(f'#runtime-overview-runtimes-panel [data-runtime="{runtime}"]')
        expect(card.locator(".status")).to_have_text("deactivated")
        expect(card).to_contain_text(f"{runtime}@example.com")
        expect(card.locator('[data-action="start-login"]')).to_have_count(0)
        expect(usage_card).to_have_count(0)
        card.get_by_role("button", name="Enable account", exact=True).click()
        expect(card.get_by_role("button", name="Disable account", exact=True)).to_be_visible()
        expect(usage_card).to_have_count(1)
        page.once("dialog", lambda dialog: dialog.accept())
        card.get_by_role("button", name="Disable account", exact=True).click()
        expect(card.locator(".status")).to_have_text("deactivated")
        expect(card).to_contain_text(f"{runtime}@example.com")
        expect(usage_card).to_have_count(0)
        expect(page.locator(f'[data-provider-status="{provider}"]')).to_contain_text("connected")
        expect(page.locator(f'[data-provider-status="{provider}"]')).not_to_contain_text("not connected")
    assert writes == [item for runtime in enabled for item in (
        {"agent_runtime": runtime, "enabled": True}, {"agent_runtime": runtime, "enabled": False},
    )]
    # Primary accounts remain controlled by the overall provider switch.
    expect(page.locator('.integration-account[data-runtime="grok"] [data-action="toggle-runtime-account"]')).to_have_count(0)
    # A provider switch remains the visible explanation when access is off.
    enabled["codex-2"] = True
    provider_access["openai"] = False
    open_home_integration(page, "openai")
    page.evaluate("""async () => {
        await (await import('/admin_ui/network.js')).loadPolicy();
        const health = await import('/admin_ui/health.js');
        await health.refreshProviderAccounts();
        await health.refreshHealth();
    }""")
    expect(page.locator('.integration-row[data-integration="openai"]')).not_to_contain_text("deactivated")
    expect(page.locator('.integration-account[data-runtime="codex-2"]')).to_contain_text("Enable OpenAI")
    print("Runtime accounts: explicit opt-in, saved identity, deactivated status and usage hiding", flush=True)
