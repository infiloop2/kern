"""Fixed LinkedIn profile resolution, last-five reads, and approved text DMs.

Website requests are made by Chromium; this adapter never calls private APIs
itself. Only bounded identity/message fields leave the isolated service.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import re
from typing import Any, TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit

from host.runtime.browser.client import BrowserError
from host.runtime.browser.providers import linkedin
from host.runtime.browser.actions.x_composer import MATCHES_TEXT
from host.runtime.core import host_errors

if TYPE_CHECKING:
    from host.runtime.browser.accounts import Profile

TEXT_LIMIT = 8000
DAILY_LIMIT = 50
MEMBER_KEY = re.compile(r"[A-Za-z0-9_-]{5,200}")
ACCOUNT_ID = re.compile(r"acct_[a-f0-9]{32}")
MAX_PROFILE_BYTES = 1_000_000


def validate_request(body: dict[str, Any], *, send: bool = False) -> tuple[str, str, str]:
    expected = {"account_id", "recipient_profile_url"} | ({"text"} if send else set())
    if set(body) != expected or not isinstance(body.get("account_id"), str) or not ACCOUNT_ID.fullmatch(body["account_id"]):
        raise BrowserError("Select a connected Browser account and provide exactly the supported fields.")
    recipient = linkedin.profile_url(body.get("recipient_profile_url"))
    text = body.get("text", "")
    if send and (not isinstance(text, str) or not text.strip() or len(text) > TEXT_LIMIT
                 or any((ord(c) < 32 and c not in "\n\t") or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in text)):
        raise BrowserError("Provide nonempty DM text of at most 8,000 characters without unsupported control characters.")
    return body["account_id"], recipient, text


def validate_recipient(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"profile_url", "name", "member_key"}:
        raise BrowserError("LinkedIn recipient evidence is invalid.")
    url = linkedin.profile_url(value["profile_url"])
    label, key = value["name"], value["member_key"]
    if (not isinstance(label, str) or not label.strip() or len(label) > 200
            or not isinstance(key, str) or not MEMBER_KEY.fullmatch(key)):
        raise BrowserError("LinkedIn recipient identity could not be verified.")
    return {"profile_url": url, "name": label, "member_key": key}


def _report(stage: str, exc: Exception) -> None:
    safe = RuntimeError(str(exc) if isinstance(exc, BrowserError) else type(exc).__name__).with_traceback(exc.__traceback__)
    host_errors.report_warning("browser.linkedin_dm", safe, context={"stage": stage, "failure_type": type(exc).__name__})


def _close(profile: Profile) -> None:
    try:
        profile.close()
    except Exception as exc:
        _report("cleanup", exc)


def _page(profile: Profile, body: dict[str, Any]) -> tuple[Any, str]:
    if profile.provider.name != "linkedin":
        raise BrowserError("This action requires a LinkedIn Browser connection.")
    account = linkedin.validate_identifier(body.get("provider_identifier"))
    if profile.lease or profile.data["state"] != "connected" or account != profile.data["provider_identifier"]:
        raise BrowserError("LinkedIn login needs attention or is under operator control.")
    page = profile.launch(site="about:blank", block_media=True).page
    page.goto(linkedin.login_url, wait_until="domcontentloaded", timeout=20000)
    if linkedin.verify_account(page) != account:
        profile.data["state"] = "needs_attention"
        profile.save()
        raise BrowserError("The signed-in LinkedIn account changed. Check the connection in Browser settings.")
    return page, account


def _member_keys(payload: object, slug: str) -> set[str]:
    """Resolve only profile entities explicitly naming the requested public ID.

    LinkedIn's bootstrap/observed responses may contain unrelated profiles. Never
    take the first URN or equate an official OAuth subject with a web member key.
    """
    found: set[str] = set()
    todo = [payload]
    visited = 0
    while todo and visited < 10000:
        item = todo.pop()
        visited += 1
        if isinstance(item, dict):
            public_id = item.get("publicIdentifier")
            if isinstance(public_id, str) and public_id.lower() == slug:
                urn = item.get("entityUrn", "")
                if isinstance(urn, str):
                    match = re.fullmatch(r"urn:li:(?:fsd_profile|fs_miniProfile|fs_profile):([A-Za-z0-9_-]{5,200})", urn)
                    if match:
                        found.add(match[1])
            todo.extend(item.values())
        elif isinstance(item, list):
            todo.extend(item)
    return found if not todo else set()


def _bounded_response_json(response: Any) -> object:
    # Playwright body() materializes decoded bytes. A post-allocation check is
    # insufficient: do not read chunked, unknown-length or compressed bodies.
    # Unknown evidence fails closed; no fallback requests or proxy changes.
    encoding = response.header_value("content-encoding")
    length = response.header_value("content-length")
    if (encoding not in {None, "", "identity"} or not isinstance(length, str)
            or not re.fullmatch(r"[0-9]{1,7}", length)
            or not 0 < int(length) <= MAX_PROFILE_BYTES):
        raise BrowserError("LinkedIn response size could not be safely bounded.")
    raw = response.body()
    if len(raw) != int(length) or len(raw) > MAX_PROFILE_BYTES:
        raise BrowserError("LinkedIn response size did not match its supported bound.")
    return json.loads(raw)


def resolve_recipient(page: Any, url: str) -> dict[str, str]:
    from playwright.sync_api import expect  # type: ignore[import-not-found]
    slug = urlsplit(url).path.split("/")[2]
    keys: set[str] = set()

    def observe(response: Any) -> None:
        parts = urlsplit(response.url)
        if parts.hostname != "www.linkedin.com" or not parts.path.startswith("/voyager/api/") or response.status != 200:
            return
        try:
            keys.update(_member_keys(_bounded_response_json(response), slug))
        except Exception:
            # Unknown formats are not identity evidence; never log provider data.
            pass

    page.on("response", observe)
    try:
        response = page.goto(url, wait_until="domcontentloaded", timeout=20000)
        if response is not None and response.status >= 400:
            raise BrowserError(f"LinkedIn returned HTTP {response.status} while opening the recipient profile.")
        if linkedin.profile_url(page.url) != url:
            raise BrowserError("The recipient profile redirected to a different identity. Use its current profile URL.")
        title = page.locator("main h1").filter(visible=True)
        expect(title).to_have_count(1, timeout=10000)
        label = title.inner_text(timeout=10000).strip()
        # Bootstrap JSON complements responses already available before hydration.
        encoded = page.locator("code").evaluate_all("""nodes => {
            const result = []; let size = 0;
            for (const node of nodes.slice(0, 100)) {
                const value = Array.from(node.childNodes).map(child => child.textContent || '').join('');
                if (value.length > 1000000 || size + value.length > 1000000) continue;
                size += value.length; result.push(value);
            }
            return result;
        }""")
        for value in encoded:
            try:
                keys.update(_member_keys(json.loads(value), slug))
            except (ValueError, TypeError):
                pass
        # The profile's own Message link can also carry the web member key.
        card = page.locator("main section").filter(has=page.locator("h1")).filter(visible=True)
        expect(card).to_have_count(1, timeout=10000)
        links = card.get_by_role("link", name=re.compile(r"^Message(?:\s|$)")).filter(visible=True)
        for href in links.evaluate_all("nodes => nodes.map(node => node.getAttribute('href') || '')"):
            match = re.fullmatch(r"(?:https://www[.]linkedin[.]com)?/messaging/compose/\?recipient=([A-Za-z0-9_-]{5,200})", href)
            if match:
                keys.add(match[1])
        if len(keys) != 1:
            raise BrowserError("LinkedIn did not expose one verified recipient identity. No message was submitted.")
        return validate_recipient({"profile_url": url, "name": label, "member_key": keys.pop()})
    finally:
        page.remove_listener("response", observe)


def open_conversation(page: Any, recipient: dict[str, str]) -> Any:
    from playwright.sync_api import expect  # type: ignore[import-not-found]
    title = page.locator("main h1").filter(visible=True)
    card = page.locator("main section").filter(has=page.locator("h1")).filter(visible=True)
    message = card.get_by_role("button", name=re.compile(r"^Message(?:\s|$)")).or_(
        card.get_by_role("link", name=re.compile(r"^Message(?:\s|$)"))).filter(visible=True)
    expect(message).to_have_count(1, timeout=10000)
    message.click(timeout=10000)
    conversation = page.locator(".msg-overlay-conversation-bubble").filter(visible=True)
    expect(conversation).to_have_count(1, timeout=10000)
    if conversation.locator("input[name='subject'], .msg-form__subject, .msg-inmail-compose").filter(visible=True).count():
        raise BrowserError("InMail is not supported by this action.")
    verify_conversation(conversation, recipient)
    return conversation


def verify_conversation(conversation: Any, recipient: dict[str, str]) -> None:
    from playwright.sync_api import expect  # type: ignore[import-not-found]
    # Inspect participants in the header, never mentions or links in message text.
    links = conversation.locator(".msg-overlay-bubble-header a[href*='/in/'], .msg-thread__link-to-profile").filter(visible=True)
    expect(links).not_to_have_count(0, timeout=10000)
    urls = {linkedin.profile_url("https://www.linkedin.com" + href if href.startswith("/in/") else href)
            for href in links.evaluate_all("nodes => nodes.map(node => node.getAttribute('href') || '')")}
    if urls != {recipient["profile_url"]}:
        raise BrowserError("LinkedIn did not confirm a one-to-one conversation with the approved recipient.")
    if conversation.locator(".msg-connections-typeahead__recipient").count() > 1:
        raise BrowserError("Group messages are not supported.")


MESSAGES = """root => {
    const events = Array.from(root.querySelectorAll('.msg-s-event-listitem'));
    return events.slice(-6).map(event => {
        const text = event.querySelector('.msg-s-event-listitem__body')?.innerText || '';
        let sender = event.closest('.msg-s-message-list__event')?.querySelector('.msg-s-message-group__profile-link, .msg-s-event-listitem__profile-link');
        if (!sender) {
            let previous = event.closest('.msg-s-message-list__event')?.previousElementSibling;
            for (let i = 0; previous && i < 30; i++, previous = previous.previousElementSibling) {
                sender = previous.querySelector('.msg-s-message-group__profile-link, .msg-s-event-listitem__profile-link');
                if (sender) break;
            }
        }
        return {
            text: text.slice(0, 8000), text_truncated: text.length > 8000,
            sender_name: (sender?.innerText || '').trim().slice(0, 200),
            sender_url: sender?.getAttribute('href') || '',
            timestamp: (event.querySelector('time')?.getAttribute('datetime') ||
                event.querySelector('.msg-s-message-group__timestamp')?.innerText || '').slice(0, 100),
            has_attachment: !!event.querySelector('.msg-s-event-listitem__attachment, .msg-s-event-listitem__image, .msg-s-event-listitem__file')
        };
    });
}"""


def read_messages(conversation: Any, account: str, recipient: dict[str, str]) -> dict[str, Any]:
    from playwright.sync_api import expect  # type: ignore[import-not-found]
    listing = conversation.locator(".msg-s-message-list-content")
    expect(listing).to_have_count(1, timeout=10000)
    expect(listing).to_be_visible(timeout=10000)
    # Scroll only to the latest end; never fetch older pages or attachments.
    listing.evaluate("node => { const scroll = node.closest('.msg-s-message-list-container'); if (scroll) scroll.scrollTop = scroll.scrollHeight; }")
    conversation.page.wait_for_function("""root => {
        const visible = node => node.getClientRects().length > 0 && getComputedStyle(node).visibility !== 'hidden';
        return Array.from(root.querySelectorAll('.msg-s-event-listitem, .msg-s-message-list__empty-state')).some(visible);
    }""", arg=listing.element_handle(), timeout=10000)
    expect(conversation.locator(".artdeco-loader").filter(visible=True)).to_have_count(0, timeout=10000)
    verify_conversation(conversation, recipient)
    rows = listing.evaluate(MESSAGES)
    if not rows:
        empty = listing.locator(".msg-s-message-list__empty-state").filter(visible=True)
        expect(empty).to_have_count(1, timeout=10000)
        expect(empty).to_be_visible(timeout=10000)
    messages = []
    for row in rows[-5:]:
        sender = ""
        try:
            href = row.pop("sender_url")
            sender = linkedin.profile_url("https://www.linkedin.com" + href if href.startswith("/in/") else href)
        except BrowserError:
            pass
        messages.append({**row, "direction": "outgoing" if sender == account else "incoming" if sender == recipient["profile_url"] else "unknown"})
    return {"status": "found" if messages else "no_existing_conversation", "recipient": recipient,
            "messages": messages, "older_messages": "yes" if len(rows) > 5 else "unknown",
            "may_mark_read": True}


def resolve(profile: Profile, body: dict[str, Any]) -> dict[str, Any]:
    if set(body) != {"provider_identifier", "recipient_profile_url"}:
        raise BrowserError("Unsupported LinkedIn resolution fields.")
    url = linkedin.profile_url(body["recipient_profile_url"])
    try:
        page, _account = _page(profile, body)
        return {"recipient": resolve_recipient(page, url)}
    finally:
        _close(profile)


def read(profile: Profile, body: dict[str, Any]) -> dict[str, Any]:
    if set(body) != {"provider_identifier", "recipient_profile_url"}:
        raise BrowserError("Unsupported LinkedIn read fields.")
    url = linkedin.profile_url(body["recipient_profile_url"])
    try:
        page, account = _page(profile, body)
        recipient = resolve_recipient(page, url)
        return read_messages(open_conversation(page, recipient), account, recipient)
    finally:
        _close(profile)


def prepare_message(conversation: Any, text: str) -> Any:
    from playwright.sync_api import expect  # type: ignore[import-not-found]
    editor = conversation.locator(".msg-form__contenteditable[contenteditable='true']").filter(visible=True)
    expect(editor).to_have_count(1, timeout=10000)
    expect(editor).to_be_editable(timeout=10000)
    # Do not overwrite an operator's existing draft.
    if not conversation.page.evaluate(MATCHES_TEXT, [editor.element_handle(), ""]):
        raise BrowserError("This LinkedIn conversation already has a draft. Clear it yourself before requesting another approval.")
    editor.press_sequentially(text, delay=0, timeout=20000)
    # Reuse the logical contenteditable reader: layout-generated line breaks
    # and image emoji must not change the exact approved copy.
    if not conversation.page.evaluate(MATCHES_TEXT, [editor.element_handle(), text]):
        raise BrowserError("The LinkedIn composer did not exactly match the approved text.")
    submit = conversation.get_by_role("button", name="Send", exact=True).filter(visible=True)
    expect(submit).to_have_count(1, timeout=10000)
    expect(submit).to_be_enabled(timeout=10000)
    return submit


def _send_response(response: Any) -> bool:
    parts = urlsplit(response.url)
    return (response.request.method == "POST" and parts.hostname == "www.linkedin.com"
            and parts.path == "/voyager/api/voyagerMessagingDashMessengerMessages"
            and parse_qs(parts.query).get("action") == ["createMessage"])


def _message_id(payload: object) -> str:
    if not isinstance(payload, dict) or payload.get("errors"):
        raise BrowserError("LinkedIn did not return a successful message receipt.")
    candidates: set[str] = set()
    todo = [payload]
    visited = 0
    while todo and visited < 10000:
        item = todo.pop()
        visited += 1
        if isinstance(item, dict):
            urn = item.get("entityUrn")
            if isinstance(urn, str) and re.fullmatch(r"urn:li:(?:msg_message|fsd_message):[A-Za-z0-9_(),:=+/-]{1,500}", urn):
                candidates.add(urn)
            todo.extend(item.values())
        elif isinstance(item, list):
            todo.extend(item)
    if todo or len(candidates) != 1:
        raise BrowserError("LinkedIn did not return one message receipt.")
    return candidates.pop()


def submit_message(conversation: Any, submit: Any, text: str) -> str:
    from playwright.sync_api import expect  # type: ignore[import-not-found]
    page = conversation.page
    events = conversation.locator(".msg-s-event-listitem")
    before = events.count()
    editor = conversation.locator(".msg-form__contenteditable[contenteditable='true']").filter(visible=True)
    if not page.evaluate(MATCHES_TEXT, [editor.element_handle(), text]):
        raise BrowserError("The LinkedIn composer changed before submission.")
    # Observe the website's response to our one Send click. Optimistic DOM
    # insertion alone must never claim success. Unknown endpoints fail closed.
    with page.expect_response(_send_response, timeout=20000) as pending:
        submit.click(timeout=5000)
    response = pending.value
    if response.status not in {200, 201}:
        raise BrowserError("LinkedIn did not accept the message submission.")
    message_id = _message_id(_bounded_response_json(response))
    expect(events).to_have_count(before + 1, timeout=20000)
    last = events.last.locator(".msg-s-event-listitem__body")
    if not page.evaluate(MATCHES_TEXT, [last.element_handle(), text]):
        raise BrowserError("LinkedIn did not confirm the submitted message text.")
    expect(events.last).not_to_have_class(re.compile(r"msg-s-event-listitem__(?:error|pending)"), timeout=20000)
    expect(events.last.locator(".msg-s-event-listitem__error, .msg-s-event-listitem__pending")).to_have_count(0, timeout=20000)
    return message_id


def send(profile: Profile, body: dict[str, Any]) -> dict[str, Any]:
    if set(body) != {"provider_identifier", "recipient", "text"}:
        raise BrowserError("Unsupported LinkedIn send fields.")
    approved = validate_recipient(body["recipient"])
    _, _url, text = validate_request({"account_id": profile.account_id, "recipient_profile_url": approved["profile_url"], "text": body["text"]}, send=True)
    day = datetime.now(timezone.utc).date().isoformat()
    usage = profile.data["usage"].get("linkedin_send_dm")
    count = usage["count"] if usage and usage["day"] == day else 0
    if count >= DAILY_LIMIT:
        raise BrowserError("The daily LinkedIn Browser sending limit has been reached.")
    try:
        try:
            page, account = _page(profile, body)
            current = resolve_recipient(page, approved["profile_url"])
            if current != approved:
                raise BrowserError("The LinkedIn recipient changed after approval. Request a new approval using the current profile.")
            conversation = open_conversation(page, approved)
            submit = prepare_message(conversation, text)
            # Recipient and sender are checked again immediately before clicking.
            if linkedin.verify_account(page) != account:
                raise BrowserError("The signed-in LinkedIn account changed before submission.")
            verify_conversation(conversation, approved)
        except Exception as exc:
            _report("prepare", exc)
            detail = str(exc) if isinstance(exc, BrowserError) else type(exc).__name__
            raise BrowserError(f"DM was not submitted. {detail} Open LinkedIn to check the account.") from None
        profile.data["usage"]["linkedin_send_dm"] = {"day": day, "count": count + 1}
        profile.save()
        try:
            message_id = submit_message(conversation, submit, text)
        except Exception as exc:
            _report("confirm", exc)
            raise BrowserError("Could not confirm sending. The DM may have been sent. Check LinkedIn before approving another attempt.") from None
        return {"status": "sent", "recipient_profile_url": approved["profile_url"], "message_id": message_id}
    finally:
        _close(profile)
