"""Exercise the real browser adapter against a local, intercepted X fixture."""
from __future__ import annotations
from contextlib import contextmanager
from html import escape
import json
import subprocess
from pathlib import Path
import socket
import struct
from time import monotonic
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from host.runtime.browser.browser import Browser
from host.runtime.browser.chromium import Chromium
from host.runtime.browser.providers import x
from host.runtime.browser.actions import x_post_tweet
from host.runtime.browser.actions.x_diagnostics import preparation_facts
from host.runtime.browser.client import BrowserError
from host.runtime.browser_network.config import LOCATIONS, Settings
from host.runtime.core.host_errors import _safe_context

EDITOR = '''<div data-testid="tweetTextarea_0" contenteditable="true" style="white-space:pre-wrap"
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


@contextmanager
def native_fixture(playwright, fixture=None, *, pattern="https://x.com/**"):
    real_popen = subprocess.Popen
    processes = []
    def popen(command, **options):
        # CI cannot create the nested sandbox; deployed readiness checks it.
        if command[0] == playwright.chromium.executable_path:
            command = [*command[:1], "--no-sandbox", *command[1:]]
        return real_popen(command, **options)
    def launch(runtime, environment, settings):
        process = Chromium(runtime, environment, settings)
        processes.append(process)
        if fixture:
            process.context.route(pattern, fixture)
        return process
    runtime = SimpleNamespace(chromium=playwright.chromium, stop=lambda: None)
    with (patch("playwright.sync_api.sync_playwright", return_value=SimpleNamespace(start=lambda: runtime)),
          patch("host.runtime.browser.chromium.BROWSER_DEBUG_PORT", 8009),
          patch("host.runtime.browser.chromium.subprocess.Popen", side_effect=popen),
          patch("host.runtime.browser.browser.Chromium", side_effect=launch)):
        yield processes
    assert all(process.child.poll() is not None and not Path(process.directory.name).exists() for process in processes)


def check_presets():
    settings = Settings(Mock(load_settings=lambda: {"mode": "direct"}))
    for location in LOCATIONS:
        settings.save(settings.prepare({"mode": "decodo", "username": "fixture", "password": "fixture", "location": location}))
        preset = settings.public()
        browser = Browser(None, "about:blank", preset)
        try:
            headers = []
            def identity_page(route):
                headers.append(route.request.headers)
                route.fulfill(status=200, content_type="text/html", body="<!doctype html><title>Identity</title>")
            browser.context.route("https://x.com/identity", identity_page)
            page = browser.page
            page.goto("https://x.com/identity")
            assert page.evaluate("navigator.language") == preset["locale"]
            assert page.evaluate("navigator.languages[0]") == preset["locale"]
            assert headers[0]["accept-language"].split(",")[0] == preset["locale"]
            assert page.evaluate("Intl.DateTimeFormat().resolvedOptions().timeZone") == preset["timezone"]
            offsets = page.evaluate("[new Date('2026-01-15T12:00:00Z').getTimezoneOffset(), new Date('2026-07-15T12:00:00Z').getTimezoneOffset()]")
            assert offsets == ([300, 240] if location == "new_york" else [0, -60]), offsets
        finally:
            browser.close()


def check_post_text(page):
    # Native contenteditable blank blocks already reproduce the old innerText
    # false mismatch, without a live X account or any submission.
    release_text = ("shipped kern v1.19.22 🎉\n\n"
                    "kern now suggests saved memories while agents are working, as the conversation changes. "
                    "earlier decisions can resurface mid-task, and agents choose which suggested pages to read.\n\n"
                    "https://github.com/infiloop2/kern")
    samples = [release_text, "\nfirst\n\n\nlast\n", "🎉 👍🏽 👩🏽‍💻 👨‍👩‍👧‍👦 🇺🇳 ❤️ ☀️",
               "  Café e\u0301\t中文 العربية שלום  \n\nhttps://example.com/a?b=c&d=e"]
    for text in samples:
        x_post_tweet.prepare_post(page, "example", text)
        editor = page.get_by_role("dialog").get_by_test_id("tweetTextarea_0")
        assert page.evaluate(x_post_tweet.MATCHES_TEXT, [editor.element_handle(), text])

    # Draft-style wrappers, empty-block placeholders, decorated links, and
    # image emoji must represent exactly the same code points as approved.
    # These fixtures also exercise verification independently of typing.
    for text in samples:
        lines = []
        for line in text.split('\n'):
            content = escape(line) or '<br data-text="true">'
            for emoji in ("🎉", "👍🏽", "👩🏽‍💻", "👨‍👩‍👧‍👦", "🇺🇳", "❤️", "☀️"):
                content = content.replace(emoji, f'<img alt="{emoji}" draggable="false">')
            if line.startswith('https://'):
                content = f'<a href="https://example.com"><span>{content}</span></a>'
            lines.append('<div data-block="true"><div><span data-offset-key="fixture">'
                         f'<span data-text="true">{content}</span></span></div></div>')
        editor.evaluate('(editor, html) => editor.innerHTML = html',
                        '<div data-contents="true">' + ''.join(lines) + '</div>')
        assert page.evaluate(x_post_tweet.MATCHES_TEXT, [editor.element_handle(), text])
        for changed in (text + ' ', text[:-1], text.replace('\n\n', '\n'), text.replace('🎉', '🎊'),
                        text.replace('🏽', ''), text.replace('\u200d', ''), text.replace('\ufe0f', '')):
            if changed != text:
                assert not page.evaluate(x_post_tweet.MATCHES_TEXT, [editor.element_handle(), changed])

    editor.evaluate('(editor) => editor.innerHTML = "<div>a<span><br></span>b<br>c</div>"')
    assert page.evaluate(x_post_tweet.MATCHES_TEXT, [editor.element_handle(), 'a\nb\nc'])
    assert not page.evaluate(x_post_tweet.MATCHES_TEXT, [editor.element_handle(), 'ab\nc'])

    # Simulate an editor doing work per keystroke. With the former 50 ms
    # artificial delay, 280 characters exceeded the old 30-second deadline.
    slow_html = HTML + '''<script>
        window.postKeys = [];
        const editor = document.querySelector('[role="dialog"] [contenteditable]');
        editor.addEventListener('keydown', event => {
            if (event.key === 'x') window.postKeys.push(event.isTrusted);
        });
        editor.addEventListener('input', () => {
            const deadline = performance.now() + 65;
            while (performance.now() < deadline) {}
        });
    </script>'''
    route_pattern = "https://x.com/compose/post"
    def slow_editor(route):
        route.fulfill(status=200, content_type="text/html", body=slow_html)
    page.route(route_pattern, slow_editor)
    try:
        x_post_tweet.prepare_post(page, "example", "x" * 280)
        assert page.get_by_role("dialog").get_by_test_id("tweetTextarea_0").inner_text() == "x" * 280
        assert page.evaluate("window.postKeys") == [True] * 280
    finally:
        page.unroute(route_pattern, slow_editor)

    # An enabled button alone is insufficient: provider input handlers can
    # alter text. Even a whitespace-only change must stop preparation.
    changed_html = HTML + '''<script>
        const editor = document.querySelector('[role="dialog"] [contenteditable]');
        editor.style.whiteSpace = 'pre-wrap';
        editor.addEventListener('keyup', () => {
            if (editor.innerText === 'exact text') editor.innerText = 'exact  text';
        });
    </script>'''
    def changed_editor(route):
        route.fulfill(status=200, content_type="text/html", body=changed_html)
    page.route(route_pattern, changed_editor)
    try:
        try:
            x_post_tweet.prepare_post(page, "example", "exact text")
            raise AssertionError("Changed approved text was accepted")
        except x_post_tweet.PreparationFailed as exc:
            assert exc.step == "verify_post_text", str(exc)
    finally:
        page.unroute(route_pattern, changed_editor)
    print("Post preparation passed: Unicode, blank lines, image emoji, slow editor, trusted keys and changed-text rejection.", flush=True)


def check_post_resources(playwright):
    resources = '''<img src="https://pbs.twimg.com/photo.jpg">
