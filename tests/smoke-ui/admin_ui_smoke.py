#!/usr/bin/env python3
"""Playwright smoke test for the admin UI against the local mock backend."""

from __future__ import annotations

import argparse
from contextlib import closing
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
import urllib.request

import workspace_smokes


REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER = REPO_ROOT / "tests/smoke-ui/run_admin_ui_mock.py"
VERSION = (REPO_ROOT / "VERSION").read_text().strip()
PASSWORD = "dev"
PLAYWRIGHT_CACHE = Path.home() / ".cache/ms-playwright"
CHROMIUM_EXECUTABLE_ENV = "PLAYWRIGHT_CHROMIUM_EXECUTABLE"
IPHONE_VIEWPORT = {"width": 390, "height": 844}


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    port = args.port or free_port()
    server = subprocess.Popen(
        [sys.executable, str(SERVER), "--port", str(port)],
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        wait_for_server(port, server)
        run_browser_smoke(
            f"http://127.0.0.1:{port}/",
            headed=args.headed,
            scope=args.scope,
            webkit=args.webkit,
        )
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait()
    return 0


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=0, help="Local port to use; defaults to a free ephemeral port.")
    parser.add_argument("--headed", action="store_true", help="Run the browser visibly.")
    parser.add_argument(
        "--webkit",
        action="store_true",
        help="Also run the generated Web App worker-startup regression in Playwright WebKit.",
    )
    parser.add_argument(
        "--scope",
        choices=("all", "core", "workspaces"),
        default="all",
        help="Smoke only the host UI core, only workspaces, or both.",
    )
    return parser.parse_args(argv)


def free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_server(port: int, proc: subprocess.Popen[str]) -> None:
    # Interpreter startup plus mock boot on a cold, loaded CI runner does not
    # reliably fit in ten seconds; the loop exits early anyway once the server
    # answers, and on process death immediately.
    deadline = time.time() + 30
    url = f"http://127.0.0.1:{port}/"
    while time.time() < deadline:
        if proc.poll() is not None:
            output = proc.stdout.read() if proc.stdout else ""
            raise RuntimeError(f"mock server exited early with {proc.returncode}\n{output}")
        try:
            with urllib.request.urlopen(url, timeout=0.5) as response:
                if response.status == 200:
                    return
        except OSError:
            pass
        # Outside the handler: a non-200 answer must back off too, not spin.
        time.sleep(0.1)
    raise TimeoutError(f"mock server did not become ready at {url}")


def run_browser_smoke(url: str, *, headed: bool, scope: str, webkit: bool = False) -> None:
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "Playwright is not installed. Run:\n"
            "  python3 -m pip install -r tests/requirements.txt\n"
            "  python3 -m playwright install chromium webkit"
        ) from exc

    with sync_playwright() as playwright:
        executable_path = chromium_executable_path()
        launch_options = {"headless": not headed}
        if executable_path:
            launch_options["executable_path"] = executable_path
        try:
            browser = playwright.chromium.launch(**launch_options)
        except PlaywrightError as exc:
            raise SystemExit(
                "Playwright Chromium is not installed. Run:\n"
                "  python3 -m playwright install chromium\n"
                f"or set {CHROMIUM_EXECUTABLE_ENV} to an existing Chromium/Chrome executable."
            ) from exc
        try:
            if scope in {"all", "core"}:
                desktop = browser.new_context()
                desktop.grant_permissions(["clipboard-read", "clipboard-write"], origin=url.rstrip("/"))
                desktop_page = desktop.new_page()
                report_page_errors(desktop_page, "admin desktop")
                login_error_mapping_smoke(desktop_page, url)
                stale_password_smoke(desktop_page, url)
                desktop_smoke(desktop_page, url)
                desktop.close()

                mobile = browser.new_context(
                    viewport=IPHONE_VIEWPORT, device_scale_factor=3, is_mobile=True, has_touch=True
                )
                mobile_page = mobile.new_page()
                report_page_errors(mobile_page, "admin mobile")
                mobile_smoke(mobile_page, url)
                mobile.close()

            if scope in {"all", "workspaces"}:
                fallback_workspaces = browser.new_context()
                fallback_page = fallback_workspaces.new_page()
                report_page_errors(fallback_page, "workspaces stylesheet fallback")
                log_in(fallback_page, url)
                workspace_smokes.web_app_stylesheet_fallback_smoke(fallback_page)
                fallback_workspaces.close()

                desktop_workspaces = browser.new_context()
                workspace_page = desktop_workspaces.new_page()
                report_page_errors(workspace_page, "workspaces desktop")
                log_in(workspace_page, url)
                workspace_smokes.desktop_smoke(workspace_page)
                desktop_workspaces.close()

                mobile_workspaces = browser.new_context(
                    viewport=IPHONE_VIEWPORT, device_scale_factor=3, is_mobile=True, has_touch=True
                )
                workspace_mobile_page = mobile_workspaces.new_page()
                report_page_errors(workspace_mobile_page, "workspaces mobile")
                log_in(workspace_mobile_page, url)
                workspace_smokes.mobile_smoke(workspace_mobile_page)
                mobile_workspaces.close()
        finally:
            browser.close()
        if webkit and scope in {"all", "workspaces"}:
            run_webkit_workspace_smoke(playwright, url, headed=headed)


def run_webkit_workspace_smoke(playwright, url: str, *, headed: bool) -> None:
    try:
        browser = playwright.webkit.launch(headless=not headed)
    except Exception as exc:
        raise SystemExit(
            "Playwright WebKit is not installed. Run:\n"
            "  python3 -m playwright install webkit"
        ) from exc
    try:
        workspace = browser.new_context()
        workspace_page = workspace.new_page()
        report_page_errors(workspace_page, "WebKit generated Web App")
        log_in(workspace_page, url)
        workspace_smokes.web_app_worker_startup_smoke(workspace_page)
        workspace.close()
    finally:
        browser.close()


def report_page_errors(page, label: str) -> None:
    page.on("pageerror", lambda error: print(f"[{label} page error] {error}", file=sys.stderr, flush=True))


def log_in(page, url: str) -> None:
    from playwright.sync_api import expect

    page.goto(url)
    expect(page.locator("#login")).to_be_visible()
    page.locator("#password").fill(PASSWORD)
    page.get_by_role("button", name="Log in").click()
    expect(page.locator("#app")).to_be_visible()


def login_error_mapping_smoke(page, url: str) -> None:
    """A post-password ceremony failure must not be mislabeled as a bad password."""
    from playwright.sync_api import expect

    recovery_message = "Public hostname changed; reset admin passkeys during reconfigure."

    def reject_passkey_start(route) -> None:
        route.fulfill(
            status=403,
            content_type="application/json",
            body=f'{{"error":{{"message":"{recovery_message}"}}}}',
        )

    page.route("**/v1/login", reject_passkey_start)
    try:
        page.goto(url)
        page.locator("#password").fill(PASSWORD)
        page.get_by_role("button", name="Log in").click()
        expect(page.locator("#login-error")).to_have_text(recovery_message)
        expect(page.locator("#login")).to_be_visible()
    finally:
        page.unroute("**/v1/login", reject_passkey_start)


def stale_password_smoke(page, url: str) -> None:
    from playwright.sync_api import expect

    page.context.add_cookies(
        [{"name": "tc_admin_session", "value": "stale", "url": url}]
    )
    page.goto(url)
    expect(page.locator("#login")).to_be_visible()
    expect(page.locator("#notice")).to_be_hidden()
    expect(page.locator("#notice")).not_to_contain_text("unauthorized")
    page.evaluate(
        "() => import('/admin_ui/helpers.js').then(({ notice }) => notice('Another error', 'error'))"
    )
    expect(page.locator("#notice")).to_have_text("Another error")
    expect(page.locator("#notice")).to_be_visible()
    page.evaluate(
        "() => import('/admin_ui/helpers.js').then(({ notice }) => notice('', ''))"
    )
    page.context.clear_cookies()


def session_activity_smoke(page) -> None:
    """Background calls do not refresh idle expiry; trusted UI activity does."""
    page.evaluate(
        """() => {
          window.__kernRealDateNow = Date.now;
          Date.now = () => window.__kernRealDateNow() + 61_000;
        }"""
    )
    try:
        inactive_path = "/v1/health?activity-smoke=inactive"
        with page.expect_request(lambda request: request.url.endswith(inactive_path)) as captured:
            page.evaluate(
                """path => import('/admin_ui/api.js')
                  .then(({ api }) => api("GET", path))""",
                inactive_path,
            )
        if "x-kern-session-activity" in captured.value.headers:
            raise AssertionError("background admin API call refreshed session activity")

        # Playwright generates trusted input events, unlike element.click() or
        # dispatchEvent(), so this exercises the same path as a real operator.
        page.keyboard.press("Shift")
        active_path = "/v1/health?activity-smoke=active"
        with page.expect_request(lambda request: request.url.endswith(active_path)) as captured:
            page.evaluate(
                """path => import('/admin_ui/api.js')
                  .then(({ api }) => api("GET", path))""",
                active_path,
            )
        if captured.value.headers.get("x-kern-session-activity") != "1":
            raise AssertionError("trusted operator input did not refresh session activity")
    finally:
        page.evaluate("() => { Date.now = window.__kernRealDateNow; delete window.__kernRealDateNow; }")


