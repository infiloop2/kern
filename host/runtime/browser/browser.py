"""Temporary Chromium, private auth snapshots, and operator screen/input controls."""
from __future__ import annotations

import base64
import ipaddress
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from host.runtime.browser.client import BrowserError

WIDTH, HEIGHT = 1100, 760
SAVED_STATE_LIMIT_BYTES = 1_000_000_000


def permitted_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname or ""
        if parsed.scheme != "https" or parsed.port not in (None, 443) or parsed.username or parsed.password or not hostname:
            return False
        try:
            return ipaddress.ip_address(hostname).is_global
        except ValueError:
            return "." in hostname and not hostname.endswith((".localhost", ".local", ".internal"))
    except ValueError:
        return False


class Browser:
    """Temporary Chromium with private authentication snapshots; no agent cookie access."""
    def __init__(self, auth_file: Path, site: str) -> None:
        from playwright.sync_api import sync_playwright  # type: ignore[import-not-found]
        self.site = site
        self.auth_file = auth_file
        self.runtime = sync_playwright().start()
        try:
            self.process = self.runtime.chromium.launch(
                headless=True, chromium_sandbox=True, timeout=30000,
                args=["--disable-quic", "--disable-background-networking"],
            )
            self.context = self.process.new_context(
                storage_state=str(auth_file) if auth_file.exists() else None,
                viewport={"width": WIDTH, "height": HEIGHT}, locale="en-US",
                user_agent=f"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{self.process.version} Safari/537.36",
                accept_downloads=False, service_workers="block",
            )
            self.context.set_default_timeout(10000)
            self.context.set_default_navigation_timeout(20000)
            self.context.route("**/*", lambda route: route.fallback() if permitted_url(route.request.url) else route.abort())
            self.context.route_web_socket("**/*", lambda route: route.close())
            self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
            for page in self.context.pages[1:]:
                page.close()
            self.context.on("page", self.adopt_page)
            self.adopt_page(self.page)
            self.page.goto(self.site, wait_until="domcontentloaded")
        except Exception:
            try:
                self.close()
            except Exception:
                pass  # Preserve the original launch/navigation error.
            raise

    def adopt_page(self, page: Any) -> None:
        if len(self.context.pages) > 8:
            page.close()
            return
        self.page = page
        page.on("dialog", lambda dialog: dialog.accept() if dialog.type == "beforeunload" else dialog.dismiss())
        def closed() -> None:
            remaining = [item for item in self.context.pages if not item.is_closed()]
            if remaining:
                self.page = remaining[-1]
        page.on("close", closed)

    def save_state(self) -> None:
        # The service worker serializes all snapshots. Include the old file while
        # budgeting the replacement so its temporary copy fits as well.
        payload = json.dumps(self.context.storage_state(indexed_db=True)).encode("utf-8")
        used = sum(path.stat().st_size for path in self.auth_file.parent.parent.rglob("*") if path.is_file())
        if used + len(payload) > SAVED_STATE_LIMIT_BYTES:
            raise BrowserError("Browser saved state reached its 1 GB total limit. Disconnect an unused account to free space. If submitting a post, check X before approving another attempt.")
        temporary = self.auth_file.with_suffix(".tmp")
        try:
            with temporary.open("wb") as stream:
                os.chmod(temporary, 0o600)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self.auth_file)
        finally:
            temporary.unlink(missing_ok=True)

    def close(self) -> None:
        try:
            if hasattr(self, "process"):
                self.process.close()
        finally:
            self.runtime.stop()

    def origin(self) -> str:
        parsed = urlsplit(self.page.url)
        return f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme == "https" else "Browser page"

    def frame(self) -> str:
        return base64.b64encode(self.page.screenshot(type="jpeg", quality=75)).decode("ascii")

    def input(self, body: dict[str, Any]) -> None:
        kind = body.get("kind")
        if kind == "click" and set(body) == {"kind", "x", "y"}:
            x, y = body["x"], body["y"]
            if type(x) is not int or type(y) is not int or not (0 <= x < WIDTH and 0 <= y < HEIGHT):
                raise BrowserError("Invalid screen coordinates.")
            self.page.mouse.click(x, y)
        elif kind == "pointer" and set(body) == {"kind", "phase", "x", "y"}:
            x, y, phase = body["x"], body["y"], body["phase"]
            if type(x) is not int or type(y) is not int or not (0 <= x < WIDTH and 0 <= y < HEIGHT) or phase not in {"down", "move", "up"}:
                raise BrowserError("Invalid pointer input.")
            self.page.mouse.move(x, y)
            if phase == "down":
                self.page.mouse.down()
            elif phase == "up":
                self.page.mouse.up()
        elif kind == "text" and set(body) == {"kind", "text"}:
            value = body["text"]
            if not isinstance(value, str) or not 1 <= len(value) <= 4096:
                raise BrowserError("Enter at most 4,096 characters at once.")
            self.page.keyboard.insert_text(value)
        elif kind == "key" and set(body) == {"kind", "key"}:
            key = body["key"]
            if key not in {"Enter", "Tab", "Shift+Tab", "Backspace", "Delete", "Escape", "ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End", "Control+a"}:
                raise BrowserError("Unsupported browser key.")
            self.page.keyboard.press(key)
        elif kind == "scroll" and set(body) == {"kind", "delta"}:
            delta = body["delta"]
            if type(delta) is not int or not -1000 <= delta <= 1000:
                raise BrowserError("Invalid scroll distance.")
            self.page.mouse.wheel(0, delta)
        elif kind == "home" and set(body) == {"kind"}:
            self.page.goto(self.site, wait_until="domcontentloaded")
        else:
            raise BrowserError("Unsupported browser input.")
