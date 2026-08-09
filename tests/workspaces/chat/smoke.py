"""Agent Chat smoke-test backend and UI checks."""

from __future__ import annotations

from http import HTTPStatus
from pathlib import Path
import re
import tempfile
from typing import Any, Callable

from host.session_options import public_session_options


ApiErrorFactory = Callable[[HTTPStatus, str], Exception]
HostApi = Callable[[str, str, dict[str, list[str]], Any], dict[str, Any]]
AGENT_CHAT_EVENT_PAGE = 6
# Agent Chat surfaces a curated set of the host's seeded threads, a lived-in mix
# of runtimes and turn states: a shipped infra fix (codex, two completed turns),
# a design audit with a failed deploy (claude_code), an active incident (codex,
# running), a cancelled dependency audit (codex), and a finished docs cleanup
# (claude_code). The app-facing thread id must equal the host thread id, since
# event and archive routes query the host by it directly.
AGENT_CHAT_THREADS: dict[str, dict[str, Any]] = {
    thread_id: {"thread_id": thread_id, "name": thread_id, "archived": False}
    for thread_id in (
        "website-redesign",
        "thread-1",
        "thread-2",
        "thread-3",
        "activity-heavy",
    )
}


def route_workspace_api(
    method: str,
    relative: str,
    query: dict[str, list[str]],
    body: Any,
    api_error: ApiErrorFactory,
    host_api: HostApi,
) -> dict[str, Any]:
    if method == "GET" and relative == "session-options":
        return {"session_options": public_session_options()}
    if method == "GET" and relative == "threads":
        archived = (query.get("archived") or ["false"])[0] == "true"
        return {"threads": _list_threads(host_api, archived=archived)}
    match = re.fullmatch(r"threads/([^/]+)/events", relative)
    if method == "GET" and match:
        thread_id = match.group(1)
        _require_agent_chat_thread(thread_id, api_error, include_archived=True)
        include_activity = (query.get("activity") or ["true"])[0] == "true"
        event_types = [
            "thread.message",
            "thread.error",
            "thread.stopped",
            # Outside the activity filter, like the real backend.
            "thread.memory_cleared",
        ]
        if include_activity:
            event_types.insert(1, "thread.activity")
        host_query = {
            "limit": [str(AGENT_CHAT_EVENT_PAGE)],
            "event_type": event_types,
        }
        host_query.update({
            key: query[key]
            for key in ("since", "before")
            if key in query
        })
        return host_api("GET", f"/v1/threads/{thread_id}/events", host_query, None)
    match = re.fullmatch(r"threads/([^/]+)/(archive|unarchive)", relative)
    if method == "POST" and match:
        thread_id, action = match.groups()
        _require_agent_chat_thread(thread_id, api_error, include_archived=True)
        AGENT_CHAT_THREADS[thread_id]["archived"] = action == "archive"
        return {"thread": dict(AGENT_CHAT_THREADS[thread_id])}
    match = re.fullmatch(r"threads/([^/]+)/name", relative)
    if method == "PUT" and match:
        thread_id = match.group(1)
        _require_agent_chat_thread(thread_id, api_error, include_archived=True)
        name = body.get("name", "").strip() if isinstance(body, dict) else ""
        if not name or len(name) > 100:
            raise api_error(HTTPStatus.BAD_REQUEST, "invalid thread name")
        AGENT_CHAT_THREADS[thread_id]["name"] = name
        return {"thread": dict(AGENT_CHAT_THREADS[thread_id])}
    if method == "POST" and relative == "messages":
        if isinstance(body, dict) and body.get("thread_id") in AGENT_CHAT_THREADS:
            thread_id = body["thread_id"]
            _require_agent_chat_thread(thread_id, api_error)
        elif isinstance(body, dict) and "thread_id" not in body:
            # Mirror the real backend: no thread_id means a new thread with
            # the next successive generated name.
            thread_id = _generate_thread_id()
        else:
            raise api_error(HTTPStatus.NOT_FOUND, "thread not found")
        host_request: dict[str, Any] = {"message": body.get("input_message", "")}
        for field in ("agent_runtime", "model", "effort"):
            if field in body:
                host_request[field] = body[field]
        response = host_api("POST", f"/v1/threads/{thread_id}/messages", {}, host_request)
        AGENT_CHAT_THREADS.setdefault(
            thread_id,
            {"thread_id": thread_id, "name": thread_id, "archived": False},
        )
        return {"action": response["status"], "thread_id": thread_id}
    match = re.fullmatch(r"threads/([^/]+)/stop", relative)
    if method == "POST" and match:
        thread_id = match.group(1)
        _require_agent_chat_thread(thread_id, api_error, include_archived=True)
        return host_api("POST", f"/v1/threads/{thread_id}/stop", {}, body)
    match = re.fullmatch(r"threads/([^/]+)/clear-memory", relative)
    if method == "POST" and match:
        thread_id = match.group(1)
        # Clearing is a write, so an archived thread is refused here.
        _require_agent_chat_thread(thread_id, api_error)
        return host_api("POST", f"/v1/threads/{thread_id}/clear-memory", {}, body)
    raise api_error(HTTPStatus.NOT_FOUND, "mock app route not found")