def open_home_integration(page, guide_id: str) -> None:
    """Return to Home when needed and open one integration card."""
    from playwright.sync_api import expect

    if not page.locator("#panel-home").is_visible():
        back = page.locator(".home-back:visible")
        if back.count():
            back.click()
        else:
            page.get_by_role("button", name="Home", exact=True).click()
    card = page.locator(f"#home-integration-groups [data-guide='{guide_id}']")
    expect(card).to_be_visible()
    card.click()
    expect(page.locator("#panel-network")).to_be_visible()
    expect(page.locator("#integration-detail-title")).not_to_have_text("Integration")


def desktop_smoke(page, url: str) -> None:
    from playwright.sync_api import expect

    log_in(page, url)
    session_activity_smoke(page)
    page.evaluate(
        "() => import('/admin_ui/helpers.js').then(({ notice }) => notice('unauthorized', 'error'))"
    )
    expect(page.locator("#notice")).to_have_text("unauthorized")
    expect(page.locator("#notice")).to_be_visible()
    page.evaluate(
        "() => import('/admin_ui/helpers.js').then(({ notice }) => notice('', ''))"
    )
    expect(page.locator("body")).to_contain_text("kern-mock")
    expect(page.locator("#agent-name")).to_have_text("Host: kern-mock")
    expect(page.locator("#mobile-nav-toggle")).to_be_hidden()
    expect(page.locator("#upgrade-notice")).to_have_count(0)
    expect(page.locator("#panel-home")).to_be_visible()
    home_sidebar_box = page.locator("#sidebar").bounding_box()
    if not home_sidebar_box:
        raise AssertionError("desktop sidebar is not visible on Home")
    expect(page.locator("#health")).to_contain_text("ok")
    version_tile = page.locator("#health .version-tile")
    expect(version_tile).to_contain_text(VERSION)
    expect(version_tile).not_to_contain_text("runtime")
    expect(version_tile).not_to_contain_text("state")
    expect(version_tile).not_to_contain_text("ok")
    expect(version_tile).not_to_contain_text("Upgrade available")
    upgrade_notice = page.locator("#health .home-upgrade-notice")
    expect(upgrade_notice).to_contain_text("Upgrade available: version 99.0.0")
    expect(upgrade_notice).to_contain_text("Use your operator plane to upgrade.")
    expect(page.locator("#health")).to_contain_text("Memory")
    expect(page.locator("#health")).to_contain_text("Admin volume")
    expect(page.locator("#health")).to_contain_text("Agent volume")
    stats = page.locator("#health .stat-history")
    expect(stats).to_have_attribute("aria-label", "Agent stats")
    expect(stats.locator(".stat-history-title")).to_have_text("Stats")
    expect(stats.locator(".history-stat-value")).to_have_text(["24", "1,286", "9,431"])
    expect(stats.locator(".history-stat-label")).to_have_text(
        ["Threads", "User messages", "Agent activity"]
    )
    expect(page.locator("#panel-home").get_by_role("button", name="Reboot host")).to_be_visible()
    expect(page.locator("#home-hero")).to_have_count(0)
    expect(page.get_by_role("button", name="New chat", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="New app", exact=True)).to_be_visible()
    # Home is the single administration destination. Chat and Apps remain
    # first-class workspace sections, without a second diagnostic nav tree.
    headings = page.locator("#sidebar .sidebar-section-title:visible")
    expect(headings).to_have_count(2)
    expect(headings.nth(0)).to_have_text("Chat")
    expect(headings.nth(1)).to_have_text("Apps")
    # Home plus the two first-class global Workspace resources.
    expect(page.locator("#sidebar .tab-button")).to_have_count(3)
    page.get_by_role("button", name="New chat", exact=True).click()
    expect(page.locator("#panel-workspace-chat")).to_be_visible()
    expect(page.locator("#panel-workspace-chat").locator(".chat-app")).to_be_visible()
    expect(page.locator("#panel-home")).to_be_hidden()
    page.get_by_role("button", name="Home", exact=True).click()
    expect(page.locator("#panel-home")).to_be_visible()
    expect(page.locator("#runtime-overview")).to_contain_text("Codex")
    expect(page.locator("#runtime-overview")).to_contain_text("Claude Code")
    expect(page.locator("#runtime-overview")).to_contain_text("Hermes")
    expect(page.locator("#runtime-overview")).to_contain_text("deactivated")
    expect(page.locator("#runtime-overview").get_by_label("Refresh provider status and usage")).to_be_visible()
    expect(page.locator(".topbar-actions").get_by_label("Refresh provider status and usage")).to_have_count(0)
    # The phone-only collapse pill stays out of the way on a wide viewport; the
    # boxes sit inline in the top bar.
    expect(page.locator(".runtime-overview-toggle")).to_be_hidden()
    # Before any login there is no usage: all four rings (5h and weekly for
    # Codex and Claude Code) render the unavailable "--" form rather than 0%.
    # Bedrock billing is reconciliation metadata in the provider details, not
    # a primary toolbar value.
    expect(page.locator("#runtime-overview .usage-ring.unavailable")).to_have_count(4)
    expect(page.locator("#runtime-overview .runtime-summary-bedrock")).to_have_count(1)
    expect(page.locator("#runtime-overview")).to_contain_text("--")
    assert_runtime_usage_type(page, minimum_number_px=8)
    assert_runtime_summaries_do_not_magnify(page)
    expect(page.locator("#panel-home").get_by_text("Agent runtimes")).to_have_count(0)
    expect(page.locator("#panel-home").get_by_text("Provider usage")).to_have_count(0)
    expect(page.get_by_role("button", name="Start Codex login")).to_have_count(0)
    expect(page.get_by_role("button", name="Start Claude login")).to_have_count(0)
    page.locator("#runtime-overview .runtime-summary", has_text="Codex").click()
    expect(page.locator("#panel-network")).to_be_visible()
    disabled_openai_row = page.locator(".integration-row[data-integration]", has_text="OpenAI")
    expect(disabled_openai_row.locator(".integration-details")).to_be_visible()
    title_box = disabled_openai_row.locator(".integration-title").bounding_box()
    account_card_box = disabled_openai_row.locator(".integration-details .detail-card").bounding_box()
    if not title_box or not account_card_box or abs(title_box["x"] - account_card_box["x"]) > 2:
        raise AssertionError("expanded integration content is not aligned with the row title after the chevron")
    expect(disabled_openai_row).not_to_contain_text("No account linked yet")
    expect(disabled_openai_row).not_to_contain_text("deactivated")
    page.locator("#panel-network .home-back").click()
    expect(page.locator("#panel-home")).to_be_visible()

    # Workspace actions that return to Home must also update the route. A
    # reload must not resurrect the integration that was open before the chat.
    page.locator("#runtime-overview .runtime-summary[data-provider='openai']").click()
    page.locator("#chat-nav-items [data-action='open-chat'][data-item-id='thread-1']").click()
    expect(page.locator("#panel-workspace-chat")).to_be_visible()
    page.locator("#panel-workspace-chat #archive-thread").click()
    expect(page.locator("#chat-nav-items")).not_to_contain_text("First chat")
    page.get_by_role("button", name="Home", exact=True).click()
    expect(page.locator("#panel-home")).to_be_visible()
    expect(page).to_have_url(re.compile(r"#home$"))
    page.reload(wait_until="domcontentloaded")
    expect(page.locator("#panel-home")).to_be_visible()
    page.locator('[data-action="show-chat-archive"]').click()
    page.locator("#chat-nav-items [data-action='unarchive-chat'][data-item-id='thread-1']").click()
    page.locator('[data-action="show-chat-archive"]').click()

    with page.expect_response(lambda response: "/v1/events" in response.url):
        page.locator("#panel-home").get_by_role("button", name=re.compile(r"Agent audit")).click()
    expect(page.locator("#panel-agent-log")).to_be_visible()
    expect(page.locator("#panel-agent-log")).to_have_css("opacity", "1")
    expect(page.locator("#events tr").nth(1)).to_be_visible()
    expect(page.locator("#events")).to_contain_text("thread.message")
    expect(page.locator("#events")).to_contain_text("agent_runtime.deactivated")
    expect(page.locator("#agent-page-summary")).to_contain_text("Page 1")
    expect(page.locator("#agent-page-summary")).to_contain_text("live")
    expect(page.locator("#agent-event-pager")).to_contain_text("Next")

    page.locator("#panel-agent-log .home-back").click()
    page.locator("#panel-home").get_by_role("button", name=re.compile(r"Agent workspace")).click()
    expect(page.locator("#panel-files")).to_be_visible()
    expect(page.locator("#file-list th").nth(0)).to_have_text("name")
    expect(page.locator("#file-list th").nth(1)).to_have_text("type")
    expect(page.locator("#file-list")).to_contain_text(".codex")
    page.locator("#file-list").get_by_role("button", name="workspace", exact=True).click()
    expect(page.locator("#file-path")).to_have_value("/workspace")
    page.locator("#file-list").get_by_role("button", name='bad" onclick="window.__xss=1" x=".txt').click()
    expect(page.locator("#file-content")).to_contain_text("quote-bearing mock file")
    if page.evaluate("() => window.__xss") is not None:
        raise AssertionError("quote-bearing filename executed as inline script")
    hostile_name = '<img src=x onerror="window.__fileNameXss=1">.txt'
    page.locator("#file-list").get_by_role("button", name=hostile_name).click()
    expect(page.locator("#file-viewer-title")).to_contain_text(hostile_name)
    expect(page.locator("#file-content")).to_contain_text("<script>window.__fileContentXss=1</script>")
    expect(page.locator("#file-content")).to_contain_text("Mock unsafe-looking file contents")
    # count() takes one sample and returns 0 for a list that is merely still
    # rendering, which would pass these escaping checks without ever looking at
    # the hostile row; to_have_count retries against the settled DOM.
    expect(page.locator("#file-list img")).to_have_count(0)
    expect(page.locator("#file-content img")).to_have_count(0)
    executed = page.evaluate(
        "() => [window.__fileNameXss, window.__fileTypeXss, "
        "window.__fileContentXss, window.__fileContentImageXss]"
    )
    if any(value is not None for value in executed):
        raise AssertionError(f"hostile file explorer value executed script: {executed}")
    page.locator("#file-list").get_by_role("button", name="notes.txt").click()
    expect(page.locator("#file-viewer-title")).to_contain_text("/workspace/notes.txt")
    expect(page.locator("#file-content")).to_contain_text("Mobile audit fixes")
    page.locator("#file-list").get_by_role("button", name="screenshot.png").click()
    expect(page.locator("#file-viewer-title")).to_contain_text("/workspace/screenshot.png")
    expect(page.locator("#file-image")).to_be_visible()
    expect(page.locator("#file-content")).to_be_hidden()
    # Visible does not mean decoded: wait for the load to settle before reading
    # naturalWidth, which is 0 on an image that is still in flight.
    expect(page.locator("#file-image")).to_have_js_property("complete", True)
    if page.locator("#file-image").evaluate("(image) => image.naturalWidth") != 1:
        raise AssertionError("workspace image preview did not decode")

    page.locator("#panel-files .home-back").click()
    with page.expect_response(lambda response: "/v1/network/events" in response.url):
        page.locator("#panel-home").get_by_role("button", name=re.compile(r"Network audit")).click()
    expect(page.locator("#panel-net-log")).to_be_visible()
    expect(page.locator("#panel-net-log")).to_have_css("opacity", "1")
    expect(page.locator("#net-events tr").nth(1)).to_be_visible()
    expect(page.locator("#net-events")).to_contain_text("deploy.acme.dev")
    expect(page.locator("#net-events")).to_contain_text("denied")
    expect(page.locator("#net-events")).to_contain_text("Host not allowed")
    expect(page.locator("#net-page-summary")).to_contain_text("Page 1")
    expect(page.locator("#net-event-pager")).to_contain_text("Next")
    expect(page.locator("#net-events")).to_contain_text("https://api.github.com:443/repos/acme/infra/actions/runs?status=failure")
    page.get_by_role("button", name="Show denied").click()
    expect(page.get_by_role("button", name="Show all")).to_be_visible()
    expect(page.locator("#net-page-summary")).to_contain_text("denied only")
    expect(page.locator("#net-events")).to_contain_text("Host not allowed")
    expect(page.locator("#net-events")).not_to_contain_text("api.openai.com")

    # Home is the only static administration destination in the sidebar. Its
    # grouped cards expose every integration and diagnostic view.
    page.locator("#panel-net-log .home-back").click()
    expect(page.locator("#panel-home")).to_be_visible()
    expect(page.locator("#sidebar .active-tab")).to_have_text("Home")
    expect(page.locator("#sidebar-configuration, #sidebar-audit")).to_have_count(0)
    expect(page.locator("#home-integration-groups .home-integration-group h3")).to_have_text(
        ["AI inference", "Tools", "Manual"]
    )
    integration_cards = page.locator("#home-integration-groups .home-integration-card")
    expect(integration_cards).to_have_count(19)
    expect(integration_cards.locator(".integration-logo")).to_have_count(19)
    expect(integration_cards.locator(".integration-logo[data-logo-source='brand']")).to_have_count(19)
    if integration_cards.locator(".integration-logo:not([aria-hidden='true'])").count():
        raise AssertionError("integration logos must remain decorative inside their labelled card buttons")
    grouped_ordering = page.locator("#home-integration-groups .home-integration-group").evaluate_all("""groups =>
      groups.map(group => [...group.querySelectorAll('.home-integration-card')].map(card => ({
        enabled: card.querySelector('[data-home-integration-status]').classList.contains('active'),
        label: card.querySelector('.home-card-copy strong').textContent,
      })))""")
    for ordering in grouped_ordering:
        assert ordering == sorted(ordering, key=lambda item: (not item["enabled"], item["label"].casefold()))
    expect(page.locator("#panel-home").get_by_role("button", name=re.compile(r"Agent processes"))).to_be_visible()
    expect(page.locator("#panel-home").get_by_role("button", name=re.compile(r"Host diagnostics"))).to_be_visible()

    open_home_integration(page, "github")
    expect(page.locator("#panel-network")).to_be_visible()
    expect(page.locator("#tab-home")).to_have_class(re.compile(r"active-tab"))
    expect(page.locator("#integration-detail-title")).to_have_text("GitHub")
    expect(page.locator("#integration-detail-logo [data-integration-logo='github']")).to_be_visible()
    expect(page.locator("#panel-network .integration-row:visible")).to_have_count(1)
    github_row = page.locator(".integration-row[data-integration='github']")
    expect(github_row).to_be_visible()
    expect(github_row.locator(".integration-details")).to_be_visible()
    expect(page.locator(".connection-guide-entry")).to_have_count(1)
    expect(page.locator("[data-guide-section='github']")).to_contain_text("Exact network boundary")
    expect(page.locator("[data-guide-section='github']")).to_contain_text(
        "results-receiver.actions.githubusercontent.com"
    )
    expect(page.locator("[data-guide-section='github']")).to_contain_text(
        "productionresultssa{0..19}.blob.core.windows.net"
    )
    expect(page.locator("#integration-detail-kind")).to_have_text("Direct network integration")
    expect(page.locator("[data-guide-section='github'] .guide-network-scope")).to_be_visible()
    expect(page.locator("[data-guide-section='github'] details.guide-network-scope")).to_have_count(0)
    expect(page.locator(".guide-network-scope .table-scroll")).to_have_count(0)
    github_guide = page.locator("[data-guide-section='github']")
    expect(github_guide.locator(".guide-capabilities")).not_to_contain_text(".github push approvals")
    expect(github_guide.locator(".guide-data-section")).to_contain_text("exposed to the entire internet")
    expect(github_guide.locator(".guide-data-section")).to_contain_text("holds .github pushes for your approval")
    expect(github_guide.locator(".guide-technical-details")).to_contain_text(
        "Disabling GitHub clears the write-repository list"
    )
    expect(github_guide.locator(".guide-technical-details")).not_to_contain_text(
        "GitHub Actions run arbitrary code"
    )
    open_home_integration(page, "openai")
    expect(page.locator("[data-guide-section='openai']")).to_contain_text("What happens to your data")
    openai_guide = page.locator("[data-guide-section='openai']")
    openai_section_headings = openai_guide.locator("h3").all_text_contents()
    if openai_section_headings.index("What it enables") > openai_section_headings.index("Connection"):
        raise AssertionError("integration capabilities should appear before connection instructions")
    expect(openai_guide.get_by_role("heading", name="Connection", exact=True)).to_have_count(1)
    expect(openai_guide.locator(":scope > .guide-section").nth(1).locator(":scope > p")).to_have_count(0)
    expect(openai_guide).not_to_contain_text("Connection steps")
    expect(openai_guide).to_contain_text("enter the displayed device code to complete sign-in")
    expect(openai_guide).to_contain_text("any host data available to Codex can go to OpenAI")
    expect(openai_guide).to_contain_text("Cached web search keeps the search query and surrounding context within OpenAI")
    expect(openai_guide).to_contain_text("What OpenAI can do with it")
    expect(openai_guide.locator(".guide-policy-point span", has_text="Before connecting")).to_have_count(1)
    expect(openai_guide).to_contain_text("OpenAI may use new conversations")
    expect(openai_guide).to_contain_text("OpenAI says new conversations are not used for model training")
    expect(openai_guide).to_contain_text("abuse or security investigations")
    expect(openai_guide).to_contain_text("How long OpenAI retains it")
    expect(openai_guide).to_contain_text("permanent deletion within 30 days")
    expect(openai_guide).not_to_contain_text("Personal subscription data settings")
    expect(openai_guide).not_to_contain_text("Third-party policies and promises")
    expect(openai_guide).not_to_contain_text("OpenAI login")
    expect(openai_guide.locator(".guide-data-flow")).to_have_count(0)
    expect(openai_guide.get_by_role("link", name="OpenAI Data Controls instructions")).to_have_attribute("href", "https://help.openai.com/en/articles/7730893-chatgpt-data-usage-for-model-training")
    expect(openai_guide.get_by_role("link", name="OpenAI consumer data usage FAQ")).to_have_attribute("href", "https://help.openai.com/en/articles/7039943")
    expect(openai_guide.get_by_role("link", name="OpenAI Codex retention and deletion")).to_have_attribute("href", "https://help.openai.com/en/articles/20001333-how-to-archive-and-delete-codex-chats-in-the-chatgpt-app")
    expect(openai_guide.get_by_role("link", name="OpenAI Privacy Policy")).to_have_attribute("href", "https://openai.com/policies/privacy-policy/")
    expect(openai_guide.locator(":scope > .guide-section > h3")).to_have_text([
        "What it enables",
        "Connection",
        "What happens to your data",
        "Technical notes",
    ])
    expect(openai_guide.get_by_role("heading", name="Protections and sensitive controls", exact=True)).to_have_count(0)
    expect(openai_guide.locator(".guide-protections li").first).to_be_visible()
    expect(openai_guide.get_by_role("heading", name="Exact network boundary", exact=True)).to_have_count(1)
    open_home_integration(page, "claude")
    claude_guide = page.locator("[data-guide-section='claude']")
    expect(claude_guide).to_contain_text("paste the authorization result when prompted")
    expect(claude_guide).to_contain_text("any host data available to Claude Code can go to Anthropic")
    expect(claude_guide).to_contain_text("Web search (optional, off by default)")
    expect(claude_guide).to_contain_text("Anthropic runs the search server-side")
    expect(claude_guide).to_contain_text("Anthropic's search partners")
    expect(claude_guide).to_contain_text("Anthropic may use new personal chats")
    expect(claude_guide).to_contain_text("past and new chats")
    expect(claude_guide).to_contain_text("improve Anthropic's safeguards")
    expect(claude_guide).to_contain_text("anonymized or de-identified data may be kept longer")
    expect(claude_guide).to_contain_text("Turn off Help Improve Claude")
    expect(claude_guide).not_to_contain_text("Third-party policies and promises")
    expect(claude_guide).to_contain_text("How long Anthropic retains it")
    expect(claude_guide).to_contain_text("up to 2 years")
    expect(claude_guide).to_contain_text("up to 7 years")
    expect(claude_guide).to_contain_text("Feedback may be kept for 5 years")
    expect(claude_guide.locator(".guide-data-flow")).to_have_count(0)
    expect(claude_guide.get_by_role("link", name="Anthropic Covered Models retention").first).to_have_attribute("href", "https://support.claude.com/en/articles/15425695-covered-models")
    expect(claude_guide.get_by_role("link", name="Anthropic consumer training policy").first).to_have_attribute("href", "https://privacy.claude.com/en/articles/10023580-is-my-data-used-for-model-training")
    open_home_integration(page, "tool:gmail")
    gmail_guide = page.locator("[data-guide-section='tool:gmail']")
    expect(page.locator("#integration-detail-kind")).to_have_text("Bundled MCP tool")
    expect(gmail_guide.locator(":scope > .guide-section > h3")).to_have_text([
        "What it enables",
        "Connection",
        "What happens to your data",
    ])
    expect(gmail_guide.locator(".guide-technical-details")).to_have_count(0)
    expect(gmail_guide.locator(":scope > .guide-section").nth(1).locator(":scope > p")).to_have_count(0)
    expect(gmail_guide).to_contain_text("send_email")
    expect(gmail_guide).to_contain_text("approval required")
    expect(gmail_guide).to_contain_text("GOOGLE_OAUTH_CLIENT_ID")
    expect(gmail_guide).to_contain_text(f"{url.rstrip('/')}/oauth/callback")
    # The callback URI and config keys render inside the setup step that needs them.
    callback = gmail_guide.locator(".guide-steps .guide-callback")
    expect(callback).to_have_count(1)
    copy_button = callback.get_by_role("button", name="Copy callback URI")
    expect(copy_button).to_be_visible()
    copy_button.click()
    copy_feedback = callback.locator("[data-callback-copy-feedback]")
    expect(copy_feedback).to_have_text("Copied")
    expect(copy_feedback).to_be_visible()
    copied_callback = page.evaluate("navigator.clipboard.readText()")
    assert copied_callback == f"{url.rstrip('/')}/oauth/callback"
    page.locator("#integration-detail-title").click()
    expect(copy_feedback).to_be_hidden()
    expect(gmail_guide.locator(".guide-steps .guide-config")).to_have_count(1)
    expect(gmail_guide).to_contain_text("Gmail search query")
    expect(gmail_guide).to_contain_text("What Google can do with it")
    expect(gmail_guide).to_contain_text("How long Google retains it")
    expect(gmail_guide.locator(".guide-data-summary article")).to_have_count(4)
    expect(gmail_guide.locator(".guide-data-flow")).to_have_count(0)
    expect(gmail_guide).to_contain_text("Google Privacy Policy")
    consent_image = gmail_guide.locator("img[alt*='App name']")
    expect(consent_image).to_have_count(1)
    consent_image.scroll_into_view_if_needed()
    expect(consent_image).to_have_js_property("complete", True)
    if consent_image.evaluate("image => image.naturalWidth") <= 0:
        raise AssertionError("the Google OAuth guide screenshot did not load")

    # Moving straight from one focused detail to another must not change what
    # the explicit Home controls mean. They always open Home; only the browser
    # Back button follows the detail-to-detail history.
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    if page.evaluate("window.scrollY") <= 0:
        raise AssertionError("integration guide did not provide a scrollable detail page")
    page.locator("#runtime-overview .runtime-summary[data-provider='openai']").click()
    expect(page.locator("#integration-detail-title")).to_have_text("OpenAI")
    detail_scroll_y = page.evaluate("window.scrollY")
    if detail_scroll_y > 24:
        raise AssertionError(f"focused Home detail retained scroll offset {detail_scroll_y}")
    page.locator("#panel-network .home-back").click()
    expect(page.locator("#panel-home")).to_be_visible()
    expect(page).to_have_url(re.compile(r"#home$"))
    page.go_back()
    expect(page.locator("#integration-detail-title")).to_have_text("OpenAI")
    page.get_by_role("button", name="Home", exact=True).click()
    expect(page.locator("#panel-home")).to_be_visible()

    open_home_integration(page, "custom_domain")
    custom_guide = page.locator("[data-guide-section='custom_domain']")
    expect(custom_guide).to_contain_text("complete HTTPS request")
    expect(custom_guide).to_contain_text("What the third party can do with it")
    expect(custom_guide).to_contain_text("How long the third party retains it")
    expect(custom_guide.locator(".guide-data-summary article")).to_have_count(4)
    for guide_label in (
        "OpenAI",
        "Claude",
        "GitHub",
        "Python packages",
        "NPM Packages",
        "Brave Search",
        "Gmail",
        "Google Calendar",
        "Custom Domain Access",
    ):
        guide_id = {
            "OpenAI": "openai", "Claude": "claude", "GitHub": "github",
            "Python packages": "python_packages", "NPM Packages": "npm_packages",
            "Brave Search": "tool:brave_search", "Gmail": "tool:gmail",
            "Google Calendar": "tool:google_calendar", "Custom Domain Access": "custom_domain",
        }[guide_label]
        open_home_integration(page, guide_id)
        current_guide = page.locator(".connection-guide-entry")
        expect(current_guide.get_by_role("heading", name="What happens to your data", exact=True)).to_have_count(1)
        expect(current_guide.locator(".guide-data-summary article")).to_have_count(4)
    open_home_integration(page, "npm_packages")
    expect(page.locator("[data-guide-section='npm_packages']")).not_to_contain_text("Review packages before use")
    expect(page.locator(".connection-guide-entry")).to_have_count(1)
    open_home_integration(page, "github")
    github_row = page.locator(".integration-row[data-integration='github']")
    # Credentials can be staged before the integration is enabled.
    expect(page.locator("#github-credential-status")).to_contain_text("No credential configured")

    # Repository controls share the GitHub detail page and activate only when
    # the integration is enabled.
    expect(page.locator("#github-expansion")).to_be_hidden()
    expect(github_row).to_contain_text("disabled")
    expect(github_row).to_contain_text("All GitHub reads are allowed")
    expect(github_row).to_contain_text("scoped to the write repositories")
    expect(page.locator("#github-expansion")).to_be_hidden()
    expect(page.locator("#github-repo")).to_be_disabled()

    # Each integration enables on its own and applies immediately.
    open_home_integration(page, "openai")
    openai_row = page.locator(".integration-row[data-integration='openai']")
    openai_row.get_by_role("button", name="Enable", exact=True).click()
    expect(page.locator("[data-integration-message='openai']")).to_contain_text("OpenAI enabled")
    expect(openai_row).to_contain_text("enabled")
    expect(openai_row).to_contain_text("No account linked yet")
    expect(openai_row.get_by_role("button", name="Disconnect")).to_have_count(0)
    open_home_integration(page, "claude")
    claude_row = page.locator(".integration-row[data-integration='claude']")
    claude_row.get_by_role("button", name="Enable", exact=True).click()
    expect(page.locator("[data-integration-message='claude']")).to_contain_text("Claude enabled")
    expect(claude_row).to_contain_text("No account linked yet")
    # Bedrock is one provider row and one validated credential, region, account,
    # and billing record for Hermes.
    open_home_integration(page, "bedrock")
    bedrock_row = page.locator(".integration-row[data-integration='bedrock']")
    expect(bedrock_row).to_have_count(1)
    expect(page.locator(".integration-row[data-integration='hermes']")).to_have_count(0)
    expect(bedrock_row).to_contain_text("No AWS credential stored yet")
    expect(bedrock_row).to_contain_text("required IAM policy")
    page.locator("#bedrock-access-key-id-bedrock").fill("AKIAMOCKOPERATOR0001")
    page.locator("#bedrock-secret-access-key-bedrock").fill("S" * 40)
    page.locator("#bedrock-region-bedrock").select_option("us-west-2")
    bedrock_row.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("[data-integration-message='bedrock']")).to_contain_text(
        "AWS credential accepted."
    )
    expect(page.locator("#bedrock-secret-access-key-bedrock")).to_have_value("")
    expect(bedrock_row).to_contain_text("AKIAMOCKOPERATOR0001")
    bedrock_row.get_by_role("button", name="Enable", exact=True).click()
    expect(page.locator("[data-integration-message='bedrock']")).to_contain_text(
        "Hermes (AWS Bedrock) enabled"
    )
    expect(bedrock_row).to_contain_text("enabled")
    expect(bedrock_row).to_contain_text("arn:aws:iam::123456789012:user/kern-bedrock")
    # One live month-to-date estimate is metered from Hermes's Bedrock responses.
    expect(bedrock_row.locator(".bedrock-usage-box")).to_have_count(1)
    expect(bedrock_row.locator(".bedrock-usage-box")).to_contain_text("MTD est. $12.75")
    expect(bedrock_row.locator(".bedrock-usage-box")).to_contain_text("1.8M in")
    expect(bedrock_row.locator(".bedrock-usage-box")).to_contain_text("2 of 210 requests unmetered")
    expect(bedrock_row.locator(".bedrock-usage-box")).not_to_contain_text("Hermes")
    expect(bedrock_row).not_to_contain_text("Cost Explorer")
    expect(page.locator("#bedrock-region-bedrock")).to_have_value("us-west-2")
    # Hermes has one live month-to-date estimate, labelled "MTD est." to flag
    # it is an estimate rather than the authoritative AWS bill.
    hermes_box = page.locator("#runtime-overview .runtime-summary", has_text="Hermes")
    expect(hermes_box).to_contain_text("MTD est.")
    expect(hermes_box).to_contain_text("$12.75")
    expect(hermes_box).to_contain_text("1.8M")
    expect(page.locator("#runtime-overview .bedrock-toolbar-lag")).to_have_count(0)
    expect(hermes_box).to_contain_text("active")
    expect(hermes_box.locator(".runtime-running-badge")).to_have_count(0)
    counter_turn_response = page.request.post(
        f"{url.rstrip('/')}/v1/threads/thread-toolbar-hermes-counter/messages",
        headers={"X-Kern-Csrf": "1"},
        data={
            "agent_runtime": "hermes",
            "model": "deepseek.v3.2",
            "effort": "high",
            "message": "Exercise the Hermes toolbar running counter.",
        },
    )
    if not counter_turn_response.ok:
        raise AssertionError(
            f"could not start Hermes toolbar counter turn: {counter_turn_response.status} "
            f"{counter_turn_response.text()}"
        )
    counter_turn = counter_turn_response.json()
    if counter_turn.get("status") != "accepted" or counter_turn.get("thread", {}).get("status") != "running":
        raise AssertionError(f"unexpected thread message response: {counter_turn}")
    # Same five-second toolbar poll as the clearing assertion below: budget a
    # full cycle plus render, not most of one.
    expect(hermes_box).to_contain_text("1 running", timeout=12000)
    stopped = page.request.post(
        f"{url.rstrip('/')}/v1/threads/thread-toolbar-hermes-counter/stop",
        headers={"X-Kern-Csrf": "1"},
    )
    if not stopped.ok:
        raise AssertionError(f"could not stop Hermes toolbar counter turn: {stopped.status} {stopped.text()}")
    # The toolbar refreshes on a five-second poll, after the other dashboard
    # sections in the same tick. Allow a full poll cycle under CI load.
    expect(hermes_box.locator(".runtime-running-badge")).to_have_count(0, timeout=12000)
    # Disconnect and reconnect operate on the one Bedrock resource.
    page.once("dialog", lambda dialog: dialog.accept())
    bedrock_row.get_by_role("button", name="Disconnect AWS", exact=True).click()
    expect(page.locator("[data-integration-message='bedrock']")).to_contain_text(
        "AWS Bedrock account disconnected"
    )
    expect(bedrock_row).to_contain_text("No AWS credential stored yet")
    expect(bedrock_row.locator(".provider-error")).to_have_count(0)
    expect(bedrock_row.get_by_role("button", name="Disconnect AWS", exact=True)).to_have_count(0)
    expect(hermes_box).to_contain_text("awaiting login")
    page.locator("#bedrock-access-key-id-bedrock").fill("AKIAMOCKOPERATOR0003")
    page.locator("#bedrock-secret-access-key-bedrock").fill("S" * 40)
    page.locator("#bedrock-region-bedrock").select_option("us-east-2")
    bedrock_row.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("[data-integration-message='bedrock']")).to_contain_text(
        "AWS credential accepted."
    )
    expect(bedrock_row).to_contain_text("arn:aws:iam::123456789012:user/kern-bedrock")
    open_home_integration(page, "github")
    github_row = page.locator(".integration-row[data-integration='github']")
    github_row.get_by_role("button", name="Enable", exact=True).click()
    github_message = github_row.locator("[data-integration-message='github']")
    expect(github_message).to_contain_text("GitHub enabled")
    expect(page.locator("#github-require-approval-status")).to_contain_text("held for approval")
    approval_enable = page.locator("[data-action='enable-github-require-approval']")
    approval_disable = page.locator("[data-action='disable-github-require-approval']")
    expect(approval_enable).to_be_disabled()
    expect(approval_enable).to_have_text("Enabled")
    expect(approval_disable).to_be_enabled()
    expect(approval_disable).to_have_text("Disable")
    # Enabling expands the row with the repository controls.
    expect(page.locator("#github-expansion")).to_be_visible()
    github_access_notice = page.locator("#github-expansion .access-notice")
    expect(github_access_notice.locator(".access-notice-icon")).to_be_visible()
    expect(github_access_notice).to_contain_text("Additional integrations connected to your repositories")
    expect(github_access_notice).to_contain_text("Vercel")
    expect(github_access_notice).to_contain_text("may expose it publicly")
    expect(page.locator("#github-repos")).to_contain_text("No write repositories configured")
    page.locator("#github-repo").fill("infiloop2/kern")
    page.get_by_role("button", name="Add write repository", exact=True).click()
    expect(github_message).to_contain_text("Write repository infiloop2/kern saved")
    repo_entry = page.locator(".repo-entry", has_text="infiloop2/kern")
    expect(repo_entry).to_contain_text("infiloop2/kern")
    expect(repo_entry).not_to_contain_text("read-write")
    expect(repo_entry).to_contain_text("1 warning")
    expect(repo_entry).not_to_contain_text("Repository audit could not verify this write target")
    repo_entry.get_by_label("Toggle repository audit details for infiloop2/kern").click()
    expect(repo_entry).to_contain_text("Repository audit could not verify this write target")
    expect(repo_entry).to_contain_text("no credential token to audit with")
    # A rejected repository input reports an error-styled message and keeps
    # the confirmation styling for ordinary feedback.
    page.locator("#github-repo").fill("not a repo")
    page.get_by_role("button", name="Add write repository", exact=True).click()
    expect(github_message).to_have_class(re.compile(r"inline-message.*error"))
    expect(github_message).to_contain_text("owner/repo")
    # The .github approval gate is on from the first GitHub enable. The mock
    # simulates the agent pushing after its first write repository is added.
    expect(page.locator("#github-require-approval-status")).to_contain_text("held for approval")
    pending_push = page.locator("#github-pending-pushes .pending-push")
    expect(pending_push).to_contain_text("infiloop2/kern")
    expect(pending_push).to_contain_text(".github/workflows/deploy.yml")
    expect(pending_push).to_contain_text("pending")
    page.once("dialog", lambda dialog: dialog.accept())
    pending_push.get_by_role("button", name="Approve & push").click()
    expect(github_message).to_contain_text("approved and pushed")
    expect(page.locator("#github-pending-pushes")).to_have_text("")
    page.locator("[data-action='disable-github-require-approval']").click()
    expect(github_message).to_contain_text(".github push approval disabled")
    expect(approval_enable).to_be_enabled()
    expect(approval_enable).to_have_text("Enable")
    expect(approval_disable).to_be_disabled()
    expect(approval_disable).to_have_text("Disabled")
    # Disabling asks for confirmation and applies immediately.
    open_home_integration(page, "npm_packages")
    npm_row = page.locator(".integration-row[data-integration='npm_packages']")
    npm_row.get_by_role("button", name="Enable", exact=True).click()
    expect(page.locator("[data-integration-message='npm_packages']")).to_contain_text("NPM Packages enabled")
    page.once("dialog", lambda dialog: dialog.accept())
    npm_row.get_by_role("button", name="Disable", exact=True).click()
    expect(page.locator("[data-integration-message='npm_packages']")).to_contain_text("NPM Packages disabled")
    expect(npm_row).to_contain_text("disabled")

    # Custom domain controls get the same focused integration page.
    open_home_integration(page, "custom_domain")
    expect(page.locator("#domain-rule-count")).to_have_text("0 domains enabled")
    expect(page.locator("#domain-rule-count")).to_have_class("status disabled")
    expect(page.locator("#custom-domain-details")).to_be_visible()
    expect(page.locator("#domain-rules")).to_contain_text("No custom domains configured")
    page.locator("#policy-domain").fill("api.example.com")
    page.locator("#policy-methods").fill("GET,HEAD")
    page.locator("#policy-allow-websocket").check()
    page.get_by_role("button", name="Add domain rule", exact=True).click()
    domain_message = page.locator("[data-integration-message='custom_domain']")
    expect(domain_message).to_contain_text("Domain rule for api.example.com saved")
    expect(page.locator("#domain-rule-count")).to_have_text("1 domain enabled")
    expect(page.locator("#domain-rule-count")).to_have_class("status enabled")
    expect(page.locator("#domain-rules")).to_contain_text("api.example.com")
    expect(page.locator("#domain-rules")).to_contain_text("GET, HEAD")
    expect(page.locator("#domain-rules")).to_contain_text("allowed")
    page.once("dialog", lambda dialog: dialog.accept())
    page.locator("#domain-rules").get_by_role("button", name="Remove", exact=True).click()
    expect(domain_message).to_contain_text("Domain rule for api.example.com removed")
    expect(page.locator("#domain-rule-count")).to_have_text("0 domains enabled")
    expect(page.locator("#domain-rule-count")).to_have_class("status disabled")
    expect(page.locator("#domain-rules")).to_contain_text("No custom domains configured")

    open_home_integration(page, "github")
    github_row = page.locator(".integration-row[data-integration='github']")
    github_message = github_row.locator("[data-integration-message='github']")
    # The credential card: no Clear button until something is configured,
    # then the configured type is stated and Clear appears next to it.
    expect(page.locator("#github-credential-status")).to_contain_text("No credential configured")
    expect(page.locator("#github-credential-form-label")).to_have_text("Set a new credential")
    expect(page.locator("#github-credential-clear")).to_be_hidden()
    page.locator("#github-token").fill("github_pat_mock")
    page.get_by_role("button", name="Set credential").click()
    expect(github_message).to_contain_text("GitHub credential stored")
    expect(page.locator("#github-credential-status")).to_contain_text("Configured: fine-grained token (PAT)")
    expect(page.locator("#github-credential-form-label")).to_have_text("Replace credential")
    # PAT mode surfaces the validation status just like app mode does.
    expect(page.locator("#github-credential-status")).to_contain_text("validation: not_checked")
    # Per-repository audits render next to each repository once a credential
    # is stored, and the re-check action refreshes them.
    expect(page.locator("#github-repos")).to_contain_text("infiloop2/kern")
    expect(page.locator("#github-repos")).to_contain_text("GitHub Actions workflows")
    page.get_by_role("button", name="Re-check repository audits").click()
    expect(github_message).to_contain_text("Repository audits refreshed")
    page.get_by_role("button", name="Clear credential").click()
    expect(page.locator("#github-credential-status")).to_contain_text("No credential configured")
    expect(page.locator("#github-credential-form-label")).to_have_text("Set a new credential")
    expect(page.locator("#github-credential-clear")).to_be_hidden()

    page.locator("#github-credential-mode").select_option("app")
    expect(page.locator("#github-app-fields")).to_be_visible()
    page.locator("#github-app-id").fill("12345")
    page.locator("#github-app-installation-id").fill("67890")
    page.locator("#github-app-private-key").fill("-----BEGIN RSA PRIVATE KEY-----\nMIIB\n-----END RSA PRIVATE KEY-----")
    page.get_by_role("button", name="Set credential").click()
    expect(github_message).to_contain_text("GitHub credential stored")
    expect(page.locator("#github-credential-status")).to_contain_text("Configured: GitHub App 12345, installation 67890")
    expect(page.locator("#github-credential-clear")).to_be_visible()
    page.get_by_role("button", name="Clear credential").click()
    expect(page.locator("#github-credential-status")).to_contain_text("No credential configured")

    tools_smoke(page, url)

    # The tool OAuth callback reloads the page; return through Home to the
    # provider detail pages before exercising provider login.
    open_home_integration(page, "openai")
    openai_row = page.locator(".integration-row[data-integration='openai']")
    expect(openai_row.get_by_role("button", name="Start Codex login")).to_be_visible()
    expect(openai_row.get_by_role("button", name="Start Codex login")).to_be_enabled()
    page.get_by_role("button", name="Start Codex login").click()
    expect(openai_row.locator(".provider-oauth")).to_contain_text("MOCK-CODEX")
    # The mock completes the device login out of band a couple of seconds
    # after it starts, like the real flow; the dashboard notices on its own
    # 5-second poll, so allow two poll rounds for the flip to render.
    with page.expect_response(
        lambda response: "/v1/agent-runtime/account" in response.url and response.request.method == "GET",
        # Nothing here triggers the request: it is purely the dashboard's own
        # 5s poll noticing the out-of-band login. Two rounds is 10s, so 8s was
        # under the budget this wait was written for.
        timeout=12000,
    ):
        pass
    expect(openai_row.get_by_role("button", name="Start Codex login")).to_have_count(0, timeout=12000)
    expect(openai_row).to_contain_text("connected: akshay@infiloop.io")
    expect(openai_row).to_contain_text("Connected account")
    expect(openai_row.locator(".connection-summary")).to_be_visible()
    expect(openai_row.locator(".connection-summary b")).to_have_count(0)
    expect(openai_row.get_by_role("button", name="Disconnect")).to_be_visible()
    codex_summary = page.locator("#runtime-overview .runtime-summary", has_text="Codex")
    expect(codex_summary).to_contain_text("active")
    expect(codex_summary.locator(".usage-ring text")).to_have_text(["8", "84"])
    # A healthy 5h window (no threshold class) beside a near-full weekly window
    # (warning), so the mock exercises both ring states at once.
    expect(codex_summary.locator(".usage-ring").nth(0)).not_to_have_class(re.compile(r"usage-(warning|critical)"))
    expect(codex_summary.locator(".usage-ring").nth(1)).to_have_class(re.compile(r"usage-warning"))
    # The reset countdown shares the single window-label line, so a summary
    # with countdowns is exactly as tall as one without.
    expect(codex_summary.locator(".usage-window")).to_have_text(["5h · 40m", "wk · 6d"])
    # Every runtime box links to its provider's Home integration page in any
    # state — active included.
    expect(codex_summary).to_have_attribute("data-action", "open-provider")
    expect(codex_summary).to_have_attribute("data-provider", "openai")

    page.locator("#panel-network .home-back").click()
    with page.expect_response(lambda response: "/v1/agent-processes" in response.url):
        page.locator("#panel-home").get_by_role("button", name=re.compile(r"Agent processes")).click()
    expect(page.locator("#panel-processes")).to_be_visible()
    expect(page.locator("#processes")).to_contain_text("codex")
    expect(page.locator("#processes")).to_contain_text("app-server")
    expect(page.locator("#processes")).not_to_contain_text("scope")
    expect(page.locator("#processes")).not_to_contain_text("run-mock-")
    open_home_integration(page, "claude")
    claude_row = page.locator(".integration-row[data-integration='claude']")
    expect(claude_row.get_by_role("button", name="Start Claude Code login")).to_be_visible()
    expect(claude_row.get_by_role("button", name="Start Claude Code login")).to_be_enabled()
    claude_row.get_by_role("button", name="Start Claude Code login").click()
    expect(claude_row.locator(".provider-oauth")).to_contain_text("Claude Code login")
    # A reload must not lose the pending login: the next health poll re-shows
    # the login card inside the expanded provider card without starting a new
    # login. Clear history.state first so this also exercises a copied detail
    # URL opened in a new browser tab, where only the hash route survives.
    page.evaluate("history.replaceState(null, '', location.href)")
    page.reload(wait_until="domcontentloaded")
    expect(page.locator("#panel-network")).to_be_visible()
    expect(page.locator("#integration-detail-title")).to_have_text("Claude")
    claude_row = page.locator(".integration-row[data-integration='claude']")
    expect(claude_row.locator(".provider-oauth")).to_contain_text("Claude Code login", timeout=12000)
    page.once("dialog", lambda dialog: dialog.accept("mock-code"))
    page.get_by_role("button", name="Submit code").click()
    expect(claude_row.locator("[data-integration-message='claude']")).to_contain_text("Claude Code login submitted")
    with page.expect_response(
        lambda response: "/v1/agent-runtime/account" in response.url and response.request.method == "GET",
        # Nothing here triggers the request: it is purely the dashboard's own
        # 5s poll noticing the out-of-band login. Two rounds is 10s, so 8s was
        # under the budget this wait was written for.
        timeout=12000,
    ):
        pass
    expect(claude_row.get_by_role("button", name="Start Claude Code login")).to_have_count(0)
    expect(claude_row).to_contain_text("connected: claude@example.invalid")
    expect(claude_row).to_contain_text("Connected account")
    expect(claude_row.locator(".connection-summary b")).to_have_count(0)
    expect(claude_row.get_by_role("button", name="Disconnect")).to_be_visible()
    claude_summary = page.locator("#runtime-overview .runtime-summary", has_text="Claude Code")
    # The Fable weekly window rides along as a third ring labeled "fable".
    expect(claude_summary.locator(".usage-ring text")).to_have_text(["97", "46", "88"])
    # Critical (session), healthy (weekly), and warning (Fable week) side by
    # side: all three ring thresholds in one chip.
    expect(claude_summary.locator(".usage-ring").nth(0)).to_have_class(re.compile(r"usage-critical"))
    expect(claude_summary.locator(".usage-ring").nth(1)).not_to_have_class(re.compile(r"usage-(warning|critical)"))
    expect(claude_summary.locator(".usage-ring").nth(2)).to_have_class(re.compile(r"usage-warning"))
    expect(claude_summary.locator(".usage-window")).to_have_text(["5h · 2h", "wk · 5d", "fable · 5d"])
    expect(claude_summary).to_have_attribute("data-action", "open-provider")
    expect(claude_summary).to_have_attribute("data-provider", "claude")
    with page.expect_response(lambda response: "/v1/agent-runtime/refresh" in response.url):
        page.locator("#runtime-overview").get_by_label("Refresh provider status and usage").click()
    expect(claude_summary.locator(".usage-ring text")).to_have_text(["63", "46", "88"])


