"""Temporary Chromium, private auth snapshots, and operator screen/input controls."""
from __future__ import annotations

import base64
import ipaddress
import re
from typing import Any, TYPE_CHECKING, cast
from urllib.parse import urlsplit

from host.runtime.browser.client import BrowserError
from host.runtime.browser.chromium import Chromium
from host.runtime.browser.display import Display
from host.runtime.core import host_errors, host_metrics

if TYPE_CHECKING:
    from playwright.sync_api import StorageState  # type: ignore[import-not-found]

WIDTH, HEIGHT = 1100, 760


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
    def __init__(self, storage_state: dict[str, Any] | None, site: str, settings: dict[str, Any], *, block_media: bool = False) -> None:
        from playwright.sync_api import sync_playwright  # type: ignore[import-not-found]
        self.site = site
        self.block_media = block_media
        self.reported_failures: set[tuple[str, str, str]] = set()
        if settings.get("error"):
            raise BrowserError(settings["error"])
        self.runtime = sync_playwright().start()
        try:
            self.display = Display()
            self.process = Chromium(self.runtime, self.display.environment, settings)
            self.context = self.process.context
            if storage_state is not None:
                self.context.set_storage_state(cast("StorageState", storage_state))
            self.context.set_default_timeout(10000)
            self.context.set_default_navigation_timeout(20000)
            self.context.route("**/*", self.route_request)
            # Keep WebSocket native: Playwright routing replaces its constructor
            # and breaks X login. Secure sockets use Chromium's HTTPS relay.
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
            if self.omit_resource(route.request):
                route.abort()
            else:
                route.fallback()
        else:
            self.record_resource_failure(route.request, "Blocked by Browser URL policy")
            route.abort()

    def omit_resource(self, request: Any) -> bool:
        # X also fetches video playlists/segments via fetch/XHR. Keep scripts
        # on abs.twimg.com: they power the composer and are not optional media.
        return self.block_media and (
            request.resource_type in {"image", "media"}
            or urlsplit(request.url).hostname == "video.twimg.com"
        )

    def record_failed_request(self, request: Any) -> None:
        if not permitted_url(request.url):
            return  # The routing policy already reported this intentional abort.
        if self.omit_resource(request):
            return  # Intentional bandwidth savings, including video fetch/XHR.
        # Raw failures can contain credentials or URL paths. Keep only Chromium's code.
        match = re.search(r"net::ERR_[A-Z_]+", request.failure or "")
        self.record_resource_failure(request, match.group(0) if match else "Request failed")

    def record_response(self, response: Any) -> None:
        if response.status >= 400:
            self.record_resource_failure(response.request, f"HTTP {response.status}")

    def record_page_error(self, page: Any, error: Any) -> None:
        # Error names can be page-controlled too. Keep only standard JS types;
        # messages/stacks may contain account content, URL paths or credentials.
        name = getattr(error, "name", "")
        kind = name if name in {"Error", "TypeError", "ReferenceError", "SyntaxError",
                               "RangeError", "URIError", "EvalError", "AggregateError"} else "Error"
        self.report_failure(f"Uncaught page script error ({kind})",
                            host=urlsplit(page.url).hostname or "", resource_type="script")

    def adopt_page(self, page: Any) -> None:
        if len(self.context.pages) > 8:
            page.close()
            return
        self.page = page
        page.set_viewport_size({"width": WIDTH, "height": HEIGHT})
        page.on("pageerror", lambda error: self.record_page_error(page, error))
        page.on("dialog", lambda dialog: dialog.accept() if dialog.type == "beforeunload" else dialog.dismiss())
        def closed() -> None:
            remaining = [item for item in self.context.pages if not item.is_closed()]
            if remaining:
                self.page = remaining[-1]
        page.on("close", closed)

    def save_state(self) -> dict[str, Any]:
        return dict(self.context.storage_state(indexed_db=True))

    def close(self) -> None:
        try:
            if hasattr(self, "process"):
                self.process.close()
        finally:
            try:
                self.runtime.stop()
            finally:
                if hasattr(self, "display"):
                    self.display.close()

    def origin(self) -> str:
        parsed = urlsplit(self.page.url)
        return f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme == "https" else "Browser page"

    def frame(self) -> str:
        try:
            # DOM/font readiness can precede the headed compositor's first
            # frame. Cross a render cycle before asking it for a capture;
            # this also works for intentionally blank pages without a paint entry.
            self.page.wait_for_function(
                "() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve(true))))",
                timeout=5000,
            )
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
        elif kind == "type" and set(body) == {"kind", "text"}:
            value = body["text"]
            if not isinstance(value, str) or len(value) != 1 or not value.isprintable():
                raise BrowserError("Expected one printable character.")
            # Physical typing needs key events; paste/mobile text stays a single
            # insertion. Playwright inserts characters outside its keyboard map.
            self.page.keyboard.type(value)
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
