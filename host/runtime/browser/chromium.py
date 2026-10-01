"""Start sandboxed Chromium with its own profile, then attach Playwright."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import tempfile
import time
from typing import Any

from host.constants import BROWSER_DEBUG_PORT, BROWSER_NETWORK_PORT
from host.runtime.browser.client import BrowserError


class Chromium:
    def __init__(self, runtime: Any, environment: dict[str, str], settings: dict[str, Any]) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="kern-browser-profile-")
        self.child: subprocess.Popen[bytes] | None = None
        self.browser: Any = None
        self.closed = False
        try:
            # One browser is active at a time. Never attach to an older browser
            # if a failed shutdown or an unexpected listener occupies its port.
            with socket.socket() as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind(("127.0.0.1", BROWSER_DEBUG_PORT))
            locale = settings.get("locale", "en-US")
            profile = Path(self.directory.name) / "Default"
            profile.mkdir()
            (profile / "Preferences").write_text(json.dumps({"intl": {"accept_languages": locale}}))
            self.child = subprocess.Popen(
                [runtime.chromium.executable_path,
                 f"--user-data-dir={self.directory.name}",
                 "--remote-debugging-address=127.0.0.1", f"--remote-debugging-port={BROWSER_DEBUG_PORT}",
                 "--no-first-run", "--no-default-browser-check", f"--lang={locale}",
                 "--window-size=1100,850", "--force-device-scale-factor=1",
                 f"--proxy-server=http://127.0.0.1:{BROWSER_NETWORK_PORT}", "--proxy-bypass-list=<-loopback>",
                 "--disable-quic", "--disable-background-networking",
                 "--force-webrtc-ip-handling-policy=disable_non_proxied_udp", "about:blank"],
                env={**environment, "TZ": settings.get("timezone", "UTC")},
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if self.child.poll() is not None:
                    raise BrowserError("The sandboxed browser could not start.")
                with socket.socket() as probe:
                    probe.settimeout(0.2)
                    if probe.connect_ex(("127.0.0.1", BROWSER_DEBUG_PORT)) == 0:
                        break
                time.sleep(0.1)
            else:
                raise BrowserError("The private browser control connection did not become ready.")
            self.browser = runtime.chromium.connect_over_cdp(
                f"http://127.0.0.1:{BROWSER_DEBUG_PORT}", timeout=max(1, (deadline - time.monotonic()) * 1000),
            )
            # Keep the original Chrome context: creating an incognito context
            # here would undo the launch/context change verified on the laptop.
            self.context = self.browser.contexts[0]
            # Download policy belongs to this CDP session. Keep it attached:
            # detaching would restore Chrome's default download behavior.
            self.control = self.browser.new_browser_cdp_session()
            self.control.send("Browser.setDownloadBehavior", {"behavior": "deny", "eventsEnabled": True})
            # Equivalent to Playwright's service_workers="block" initialization;
            # a fresh profile has no previously registered workers.
            self.context.add_init_script("""
                if (navigator.serviceWorker) navigator.serviceWorker.register = async () => {
                    console.warn('Service Worker registration blocked');
                };
            """)
        except Exception:
            try:
                self.close()
            except Exception:
                pass  # Preserve the launch/attachment failure.
            raise

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            if self.browser is not None:
                self.browser.close()
        finally:
            try:
                if self.child is not None:
                    try:
                        os.killpg(self.child.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    try:
                        self.child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(self.child.pid, signal.SIGKILL)
                        self.child.wait(timeout=5)
                    # The parent can exit before a renderer. Reap any remaining
                    # members of this launch's process group before deleting it.
                    try:
                        os.killpg(self.child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            finally:
                self.directory.cleanup()
