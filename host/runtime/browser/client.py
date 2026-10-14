"""Bounded IPC to the browser service. Authorization is checked using peer UID."""
from __future__ import annotations

import http.client
import json
import socket
import struct
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote

from host.constants import BROWSER_SOCKET_PATH


class BrowserError(RuntimeError):
    pass


MAX_SCREENSHOT_BYTES = 20 * 1024 * 1024
MAX_SCREENSHOT_WIDTH = 1100
MAX_SCREENSHOT_HEIGHT = 16000


@dataclass(frozen=True)
class Screenshot:
    png: bytes
    width: int
    height: int
    clipped: bool
    url: str


class BrowserActionNotStarted(BrowserError):
    """The service rejected admission before queueing any browser work."""

    def __init__(self) -> None:
        super().__init__("Browser is busy. The action was not started. Wait for the current action to finish.")


class Connection(http.client.HTTPConnection):
    def __init__(self) -> None:
        super().__init__("localhost", timeout=240)

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(BROWSER_SOCKET_PATH)
        except OSError:
            sock.close()
            raise
        self.sock = sock


def request(path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    conn = Connection()
    try:
        conn.request("POST", path, json.dumps(body or {}, ensure_ascii=False).encode("utf-8"), {"Content-Type": "application/json"})
        response = conn.getresponse()
        raw = response.read(3 * 1024 * 1024 + 1)
        if len(raw) > 3 * 1024 * 1024:
            raise ValueError("oversized response")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError("invalid response")
        if response.status != 200:
            if response.status == 409 and result.get("code") == "browser_busy" and result.get("execution_state") == "not_started":
                raise BrowserActionNotStarted()
            # Only our service's bounded errors; browser/provider errors never cross IPC.
            raise BrowserError(str(result.get("error", "Browser unavailable.")))
        return result
    except (OSError, ValueError, http.client.HTTPException) as exc:
        raise BrowserError("Browser service unavailable. Check Host diagnostics. If submitting a post or message, check the website before approving another attempt.") from exc
    finally:
        conn.close()


def screenshot(url: str) -> Screenshot:
    """Receive one bounded PNG over the existing tools-only Unix socket."""
    conn = Connection()
    try:
        conn.request("POST", "/actions/screenshot_url", json.dumps({"url": url}).encode("utf-8"),
                     {"Content-Type": "application/json"})
        response = conn.getresponse()
        if response.status != 200:
            raw = response.read(8193)
            if len(raw) > 8192:
                raise ValueError("oversized error")
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ValueError("invalid error")
            if response.status == 409 and result.get("code") == "browser_busy" and result.get("execution_state") == "not_started":
                raise BrowserActionNotStarted()
            raise BrowserError(str(result.get("error", "Browser screenshot failed.")))
        length = int(response.getheader("Content-Length", ""))
        if response.getheader("Content-Type") != "image/png" or not 24 <= length <= MAX_SCREENSHOT_BYTES:
            raise ValueError("invalid image response")
        raw = response.read(MAX_SCREENSHOT_BYTES + 1)
        if len(raw) != length or raw[:16] != b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR":
            raise ValueError("invalid PNG")
        width, height = struct.unpack(">II", raw[16:24])
        if not 1 <= width <= MAX_SCREENSHOT_WIDTH or not 1 <= height <= MAX_SCREENSHOT_HEIGHT:
            raise ValueError("oversized dimensions")
        clipped = response.getheader("X-Kern-Screenshot-Clipped")
        final_url = unquote(response.getheader("X-Kern-Screenshot-URL", ""))
        if clipped not in {"true", "false"} or len(final_url.encode("utf-8")) > 4096:
            raise ValueError("invalid screenshot metadata")
        return Screenshot(raw, width, height, clipped == "true", final_url)
    except (OSError, ValueError, http.client.HTTPException) as exc:
        raise BrowserError("Browser screenshot unavailable or incomplete. Check Host diagnostics.") from exc
    finally:
        conn.close()