def assert_runtime_usage_type(page, minimum_number_px: float) -> None:
    """Quota and Hermes values share readable type in the fixed-height rings."""
    from playwright.sync_api import expect

    quota_number = page.locator("#runtime-overview .usage-ring text").first
    quota_label = page.locator("#runtime-overview .usage-window").first
    hermes_number = page.locator("#runtime-overview .runtime-summary-bedrock .runtime-stat-value").first
    hermes_label = page.locator("#runtime-overview .runtime-summary-bedrock .runtime-stat-label").first
    quota_size = quota_number.evaluate("element => getComputedStyle(element).fontSize")
    hermes_size = hermes_number.evaluate("element => getComputedStyle(element).fontSize")
    if quota_size != hermes_size:
        raise AssertionError(f"quota number size {quota_size} does not match Hermes {hermes_size}")
    if float(quota_size.removesuffix("px")) < minimum_number_px:
        raise AssertionError(f"runtime usage number is too small: {quota_size}")
    quota_label_size = quota_label.evaluate("element => getComputedStyle(element).fontSize")
    hermes_label_size = hermes_label.evaluate("element => getComputedStyle(element).fontSize")
    if quota_label_size != hermes_label_size:
        raise AssertionError(
            f"quota label size {quota_label_size} does not match Hermes {hermes_label_size}"
        )
    expect(page.locator("#runtime-overview .usage-ring svg").first).to_have_css("height", "20px")


