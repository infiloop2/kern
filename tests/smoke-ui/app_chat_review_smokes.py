"""App activity paging, reading anchors, and composer viewport changes."""

import re
from typing import Any
from urllib.parse import parse_qs, urlparse


def run(page: Any, url: str, log_in: Any, *, scroll_during_load: bool = False, short_transcript: bool = False) -> None:
    from playwright.sync_api import expect

    events = [
        {"seq": 1, "event_type": "thread.message", "payload": {"source": "agent", "message": "Before clear"}},
        {"seq": 100, "event_type": "thread.memory_cleared", "payload": {"message": "Working memory cleared."}},
    ]
    replies = {101, 130} if short_transcript else {101, 115, 130, 140, 155}
    for seq in range(101, 156):
        if seq in replies:
            paragraphs = 0 if short_transcript else 80 if seq == 140 else 2 if seq == 155 else 8
            payload = {"source": "agent", "message": f"Reply {seq}\n\n" + "\n\n".join(
                f"Response paragraph {index}." for index in range(paragraphs)
            )}
            kind = "thread.message"
        else:
            payload = {"activity": {"provider": "codex", "activity_id": f"step-{seq}",
                                    "kind": "command", "status": "completed", "title": f"Step {seq}",
                                    "detail": "\n".join(f"Output line {index}" for index in range(40))
                                    if seq == 154 else "Completed the command."}}
            kind = "thread.activity"
        events.append({"seq": seq, "event_type": kind, "payload": payload})

    held_activity_page: list[Any] = []

    def event_page(route: Any) -> None:
        query = parse_qs(urlparse(route.request.url).query)
        rows = [event for event in events if query.get("activity") != ["false"] or event["event_type"] != "thread.activity"]
        if "since" in query:
            rows = [event for event in rows if event["seq"] > int(query["since"][0])][:6]
        else:
            before = int(query.get("before", ["10000"])[0])
            rows = [event for event in rows if event["seq"] < before][-6:]
        if scroll_during_load and query.get("activity") != ["false"] and not ({"before", "since"} & query.keys()):
            held_activity_page.append((route, rows))
            return
        route.fulfill(json={"events": rows})

    page.route("**/v1/workspace/web-apps/apps/*/conversation/events*", event_page)
    log_in(page, url)
    nav = page.get_by_role("button", name="Open navigation", exact=True)
    if nav.is_visible():
        nav.click()
    page.get_by_role("button", name="New app", exact=True).click()
    app = page.locator("#panel-workspace-web-apps")
    expect(app.locator("#app-title")).to_have_text(re.compile(r"app-\d+"))
    app_id = app.locator("#app-title").inner_text()
    original_viewport = page.viewport_size
    try:
        app.locator("#history-toggle").click()
        expect(app.locator(".chat-history-entry.stopped")).to_contain_text("Working memory cleared.")
        expect(app.locator("#chat-history-list")).not_to_contain_text("Before clear")
        scroll = app.locator("#chat-history-scroll")
        area = app.locator("#message")
        toggle = app.get_by_role("switch", name="Activity", exact=True)
        more = app.locator("#chat-history-more")

        def align_reply(seq: int, offset: int = 20, *, resize_during_scroll: bool = False) -> None:
            app.locator(f'.chat-history-entry.agent:has-text("Reply {seq}")').evaluate("""(element, [offset, resizeDuringScroll]) => {
                const scroll = document.querySelector('#panel-workspace-web-apps').shadowRoot.querySelector('#chat-history-scroll');
                const previous = scroll.scrollTop;
                return new Promise(resolve => {
                    const settled = () => { scroll.removeEventListener('scroll', settled); resolve(); };
                    scroll.addEventListener('scroll', settled, {once: true});
                    scroll.scrollTop += element.getBoundingClientRect().top - scroll.getBoundingClientRect().top - offset;
                    if (resizeDuringScroll) window.dispatchEvent(new Event("resize"));
                    if (scroll.scrollTop === previous) settled();
                });
            }""", [offset, resize_during_scroll])

        def expect_reply_offset(seq: int, offset: int = 20) -> None:
            try:
                page.wait_for_function("""([seq, offset]) => {
                    const root = document.querySelector('#panel-workspace-web-apps').shadowRoot;
                    const entry = [...root.querySelectorAll('.chat-history-entry.agent')]
                        .find(element => element.textContent.includes(`Reply ${seq}`));
                    const scroll = root.querySelector('#chat-history-scroll');
                    return entry && Math.abs(entry.getBoundingClientRect().top - scroll.getBoundingClientRect().top - offset) < 2;
                }""", arg=[seq, offset])
            except Exception:
                actual = scroll.evaluate("""(scroll, seq) => {
                    const entry = [...scroll.querySelectorAll('.chat-history-entry.agent')]
                        .find(element => element.textContent.includes(`Reply ${seq}`));
                    return {top: scroll.scrollTop, height: scroll.scrollHeight, client: scroll.clientHeight,
                        offset: entry?.getBoundingClientRect().top - scroll.getBoundingClientRect().top};
                }""", seq)
                raise AssertionError(f"Reply {seq} expected offset {offset}; actual {actual}") from None


        if short_transcript:
            toggle.click()
            # The short viewport can automatically page again after the first
            # response. Wait for pagination, not a transient six-row count.
            expect(more).to_be_visible()
            for _ in range(10):
                if not more.is_visible():
                    break
                more.evaluate("element => { if (!element.hidden) element.click(); }")
                expect(more).to_have_text("Load earlier messages")
            expect(app.locator(".chat-history-entry.activity")).to_have_count(53)
            # Start pinned at the tail, then scroll and toggle in one task,
            # before the browser delivers the upward scroll event.
            scroll.evaluate("""scroll => {
                const root = scroll.getRootNode();
                scroll.scrollTop = scroll.scrollHeight;
                scroll.dispatchEvent(new Event('scroll'));
                const reply = root.querySelector('[data-entry-key="event-130"]');
                scroll.scrollTop += reply.getBoundingClientRect().top - scroll.getBoundingClientRect().top - 20;
                root.querySelector('#activity-toggle').click();
            }""")
            expect_reply_offset(130)
            for _ in range(3):
                page.evaluate("() => window.KernWebApps.refresh()")
                page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
                expect_reply_offset(130)
            print("PASS: Short transcript preserves its reading anchor", flush=True)
            return

        # The conversation lane knows the clear, while the full lane still has
        # several pages of valid post-clear activity to fetch.
        align_reply(140)
        toggle.click()
        if scroll_during_load:
            for _ in range(500):
                if held_activity_page:
                    break
                page.wait_for_timeout(10)
            assert held_activity_page, "Activity request did not reach the held response"
            align_reply(130)
            # A newer poll supersedes the request started by the toggle.
            page.evaluate("() => { void window.KernWebApps.refresh(); }")
            for _ in range(500):
                if len(held_activity_page) >= 2:
                    break
                page.wait_for_timeout(10)
            assert len(held_activity_page) >= 2, "The competing poll did not reach Activity"
            for route, rows in held_activity_page:
                route.fulfill(json={"events": rows})
            held_activity_page.clear()
        expect(more).to_be_visible()
        expect(app.locator(".chat-history-entry.activity")).to_have_count(5)
        if scroll_during_load:
            expect_reply_offset(130)
        else:
            expect_reply_offset(140)
        for _ in range(10):
            if not more.is_visible():
                break
            # Scrolling this button into view can itself page history and
            # hide it before Playwright clicks. Activate the current control.
            more.evaluate("element => { if (!element.hidden) element.click(); }")
            expect(more).to_have_text("Load earlier messages")
        expect(app.locator(".chat-history-entry.activity")).to_have_count(50)
        expect(more).to_be_hidden()
        expect(app.locator("#chat-history-list")).not_to_contain_text("Before clear")

        # Reopening an initialized lane immediately fetches activity that
        # arrived while only the conversation lane was being refreshed.
        toggle.click()
        events.append({"seq": 156, "event_type": "thread.activity", "payload": {"activity": {
            "provider": "codex", "activity_id": "step-156", "kind": "command",
            "status": "completed", "title": "New hidden activity", "detail": "Completed the command.",
        }}})
        toggle.click()
        expect(app.locator(".chat-history-entry.activity")).to_have_count(51)

        # Opening long output must stop tail following before a state poll.
        scroll.evaluate("element => { element.scrollTop = element.scrollHeight; }")
        card = app.locator('.activity-card[data-activity-id="step-154"]')
        card.locator("summary").click()
        expect(card).to_have_attribute("open", "")
        page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
        position = scroll.evaluate("element => element.scrollTop")
        page.evaluate("() => window.KernWebApps.refresh()")
        assert abs(scroll.evaluate("element => element.scrollTop") - position) < 2
        card.locator("summary").click()

        align_reply(130)
        toggle.click()
        expect_reply_offset(130)
        toggle.click()
        expect_reply_offset(130)

        # Preserve the response being read even when its top is offscreen
        # and a later message follows activity below it.
        align_reply(130, -80)
        toggle.click()
        expect_reply_offset(130, -80)
        toggle.click()
        expect_reply_offset(130, -80)

        # Hiding activity near the tail may need temporary bottom spacing.
        # Scrolling to the end must remove it and resume tail following.
        app.locator('.chat-history-entry.agent:has-text("Reply 140")').evaluate("""element => {
            const scroll = document.querySelector('#panel-workspace-web-apps').shadowRoot.querySelector('#chat-history-scroll');
            scroll.scrollTop += element.getBoundingClientRect().bottom - scroll.getBoundingClientRect().top - 60;
        }""")
        page.evaluate("() => new Promise(requestAnimationFrame)")
        toggle.click()
        page.wait_for_function("""() => document.querySelector('#panel-workspace-web-apps')
            .shadowRoot.querySelector('#chat-history-list').style.getPropertyValue('--activity-anchor-space') !== ''""")
        saved_position = scroll.evaluate("element => element.scrollTop")
        page.evaluate("() => window.KernWebApps.refresh()")
        reply = app.locator('.chat-history-entry.agent:has-text("Reply 140")')
        reply.dispatch_event("pointerdown")
        reply.dispatch_event("keydown", {"key": "Enter"})
        assert app.locator("#chat-history-list").evaluate("element => element.style.getPropertyValue('--activity-anchor-space')")
        assert abs(scroll.evaluate("element => element.scrollTop") - saved_position) < 2
        scroll.hover()
        page.mouse.wheel(0, 600)
        page.wait_for_function("""() => document.querySelector('#panel-workspace-web-apps')
            .shadowRoot.querySelector('#chat-history-list').style.getPropertyValue('--activity-anchor-space') === ''""")

        # Focus precedes the keyboard/viewport shrinking. Tail readers stay
        # pinned; readers above the tail keep their place.
        scroll.evaluate("element => { element.scrollTop = element.scrollHeight; }")
        page.evaluate("() => new Promise(requestAnimationFrame)")
        area.focus()
        page.set_viewport_size({**original_viewport, "height": original_viewport["height"] - 250})
        page.wait_for_function("""() => {
            const scroll = document.querySelector('#panel-workspace-web-apps').shadowRoot.querySelector('#chat-history-scroll');
            return scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 2;
        }""")
        page.set_viewport_size(original_viewport)
        page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
        area.evaluate("element => element.blur()")
        align_reply(130, resize_during_scroll=True)
        expect_reply_offset(130)
        area.focus()
        page.set_viewport_size({**original_viewport, "height": original_viewport["height"] - 250})
        expect_reply_offset(130)
        print("PASS: App activity paging, reading anchors, and viewport tail", flush=True)
    finally:
        page.set_viewport_size(original_viewport)
        page.evaluate("appId => window.KernHost.api('POST', `/v1/workspace/web-apps/apps/${appId}/archive`, {})", app_id)