def desktop_smoke(page: Any) -> None:
    from playwright.sync_api import expect

    new_chat = page.get_by_role("button", name="New chat", exact=True)
    expect(new_chat).to_be_visible()
    new_chat.click()
    expect(page.locator("#panel-workspace-chat")).to_be_visible()
    page.get_by_role("button", name="Home", exact=True).click()
    new_chat.click()
    expect(page.locator("#panel-workspace-chat")).to_be_visible()
    frame = page.locator("#panel-workspace-chat")

    expect(frame.locator(".app-frame-title")).to_have_text("Agent Chat")
    expect(frame.locator("#status")).to_be_hidden()
    # The host sidebar owns the lived-in list across runtimes and turn states.
    expect(page.locator("#chat-nav-items")).to_contain_text("website-redesign")
    expect(page.locator("#chat-nav-items")).to_contain_text("thread-1")

    _open_host_thread(page, "website-redesign")
    expect(frame.locator(".thread-title")).to_have_text("website-redesign")
    expect(frame.get_by_role("switch", name="Activity", exact=True)).to_be_visible()
    expect(frame.locator("#new-task-runtime")).to_have_value("claude_code")
    expect(frame.locator("#new-task-runtime")).to_be_enabled()
    frame.locator("#new-task-model").select_option("claude-fable-5")
    expect(frame.locator("#session-change-warning")).to_be_visible()
    expect(frame.locator("#session-change-warning")).to_contain_text(
        "provider cache reads will be invalidated"
    )
    frame.locator("#new-task-model").select_option("claude-opus-5")
    expect(frame.locator("#session-change-warning")).to_be_hidden()
    page.once("dialog", lambda dialog: dialog.accept("Website refresh"))
    frame.get_by_role("button", name="Rename thread", exact=True).click()
    expect(frame.locator(".thread-title")).to_have_text("Website refresh")
    expect(page.locator("#chat-nav-items")).to_contain_text("Website refresh")
    page.once("dialog", lambda dialog: dialog.accept("website-redesign"))
    frame.get_by_role("button", name="Rename thread", exact=True).click()
    expect(frame.locator(".thread-title")).to_have_text("website-redesign")
    # Clearing working memory records a visible boundary and keeps the
    # transcript: the operator must be able to tell it took effect without
    # believing their history was deleted. Cleared with activity hidden,
    # because that view drops thread.activity and would otherwise show no
    # confirmation at all.
    frame.get_by_role("switch", name="Activity", exact=True).click()
    page.once("dialog", lambda dialog: dialog.accept())
    frame.get_by_role("button", name="Clear working memory", exact=True).click()
    expect(frame.locator("#thread-detail")).to_contain_text("Working memory cleared")
    expect(frame.locator("#thread-detail")).to_contain_text(
        "Earlier messages stay visible but are no longer sent to it"
    )

    # The desktop instruction line begins on the same vertical guide as the
    # prompt text instead of floating centered beneath the composer.
    prompt_box = frame.locator("#new-task").bounding_box()
    hint = frame.locator("#composer-hint")
    hint_box = hint.bounding_box()
    hint_padding = hint.evaluate(
        "element => Number.parseFloat(getComputedStyle(element).paddingLeft)"
    )
    if not prompt_box or not hint_box:
        raise AssertionError("composer prompt or instruction line is not visible")
    if abs(prompt_box["x"] - (hint_box["x"] + hint_padding)) > 1:
        raise AssertionError(
            "composer instruction line is not aligned with the prompt text: "
            f"prompt={prompt_box}, hint={hint_box}, padding={hint_padding}"
        )
    frame.get_by_role("switch", name="Activity", exact=True).click()
    expect(frame.locator("#thread-detail")).to_contain_text("Working memory cleared")

    _load_older_history(frame, expected_turns=2)
    expect(frame.locator("#thread-detail")).to_contain_text("Push the responsive fixes")
    expect(frame.locator("#thread-detail")).to_contain_text(
        "network access to deploy.acme.dev denied by policy"
    )
    expect(frame.locator("#thread-detail .status")).to_have_count(0)
    expect(frame.locator("#thread-detail .thread-entry").nth(0)).to_contain_text("Audit the marketing site")

    # Unsent text belongs to its thread and returns when the operator switches
    # back, rather than leaking into whichever thread is selected next.
    frame.locator("#new-task").fill("Website-specific unsent draft")
    _open_host_thread(page, "thread-1")
    expect(frame.locator("#new-task")).to_have_value("")
    frame.locator("#new-task").fill("Thread-one unsent draft")
    _open_host_thread(page, "website-redesign")
    expect(frame.locator("#new-task")).to_have_value("Website-specific unsent draft")
    frame.locator("#new-task").fill("")
    _open_host_thread(page, "thread-1")
    expect(frame.locator("#new-task")).to_have_value("Thread-one unsent draft")
    frame.locator("#new-task").fill("")

    _start_host_chat(page)
    expect(frame.locator(".thread-title")).to_have_text("New thread")
    # The operator never types a thread id: the composer has no thread field
    # and the backend generates the next successive name on send.
    expect(frame.locator("#new-task-thread")).to_have_count(0)
    upload_requests = []
    page.on(
        "request",
        lambda request: upload_requests.append(request.url)
        if "/v1/agent-files/upload?" in request.url
        else None,
    )
    with page.expect_file_chooser() as chooser:
        frame.get_by_role("button", name="Attach files").click()
    chooser.value.set_files([
        {
            "name": "reference image.png",
            "mimeType": "image/png",
            "buffer": b"mock-image-bytes",
        },
        {
            "name": "notes.txt",
            "mimeType": "text/plain",
            "buffer": b"remove me",
        },
        {
            "name": "brief.pdf",
            "mimeType": "application/pdf",
            "buffer": b"mock-pdf-bytes",
        },
    ])
    expect(frame.locator("#attachments .attachment")).to_have_count(3)
    expect(frame.locator("#attachments")).to_contain_text("reference image.png")
    expect(frame.locator("#attachments")).to_contain_text("brief.pdf")
    frame.get_by_role("button", name="Remove notes.txt").click()
    expect(frame.locator("#attachments .attachment")).to_have_count(2)
    expect(frame.locator("#attachments")).not_to_contain_text("notes.txt")
    assert upload_requests == [], "selecting and removing attachments must not upload them before Send"
    frame.locator("#new-task").fill("agent workspace smoke task")
    frame.locator("#new-task-runtime").select_option("codex")
    expect(frame.locator("#new-task-model option")).to_have_count(3)
    frame.locator("#new-task-model").select_option("gpt-5.6-luna")
    expect(frame.locator("#new-task-effort option")).to_have_count(2)
    expect(frame.locator("#new-task-effort")).not_to_contain_text("Ultra")
    frame.locator("#new-task-effort").select_option("max")
    frame.get_by_role("button", name="Send").click()
    expect(frame.locator("#status")).to_contain_text("Service Unavailable")
    expect(frame.locator(".thread-title")).to_have_text("New thread")
    assert len(upload_requests) == 2, "the first Send must stop after the second attachment fails"
    frame.get_by_role("button", name="Send").click()
    expect(frame.locator(".thread-title")).to_have_text(re.compile(r"^thread-[0-9]+$"))
    assert len(upload_requests) == 3, "retry must upload only the unfinished attachment"
    assert sum("reference%20image.png" in url for url in upload_requests) == 1
    assert sum("brief.pdf" in url for url in upload_requests) == 2
    generated_thread = frame.locator(".thread-title").inner_text()
    expect(frame.locator("#thread-detail")).to_contain_text("agent workspace smoke task")
    expect(frame.locator("#thread-detail")).to_contain_text(
        "[User-uploaded file: user-files/20260722T120000.000000Z_reference image.png]"
    )
    expect(frame.locator("#thread-detail")).to_contain_text(
        "[User-uploaded file: user-files/20260722T120000.000000Z_brief.pdf]"
    )
    expect(frame.locator("#thread-detail")).not_to_contain_text("notes.txt")
    with tempfile.TemporaryDirectory() as temporary_directory:
        oversized = Path(temporary_directory) / "oversized.bin"
        with oversized.open("wb") as file:
            file.truncate(25 * 1024 * 1024 + 1)
        with page.expect_file_chooser() as chooser:
            frame.get_by_role("button", name="Attach files").click()
        chooser.value.set_files(str(oversized))
        expect(frame.locator("#attachments")).to_contain_text("25 MiB max")
        expect(frame.get_by_role("button", name="Send")).to_be_disabled()
        assert len(upload_requests) == 3, "an oversized selection must not start an upload"
        frame.get_by_role("button", name="Remove oversized.bin").click()
        expect(frame.locator("#attachments")).to_be_hidden()
        expect(frame.get_by_role("button", name="Send")).to_be_enabled()
    with page.expect_file_chooser() as chooser:
        frame.get_by_role("button", name="Attach files").click()
    chooser.value.set_files(
        [
            {
                "name": f"extra-{index}.txt",
                "mimeType": "text/plain",
                "buffer": b"extra",
            }
            for index in range(11)
        ]
    )
    expect(frame.locator("#status")).to_have_text("You can attach up to 10 files.")
    expect(frame.locator("#attachments")).to_be_hidden()
    assert len(upload_requests) == 3, "too many selections must not start an upload"
    # Let the turn the previous Send started finish before sending again. A
    # message delivered into a running turn only steers it and inherits its
    # remaining wall clock, which can run out midway through the archive
    # refusal below; sending into an idle thread starts a fresh turn whose full
    # duration covers the few round trips that follow.
    expect(frame.locator("#composer-running")).to_be_hidden(timeout=25_000)
    frame.locator("#new-task").fill("agent workspace smoke follow up")
    frame.get_by_role("button", name="Send").click()
    expect(frame.locator("#thread-detail")).to_contain_text("agent workspace smoke follow up")
    # A running thread cannot be archived (UX-014): archiving would hide it
    # while the turn kept going, with Stop out of reach. Stop it first. The
    # send above always starts a turn, so this is a state to wait for rather
    # than a condition to sample: an is_visible() branch here would skip the
    # refusal silently, or arm the dialog handler below for a Stop button that
    # is no longer there.
    expect(frame.locator("#composer-running")).to_be_visible()
    frame.get_by_role("button", name="Archive", exact=True).click()
    expect(frame.locator("#status")).to_have_text(
        "Stop the agent before archiving this thread."
    )
    page.once("dialog", lambda dialog: dialog.accept())
    frame.locator("#composer-running").get_by_role("button", name="Stop").click()
    expect(frame.locator("#composer-running")).to_be_hidden()
    frame.get_by_role("button", name="Archive", exact=True).click()
    expect(frame.locator(".thread-title")).to_have_text("New thread")
    expect(page.locator("#chat-nav-items")).not_to_contain_text(generated_thread)
    _toggle_host_chat_archive(page)
    expect(page.locator("#chat-nav-items")).to_contain_text(generated_thread)
    _open_host_thread(page, generated_thread)
    expect(frame.locator("#composer")).to_be_hidden()
    expect(frame.get_by_role("button", name="Unarchive")).to_be_visible()
    frame.get_by_role("button", name="Unarchive").click()
    expect(page.locator("#chat-nav-items")).not_to_contain_text(generated_thread)
    _toggle_host_chat_archive(page)
    expect(page.locator("#chat-nav-items")).to_contain_text(generated_thread)
    _open_host_thread(page, generated_thread)
    _assert_single_scroll(page, frame, "Chat workspace (desktop)")