<video src="https://video.twimg.com/clip.mp4" preload="auto"></video>
<script src="https://abs.twimg.com/main.js"></script>
<script>window.videoFetch = fetch('https://video.twimg.com/playlist.m3u8')
    .then(() => 'loaded', () => 'blocked');</script>'''
    reached = []
    def fixture(route):
        reached.append(route.request.url)
        if route.request.resource_type == "document":
            route.fulfill(content_type="text/html", body=HTML + resources)
        elif route.request.url == "https://abs.twimg.com/main.js":
            route.fulfill(content_type="application/javascript", body="window.composerScriptLoaded = true")
        elif route.request.method == "POST":
            route.fulfill(content_type="application/json", body=json.dumps({"data": {"create_tweet": {"tweet_results": {"result": {
                "rest_id": "123", "core": {"user_results": {"result": {"core": {"screen_name": "example"}}}}
            }}}}}))
        else:
            route.fulfill(body="fixture media", headers={"Access-Control-Allow-Origin": "*"})
    with native_fixture(playwright, fixture, pattern="**/*"):
        for block_media in (True, False):
            reached.clear()
            browser = Browser(None, "https://x.com/compose/post", {}, block_media=block_media)
            try:
                page = browser.page
                assert page.evaluate("window.videoFetch") == ("blocked" if block_media else "loaded")
                assert page.evaluate("window.composerScriptLoaded") is True
                if block_media:
                    x_post_tweet.prepare_post(page, "example", "hello")
                    assert x_post_tweet.submit_prepared_post(page, "example") == "https://x.com/example/status/123"
                    assert not any("pbs.twimg.com" in url or "video.twimg.com" in url for url in reached), reached
                else:
                    assert "https://pbs.twimg.com/photo.jpg" in reached, reached
                    assert "https://video.twimg.com/playlist.m3u8" in reached, reached
            finally:
                browser.close()
    print("Browser posting skips media before navigation and preserves composer scripts/submission; operator media still loads.", flush=True)


def check_reply_diagnostics(page):
    # Representative structural states, not a claim to reproduce the live X
    # incident. Diagnostics must distinguish these without returning text.
    article = '''<article data-testid="tweet"><a href="/someone/status/12345">private target text</a>