def assert_runtime_summaries_do_not_magnify(page) -> None:
    """Provider summaries retain their size when a desktop pointer hovers."""
    from playwright.sync_api import expect

    for runtime in ("Codex", "Claude Code", "Hermes"):
        summary = page.locator("#runtime-overview .runtime-summary", has_text=runtime)
        expect(summary).to_have_css("transform", "none")
        summary.hover()
        expect(summary).to_have_css("transform", "none")
        page.mouse.move(0, 0)


def tools_smoke(page, url: str) -> None:
    """Every bundled tool is discoverable from Home and opens one focused,
    fully configured detail page with its manifest-backed guide."""
    from playwright.sync_api import expect

    if not page.locator("#panel-home").is_visible():
        page.locator(".home-back:visible").click()
    expected_tool_ids = sorted(
        path.parent.name
        for path in (REPO_ROOT / "host/tools").glob("*/__init__.py")
        if path.parent.name != "shared"
    )
    tool_cards = page.locator("#home-integration-groups [data-guide^='tool:']")
    expect(tool_cards).to_have_count(len(expected_tool_ids))
    rendered_ids = sorted(tool_cards.evaluate_all(
        "cards => cards.map(card => card.dataset.guide.slice(5))"
    ))
    assert rendered_ids == expected_tool_ids

    for tool_id in expected_tool_ids:
        open_home_integration(page, f"tool:{tool_id}")
        row = page.locator(f"#tools [data-tool-row='{tool_id}']")
        expect(row).to_be_visible()
        expect(page.locator("#panel-network .integration-row:visible")).to_have_count(1)
        expect(row.locator("[data-tool-details]")).to_be_visible()
        guide = page.locator(f"[data-guide-section='tool:{tool_id}']")
        expect(guide).to_be_visible()
        expect(guide.get_by_role("heading", name="What happens to your data", exact=True)).to_have_count(1)
        expect(guide.locator(".guide-data-summary article")).to_have_count(4)

    open_home_integration(page, "tool:gmail")
    gmail_row = page.locator("#tools [data-tool-row='gmail']")
    expect(gmail_row).to_contain_text("connected: akshay@infiloop.io")
    expect(gmail_row).to_contain_text("GOOGLE_OAUTH_CLIENT_ID")
    gmail_approvals = gmail_row.locator(".tool-approvals")
    expect(gmail_approvals).to_contain_text("Invoice follow-up")
    pending_row = gmail_approvals.locator("tr", has_text="Invoice follow-up")
    pending_row.get_by_text("exact payload").click()
    expect(pending_row).to_contain_text("billing@acme.dev")
    page.once("dialog", lambda dialog: dialog.accept())
    pending_row.get_by_role("button", name="Approve").click()
    expect(gmail_row.locator("[data-tool-message='gmail']")).to_contain_text("Approved and executed")

    # A cancelled provider callback reloads the shell. The focused row is
    # rendered first so its callback result cannot be erased by tab refresh.
    page.evaluate("sessionStorage.setItem('kern_tool_connect', 'gmail')")
    page.goto(f"{url.rstrip('/')}/oauth/callback?error=access_denied", wait_until="domcontentloaded")
    expect(page.locator("#panel-network")).to_be_visible()
    expect(page.locator("#integration-detail-title")).to_have_text("Gmail")
    expect(page.locator("[data-tool-message='gmail']")).to_contain_text(
        "Connect cancelled: access_denied."
    )

    open_home_integration(page, "tool:brave_search")
    brave_row = page.locator("#tools [data-tool-row='brave_search']")
    config_input = page.locator("#tool-config-brave_search-BRAVE_SEARCH_API_KEY")
    config_input.fill("mock-brave-key")
    brave_row.get_by_role("button", name="Save").click()
    expect(brave_row).to_contain_text("set")
    config_input.fill("")
    brave_row.get_by_role("button", name="Save").click()
    expect(brave_row).to_contain_text("not set")

    page.locator("#panel-network .home-back").click()
    with page.expect_response(lambda response: "/v1/tools/events" in response.url):
        page.locator("#panel-home").get_by_role("button", name=re.compile(r"Tool audit")).click()
    expect(page.locator("#tool-events")).to_contain_text("brave_search")
    expect(page.locator("#tool-events")).to_contain_text("oauth_connect")


