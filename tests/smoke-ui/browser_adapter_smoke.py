"""Exercise the real browser adapter against a local, intercepted X fixture."""
from __future__ import annotations
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from host.runtime.browser.browser import Browser
from host.runtime.browser.providers import x
from host.runtime.browser.actions import x_post_tweet

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


def run(playwright, executable_path):
    calls = []
    response_override = None
    def fixture(route):
        if route.request.method == "POST":
            calls.append(route.request.post_data)
            route.fulfill(status=200, content_type="application/json", body=json.dumps(response_override or {"data": {"create_tweet": {"tweet_results": {"result": {
                "rest_id": "123", "core": {"user_results": {"result": {"core": {"screen_name": "example"}}}},
                "legacy": {"in_reply_to_status_id_str": "12345"} if "reply" in route.request.post_data else {}
            }}}}}))
        else:
            route.fulfill(status=200, content_type="text/html", body=REPLY_HTML if route.request.url.endswith("/status/12345") else HTML)
    real_launch = playwright.chromium.launch
    processes = []
    def launch(**options):
        # CI cannot create the nested sandbox; host smoke checks it separately.
        options.update(chromium_sandbox=False, executable_path=executable_path)
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
        auth_file = account_dir / "auth.json"
        browser = Browser(auth_file, "https://x.com/")
        try:
            assert x.verify_account(browser.page) == "example"
            assert browser.origin() == "https://x.com"
            assert browser.frame()
            user_agent = browser.page.evaluate("navigator.userAgent")
            assert "HeadlessChrome" not in user_agent and f"Chrome/{processes[-1].version}" in user_agent
            browser.page.get_by_role("dialog").get_by_test_id("tweetTextarea_0").click()
            browser.input({"kind": "text", "text": "😀" * 4096})
            assert browser.page.get_by_role("dialog").get_by_test_id("tweetTextarea_0").inner_text() == "😀" * 4096
            browser.page.evaluate("window.onbeforeunload = () => 'Unsaved draft'")
            browser.input({"kind": "home"})
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
            browser.save_state()
        finally:
            browser.close()
        assert not processes[-1].is_connected()
        assert list(account_dir.iterdir()) == [auth_file]
        assert auth_file.stat().st_mode & 0o777 == 0o600
        restored = Browser(auth_file, "https://x.com/")
        try:
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
            restored.save_state()
        finally:
            restored.close()
        refreshed = Browser(auth_file, "https://x.com/")
        try:
            assert refreshed.page.evaluate("localStorage.getItem('account')") == "refreshed"
        finally:
            refreshed.close()
        other = Browser(Path(directory) / "other-auth.json", "https://x.com/")
        try:
            assert not other.context.cookies()
            assert other.page.evaluate("localStorage.getItem('account')") is None
        finally:
            other.close()
        assert all(not process.is_connected() for process in processes)
        print(f"Browser storage restore passed: {auth_file.stat().st_size} bytes of fixture state, cookies/localStorage/IndexedDB restored across process restarts; accounts isolated.")
