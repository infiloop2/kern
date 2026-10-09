"""Offline Chromium fixtures for LinkedIn's strict DOM/receipt contract.

These fixtures exercise real selectors and input; they do not prove live-site
compatibility. Unknown LinkedIn markup and receipt formats must fail closed.
"""
import json

from browser_adapter_smoke import native_fixture
from host.runtime.browser.browser import Browser
from host.runtime.browser.providers import linkedin
from host.runtime.browser.actions import linkedin_dm as dm
from host.runtime.browser.client import BrowserError

OWNER = "https://www.linkedin.com/in/owner/"
TARGET = "https://www.linkedin.com/in/recipient/"
KEY = "ACoARecipient"
NAV = '''<nav><div class="global-nav__me"><button onclick="document.getElementById('me-menu').hidden = !document.getElementById('me-menu').hidden">Me</button>
<div id="me-menu" hidden><a href="/in/owner/">View Profile</a></div></div></nav>'''


def fixture_html(*, group=False, empty=False, identity=True, pending=False, delayed=False):
    bootstrap = json.dumps({"included": [{"publicIdentifier": "recipient", "entityUrn": "urn:li:fsd_profile:" + KEY}]}) if identity else '{}'
    rows = '' if empty else ''.join(f'''<li class="msg-s-message-list__event"><div class="msg-s-event-listitem">
<a class="msg-s-event-listitem__profile-link" href="/in/{'owner' if n % 2 else 'recipient'}/">{'Owner' if n % 2 else 'Recipient'}</a>
<time datetime="2026-10-08T00:00:0{n}Z"></time><p class="msg-s-event-listitem__body">message {n}</p></div></li>''' for n in range(7))
    if delayed:
        rows = '<li class="msg-s-message-list__empty-state" hidden>Hidden placeholder</li>'
    return f'''<!doctype html><html><body>{NAV}
<main><section><h1>Recipient</h1><button onclick="document.getElementById('thread').hidden=false">Message</button></section></main>
<code><!--{bootstrap}--></code>
<section id="thread" hidden class="msg-overlay-conversation-bubble">
<header class="msg-overlay-bubble-header"><a href="/in/recipient/">Recipient</a>{'<a href="/in/other/">Other</a>' if group else ''}</header>
<div class="msg-s-message-list-container"><ul class="msg-s-message-list-content">{rows}{'<li class="msg-s-message-list__empty-state">No messages</li>' if empty else ''}</ul></div>
<div class="msg-form__contenteditable" contenteditable="true" role="textbox" style="white-space:pre-wrap"></div>
<button onclick="sendFixture()">Send</button></section>
<script>
window.submissions = 0;
{("setTimeout(() => { document.querySelector('.msg-s-message-list-content').insertAdjacentHTML('beforeend', '<li class=msg-s-message-list__event><div class=msg-s-event-listitem><p class=msg-s-event-listitem__body>delayed message</p></div></li>'); }, 500);" if delayed else "")}
async function sendFixture() {{
    window.submissions++;
    const text = window.fixtureSentText ?? document.querySelector('[contenteditable]').innerText;
    const response = await fetch('/voyager/api/voyagerMessagingDashMessengerMessages?action=createMessage', {{method:'POST', body:JSON.stringify({{text}})}});
    if (!response.ok) return;
    const item = document.createElement('li'); item.className = 'msg-s-message-list__event';
    const event = document.createElement('div'); event.className = 'msg-s-event-listitem';
    const body = document.createElement('p'); body.className = 'msg-s-event-listitem__body'; body.style.whiteSpace = 'pre-wrap'; body.textContent = text;
    event.append(body); item.append(event);
    {'event.classList.add("msg-s-event-listitem__pending");' if pending else ''}
    document.querySelector('.msg-s-message-list-content').append(item);
}}
</script></body></html>'''


def run(playwright):
    state = {"html": fixture_html(), "receipt": {"value": {"entityUrn": "urn:li:msg_message:fixture-1"}}}
    def route(request):
        if request.request.method == "POST":
            request.fulfill(status=200, content_type="application/json", body=json.dumps(state["receipt"]))
        else:
            request.fulfill(status=200, content_type="text/html", body=state["html"])
    with native_fixture(playwright, route, pattern="https://www.linkedin.com/**"):
        browser = Browser(None, "about:blank", {"mode": "direct"})
        try:
            page = browser.page
            page.goto(linkedin.login_url)
            assert linkedin.verify_account(page) == OWNER
            recipient = dm.resolve_recipient(page, TARGET)
            assert recipient == {"profile_url": TARGET, "name": "Recipient", "member_key": KEY}
            conversation = dm.open_conversation(page, recipient)
            result = dm.read_messages(conversation, OWNER, recipient)
            assert [m["text"] for m in result["messages"]] == [f'message {i}' for i in range(2, 7)]
            assert [m["direction"] for m in result["messages"]] == ['incoming', 'outgoing', 'incoming', 'outgoing', 'incoming']
            assert result["older_messages"] == "yes"
            text = "hello 🎉\n\n  trailing spaces  "
            submit = dm.prepare_message(conversation, text)
            page.evaluate("text => window.fixtureSentText = text", text)
            assert dm.submit_message(conversation, submit, text) == "urn:li:msg_message:fixture-1"
            assert page.evaluate("window.submissions") == 1

            # Existing drafts are not overwritten; group recipients fail closed.
            try:
                dm.prepare_message(conversation, "overwrite")
                raise AssertionError("existing draft accepted")
            except BrowserError:
                pass
            state["html"] = fixture_html(group=True)
            recipient = dm.resolve_recipient(page, TARGET)
            try:
                dm.open_conversation(page, recipient)
                raise AssertionError("group accepted")
            except BrowserError:
                pass
            assert page.evaluate("window.submissions") == 0

            state["html"] = fixture_html(delayed=True)
            recipient = dm.resolve_recipient(page, TARGET)
            result = dm.read_messages(dm.open_conversation(page, recipient), OWNER, recipient)
            assert result["status"] == "found" and result["messages"][0]["text"] == "delayed message"

            state["html"] = fixture_html(empty=True)
            recipient = dm.resolve_recipient(page, TARGET)
            result = dm.read_messages(dm.open_conversation(page, recipient), OWNER, recipient)
            assert result["status"] == "no_existing_conversation" and not result["messages"]

            state["html"] = fixture_html(identity=False)
            try:
                dm.resolve_recipient(page, TARGET)
                raise AssertionError("unverified member accepted")
            except BrowserError:
                pass

            state["html"] = fixture_html()
            state["receipt"] = {"optimistic": True}
            recipient = dm.resolve_recipient(page, TARGET)
            conversation = dm.open_conversation(page, recipient)
            submit = dm.prepare_message(conversation, "uncertain")
            try:
                dm.submit_message(conversation, submit, "uncertain")
                raise AssertionError("optimistic receipt accepted")
            except BrowserError:
                pass
            assert page.evaluate("window.submissions") == 1
        finally:
            browser.close()