def mobile_smoke(page: Any) -> None:
    from playwright.sync_api import expect

    _start_host_chat(page)
    expect(page.locator("#panel-workspace-chat")).to_be_visible()
    frame = page.locator("#panel-workspace-chat")
    _open_mobile_host_navigation(page)
    expect(page.locator("#chat-nav-items")).to_contain_text("website-redesign")
    expect(page.locator("#chat-nav-items")).to_contain_text("thread-1")
    expect(page.locator("#chat-nav-items")).to_contain_text("thread-2")
    # The drawer intentionally sits above the full-screen backdrop. Click its
    # exposed right edge rather than Playwright's center point, which is under
    # the drawer on a 390px viewport.
    page.locator("#nav-backdrop").click(position={"x": 380, "y": 400})
    _open_host_thread(page, "thread-1")
    expect(frame.locator("#thread-detail")).to_contain_text("Document the theming setup")
    _assert_frame_no_horizontal_overflow(frame, "Chat workspace")
    _assert_single_scroll(page, frame, "Chat workspace (mobile)")
    _assert_initial_tail(frame)
    _load_older_history(frame, expected_turns=5)
    _assert_full_message_stream(frame)
    _assert_mobile_chat_scrolling(page, frame)
    _assert_thread_view_memory(page, frame)
    _assert_rich_activity_stream(page, frame)
    _assert_mobile_composer_ergonomics(frame)
    _assert_mobile_keyboard_viewport_recovery(page, frame)
    _assert_mobile_running_message(page, frame)
    _assert_mobile_send_flow(page, frame)
    _assert_long_message_has_no_horizontal_overflow(frame)


