"""Persistent privacy notices, refresh recovery, and desktop/mobile geometry."""
import os
from pathlib import Path


def run(page, url, log_in, open_home_integration):
    from playwright.sync_api import expect

    rows = [
        {"agent_runtime": "claude_code", "provider": "claude", "status": "active", "account_id": "claude", "email": "operator@example.com"},
        {"agent_runtime": "grok", "provider": "xai", "status": "active", "account_id": "grok", "email": "operator@example.com"},
        {"agent_runtime": "grok-2", "provider": "xai", "status": "active", "enabled": True, "account_id": "grok2", "email": "work@example.com"},
        # Even an unexpected Codex field must not produce a notice.
        {"agent_runtime": "codex", "provider": "openai", "status": "active", "data_training": {"allowed": True}},
    ]
    for row in rows[:3]:
        row["data_training"] = {"allowed": None, "checked_at": None, "attempted_at": None}
    attempts = []
    mode = "failed"
    held = []
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def accounts(route):
        route.fulfill(json={"accounts": rows})

    def health(route):
        response = route.fetch()
        body = response.json()
        body["agent_runtime"] = {"runtimes": [
            {"type": row["agent_runtime"], "status": row["status"], "active_thread_ids": []} for row in rows
        ]}
        route.fulfill(response=response, json=body)

    def policy(route):
        response = route.fetch()
        body = response.json()
        body["network_controls"]["network_integrations"].update({"claude": {"enabled": True}, "xai": {"enabled": True}})
        route.fulfill(response=response, json=body)

    def refresh(route):
        runtime = route.request.post_data_json["agent_runtime"]
        attempts.append(runtime)
        if mode == "held":
            held.append(route)
        elif mode == "failed":
            route.fulfill(status=502, json={"error": {"message": "Provider unavailable"}})
        else:
            row = next(row for row in rows if row["agent_runtime"] == runtime)
            row["data_training"] = {"allowed": False, "checked_at": "2026-10-16T12:00:00Z", "attempted_at": "2026-10-16T12:00:00Z"}
            accounts(route)

    def reload_accounts():
        page.evaluate("async () => { await (await import('/admin_ui/health.js')).refreshProviderAccounts(); }")

    def capture(name):
        directory = os.environ.get("KERN_UI_SCREENSHOT_DIR")
        if directory:
            target = Path(directory)
            target.mkdir(parents=True, exist_ok=True)
            width = page.viewport_size["width"]
            page.screenshot(path=str(target / f"training-{name}-{width}.png"), full_page=False)

    page.route("**/v1/agent-runtime/account", accounts)
    page.route("**/v1/agent-runtime/refresh", refresh)
    page.route("**/v1/health", health)
    page.route("**/v1/network/policy", policy)
    log_in(page, url)
    banner = page.locator("#training-warning")
    expect(banner).to_be_hidden()
    expect(page.get_by_role("button", name="Refresh", exact=True)).to_have_count(0)
    open_home_integration(page, "claude")
    expect(page.locator('.integration-account[data-runtime="claude_code"]')).to_contain_text("Data training status has not been verified yet.")

    for row in (rows[0], rows[2]):
        row["data_training"] = {"allowed": True, "checked_at": "2026-10-16T10:00:00Z", "attempted_at": "2026-10-16T10:00:00Z"}
    reload_accounts()
    expect(banner).to_be_visible()
    expect(banner).to_contain_text("Claude Code")
    expect(banner).to_contain_text("Grok 2")
    expect(banner).not_to_contain_text("Codex")
    expect(page.locator('.integration-account[data-runtime="claude_code"]')).to_contain_text("/privacy-settings")
    capture("warning")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "horizontal overflow"
    topbar = page.locator('.topbar').bounding_box()
    app = page.locator('#app').bounding_box()
    assert app['y'] >= topbar['y'] + topbar['height'] - 1, (topbar, app)
    # Runtime links lead to the existing provider settings.
    banner.get_by_role("button", name="Grok 2", exact=True).click()
    expect(page.locator('#integration-detail-title')).to_have_text("Grok")
    expect(page.locator('.integration-account[data-runtime="grok-2"]')).to_contain_text("/privacy")

    banner.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.locator("#notice")).to_contain_text("Could not refresh data training settings")
    expect(banner).to_be_visible()
    assert sorted(attempts) == ["claude_code", "grok-2"], attempts
    # Disabled extra accounts never warn; the remaining request shows progress.
    rows[2]["enabled"] = False
    reload_accounts()
    expect(banner).not_to_contain_text("Grok 2")
    mode = "held"
    banner.get_by_role("button", name="Refresh", exact=True).click()
    expect(banner.get_by_role("button", name="Refreshing…", exact=True)).to_be_disabled()
    page.wait_for_function("document.querySelector('[data-action=refresh-training]').disabled")
    assert len(held) == 1
    rows[0]["data_training"] = {"allowed": False, "checked_at": "2026-10-16T12:00:00Z", "attempted_at": "2026-10-16T12:00:00Z"}
    accounts(held.pop())
    expect(banner).to_be_hidden()
    expect(page.get_by_role("button", name="Refresh", exact=True)).to_have_count(0)
    capture("cleared")
    rows[0]["status"] = "error"
    reload_accounts()
    open_home_integration(page, "claude")
    expect(page.locator('.integration-account[data-runtime="claude_code"]')).to_contain_text("Last confirmed setting")
    expect(banner).to_be_hidden()
    rows[0]["status"] = "active"
    # A saved warning survives a failed provider recheck with explicit stale copy.
    rows[0]["data_training"] = {"allowed": True, "checked_at": "2026-10-15T10:00:00Z", "attempted_at": "2026-10-16T12:00:00Z"}
    reload_accounts()
    expect(banner).to_contain_text("Showing the last confirmed result")
    capture("failed")
    page.locator('#logout-button').click()
    expect(page.locator('#login')).to_be_visible()
    expect(banner).to_be_hidden()
    assert not errors, errors