def mobile_smoke(page, url: str) -> None:
    """iPhone-sized pass: layout must not overflow and core flows must work."""
    from playwright.sync_api import expect

    log_in(page, url)
    expect(page.locator("#panel-home")).to_be_visible()
    expect(page.locator("#health")).to_contain_text("ok")
    expect(page.locator("#agent-name")).to_be_visible()
    expect(page.locator("#agent-name")).to_have_text("Host: kern-mock")
    expect(page.locator("#mobile-nav-toggle")).to_be_visible()
    expect(page.locator("#mobile-nav-toggle")).to_have_attribute("aria-expanded", "false")
    expect(page.locator("#upgrade-notice")).to_have_count(0)
    expect(page.locator("#nav-backdrop")).to_be_hidden()
    # On a phone the three usage boxes collapse behind a single pill so an open
    # app keeps the full screen; the boxes stay hidden until the pill is tapped.
    overview_toggle = page.locator(".runtime-overview-toggle")
    expect(overview_toggle).to_be_visible()
    expect(overview_toggle).to_contain_text("Agent usage")
    expect(overview_toggle).to_have_attribute("aria-expanded", "false")
    expect(page.locator("#runtime-overview .runtime-summary").first).to_be_hidden()
    # Opening runs the hard provider refresh (the phone's replacement for a
    # separate refresh button) and drops the panel as a floating overlay.
    with page.expect_request(re.compile(r"/v1/agent-runtime/refresh")):
        overview_toggle.click()
    expect(overview_toggle).to_have_attribute("aria-expanded", "true")
    expect(page.locator("#runtime-overview .runtime-overview-panel")).to_have_css("position", "absolute")
    # The overlay carries no refresh button; the open gesture is the refresh.
    expect(page.locator("#runtime-overview .runtime-refresh")).to_be_hidden()
    # Both subscription runtimes are active by now (the desktop pass logged them in);
    # Claude Code carries the extra model-week ring.
    for runtime, rings in (("Codex", 2), ("Claude Code", 3)):
        summary = page.locator("#runtime-overview .runtime-summary", has_text=runtime)
        expect(summary).to_be_visible()
        expect(summary.locator(".usage-ring")).to_have_count(rings)
        for index in range(rings):
            expect(summary.locator(".usage-ring").nth(index)).to_be_visible()
    hermes_box = page.locator("#runtime-overview .runtime-summary", has_text="Hermes")
    expect(hermes_box).to_be_visible()
    expect(page.locator("#runtime-overview .runtime-summary-bedrock")).to_have_count(1)
    expect(page.locator("#runtime-overview .runtime-stat-cost")).to_have_count(1)
    assert_runtime_usage_type(page, minimum_number_px=10)
    runtime_panel = page.locator("#runtime-overview .runtime-overview-panel")
    panel_widths = runtime_panel.evaluate(
        "element => ({client: element.clientWidth, scroll: element.scrollWidth})"
    )
    if panel_widths["scroll"] > panel_widths["client"]:
        raise AssertionError(f"mobile runtime panel overflows horizontally: {panel_widths}")
    summary_heights = page.locator("#runtime-overview .runtime-summary").evaluate_all(
        "elements => elements.map(element => element.getBoundingClientRect().height)"
    )
    if any(height > 41 for height in summary_heights):
        raise AssertionError(f"mobile runtime rows grew beyond the 40px design: {summary_heights}")
    # Escape dismisses the overlay like a menu; the boxes hide again.
    page.keyboard.press("Escape")
    expect(overview_toggle).to_have_attribute("aria-expanded", "false")
    expect(page.locator("#runtime-overview .runtime-summary").first).to_be_hidden()
    # Chat and Apps remain in the navigation drawer; Home has no duplicate
    # hero action on mobile.
    expect(page.locator("#home-hero")).to_have_count(0)
    expect(page.locator("#home-integration-groups .home-integration-card .integration-logo")).to_have_count(19)
    assert_no_horizontal_overflow(page, "home")

    # The drawer closes on backdrop click, Escape, and destination selection.
    open_mobile_navigation(page)
    expect(page.get_by_role("button", name="New chat", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="New app", exact=True)).to_be_visible()
    page.locator("#nav-backdrop").click(position={"x": 380, "y": 400})
    expect(page.locator("#nav-backdrop")).to_be_hidden()
    open_mobile_navigation(page)
    page.keyboard.press("Escape")
    expect(page.locator("#nav-backdrop")).to_be_hidden()

    page.locator("#panel-home").get_by_role("button", name=re.compile(r"Agent audit")).click()
    expect(page.locator("#events")).to_contain_text("thread.message")
    assert_no_horizontal_overflow(page, "agent event log")

    page.locator("#panel-agent-log .home-back").click()
    page.locator("#panel-home").get_by_role("button", name=re.compile(r"Agent processes")).click()
    expect(page.locator("#panel-processes")).to_be_visible()
    assert_no_horizontal_overflow(page, "agent processes")

    page.locator("#panel-processes .home-back").click()
    page.locator("#panel-home").get_by_role("button", name=re.compile(r"Agent workspace")).click()
    expect(page.locator("#file-list")).to_contain_text("workspace")
    assert_no_horizontal_overflow(page, "agent workspace")

    page.locator("#panel-files .home-back").click()
    open_home_integration(page, "tool:google_calendar")
    expect(page.locator("#panel-network")).to_be_visible()
    expect(page.locator("#integration-detail-title")).to_have_text("Google Calendar")
    expect(page.locator("#integration-detail-logo [data-integration-logo='tool:google_calendar']")).to_be_visible()
    expect(page.locator("#panel-network .integration-row:visible")).to_have_count(1)
    assert_no_horizontal_overflow(page, "integration detail")
    # Wide status chips (a connected account) must not crush the row name:
    # the title keeps a readable width on a phone.
    calendar_title = page.locator(".integration-row[data-tool-row='google_calendar'] h2")
    title_box = calendar_title.bounding_box()
    if not title_box or title_box["width"] < 60:
        raise AssertionError(f"tool row title is crushed on a phone viewport: {title_box}")

    # Exercise all three summary states on a phone: managed disabled,
    # enabled-only, and enabled plus a connected OAuth identity. Status owns a
    # full line above the action pair so the account cannot be squeezed.
    connected_row = page.locator(".integration-row[data-tool-row='google_calendar']")
    # text_content() waits for attachment but not for content, and the tools
    # list is rebuilt on the 5s poll, so a read can land on a freshly attached
    # empty chip row. Wait for the chips to actually say something first, then
    # confirm the click landed rather than letting the assertions below be the
    # synchronization.
    expect(connected_row.locator(".status-chips")).to_contain_text("enabled")
    expect(connected_row.locator(".status-chips")).to_contain_text("connected: akshay@infiloop.io")
    for row in (connected_row,):
        chips_box = row.locator(".status-chips").bounding_box()
        actions_box = row.locator(".integration-actions").bounding_box()
        if not chips_box or not actions_box or chips_box["y"] + chips_box["height"] > actions_box["y"] + 1:
            raise AssertionError("phone integration status overlaps or competes with its actions")
    connected_label = connected_row.locator(".chip-label")
    if connected_label.evaluate("element => element.scrollWidth > element.clientWidth + 1"):
        raise AssertionError("connected account identity is truncated on a phone")

    open_home_integration(page, "claude")
    claude_subtitle = page.locator(".integration-row[data-integration='claude'] .integration-subtitle")
    expect(claude_subtitle).to_be_hidden()
    expect(page.locator("#integration-detail-summary")).to_have_text(
        "Connect your Anthropic subscription and let your agent use Claude Code for tasks. Web search is optional and off by default."
    )

    open_home_integration(page, "tool:gmail")
    expect(page.locator(".connection-guide-entry")).to_have_count(1)
    gmail_guide = page.locator("[data-guide-section='tool:gmail']")
    expect(gmail_guide.get_by_role("heading", name="Connection", exact=True)).to_have_count(1)
    expect(gmail_guide).not_to_contain_text("Connection steps")
    expect(page.locator("[data-guide-section='tool:gmail']")).to_contain_text("What happens to your data")
    expect(page.locator("#panel-network .home-back")).to_be_visible()
    assert_no_horizontal_overflow(page, "Gmail integration")

    page.locator("#panel-network .home-back").click()
    page.locator("#panel-home").get_by_role("button", name=re.compile(r"Network audit")).click()
    expect(page.locator("#net-events")).to_contain_text("deploy.acme.dev")
    expect(page.locator("#net-events")).to_contain_text("Host not allowed")
    expect(page.locator("#net-event-pager")).to_contain_text("1")
    expect(page.locator("#net-event-pager")).to_contain_text("Next")
    assert_no_horizontal_overflow(page, "network audit log")

    page.locator("#panel-net-log .home-back").click()
    page.locator("#panel-home").get_by_role("button", name=re.compile(r"Tool audit")).click()
    expect(page.locator("#tool-events")).to_contain_text("oauth_connect")
    assert_no_horizontal_overflow(page, "tool audit log")

    page.locator("#panel-tool-log .home-back").click()
    page.locator("#panel-home").get_by_role("button", name=re.compile(r"Host diagnostics")).click()
    expect(page.locator("#host-diagnostics")).to_contain_text("agentic_web_app.request")
    expect(page.locator("#host-diagnostics")).to_contain_text("orchestrator.execution")
    assert_no_horizontal_overflow(page, "host diagnostics")