def _open_mobile_host_navigation(page: Any) -> None:
    from playwright.sync_api import expect

    toggle = page.locator("#mobile-nav-toggle")
    if not toggle.is_visible():
        return
    toggle.click()
    expect(page.locator("#sidebar")).to_have_class(re.compile(r"mobile-open"))


def _open_host_thread(page: Any, name: str) -> None:
    from playwright.sync_api import expect

    _open_mobile_host_navigation(page)
    thread = page.locator("#chat-nav-items .workspace-nav-item", has_text=name)
    expect(thread).to_be_visible()
    thread.click()
    expect(page.locator("#panel-workspace-chat")).to_be_visible()


def _start_host_chat(page: Any) -> None:
    from playwright.sync_api import expect

    _open_mobile_host_navigation(page)
    page.get_by_role("button", name="New chat", exact=True).click()
    expect(page.locator("#panel-workspace-chat")).to_be_visible()


def _toggle_host_chat_archive(page: Any) -> None:
    from playwright.sync_api import expect

    _open_mobile_host_navigation(page)
    toggle = page.locator('[data-action="show-chat-archive"]')
    expect(toggle).to_be_visible()
    toggle.click()
    expect(page.locator("#panel-home")).to_be_visible()


def _assert_initial_tail(frame: Any) -> None:
    """Opening a long thread preloads three bounded pages at the bottom."""
    from playwright.sync_api import expect

    expect(frame.get_by_role("button", name="Load earlier messages")).to_be_visible()
    metrics = frame.locator("#chat-scroll").evaluate(
        "element => [element.scrollHeight, element.clientHeight, element.scrollTop]"
    )
    scroll_height, client_height, scroll_top = metrics
    # A mounted surface can be tall enough for the preloaded tail to fit
    # exactly. The earlier-history button above proves a cursor remains; only
    # assert that the current tail is positioned at its newest edge.
    if scroll_top < max(0, scroll_height - client_height) - 2:
        raise AssertionError(f"opening a thread did not land at the newest preloaded history: {metrics}")


def _load_older_history(frame: Any, *, expected_turns: int) -> None:
    """Exercise backward pagination until the seeded thread is fully loaded."""
    from playwright.sync_api import expect

    button = frame.locator("#load-earlier")
    loader = frame.locator("#history-loader")
    # Selecting a thread updates its title before the initial event requests
    # complete. Wait for a real cursor so a hidden loader means the three-page
    # preload exhausted history, rather than merely not having started yet.
    expect(loader).to_have_attribute("data-oldest-seq", re.compile(r"\d+"))
    for _ in range(100):
        if not button.is_visible():
            break
        button.evaluate(
            """element => {
              const loader = element.closest("#history-loader");
              const before = loader.dataset.oldestSeq;
              element.click();
              return new Promise((resolve, reject) => {
                const finished = () => loader.hidden || loader.dataset.oldestSeq !== before;
                if (finished()) {
                  resolve();
                  return;
                }
                const observer = new MutationObserver(() => {
                  if (!finished()) return;
                  clearTimeout(timer);
                  observer.disconnect();
                  resolve();
                });
                const timer = setTimeout(() => {
                  observer.disconnect();
                  reject(new Error("older history cursor did not advance"));
                }, 5000);
                observer.observe(loader, {
                  attributes: true,
                  attributeFilter: ["hidden", "data-oldest-seq"],
                });
              });
            }"""
        )
    expect(button).to_be_hidden()
    # count() is a point-in-time sample and the history pane is patched entry
    # by entry, so wait for the last expected entry to exist before counting.
    expect(frame.locator("#thread-detail .thread-entry").nth(expected_turns - 1)).to_be_attached()
    entry_count = frame.locator("#thread-detail .thread-entry").count()
    if entry_count < expected_turns:
        raise AssertionError(
            f"fully loaded history has only {entry_count} entries; expected at least {expected_turns}"
        )


def _assert_full_message_stream(frame: Any) -> None:
    """The flat thread renders every message in chronological order."""
    from playwright.sync_api import expect

    history = frame.locator("#thread-detail")
    # Interim agent progress from the seeded stream.
    expect(history).to_contain_text("Reproduced the flash with CPU throttling")
    expect(history.locator(".thread-user", has_text="scrollbar matches")).to_have_count(1)
    # Identical accepted messages remain distinct chronological entries; the
    # flat renderer must not deduplicate a later live message against the
    # original request.
    expect(history.locator(
        ".thread-user",
        has_text="The toggle still flashes light theme for a frame on a cold load. Fix the flash.",
    )).to_have_count(2)
    # The final answer still lands after the interim stream.
    expect(history).to_contain_text("no flash in 20 cold loads")


