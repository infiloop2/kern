"""Compact chat attachments, authenticated loading, fallback and modal behavior."""
from __future__ import annotations

import os
from pathlib import Path
import struct
from urllib.parse import parse_qs, urlsplit
import zlib


def png(width: int, height: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    rows = b"".join(b"\0" + bytes((56, 100 + int(y / height * 100), 180)) * width for y in range(height))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">2I5B", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


def run(page, url: str, log_in) -> None:
    from playwright.sync_api import expect

    requests = []
    transient_recovered = False
    landscape = png(1200, 640)
    portrait = png(400, 1000)
    thin_image = png(1, 8192)
    source = (
        "Here are the two layout options.\n\n"
        "[Desktop layout](/mnt/kern-agent/agent-home/screenshots/desktop.png)\n\n"
        "![Mobile layout](</mnt/kern-agent/agent-home/screenshots/mobile image.png>)\n\n"
        "[Thin image](/mnt/kern-agent/agent-home/screenshots/thin.png)\n\n"
        "[Large original](/mnt/kern-agent/agent-home/screenshots/large.png)\n\n"
        "[Missing image](/mnt/kern-agent/agent-home/screenshots/missing.png)\n\n"
        "[Invalid image](/mnt/kern-agent/agent-home/screenshots/broken.png)\n\n"
        "[Temporary failure](/mnt/kern-agent/agent-home/screenshots/temporary.png)\n\n"
        "[Vector original](/mnt/kern-agent/agent-home/screenshots/vector.svg)\n\n"
        "[External image](https://example.com/image.png)"
    )

    def events(route):
        query = parse_qs(urlsplit(route.request.url).query)
        route.fulfill(json={"events": [] if "since" in query else [{
            "seq": 1, "thread_id": "thread-1", "event_type": "thread.message",
            "payload": {"source": "agent", "message": source},
        }]})

    def preview(route):
        assert route.request.headers.get("x-kern-csrf") == "1"
        path = parse_qs(urlsplit(route.request.url).query)["path"][0]
        requests.append(path)
        if path.endswith("temporary.png") and not transient_recovered:
            route.fulfill(status=500, json={"error": {"message": "temporary failure"}})
        elif path.endswith("large.png"):
            route.fulfill(status=400, json={"error": {"message": "image is larger than the 5 MiB chat preview limit"}})
        elif path.endswith("missing.png"):
            route.fulfill(status=404, json={"error": {"message": "path not found"}})
        else:
            route.fulfill(content_type="image/png", body=(b"invalid" if path.endswith("broken.png") else thin_image if "thin" in path else portrait if "mobile" in path else landscape))

    page.route("**/v1/workspace/chat/threads/thread-1/events*", events)
    page.route("**/v1/agent-files/image-preview?*", preview)
    log_in(page, url)
    page.evaluate("window.KernHost.openWorkspace('chat', 'thread-1')")
    root = page.locator("#panel-workspace-chat")
    scroller = root.locator("#chat-scroll")
    expect(root.locator(".chat-image")).to_have_count(7)
    scroller.evaluate("node => { node.style.scrollBehavior = 'auto'; node.scrollTop = 0; }")
    first = root.get_by_role("button", name="Expand image: Desktop layout", exact=True)
    first.scroll_into_view_if_needed()
    expect(first).to_be_enabled()
    image = first.locator("img")
    expect(image).to_be_visible()
    bounds = first.bounding_box()
    assert bounds and bounds["width"] <= 421 and bounds["height"] <= page.viewport_size["height"] * .25 + 2
    assert image.evaluate("img => img.naturalWidth / img.naturalHeight") == 1200 / 640
    assert image.evaluate("img => getComputedStyle(img).objectFit") == "contain"
    assert root.evaluate("node => node.scrollWidth <= node.clientWidth")
    screenshot_dir = os.environ.get("KERN_CHAT_IMAGE_SCREENSHOTS")
    size = "mobile" if page.viewport_size["width"] < 500 else "desktop"
    if screenshot_dir:
        Path(screenshot_dir).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(screenshot_dir) / f"{size}-chat.png"))
    position = scroller.evaluate("node => node.scrollTop")
    first.click()
    viewer = page.get_by_role("dialog", name="Image viewer")
    expect(viewer).to_be_visible()
    expect(viewer.locator("img")).to_have_attribute("alt", "Desktop layout")
    close = viewer.get_by_role("button", name="Close image viewer")
    expect(close).to_be_focused()
    page.keyboard.press("Tab")
    expect(close).to_be_focused()
    if screenshot_dir:
        page.screenshot(path=str(Path(screenshot_dir) / f"{size}-expanded.png"))
    page.keyboard.press("Escape")
    expect(viewer).not_to_be_visible()
    expect(first).to_be_focused()
    assert scroller.evaluate("node => node.scrollTop") == position
    first.click()
    close.click()
    expect(viewer).not_to_be_visible()
    second = root.get_by_role("button", name="Expand image: Mobile layout", exact=True)
    second.scroll_into_view_if_needed()
    expect(second).to_be_enabled()
    assert 119 <= second.bounding_box()["width"] <= 121
    assert second.locator("img").evaluate("img => img.naturalHeight") == 1000
    thin = root.get_by_role("button", name="Expand image: Thin image", exact=True)
    thin.scroll_into_view_if_needed()
    expect(thin).to_be_enabled()
    assert 119 <= thin.bounding_box()["width"] <= 121
    thin_link = root.get_by_role("button", name="Thin image", exact=True)
    expect(thin_link).to_be_visible()
    assert thin_link.bounding_box()["width"] >= 44
    for name in ("Large original", "Missing image", "Invalid image"):
        card = root.locator(".chat-image").filter(has=page.get_by_role("button", name=name, exact=True))
        card.scroll_into_view_if_needed()
        expect(card.locator(".chat-image-status")).to_have_text("Preview unavailable")
        expect(card.get_by_role("button", name=name, exact=True)).to_be_enabled()
    transient = root.locator(".chat-image").filter(has=page.get_by_role("button", name="Temporary failure", exact=True))
    transient.scroll_into_view_if_needed()
    retry = transient.get_by_role("button", name="Retry preview", exact=True)
    expect(retry).to_be_visible()
    attempts_before_retry = requests.count("/screenshots/temporary.png")
    transient_recovered = True
    retry.click()
    expect(transient.get_by_role("button", name="Expand image: Temporary failure", exact=True)).to_be_enabled()
    expect(retry).to_be_hidden()
    assert requests.count("/screenshots/temporary.png") == attempts_before_retry + 1
    assert not any(path.endswith("svg") or "example.com" in path for path in requests)
    root.get_by_role("button", name="Vector original", exact=True).click()
    expect(page.locator("#panel-files")).to_be_visible()
    # Switching conversations releases removed image URLs and closes their viewer.
    page.evaluate("window.KernHost.openWorkspace('chat', 'thread-1')")
    first.scroll_into_view_if_needed()
    expect(first).to_be_enabled()
    first.click()
    page.evaluate("window.KernChat.newThread()")
    expect(viewer).not_to_be_visible()
    assert page.evaluate("!document.body.classList.contains('chat-image-viewer-open')")
    page.evaluate("window.KernHost.openWorkspace('chat', 'thread-1')")
    first.scroll_into_view_if_needed()
    expect(first).to_be_enabled()
    first.click()
    page.route("**/v1/agent-files/read?path=/expired", lambda route: route.fulfill(
        status=401, json={"error": {"message": "unauthorized"}},
    ))
    page.evaluate("window.KernHost.api('GET', '/v1/agent-files/read?path=/expired').catch(() => {})")
    expect(page.locator("#login")).to_be_visible()
    expect(viewer).not_to_be_visible()
    expect(page.locator(".chat-image-viewer img[src]")).to_have_count(0)
    page.locator("#password").fill("dev")
    page.get_by_role("button", name="Log in", exact=True).click()
    expect(page.locator("#app")).to_be_visible()
    page.evaluate("window.KernHost.openWorkspace('chat', 'thread-1')")
    first.scroll_into_view_if_needed()
    expect(first).to_be_enabled()
    print(f"PASS: {size} chat image previews, fallback, sizing, fullscreen and session cleanup")