def open_mobile_navigation(page) -> None:
    from playwright.sync_api import expect

    page.locator("#mobile-nav-toggle").click()
    expect(page.locator("#mobile-nav-toggle")).to_have_attribute("aria-expanded", "true")
    expect(page.locator("#sidebar")).to_have_class(re.compile(r"mobile-open"))
    expect(page.locator("#nav-backdrop")).to_be_visible()
    expect(page.locator("#mobile-nav-close")).to_be_focused()
    if page.locator(".topbar").evaluate("element => element.inert") is not True:
        raise AssertionError("the top bar must be inert behind the open navigation drawer")
    if page.locator("#app > .shell > main").evaluate("element => element.inert") is not True:
        raise AssertionError("the active page must be inert behind the open navigation drawer")


def mobile_go_to(page, name: str, *, exact: bool = False) -> None:
    from playwright.sync_api import expect

    open_mobile_navigation(page)
    page.locator("#sidebar").get_by_role("button", name=name, exact=exact).click()
    expect(page.locator("#nav-backdrop")).to_be_hidden()
    expect(page.locator("#mobile-nav-toggle")).to_have_attribute("aria-expanded", "false")
    if page.locator(".topbar").evaluate("element => element.inert") is not False:
        raise AssertionError("the top bar must leave inert state after the drawer closes")
    if page.locator("#app > .shell > main").evaluate("element => element.inert") is not False:
        raise AssertionError("the active page must leave inert state after the drawer closes")