def _assert_rich_activity_stream(page: Any, frame: Any) -> None:
    """Every provider-neutral activity kind receives its distinct rich card."""
    from playwright.sync_api import expect

    history = frame.locator("#thread-detail")
    expected = {
        "reasoning": ("Reasoning", "Reasoning"),
        "plan": ("Fix first-paint ordering", "Plan"),
        "command": ("npm test", "Command"),
        "file_change": ("File changes", "File change"),
        "tool": ("Tool: browser", "Tool"),
        "agent": ("Sub-agent activity", "Sub-agent"),
        "search": ("Web search", "Search"),
        "image": ("Viewed image", "Image"),
        "wait": ("Waiting for cold load", "Wait"),
        "status": ("Browser session initialized", "Status"),
    }
    for kind, (title, label) in expected.items():
        card = history.locator(f".activity-{kind}", has_text=title)
        expect(card).to_have_count(1)
        expect(card.locator(".activity-kind")).to_have_text(label)
    command = history.locator(".activity-command", has_text="npm test")
    command.locator("summary").click()
    expect(command).to_contain_text("Terminal output")
    expect(command).to_contain_text("6 passed")
    expect(command.locator(".activity-status")).to_have_text("exit 0")
    expect(history.locator(".activity-status", has_text="completed")).to_have_count(0)
    failed = history.locator(".activity-status.failed")
    expect(failed).to_have_count(1)
    expect(failed).to_have_text("failed")
    expect(
        history.locator(".activity-card.activity-static", has_text="Browser session initialized")
    ).to_have_count(1)
    _assert_activity_visibility_toggle(page, frame, history)


def _assert_activity_visibility_toggle(page: Any, frame: Any, turn: Any) -> None:
    """Activity reflows as complete rows without moving the reading anchor."""
    from playwright.sync_api import expect

    cards = turn.locator(".activity-card")
    card_count = cards.count()
    if card_count < 1:
        raise AssertionError("activity visibility toggle has no seeded cards to exercise")
    activity_rows = turn.locator(".thread-activity")
    expect(activity_rows).to_have_count(card_count)
    anchor = turn.locator(".thread-agent", has_text="Root cause:").last
    anchor.evaluate("element => element.scrollIntoView({ block: 'start' })")
    anchor_before = anchor.bounding_box()
    if not anchor_before:
        raise AssertionError("activity toggle reading anchor is not visible")
    toggle = frame.get_by_role("switch", name="Activity", exact=True)
    expect(toggle).to_have_attribute("aria-checked", "true")
    toggle.click()
    expect(toggle).to_have_attribute("aria-checked", "false")
    expect(toggle).to_have_attribute("title", "Show agent activity")
    expect(turn.locator(".activity-card:visible")).to_have_count(0)
    expect(turn.locator(".thread-activity:visible")).to_have_count(0)
    anchor_hidden = anchor.bounding_box()
    if not anchor_hidden or abs(anchor_hidden["y"] - anchor_before["y"]) > 1:
        raise AssertionError(
            "hiding activity moved the reading anchor: "
            f"{anchor_before} -> {anchor_hidden}"
        )
    max_gap, grid_gap = turn.evaluate(
        """element => {
          const visible = [...element.children].filter(
            child => getComputedStyle(child).display !== "none",
          );
          const gaps = visible.slice(1).map((child, index) => (
            child.getBoundingClientRect().top
              - visible[index].getBoundingClientRect().bottom
          ));
          return [
            gaps.length ? Math.max(...gaps) : 0,
            parseFloat(getComputedStyle(element).rowGap),
          ];
        }"""
    )
    if max_gap > grid_gap + 1:
        raise AssertionError(
            f"hidden activity left a blank conversation row: gap={max_gap}, grid={grid_gap}"
        )
    # Conversation messages remain visible; this is not a whole-turn filter.
    expect(turn.get_by_text("no flash in 20 cold loads", exact=False)).to_be_visible()
    toggle.click()
    expect(toggle).to_have_attribute("aria-checked", "true")
    expect(toggle).to_have_attribute("title", "Hide agent activity")
    expect(turn.locator(".activity-card:visible")).to_have_count(card_count)
    anchor_shown = anchor.bounding_box()
    if not anchor_shown or abs(anchor_shown["y"] - anchor_before["y"]) > 1:
        raise AssertionError(
            "showing activity moved the reading anchor: "
            f"{anchor_before} -> {anchor_shown}"
        )
    scroller = frame.locator("#chat-scroll")
    scroller.evaluate("element => { element.scrollTop = element.scrollHeight; }")
    for expected_state in ("false", "true"):
        toggle.click()
        expect(toggle).to_have_attribute("aria-checked", expected_state)
        distance_from_bottom = scroller.evaluate(
            "element => element.scrollHeight - element.scrollTop - element.clientHeight"
        )
        if distance_from_bottom > 1:
            raise AssertionError(
                "activity reflow detached the newest message from the viewport: "
                f"{distance_from_bottom}px"
            )

    # Opening a different thread while activity is hidden must page the
    # conversation lane. Its latest forty raw events are activity, so the old
    # prompt can appear in the initial view only when filtering happens before
    # the backend applies its six-event page limit.
    toggle.click()
    _open_host_thread(page, "activity-heavy")
    expect(frame.locator("#thread-detail")).to_contain_text(
        "Conversation marker before dense activity"
    )
    expect(frame.locator("#thread-detail")).to_contain_text(
        "Conversation marker after dense activity"
    )
    toggle.click()
    _open_host_thread(page, "thread-1")


