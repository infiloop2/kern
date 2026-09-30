"""Exercise the real browser adapter against a local, intercepted X fixture."""
from __future__ import annotations
import json
from pathlib import Path
import socket
import struct
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from host.runtime.browser.browser import Browser
from host.runtime.browser.providers import x
from host.runtime.browser.actions import x_post_tweet
from host.runtime.browser.client import BrowserError
from host.runtime.browser_network.config import LOCATIONS, Settings

EDITOR = '''<div data-testid="tweetTextarea_0" contenteditable="true"
 oninput="setTimeout(() => this.parentElement.querySelector('button').disabled=false, 150)"></div>
<button disabled data-testid="tweetButton" onclick="fetch('/i/api/graphql/fixture/CreateTweet', {method:'POST', body:this.parentElement.querySelector('[contenteditable]').textContent})">Post</button>'''
INLINE = '<div data-testid="tweetTextarea_0" contenteditable="true"></div><button data-testid="tweetButton">Inline post</button>'
HTML = f'''<!doctype html><html><body>
<a data-testid="AppTabBar_Profile_Link" href="/example">Profile</a>
{INLINE}<div role="dialog">{EDITOR}</div>
</body></html>'''
REPLY_HTML = f'''<!doctype html><html><body>
<a data-testid="AppTabBar_Profile_Link" href="/example">Profile</a>
{INLINE}<div id="composer" role="dialog" hidden>{EDITOR}</div>
<template id="target"><article data-testid="tweet"><a href="/someone/status/12345">Target</a><button data-testid="reply" onclick="document.getElementById('composer').hidden=false">Reply</button></article></template>
<script>setTimeout(() => document.body.append(document.getElementById('target').content.cloneNode(true)), 250);</script>
</body></html>'''


