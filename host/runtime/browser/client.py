"""Bounded IPC to the browser service. Authorization is checked using peer UID."""
from __future__ import annotations

import http.client
import json
import socket
from typing import Any

from host.constants import BROWSER_SOCKET_PATH


class BrowserError(RuntimeError):
    pass


class Connection(http.client.HTTPConnection):
    def __init__(self) -> None:
        super().__init__("localhost", timeout=180)

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
            # Only our service's bounded errors; browser/provider errors never cross IPC.
            raise BrowserError(str(result.get("error", "Browser unavailable.")))
        return result
    except (OSError, ValueError, http.client.HTTPException) as exc:
        raise BrowserError("Browser service unavailable. Check Host diagnostics. If submitting a post or message, check the website before approving another attempt.") from exc
    finally:
        conn.close()