def _assert_mobile_chat_scrolling(page: Any, frame: Any) -> None:
    """The long seeded thread must scroll freely on a phone and a background
    poll must not touch the DOM under the reader's finger."""
    from playwright.sync_api import expect

    scroller = frame.locator("#chat-scroll")
    expect(frame.locator("#thread-detail .thread-entry").nth(4)).to_be_attached()
    if frame.locator("#thread-detail .thread-entry").count() < 5:
        raise AssertionError("thread-1 did not render enough flat history entries")
    metrics = scroller.evaluate(
        "element => [element.scrollHeight, element.clientHeight, element.scrollTop]"
    )
    scroll_height, client_height, scroll_top = metrics
    if scroll_height - client_height < client_height:
        raise AssertionError(
            f"seeded thread-1 is not long enough to exercise scrolling: {metrics}"
        )
    # The whole history is reachable: jump to the top and read the first entry.
    scroller.evaluate("element => { element.scrollTop = 0; }")
    expect(frame.locator("#thread-detail .thread-entry").nth(0)).to_be_in_viewport()
    # Park mid-history, mark the first rendered entry, and sit through a full
    # 5-second poll: the scroll position must hold and the DOM node must be
    # the same object (an innerHTML rebuild would kill touch momentum).
    scroller.evaluate("element => { element.scrollTop = Math.floor(element.scrollHeight / 2); }")
    parked = scroller.evaluate("element => element.scrollTop")
    frame.locator("#thread-detail .thread-entry").nth(0).evaluate("element => { element.dataset.smokeProbe = 'kept'; }")
    page.wait_for_timeout(6000)
    after = scroller.evaluate("element => element.scrollTop")
    if after != parked:
        raise AssertionError(f"a background poll moved the reading position: {parked} -> {after}")
    probe = frame.locator("#thread-detail .thread-entry").nth(0).evaluate("element => element.dataset.smokeProbe")
    if probe != "kept":
        raise AssertionError("a background poll rebuilt the chat history DOM while reading")


def _assert_thread_view_memory(page: Any, frame: Any) -> None:
    """A thread switch retains the loaded window and the reader's position."""
    from playwright.sync_api import expect

    scroller = frame.locator("#chat-scroll")
    previous_scroll_top = scroller.evaluate("element => element.scrollTop")
    _open_host_thread(page, "thread-2")
    expect(frame.locator(".thread-title")).to_have_text("thread-2")
    expect(frame.locator("#thread-detail")).to_contain_text("Draft a launch blog post")

    _open_host_thread(page, "thread-1")
    expect(frame.locator(".thread-title")).to_have_text("thread-1")
    # Selecting a thread swaps the title before its restored history renders,
    # so gate the raw count() and the scroll read below on the restored window
    # actually being back rather than on the title alone.
    expect(frame.locator("#thread-detail .thread-entry").nth(4)).to_be_attached()
    if frame.locator("#thread-detail .thread-entry").count() < 5:
        raise AssertionError("restored thread lost its loaded history entries")
    expect(frame.get_by_role("button", name="Load earlier messages")).to_be_hidden()
    restored_scroll_top = scroller.evaluate("element => element.scrollTop")
    if abs(restored_scroll_top - previous_scroll_top) > 1:
        raise AssertionError(
            "returning to a thread did not restore its reading position: "
            f"{previous_scroll_top} -> {restored_scroll_top}"
        )


def _assert_mobile_composer_ergonomics(frame: Any) -> None:
    """iOS-critical input ergonomics: 16px fields so focus does not zoom the
    page, and thumb-sized primary controls."""
    composer_font = frame.locator("#new-task").evaluate(
        "element => parseFloat(getComputedStyle(element).fontSize)"
    )
    if composer_font < 16:
        raise AssertionError(f"composer font below 16px zooms iOS on focus: {composer_font}px")
    send_box = frame.locator("#create-task").bounding_box()
    if not send_box or send_box["height"] < 43 or send_box["width"] < 43:
        raise AssertionError(f"send button is below thumb size on a phone: {send_box}")
    attach_box = frame.locator("#attach-file").bounding_box()
    if not attach_box or attach_box["height"] < 43 or attach_box["width"] < 43:
        raise AssertionError(f"attach button is below thumb size on a phone: {attach_box}")
    clipped = frame.locator(".composer").evaluate(
        "element => element.getBoundingClientRect().bottom > window.innerHeight + 1"
    )
    if clipped:
        raise AssertionError("composer is clipped below the app frame viewport")


def _assert_mobile_keyboard_viewport_recovery(page: Any, frame: Any) -> None:
    """The host shell follows a contracting mobile viewport and fully recovers.

    iOS keeps ``vh`` tied to the large viewport while the software keyboard
    reduces ``dvh``. Exercise modern and compact iPhone dimensions and pin the
    critical CSS override so the host cannot retain its large-viewport minimum
    and pan the focused workspace underneath the usage toolbar.
    """
    from playwright.sync_api import expect

    body = page.locator("body")
    expect(body).to_have_class(re.compile(r"\bviewport-panel-open\b"))
    expect(body).to_have_css("display", "grid")
    expect(body).to_have_css("min-height", "0px")
    _assert_mobile_usage_overlay_over_app(page)
    composer = frame.locator("#new-task")
    for width, full_height, keyboard_height in (
        (390, 844, 500),
        (375, 667, 360),
    ):
        page.set_viewport_size({"width": width, "height": full_height})
        composer.focus()
        composer.fill("keyboard viewport probe")
        page.set_viewport_size({"width": width, "height": keyboard_height})
        _assert_mobile_app_owns_viewport(page, frame, f"{width}x{full_height} keyboard")

        composer.evaluate("element => element.blur()")
        page.set_viewport_size({"width": width, "height": full_height})
        _assert_mobile_app_owns_viewport(page, frame, f"{width}x{full_height} restored")

    composer.fill("")
    page.set_viewport_size({"width": 390, "height": 844})

    # Leaving a viewport-owned surface tears the shell mode down; reopening it
    # establishes the mode again without retaining keyboard geometry or
    # placing the surface beneath the toolbar.
    page.locator("#mobile-nav-toggle").click()
    page.get_by_role("button", name="Home", exact=True).click()
    expect(body).not_to_have_class(re.compile(r"\bviewport-panel-open\b"))
    expect(page.locator("#panel-home")).to_be_visible()
    _open_mobile_host_navigation(page)
    page.get_by_role("button", name="New chat", exact=True).click()
    expect(body).to_have_class(re.compile(r"\bviewport-panel-open\b"))
    _assert_mobile_app_owns_viewport(page, frame, "390x844 app reopened")