def assert_no_horizontal_overflow(page, panel: str) -> None:
    overflow = page.evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )
    if overflow > 1:
        offenders = page.evaluate(
            """() => [...document.querySelectorAll('body *')]
              .map(element => {
                const rect = element.getBoundingClientRect();
                return { tag: element.tagName, id: element.id, className: String(element.className),
                  left: Math.round(rect.left), right: Math.round(rect.right), width: Math.round(rect.width) };
              })
              .filter(item => item.right > document.documentElement.clientWidth + 1)
              .sort((left, right) => right.right - left.right)
              .slice(0, 8)"""
        )
        raise AssertionError(
            f"{panel} panel overflows horizontally by {overflow}px on a phone viewport: {offenders}"
        )


def chromium_executable_path() -> str | None:
    configured = os.environ.get(CHROMIUM_EXECUTABLE_ENV)
    if configured:
        configured_path = Path(configured)
        if configured_path.is_file():
            return str(configured_path)
        raise SystemExit(f"{CHROMIUM_EXECUTABLE_ENV} does not point to a file: {configured}")

    for pattern in (
        "chromium-*/chrome-linux/chrome",
        "chromium_headless_shell-*/chrome-linux/headless_shell",
    ):
        for candidate in sorted(PLAYWRIGHT_CACHE.glob(pattern), reverse=True):
            if candidate.is_file():
                return str(candidate)
    return None


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
