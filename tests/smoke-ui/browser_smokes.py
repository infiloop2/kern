"""Focused saved-website and browser takeover UX; no external website access."""
from __future__ import annotations
import base64
import json
import os
from pathlib import Path
from playwright.sync_api import expect


def run(page, url, log_in, open_home_integration):
    context = page.context
    # Real JPEG bytes so the popup can render and receive scaled coordinates.
    fixture = context.new_page()
    fixture.set_content('<main style="padding:40px;font:24px sans-serif">Website login fixture</main>')
    jpeg = base64.b64encode(fixture.screenshot(type="jpeg")).decode()
    fixture.close()
    sessions = {}
    inputs = []
    fail_cancel = False
    def route(request_route):
        operation = request_route.request.url.rsplit("/", 1)[-1]
        body = request_route.request.post_data_json
        key = body.get("account_id")
        if operation == "list":
            result = {"accounts": list(sessions.values())}
        elif operation == "create":
            assert body == {"provider": "x"}
            result = {"login_id": "login_" + "a" * 32}
        elif operation == "check":
            sessions[key]["state"] = "connected"
            result = sessions[key]
        elif operation == "open":
            if key:
                sessions[key]["state"] = "needs_attention"
            else:
                assert body["login_id"] == "login_" + "a" * 32
            result = {"site": "https://x.com/", "lease": "test-operator-lease", "width": 1100, "height": 760}
        elif operation == "frame":
            assert body["lease"] == "test-operator-lease"
            result = {"image": jpeg, "origin": "https://x.com"}
        elif operation == "input":
            inputs.append(body)
            result = {"ok": True}
        elif operation == "save":
            key = key or "acct_" + "a" * 32
            sessions[key] = {"account_id": key, "provider": "x", "provider_identifier": "example", "state": "connected", "checked_at": "2026-09-28T14:00:00Z"}
            result = sessions[key]
        elif operation == "cancel":
            if fail_cancel:
                request_route.fulfill(status=409, content_type="application/json", body=json.dumps({"error": {"message": "Fixture close failed"}}))
                return
            result = {"ok": True}
        elif operation == "disconnect":
            del sessions[key]
            result = {"ok": True}
        else:
            raise AssertionError(operation)
        request_route.fulfill(status=200, content_type="application/json", body=json.dumps(result))
    context.route("**/v1/browser/*", route)
    log_in(page, url)
    open_home_integration(page, "tool:browser")
    expect(page.locator("#panel-network .guide-action-cost")).to_have_count(0)
    enable = page.locator('[data-action="enable-tool"][data-tool="browser"]')
    if enable.is_enabled():
        with page.expect_response(lambda response: response.url.endswith("/v1/tools/browser/enable")):
            enable.click()
    with page.expect_popup() as popup_info:
        page.get_by_role("button", name="Connect X account", exact=True).click()
    popup = popup_info.value
    expect(popup.locator("#browser-screen")).to_be_visible()
    expect(page.locator("#browser-sessions")).to_contain_text("No saved X accounts yet")
    if directory := os.environ.get("KERN_BROWSER_SCREENSHOTS"):
        Path(directory).mkdir(parents=True, exist_ok=True)
        popup.screenshot(path=str(Path(directory) / "browser-popup-desktop.png"), full_page=True)
        popup.set_viewport_size({"width": 390, "height": 844})
        popup.screenshot(path=str(Path(directory) / "browser-popup-mobile.png"), full_page=True)
        popup.set_viewport_size({"width": 1160, "height": 950})
    popup.locator("#browser-screen").click(position={"x": 80, "y": 80})
    popup.locator("#browser-screen").press("a")
    with popup.expect_response(lambda response: response.url.endswith("/v1/browser/input") and response.request.post_data_json.get("text") == "😀"):
        popup.locator("#browser-screen").dispatch_event("keydown", {"key": "😀"})
    assert any(item.get("text") == "😀" for item in inputs)
    popup.set_viewport_size({"width": 390, "height": 844})
    expect(popup.locator("#browser-text")).to_be_visible()
    popup.locator("#browser-screen").click(position={"x": 80, "y": 80})
    popup.locator("#browser-text").fill("private fixture password")
    popup.get_by_role("button", name="Send text").click()
    expect(popup.locator("#browser-text")).to_have_value("")
    with popup.expect_event("close"):
        popup.get_by_role("button", name="Save and close").click()
    expect(page.locator("#browser-sessions")).to_contain_text("@example")
    page.get_by_role("button", name="Check login").click()
    expect(page.locator("#browser-settings-message")).to_have_text("Login verified for @example.")
    assert any(item.get("text") == "private fixture password" for item in inputs)
    expect(page.locator('[name="automatic"]')).to_have_count(0)
    expect(page.locator('[name="limit"]')).to_have_count(0)
    if directory := os.environ.get("KERN_BROWSER_SCREENSHOTS"):
        page.screenshot(path=str(Path(directory) / "browser-sessions-desktop.png"), full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        page.screenshot(path=str(Path(directory) / "browser-sessions-mobile.png"), full_page=True)
        page.set_viewport_size({"width": 1280, "height": 900})
    # Cancelling a reconnect keeps the account paused until a login check.
    with page.expect_popup() as popup_info:
        page.get_by_role("button", name="Open browser", exact=True).click()
    popup = popup_info.value
    expect(popup.locator("#browser-screen")).to_be_visible()
    fail_cancel = True
    popup.get_by_role("button", name="Close", exact=True).click()
    expect(popup.locator("#browser-message")).to_have_text("Fixture close failed")
    assert not popup.is_closed()
    fail_cancel = False
    with popup.expect_event("close"):
        popup.get_by_role("button", name="Close", exact=True).click()
    expect(page.locator("#browser-sessions")).to_contain_text("Login needs attention; agent actions paused")
    page.get_by_role("button", name="Check login").click()
    expect(page.locator("#browser-sessions")).to_contain_text("Login verified")
    page.once("dialog", lambda dialog: dialog.accept())
    page.get_by_role("button", name="Disconnect", exact=True).click()
    expect(page.locator("#browser-sessions")).to_contain_text("No saved X accounts yet")
    context.unroute("**/v1/browser/*", route)