def _assert_mobile_usage_overlay_over_app(page: Any) -> None:
    """The host usage panel floats over the workspace without reflowing it."""
    from playwright.sync_api import expect

    surface = page.locator("#panel-workspace-chat")
    surface_before = surface.bounding_box()
    overview_toggle = page.locator(".runtime-overview-toggle")
    expect(overview_toggle).to_have_attribute("aria-expanded", "false")
    with page.expect_request(re.compile(r"/v1/agent-runtime/refresh")):
        overview_toggle.click()
    expect(overview_toggle).to_have_attribute("aria-expanded", "true")
    panel = page.locator("#runtime-overview .runtime-overview-panel")
    expect(panel).to_be_visible()
    expect(panel).to_have_css("position", "absolute")
    surface_expanded = surface.bounding_box()
    if not surface_before or not surface_expanded:
        raise AssertionError("Chat workspace disappeared while opening the usage overlay")
    if (
        abs(surface_expanded["y"] - surface_before["y"]) > 1
        or abs(surface_expanded["height"] - surface_before["height"]) > 1
    ):
        raise AssertionError(
            "opening the usage overlay reflowed Agent Chat: "
            f"before={surface_before}, expanded={surface_expanded}"
        )

    overview_toggle.click()
    expect(overview_toggle).to_have_attribute("aria-expanded", "false")
    expect(panel).to_be_hidden()
    surface_collapsed = surface.bounding_box()
    if not surface_collapsed or (
        abs(surface_collapsed["y"] - surface_before["y"]) > 1
        or abs(surface_collapsed["height"] - surface_before["height"]) > 1
    ):
        raise AssertionError(
            "closing the usage overlay changed Agent Chat geometry: "
            f"before={surface_before}, collapsed={surface_collapsed}"
        )


def _assert_mobile_app_owns_viewport(page: Any, frame: Any, label: str) -> None:
    metrics = page.evaluate(
        """() => {
          const rect = element => {
            const box = element.getBoundingClientRect();
            return {top: box.top, bottom: box.bottom, height: box.height};
          };
          return {
            viewportHeight: window.innerHeight,
            scrollY: window.scrollY,
            documentOverflow:
              document.documentElement.scrollHeight - document.documentElement.clientHeight,
            body: rect(document.body),
            topbar: rect(document.querySelector(".topbar")),
            app: rect(document.querySelector("#app")),
            surface: rect(document.querySelector('#panel-workspace-chat')),
          };
        }"""
    )
    viewport_height = metrics["viewportHeight"]
    if abs(metrics["body"]["height"] - viewport_height) > 1:
        raise AssertionError(f"{label}: host body does not match the viewport: {metrics}")
    if abs(metrics["body"]["top"]) > 1 or metrics["body"]["bottom"] > viewport_height + 1:
        raise AssertionError(f"{label}: host body shifted outside the viewport: {metrics}")
    if metrics["scrollY"] > 1 or metrics["documentOverflow"] > 1:
        raise AssertionError(f"{label}: outer host page became scrollable: {metrics}")
    if metrics["app"]["top"] < metrics["topbar"]["bottom"] - 1:
        raise AssertionError(f"{label}: host usage toolbar overlaps the app: {metrics}")
    if metrics["surface"]["bottom"] > viewport_height + 1:
        raise AssertionError(f"{label}: Chat workspace extends below the viewport: {metrics}")

    surface_box = page.locator("#panel-workspace-chat").bounding_box()
    chat_head_box = frame.locator(".chat-head").bounding_box()
    if not surface_box or not chat_head_box:
        raise AssertionError(f"{label}: Chat workspace or header is not visible")
    surface_top = surface_box["y"]
    surface_bottom = surface_top + surface_box["height"]
    head_top = chat_head_box["y"]
    head_bottom = head_top + chat_head_box["height"]
    if head_top < surface_top - 1 or head_bottom > surface_bottom + 1:
        raise AssertionError(
            f"{label}: chat header is clipped by the host toolbar: "
            f"surface={surface_box}, header={chat_head_box}"
        )


def _assert_mobile_running_message(page: Any, frame: Any) -> None:
    """The one composer sends another message into running work."""
    from playwright.sync_api import expect

    _open_host_thread(page, "thread-2")
    expect(frame.locator("#thread-detail")).to_contain_text("Tighten the intro")
    history = frame.locator("#thread-detail")
    expect(history.locator(".status")).to_have_count(0)
    expect(frame.locator(".task-steer-input")).to_have_count(0)
    expect(history.get_by_role("button", name="Stop")).to_have_count(0)
    expect(frame.locator("#composer-running")).to_be_visible()
    expect(frame.locator("#composer-running")).to_contain_text("Agent is working")
    expect(frame.locator("#composer-running").get_by_role("button", name="Stop")).to_be_visible()
    expect(frame.locator("#new-task-runtime")).to_be_disabled()
    frame.locator("#composer-options").hover()
    expect(frame.locator("#session-options-note")).to_be_visible()
    expect(frame.locator("#session-options-note")).to_contain_text(
        "Stop this thread first"
    )
    live_command = history.locator(
        ".activity-command.started",
        has_text="python word_count.py",
    )
    expect(live_command).to_have_count(1)
    expect(live_command).not_to_have_attribute("open", "")
    expect(live_command.locator(".activity-phase")).to_have_text("Started")
    activity_animation = live_command.locator(".activity-icon").evaluate(
        "element => getComputedStyle(element).animationName"
    )
    if activity_animation != "none":
        raise AssertionError(
            f"started activity still implies live work with animation {activity_animation!r}"
        )
    composer = frame.locator("#new-task")
    expect(composer).to_have_attribute(
        "placeholder",
        "Send another message",
    )
    composer.fill("keep the beta tester thank-you")
    composer.press("Enter")
    expect(composer).to_have_value("")
    expect(
        history.locator(
            ".thread-user",
            has_text="keep the beta tester thank-you",
        )
    ).to_have_count(1)


