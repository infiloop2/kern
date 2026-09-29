"""Temporary Chromium, private auth snapshots, and operator screen/input controls."""
from __future__ import annotations

import base64
import ipaddress
import json
import os
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlsplit

from host.runtime.browser.client import BrowserError
from host.runtime.core import host_errors, host_metrics

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
        self.reported_failures: set[tuple[str, str, str]] = set()
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
            self.context.route("**/*", self.route_request)
            self.context.route_web_socket("**/*", lambda route: route.close())
            self.context.on("requestfailed", self.record_failed_request)
            self.context.on("response", self.record_response)
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

    def report_failure(self, reason: str, *, host: str = "", resource_type: str = "") -> None:
        # Session-wide event facts, not a diagnosis of the currently displayed page.
        # Bound noise and synchronous journal writes, including repeated frame failures.
        key = (reason, host, resource_type)
        if key in self.reported_failures or len(self.reported_failures) >= 20:
            return
        self.reported_failures.add(key)
        host_errors.report_warning(
            "browser.session", reason,
            context={"host": host, "resource_type": resource_type, **host_metrics.service_resource_snapshot("browser")},
        )

    def record_resource_failure(self, request: Any, reason: str) -> None:
        if request.resource_type not in {"document", "script", "stylesheet", "xhr", "fetch"}:
            return
        try:
            host = urlsplit(request.url).hostname or ""
        except ValueError:
            host = ""
        self.report_failure(reason, host=host, resource_type=request.resource_type)

    def route_request(self, route: Any) -> None:
        if permitted_url(route.request.url):
            route.fallback()
        else:
            self.record_resource_failure(route.request, "Blocked by Browser URL policy")
            route.abort()

    def record_failed_request(self, request: Any) -> None:
        if not permitted_url(request.url):
            return  # The routing policy already reported this intentional abort.
        # Raw failures can contain credentials or URL paths. Keep only Chromium's code.
        match = re.search(r"net::ERR_[A-Z_]+", request.failure or "")
        self.record_resource_failure(request, match.group(0) if match else "Request failed")

    def record_response(self, response: Any) -> None:
        if response.status >= 400:
            self.record_resource_failure(response.request, f"HTTP {response.status}")

    def adopt_page(self, page: Any) -> None:
        if len(self.context.pages) > 8:
            page.close()
            return
        self.page = page
        page.on("pageerror", lambda _error: self.report_failure("Uncaught page script error"))
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
        try:
            return base64.b64encode(self.page.screenshot(type="jpeg", quality=75)).decode("ascii")
        except Exception:
            self.report_failure("Screenshot capture failed")
            raise

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
        elif kind == "reload" and set(body) == {"kind"}:
            self.page.reload(wait_until="domcontentloaded")
        else:
            raise BrowserError("Unsupported browser input.")