def run(playwright):
    calls = []
    user_agents = []
    response_override = None
    def fixture(route):
        user_agents.append(route.request.headers.get("user-agent"))
        if route.request.method == "POST":
            calls.append(route.request.post_data)
            route.fulfill(status=200, content_type="application/json", body=json.dumps(response_override or {"data": {"create_tweet": {"tweet_results": {"result": {
                "rest_id": "123", "core": {"user_results": {"result": {"core": {"screen_name": "example"}}}},
                "legacy": {"in_reply_to_status_id_str": "12345"} if "reply" in route.request.post_data else {}
            }}}}}))
        elif route.request.url.endswith("/blank"):
            route.fulfill(status=200, content_type="text/html", body="<!doctype html><html><body></body></html>")
        else:
            route.fulfill(status=200, content_type="text/html", body=REPLY_HTML if route.request.url.endswith("/status/12345") else HTML)
    real_launch = playwright.chromium.launch
    processes = []
    displays = []
    def launch(**options):
        # CI cannot create the nested sandbox; host smoke checks it separately.
        options.update(chromium_sandbox=False)
        process = real_launch(**options)
        processes.append(process)
        def new_context(**context_options):
            context = process.new_context(**context_options)
            context.route("https://x.com/**", fixture)
            return context
        return SimpleNamespace(new_context=new_context, close=process.close, version=process.version)
    runtime = SimpleNamespace(chromium=playwright.chromium, stop=lambda: None)
    with TemporaryDirectory() as directory, patch("playwright.sync_api.sync_playwright", return_value=SimpleNamespace(start=lambda: runtime)), patch.object(playwright.chromium, "launch", side_effect=launch):
        account_dir = Path(directory) / "account"
        account_dir.mkdir()
        snapshot = None
        browser = Browser(snapshot, "https://x.com/", {"locale": "en-GB", "timezone": "Europe/London"})
        displays.append(browser.display)
        try:
            assert x.verify_account(browser.page) == "example"
            assert browser.origin() == "https://x.com"
            assert browser.frame()
            user_agent = browser.page.evaluate("navigator.userAgent")
            major = processes[-1].version.split(".")[0]
            assert "HeadlessChrome" not in user_agent and f"Chrome/{major}." in user_agent
            assert user_agents[0] == user_agent
            brands = browser.page.evaluate("navigator.userAgentData.brands")
            assert any(brand["brand"] in {"Chromium", "Google Chrome"} and brand["version"] == major for brand in brands)
            assert browser.page.evaluate("navigator.webdriver") is False
            assert browser.page.evaluate("navigator.language") == "en-GB"
            assert browser.page.evaluate("Intl.DateTimeFormat().resolvedOptions().timeZone") == "Europe/London"
            # Each preset must work in Chromium and agree across headers and JS APIs.
            settings = Settings(Mock(load_settings=lambda: {"mode": "direct"}))
            for location in LOCATIONS:
                settings.save(settings.prepare({"mode": "decodo", "username": "fixture", "password": "fixture", "location": location}))
                preset = settings.public()
                context = browser.process.new_context(locale=preset["locale"], timezone_id=preset["timezone"])
                try:
                    headers = []
                    def identity_page(route):
                        headers.append(route.request.headers)
                        route.fulfill(status=200, content_type="text/html", body="<!doctype html><title>Identity</title>")
                    context.route("https://x.com/identity", identity_page)
                    page = context.new_page()
                    page.goto("https://x.com/identity")
                    assert page.evaluate("navigator.language") == preset["locale"]
                    assert page.evaluate("navigator.languages[0]") == preset["locale"]
                    assert headers[0]["accept-language"].split(",")[0] == preset["locale"]
                    assert page.evaluate("Intl.DateTimeFormat().resolvedOptions().timeZone") == preset["timezone"]
                    offsets = page.evaluate("[new Date('2026-01-15T12:00:00Z').getTimezoneOffset(), new Date('2026-07-15T12:00:00Z').getTimezoneOffset()]")
                    assert offsets == ([300, 240] if location == "new_york" else [0, -60]), offsets
                finally:
                    context.close()
            identity = browser.page.evaluate("({ua: navigator.userAgent, platform: navigator.platform, languages: navigator.languages, screen: [screen.width, screen.height], scale: devicePixelRatio})")
            browser.page.evaluate("""() => {
                window.pointerEvents = [];
                const target = document.createElement('button');
                target.id = 'pointer-fixture';
                target.style = 'position:fixed;left:500px;top:400px;width:100px;height:60px';
                target.textContent = 'Hover and click';
                for (const name of ['pointerover', 'pointerdown', 'pointerup', 'click']) {
                    target.addEventListener(name, event => window.pointerEvents.push({type: event.type, trusted: event.isTrusted}));
                }
                document.body.append(target);
            }""")
            for phase in ('move', 'down', 'up'):
                browser.input({'kind': 'pointer', 'phase': phase, 'x': 550, 'y': 430})
            events = browser.page.evaluate('window.pointerEvents')
            assert [event['type'] for event in events] == ['pointerover', 'pointerdown', 'pointerup', 'click']
            assert all(event['trusted'] for event in events)
            # Even other local clients need this display's private cookie.
            number = browser.display.environment["DISPLAY"].removeprefix(":")
            with socket.socket(socket.AF_UNIX) as client:
                client.settimeout(3)
                client.connect(f"/tmp/.X11-unix/X{number}")
                client.sendall(struct.pack("<BBHHHHH", ord("l"), 0, 11, 0, 0, 0, 0))
                assert client.recv(8)[0] == 0, "Unauthenticated X11 client was accepted"
            browser.page.get_by_role("dialog").get_by_test_id("tweetTextarea_0").click()
            browser.page.evaluate("""() => {
                window.typedEvents = [];
                for (const name of ['keydown', 'keypress', 'input', 'keyup']) {
                    document.addEventListener(name, event => window.typedEvents.push({type: event.type, trusted: event.isTrusted}));
                }
            }""")
            browser.input({"kind": "type", "text": "a"})
            events = browser.page.evaluate("window.typedEvents")
            assert [event["type"] for event in events] == ["keydown", "keypress", "input", "keyup"]
            assert all(event["trusted"] for event in events)
            browser.input({"kind": "type", "text": "😀"})
            assert browser.page.get_by_role("dialog").get_by_test_id("tweetTextarea_0").inner_text() == "a😀"
            browser.input({"kind": "key", "key": "Control+a"})
            browser.input({"kind": "text", "text": "😀" * 4096})
            assert browser.page.get_by_role("dialog").get_by_test_id("tweetTextarea_0").inner_text() == "😀" * 4096
            browser.page.evaluate("window.onbeforeunload = () => 'Unsaved draft'")
            browser.input({"kind": "home"})
            exact_text = "First line\nCafé 😀 +\tend".ljust(280, "x")
            x_post_tweet.prepare_post(browser.page, "example", exact_text)
            assert browser.page.get_by_role("dialog").get_by_test_id("tweetTextarea_0").inner_text() == exact_text
            x_post_tweet.prepare_post(browser.page, "example", "hello from fixture")
            assert x_post_tweet.submit_prepared_post(browser.page, "example") == "https://x.com/example/status/123"
            x_post_tweet.prepare_post(browser.page, "example", "reply from fixture", "12345")
            assert x_post_tweet.submit_prepared_post(browser.page, "example", "12345") == "https://x.com/example/status/123"
            assert calls == ["hello from fixture", "reply from fixture"]
            response_override = {"data": {"create_tweet": {"tweet_results": {"result": {
                "__typename": "TweetWithVisibilityResults", "tweet": {
                    "rest_id": "456", "core": {"user_results": {"result": {"legacy": {"screen_name": "example"}}}},
                    "legacy": {"in_reply_to_status_id_str": "12345"}
                }
            }}}}}
            assert x_post_tweet.submit_prepared_post(browser.page, "example", "12345") == "https://x.com/example/status/456"
            for code in (187, 226, 88):
                response_override = {"data": {"create_tweet": None}, "errors": [{"code": code, "message": "private provider response"}]}
                try:
                    x_post_tweet.submit_prepared_post(browser.page, "example")
                    raise AssertionError("rejection was reported as success")
                except x_post_tweet.PostRejected as exc:
                    assert f"code {code}" in str(exc) and "private" not in str(exc)
            response_override = None
            browser.context.add_cookies([{"name": "session", "value": "fixture", "domain": "x.com", "path": "/", "expires": 2000000000, "secure": True, "httpOnly": True}])
            browser.page.evaluate("""async () => {
                localStorage.setItem('account', 'example');
                sessionStorage.setItem('transient', 'not saved');
                const db = await new Promise((resolve, reject) => {
                    const request = indexedDB.open('fixture', 1);
                    request.onupgradeneeded = () => request.result.createObjectStore('auth');
                    request.onsuccess = () => resolve(request.result);
                    request.onerror = () => reject(request.error);
                });
                await new Promise((resolve, reject) => {
                    const transaction = db.transaction('auth', 'readwrite');
                    transaction.objectStore('auth').put('dummy-token', 'token');
                    transaction.oncomplete = resolve;
                    transaction.onerror = () => reject(transaction.error);
                });
                db.close();
            }""")
            snapshot = browser.save_state()
        finally:
            browser.close()
        assert not processes[-1].is_connected()
        assert list(account_dir.iterdir()) == []
        restored = Browser(snapshot, "https://x.com/", {"locale": "en-GB", "timezone": "Europe/London"})
        displays.append(restored.display)
        try:
            assert restored.page.evaluate("({ua: navigator.userAgent, platform: navigator.platform, languages: navigator.languages, screen: [screen.width, screen.height], scale: devicePixelRatio})") == identity
            cookie = next(cookie for cookie in restored.context.cookies() if cookie["name"] == "session")
            assert cookie["value"] == "fixture" and cookie["httpOnly"] and cookie["secure"]
            assert restored.page.evaluate("localStorage.getItem('account')") == "example"
            assert restored.page.evaluate("sessionStorage.getItem('transient')") is None
            assert restored.page.evaluate("""async () => {
                const db = await new Promise(resolve => {
                    const request = indexedDB.open('fixture');
                    request.onsuccess = () => resolve(request.result);
                });
                const value = await new Promise(resolve => {
                    const request = db.transaction('auth').objectStore('auth').get('token');
                    request.onsuccess = () => resolve(request.result);
                });
                db.close();
                return value;
            }""") == "dummy-token"
            restored.page.evaluate("localStorage.setItem('account', 'refreshed')")
            snapshot = restored.save_state()
        finally:
            restored.close()
        refreshed = Browser(snapshot, "https://x.com/", {"locale": "en-GB", "timezone": "Europe/London"})
        displays.append(refreshed.display)
        try:
            assert refreshed.page.evaluate("localStorage.getItem('account')") == "refreshed"
        finally:
            refreshed.close()
        other = Browser(None, "https://x.com/blank", {})
        displays.append(other.display)
        try:
            assert other.frame(), "An intentionally blank page must still produce a screenshot"
            assert not other.context.cookies()
            assert other.page.evaluate("localStorage.getItem('account')") is None
        finally:
            other.close()
        assert all(not process.is_connected() for process in processes)
        assert all(display.process.poll() is not None and not Path(display.directory.name).exists() for display in displays)
        print(f"Browser storage restore passed: {len(str(snapshot))} characters of fixture state, cookies/localStorage/IndexedDB restored across process restarts; accounts isolated.")
    run_proxy_failure(playwright)


