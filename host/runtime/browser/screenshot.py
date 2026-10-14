"""One anonymous, bounded page capture. No saved account or session state."""
from __future__ import annotations

import base64
import math
import threading
from typing import Any

from host.runtime.browser.client import BrowserError, Screenshot, MAX_SCREENSHOT_BYTES, MAX_SCREENSHOT_HEIGHT, MAX_SCREENSHOT_WIDTH

CAPTURE_SECONDS = 60


def capture(browser: Any, url: str) -> Screenshot:
    # Playwright/CDP timeouts alone do not bound a wedged renderer. Kill only
    # this temporary Chromium process group if the whole capture overruns.
    expired = threading.Event()

    def timeout() -> None:
        expired.set()
        browser.process.kill()

    timer = threading.Timer(CAPTURE_SECONDS, timeout)
    timer.start()
    try:
        page = browser.page
        # Popups must not replace the requested page or become extra captures.
        browser.context.remove_listener("page", browser.adopt_page)
        browser.context.on("page", lambda popup: popup.close())
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(1500)
        session = browser.context.new_cdp_session(page)
        try:
            def size() -> tuple[int, int]:
                content = session.send("Page.getLayoutMetrics")["cssContentSize"]
                values = (content["width"], content["height"])
                if any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in values):
                    raise BrowserError("Browser returned invalid page dimensions.")
                return math.ceil(values[0]), math.ceil(values[1])

            def scroll(y: int) -> None:
                result = session.send("Runtime.evaluate", {
                    "expression": f"window.scrollTo(0, {y})", "timeout": 500,
                })
                if "exceptionDetails" in result:
                    raise BrowserError("The page could not be prepared for a screenshot.")
                page.wait_for_timeout(150)

            # Encourage lazy-loaded content to appear, without chasing an
            # infinite feed. Recheck dimensions as the document grows.
            for y in range(760, MAX_SCREENSHOT_HEIGHT, 760):
                _, height = size()
                if y >= height:
                    break
                scroll(y)
            scroll(0)
            width, height = size()
            clipped = width > MAX_SCREENSHOT_WIDTH or height > MAX_SCREENSHOT_HEIGHT
            width, height = min(width, MAX_SCREENSHOT_WIDTH), min(height, MAX_SCREENSHOT_HEIGHT)
            result = session.send("Page.captureScreenshot", {
                "format": "png", "captureBeyondViewport": True,
                "clip": {"x": 0, "y": 0, "width": width, "height": height, "scale": 1},
            })
            data = result.get("data")
            if not isinstance(data, str) or len(data) > 4 * ((MAX_SCREENSHOT_BYTES + 2) // 3):
                raise BrowserError("Screenshot exceeds the image byte limit.")
            png = base64.b64decode(data, validate=True)
            if len(png) > MAX_SCREENSHOT_BYTES:
                raise BrowserError("Screenshot exceeds the image byte limit.")
            if expired.is_set():
                raise BrowserError("Browser screenshot timed out.")
            return Screenshot(png, width, height, clipped, page.url.encode("utf-8")[:4096].decode("utf-8", errors="ignore"))
        finally:
            try:
                session.detach()
            except Exception:
                pass  # Renderer teardown must not mask the capture failure.
    finally:
        timer.cancel()
        timer.join()