def run_activity_only_paging(page: Any, url: str, log_in: Any) -> None:
    from playwright.sync_api import expect

    def event_page(route: Any) -> None:
        query = parse_qs(urlparse(route.request.url).query)
        before = int(query.get("before", ["61"])[0])
        rows = [] if query.get("activity") == ["false"] or "since" in query else [
            {"seq": seq, "event_type": "thread.activity", "payload": {"activity": {
                "provider": "codex", "activity_id": f"only-{seq}", "kind": "command",
                "status": "completed", "title": f"Command {seq}", "detail": "Finished.",
            }}} for seq in range(max(1, before - 6), before)
        ]
        route.fulfill(json={"events": rows})

    page.route("**/v1/workspace/web-apps/apps/*/conversation/events*", event_page)
    log_in(page, url)
    page.get_by_role("button", name="New app", exact=True).click()
    app = page.locator("#panel-workspace-web-apps")
    expect(app.locator("#app-title")).to_have_text(re.compile(r"app-\d+"))
    app_id = app.locator("#app-title").inner_text()
    try:
        app.locator("#history-toggle").click()
        app.get_by_role("switch", name="Activity", exact=True).click()
        more = app.locator("#chat-history-more")
        expect(more).to_be_visible()
        for _ in range(3):
            more.evaluate("element => element.click()")
            expect(more).to_have_text("Load earlier messages")
        card = app.locator('.activity-card[data-activity-id="only-50"]')
        card.evaluate("""element => {
            const scroll = document.querySelector('#panel-workspace-web-apps').shadowRoot.querySelector('#chat-history-scroll');
            return new Promise(resolve => {
                scroll.addEventListener('scroll', () => resolve(), {once: true});
                scroll.scrollTop += element.getBoundingClientRect().top - scroll.getBoundingClientRect().top - 20;
            });
        }""")
        more.evaluate("element => element.click()")
        expect(more).to_have_text("Load earlier messages")
        page.wait_for_function("""() => {
            const root = document.querySelector('#panel-workspace-web-apps').shadowRoot;
            return Math.abs(root.querySelector('[data-activity-id="only-50"]').getBoundingClientRect().top
                - root.querySelector('#chat-history-scroll').getBoundingClientRect().top - 20) < 2;
        }""")
        print("PASS: Activity-only history preserves the visible card when paging", flush=True)
    finally:
        page.evaluate("appId => window.KernHost.api('POST', `/v1/workspace/web-apps/apps/${appId}/archive`, {})", app_id)