def run_proxy_failure(playwright):
    """A reachable website must receive nothing when Decodo or the relay fails."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import ssl
    import subprocess
    import threading
    from playwright.sync_api import Error
    from host.runtime.browser.accounts import Accounts
    from host.runtime.browser_network.relay import TunnelServer

    requests = []
    class Website(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass
        def do_GET(self):
            requests.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"fixture reachable")

    with TemporaryDirectory() as directory:
        root = Path(directory)
        cert, key = root / "cert.pem", root / "key.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                        "-keyout", str(key), "-out", str(cert), "-days", "1", "-subj", "/CN=localhost"],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(cert, key)
        with ThreadingHTTPServer(("127.0.0.1", 8010), Website) as website:
            website.socket = tls.wrap_socket(website.socket, server_side=True)
            website_worker = threading.Thread(target=website.serve_forever, daemon=True)
            website_worker.start()
            try:
                url = "https://127.0.0.1:8010/"
                # Positive control proves the origin would accept a direct request.
                direct = playwright.chromium.launch(chromium_sandbox=False)
                try:
                    page = direct.new_page(ignore_https_errors=True)
                    page.goto(url + "control")
                    assert page.text_content("body") == "fixture reachable"
                finally:
                    direct.close()
                requests.clear()
                store = Mock()
                store.load_settings.return_value = {"mode": "direct"}
                store.save_settings.side_effect = lambda value: setattr(store.load_settings, "return_value", value)
                accounts = Accounts(store)
                accounts.dispatch("network_save", {"mode": "decodo", "username": "fixture", "password": "secret",
                                  "location": "new_york"})
                real_launch = playwright.chromium.launch
                runtime = SimpleNamespace(chromium=playwright.chromium, stop=lambda: None)
                def launch(**options):
                    return real_launch(**{**options, "chromium_sandbox": False})
                with patch("playwright.sync_api.sync_playwright", return_value=SimpleNamespace(start=lambda: runtime)), patch.object(playwright.chromium, "launch", side_effect=launch), patch("host.runtime.browser.browser.BROWSER_NETWORK_PORT", 8011), patch("host.runtime.browser.browser.permitted_url", return_value=True), patch("host.runtime.browser_network.relay.target", return_value="127.0.0.1"), patch("host.runtime.browser_network.relay.connect_proxy", side_effect=BrowserError("fixture gateway unavailable")) as proxy, patch("socket.create_connection") as fallback:
                    with TunnelServer(accounts.network, port=8011) as tunnel:
                        worker = threading.Thread(target=tunnel.serve_forever, daemon=True)
                        worker.start()
                        browser = None
                        try:
                            browser = accounts.launch_browser(None, "about:blank")
                            try:
                                browser.page.goto(url + "proxy-failed")
                                raise AssertionError("Navigation unexpectedly bypassed failed Decodo")
                            except Error as exc:
                                assert "ERR_TUNNEL_CONNECTION_FAILED" in str(exc), str(exc)
                            assert proxy.called
                            fallback.assert_not_called()
                        finally:
                            if browser:
                                browser.close()
                            tunnel.shutdown()
                            worker.join()
                    # Also fail closed when the local relay itself is unavailable.
                    browser = accounts.launch_browser(None, "about:blank")
                    try:
                        try:
                            browser.page.goto(url + "relay-stopped")
                            raise AssertionError("Navigation unexpectedly bypassed stopped relay")
                        except Error as exc:
                            # Some hosts filter refused loopback connections instead of returning ECONNREFUSED.
                            assert any(code in str(exc) for code in ("ERR_PROXY_CONNECTION_FAILED", "ERR_TIMED_OUT")), str(exc)
                    finally:
                        browser.close()
                    assert requests == [], requests
            finally:
                website.shutdown()
                website_worker.join()
    print("Browser proxy failure passed: failed Decodo and stopped relay never contacted the reachable origin.")