<button data-testid="reply">private button text</button></article>'''
    fixtures = [
        ("", {"target_count": 0}),
        (article * 2, {"target_count": 2}),
        (article.replace('<button ', '<button hidden '), {"reply_visible": False}),
        (article.replace('<button ', '<button disabled '), {"reply_enabled": False}),
        (article + '<div style="position:fixed;inset:0;background:white"></div>',
         {"reply_visible": True, "reply_enabled": True, "reply_center_unobstructed": False,
          "reply_in_viewport": True, "reply_center_hit_tag": "div"}),
        (article, {"reply_visible": True, "reply_enabled": True, "reply_center_unobstructed": True,
                   "target_id_link_count": 1, "target_id_article_count": 1}),
        (article + f'<div role="dialog">{EDITOR}</div>', {"dialog_count": 1, "composer_count": 1,
          "visible_dialog_count": 1, "visible_popup_editor_count": 1, "inline_editor_count": 0}),
        (article.replace('/status/12345', '/status/12345?private-query'),
         {"target_count": 0, "target_id_article_count": 1, "target_id_query_link_count": 1,
          "target_id_exact_suffix_link_count": 0}),
        (article.replace('/status/12345', '/status/12345/photo/1'),
         {"target_count": 0, "target_id_extra_path_link_count": 1}),
        (article.replace('>private target text</a>', '><time>private timestamp</time></a>'),
         {"target_id_timestamp_article_count": 1}),
        ('<a href="https://x.com/someone/status/12345?private-query">private text</a>' +
         article.replace('/status/12345', '/status/123456'),
         {"target_count": 0, "target_id_link_count": 1, "target_id_article_count": 0}),
        (article.replace('/someone/status/12345', 'https://private.example/status/12345?private-query'),
         {"target_count": 0, "target_id_link_count": 0}),
        (article.replace('<button ', '<button style="position:absolute;top:5000px" '),
         {"reply_visible": True, "reply_in_viewport": False, "reply_center_hit_tag": ""}),
        (article + '<div data-testid="mask" role="dialog" style="position:fixed;inset:0;background:white"></div>',
         {"reply_center_unobstructed": False, "reply_center_hit_test_id": "mask", "reply_center_hit_role": "dialog"}),
        (article + '<private-secret data-testid="private-token" role="private-role" style="display:block;position:fixed;inset:0"></private-secret>',
         {"reply_center_hit_tag": "other", "reply_center_hit_test_id": "other", "reply_center_hit_role": "other"}),
        (INLINE + f'<div role="dialog" hidden>{EDITOR}</div>',
         {"inline_editor_count": 1, "popup_editor_count": 1, "visible_popup_editor_count": 0,
          "visible_dialog_count": 0}),
        ('<input autocomplete="username" value="private-account"><input type="password" value="private-password">',
         {"login_input_visible": True}),
        ('<input autocomplete="username" hidden value="private-account">', {"login_input_visible": False}),
        ('<a href="/other/status/99999?private-token">private</a>' * 2000 + article,
         {"target_link_scan_truncated": True, "target_id_link_count": 0, "target_count": 1}),
    ]
    for html, expected in fixtures:
        page.set_content('<!doctype html><base href="https://x.com/">' + html)
        facts = preparation_facts(page, "12345")
        assert {key: facts[key] for key in expected} == expected, facts
        assert "snapshot_incomplete" not in facts, facts
        assert facts["snapshot_phase"] == "after_failure", facts
        assert len(json.dumps(facts).encode()) < 4096, facts
        assert _safe_context(facts) == facts, facts
        assert "private" not in json.dumps(facts), facts
        assert "https://" not in json.dumps(facts), facts
        # A read-only snapshot must not scroll to an offscreen control.
        assert page.evaluate('scrollY') == 0
    # Main-world scripts may replace built-ins. Return values must pass the
    # Python allowlist, and looping DOM APIs must not trap the failure handler.
    hostile_fixtures = [
        (article + '''<script>
            const assign = Object.assign;
            Object.assign = (target, ...sources) => {
                const result = assign(target, ...sources);
                result.body_text = document.body.textContent;
                result.visible_dialog_count = document.body.textContent;
                result.reply_target_id = document.body.textContent;
                return result;
            };
        </script>''', {"target_count": 1, "target_id_link_count": 1},
         ("body_text", "visible_dialog_count")),
        (article + '''<private-secret data-testid="private-token" role="private-role"
            style="display:block;position:fixed;inset:0"></private-secret>
            <script>Array.prototype.includes = () => true;</script>''',
         {"target_count": 1, "reply_center_unobstructed": False},
         ("reply_center_hit_tag", "reply_center_hit_role", "reply_center_hit_test_id")),
        (article + '<script>Object.assign = () => { while (true) {} };</script>',
         {"target_count": 1, "reply_center_unobstructed": True}, ("target_id_link_count",)),
        (article + '<script>document.querySelectorAll = () => { while (true) {} };</script>',
         {"target_count": 1}, ("reply_center_unobstructed", "target_id_link_count")),
    ]
    for html, expected, excluded in hostile_fixtures:
        hostile_page = page.context.new_page()
        try:
            hostile_page.route('**/*', lambda route: route.abort())
            hostile_page.set_content('<!doctype html><base href="https://x.com/">' + html)
            started = monotonic()
            facts = preparation_facts(hostile_page, "12345")
            assert monotonic() - started < 3, facts
            assert facts["snapshot_incomplete"] is True, facts
            assert {key: facts[key] for key in expected} == expected, facts
            assert not any(key in facts for key in excluded), facts
            assert facts["reply_target_id"] == "12345", facts
            assert facts["snapshot_phase"] == "after_failure", facts
            assert "private" not in json.dumps(facts), facts
            assert _safe_context(facts) == facts, facts
            assert hostile_page.evaluate('scrollY') == 0
        finally:
            hostile_page.close()
    print("Reply diagnostics distinguish link shapes, missing/duplicate targets, offscreen/covered controls, editors and login inputs without page content.", flush=True)
    print("Hostile page scripts cannot inject raw fields or trap snapshot execution; original locator facts survive.", flush=True)


def run(playwright):
    check_post_resources(playwright)
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
    displays = []
    with TemporaryDirectory() as directory, native_fixture(playwright, fixture) as processes:
        check_presets()
        print("Native Chromium location presets passed.", flush=True)
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
            major = processes[-1].browser.version.split(".")[0]
            assert "HeadlessChrome" not in user_agent and f"Chrome/{major}." in user_agent
            assert user_agents[0] == user_agent
            brands = browser.page.evaluate("navigator.userAgentData.brands")
            assert any(brand["brand"] in {"Chromium", "Google Chrome"} and brand["version"] == major for brand in brands)
            assert browser.page.evaluate("navigator.webdriver") is False
            assert browser.page.evaluate("navigator.language") == "en-GB"
            assert browser.page.evaluate("Intl.DateTimeFormat().resolvedOptions().timeZone") == "Europe/London"
            assert browser.context is browser.process.browser.contexts[0]
            # Routing WebSockets replaces the page's native constructor even
            # when the page never opens a connection. Keep login pages native.
            assert browser.page.evaluate("Function.prototype.toString.call(WebSocket)") == "function WebSocket() { [native code] }"
            assert browser.page.evaluate("navigator.serviceWorker.register('/worker.js').then(() => true)") is True
            assert browser.page.evaluate("navigator.serviceWorker.getRegistrations().then(items => items.length)") == 0
            browser.context.route("https://x.com/download", lambda route: route.fulfill(
                status=200, headers={"Content-Disposition": "attachment; filename=fixture.txt"}, body="fixture",
            ))
            with browser.page.expect_download() as downloading:
                browser.page.evaluate("() => { const a = document.createElement('a'); a.href='/download'; a.click(); }")
            assert downloading.value.failure(), "Downloads must remain denied in the native context"
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
            check_post_text(browser.page)
            exact_text = "First line\nCafé 😀 +\tend".ljust(280, "x")
            x_post_tweet.prepare_post(browser.page, "example", exact_text)
            assert browser.page.get_by_role("dialog").get_by_test_id("tweetTextarea_0").inner_text() == exact_text
            x_post_tweet.prepare_post(browser.page, "example", "hello from fixture")
            assert x_post_tweet.submit_prepared_post(browser.page, "example") == "https://x.com/example/status/123"
            x_post_tweet.prepare_post(browser.page, "example", "reply from fixture", "12345")
            assert x_post_tweet.submit_prepared_post(browser.page, "example", "12345") == "https://x.com/example/status/123"
            assert calls == ["hello from fixture", "reply from fixture"]
            diagnostic_page = browser.context.new_page()
            try:
                check_reply_diagnostics(diagnostic_page)
            finally:
                diagnostic_page.close()
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
            print("Native Chromium screenshots, input and posting fixtures passed.", flush=True)
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
        assert not processes[-1].browser.is_connected()
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
        assert all(not process.browser.is_connected() for process in processes)
        assert all(display.process.poll() is not None and not Path(display.directory.name).exists() for display in displays)
        print(f"Browser storage restore passed: {len(str(snapshot))} characters of fixture state, cookies/localStorage/IndexedDB restored across process restarts; accounts isolated.")
    run_proxy_failure(playwright)


def run_proxy_failure(playwright):
    """Native WSS uses the relay; HTTPS/WSS never fall back when it fails."""
    import base64
    import hashlib
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
            if self.headers.get("Upgrade", "").lower() == "websocket":
                self.protocol_version = "HTTP/1.1"
                key = self.headers["Sec-WebSocket-Key"]
                accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
                self.send_response(101)
                self.send_header("Upgrade", "websocket")
                self.send_header("Connection", "Upgrade")
                self.send_header("Sec-WebSocket-Accept", accept)
                self.end_headers()
                message = b"native websocket fixture"
                self.wfile.write(bytes([0x81, len(message)]) + message)
                self.wfile.flush()
                return
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
                def websocket_result(page, url):
                    page.evaluate("""url => {
                        window.websocketResult = null;
                        const ws = new WebSocket(url);
                        ws.onmessage = event => { window.websocketResult = event.data; ws.close(); };
                        ws.onerror = () => { window.websocketResult = 'failed'; };
                    }""", url)
                    page.wait_for_function("window.websocketResult !== null")
                    return page.evaluate("window.websocketResult")

                # Only the external gateway dial is substituted. Chromium's
                # CONNECT, production relay and website TLS/upgrade are real.
                def fixture_gateway(_endpoint, host, _credentials):
                    if host != "fixture.example":
                        raise BrowserError("No fixture for Chromium background traffic")
                    return socket.create_connection(("127.0.0.1", 8010), timeout=10)
                with native_fixture(playwright), patch("host.runtime.browser.chromium.BROWSER_NETWORK_PORT", 8011), patch(
                    "host.runtime.browser_network.relay.connect_proxy",
                    side_effect=fixture_gateway,
                ) as proxy:
                    with TunnelServer(accounts.network, port=8011) as tunnel:
                        worker = threading.Thread(target=tunnel.serve_forever, daemon=True)
                        worker.start()
                        browser = None
                        try:
                            browser = accounts.launch_browser(None, "about:blank")
                            # Trust this test's self-signed origin only in the fixture.
                            control = browser.context.new_cdp_session(browser.page)
                            control.send("Security.setIgnoreCertificateErrors", {"ignore": True})
                            assert websocket_result(browser.page, "wss://fixture.example/websocket") == "native websocket fixture"
                            assert requests == ["/websocket"], requests
                            proxy.assert_any_call(("gate.decodo.com", 7000), "fixture.example",
                                                  (accounts.network.settings.proxy_username(), "secret"))
                            def fixture_calls():
                                return sum(call.args[1] == "fixture.example" for call in proxy.call_args_list)
                            count = fixture_calls()
                            assert websocket_result(browser.page, "wss://fixture.example:8443/blocked") == "failed"
                            assert fixture_calls() == count, "Non-443 WSS escaped the relay port restriction"
                            requests.clear()
                        finally:
                            if browser:
                                browser.close()
                            tunnel.shutdown()
                            worker.join()
                print("Native WSS passed through the HTTPS relay; non-443 WSS was rejected.", flush=True)
                with native_fixture(playwright), patch("host.runtime.browser.chromium.BROWSER_NETWORK_PORT", 8011), patch("host.runtime.browser.browser.permitted_url", return_value=True), patch("host.runtime.browser_network.relay.target", return_value="127.0.0.1"), patch("host.runtime.browser_network.relay.connect_proxy", side_effect=BrowserError("fixture gateway unavailable")) as proxy, patch("socket.create_connection") as fallback:
                    with TunnelServer(accounts.network, port=8011) as tunnel:
                        worker = threading.Thread(target=tunnel.serve_forever, daemon=True)
                        worker.start()
                        browser = None
                        try:
                            browser = accounts.launch_browser(None, "about:blank")
                            assert websocket_result(browser.page, "wss://fixture.example/proxy-failed") == "failed"
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
                        assert websocket_result(browser.page, "wss://fixture.example/relay-stopped") == "failed"
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
    print("Browser proxy failure passed: HTTPS/WSS never contacted the reachable origin after failed Decodo or stopped relay.")