def _assert_mobile_send_flow(page: Any, frame: Any) -> None:
    """Starting a thread from a phone: no thread-id typing, generated name,
    the sent message lands in view at the bottom."""
    from playwright.sync_api import expect

    _start_host_chat(page)
    expect(frame.locator(".thread-title")).to_have_text("New thread")
    expect(frame.locator("#new-task-thread")).to_have_count(0)
    frame.locator("#new-task").fill("mobile smoke: check the deploy status")
    frame.get_by_role("button", name="Send").click()
    expect(frame.locator(".thread-title")).to_have_text(re.compile(r"^thread-[0-9]+$"))
    expect(frame.locator("#thread-detail")).to_contain_text("mobile smoke: check the deploy status")
    sent_bubble = frame.locator("#thread-detail .thread-user").last
    expect(sent_bubble).to_be_in_viewport()
    # A running thread cannot be archived (UX-014), so stop the turn first.
    # The send above started this brand-new thread's first turn, so the running
    # composer is a state to wait for, not one to sample: an is_visible() race
    # would either skip the Stop and hit the archive refusal, or leave the
    # dialog handler below armed for a later unrelated dialog.
    expect(frame.locator("#composer-running")).to_be_visible()
    page.once("dialog", lambda dialog: dialog.accept())
    frame.locator("#composer-running").get_by_role("button", name="Stop").click()
    expect(frame.locator("#composer-running")).to_be_hidden()
    frame.get_by_role("button", name="Archive", exact=True).click()
    expect(frame.locator(".thread-title")).to_have_text("New thread")


def _generate_thread_id() -> str:
    numbers = [
        int(match.group(1))
        for thread_id in AGENT_CHAT_THREADS
        if (match := re.fullmatch(r"thread-([1-9][0-9]*)", thread_id)) is not None
    ]
    return f"thread-{max(numbers, default=0) + 1}"


def _list_threads(host_api: HostApi, *, archived: bool = False) -> list[dict[str, Any]]:
    """Mirror the real backend's paged host-summary join.

    The host contributes session config and live status; the app contributes
    names, archive state, and the ownership set.
    """
    recorded = {
        thread_id: thread
        for thread_id, thread in AGENT_CHAT_THREADS.items()
        if thread["archived"] == archived
    }
    summaries = []
    seen_before: set[str] = set()
    before: str | None = None
    while True:
        query = {"limit": ["100"]}
        if before is not None:
            query["before"] = [before]
        page = host_api("GET", "/v1/threads", query, None)
        summaries.extend(page.get("threads") or [])
        next_before = page.get("next_before")
        if not isinstance(next_before, str) or not next_before:
            break
        if next_before in seen_before:
            raise AssertionError("thread list pagination returned a repeated cursor")
        seen_before.add(next_before)
        before = next_before
    threads = [
        {
            **summary,
            "name": recorded[summary["thread_id"]]["name"],
            "archived": archived,
        }
        for summary in summaries
        if summary["thread_id"] in recorded
    ]
    return sorted(threads, key=lambda item: item["last_used_at"], reverse=True)


def _require_agent_chat_thread(
    thread_id: str,
    api_error: ApiErrorFactory,
    *,
    include_archived: bool = False,
) -> None:
    thread = AGENT_CHAT_THREADS.get(thread_id)
    if thread is None or (thread["archived"] and not include_archived):
        raise api_error(HTTPStatus.NOT_FOUND, "thread not found")


def _assert_single_scroll(page: Any, frame: Any, label: str) -> None:
    """With a workspace open, only its internal panes may scroll vertically."""
    outer = page.evaluate(
        "() => document.documentElement.scrollHeight - document.documentElement.clientHeight"
    )
    if outer > 1:
        raise AssertionError(f"{label}: outer page scrolls vertically by {outer}px")
    inner = frame.locator(".chat-app").evaluate(
        "element => element.scrollHeight - element.clientHeight"
    )
    if inner > 1:
        raise AssertionError(f"{label}: workspace scrolls vertically by {inner}px")


def _assert_frame_no_horizontal_overflow(frame: Any, label: str) -> None:
    overflow = frame.locator(".chat-app").evaluate(
        "element => element.scrollWidth - element.clientWidth"
    )
    if overflow > 1:
        raise AssertionError(f"{label} frame overflows horizontally by {overflow}px")


def _assert_long_message_has_no_horizontal_overflow(frame: Any) -> None:
    """Assistant messages, including long inline values, wrap in the chat pane."""
    message = (
        "I’m keeping the guidance exactly where it is under Tools, but making it operational: "
        "name the common GraphQL-backed commands, show REST replacements, and tell the agent "
        "how to derive the repository without `gh repo view`. The shim will add the same hint "
        "only when it detects a failed `/graphql` request, preserving normal `gh` output and "
        "exit codes.\n\n"
        f"Long inline value: `{'x' * 1_024}`."
    )
    frame.locator("#thread-detail").evaluate(
        """(element, content) => {
          const turn = document.createElement("article");
          turn.className = "thread-entry";
          const rendered = document.createElement("div");
          rendered.className = "thread-agent md-content";
          rendered.innerHTML = window.KernRichText.renderMarkdown(content);
          turn.append(rendered);
          element.append(turn);
        }""",
        message,
    )
    overflow = frame.locator("#chat-scroll").evaluate(
        "element => element.scrollWidth - element.clientWidth"
    )
    if overflow > 1:
        raise AssertionError(
            "agent_chat long assistant message is clipped by the chat pane "
            f"by {overflow}px"
        )
    _assert_frame_no_horizontal_overflow(frame, "agent_chat long assistant message")
