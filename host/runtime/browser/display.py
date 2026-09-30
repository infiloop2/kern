"""A short-lived, authenticated X11 display for windowed Chromium on Linux."""
from __future__ import annotations

import os
from pathlib import Path
import re
import secrets
import select
import struct
import subprocess
import tempfile
import time

from host.runtime.browser.client import BrowserError


class Display:
    def __init__(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="kern-browser-display-")
        self.process: subprocess.Popen[bytes] | None = None
        try:
            authority = Path(self.directory.name) / "Xauthority"
            # Xauthority record: FamilyWild, address, display, protocol, cookie.
            # A wildcard lets Xvfb allocate a free display via -displayfd; access
            # still requires the random cookie, kept in this private directory.
            fields = (b"", b"", b"MIT-MAGIC-COOKIE-1", secrets.token_bytes(16))
            authority.write_bytes(struct.pack("!H", 65535) + b"".join(
                struct.pack("!H", len(value)) + value for value in fields
            ))
            authority.chmod(0o600)
            self.process = subprocess.Popen(
                ["Xvfb", "-displayfd", "1", "-screen", "0", "1280x960x24",
                 "-nolisten", "tcp", "-auth", str(authority), "-noreset"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
            assert self.process.stdout is not None
            deadline = time.monotonic() + 10
            number = b""
            while b"\n" not in number and len(number) < 6:
                if not select.select([self.process.stdout], [], [], max(0, deadline - time.monotonic()))[0]:
                    raise BrowserError("The private browser display did not become ready.")
                chunk = os.read(self.process.stdout.fileno(), 6 - len(number))
                if not chunk:
                    break
                number += chunk
            if not re.fullmatch(rb"[0-9]{1,5}\n", number):
                raise BrowserError("The private browser display could not start.")
            self.environment = {**os.environ, "DISPLAY": ":" + number.decode().strip(), "XAUTHORITY": str(authority)}
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        try:
            if self.process is not None:
                if self.process.poll() is None:
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait()
                if self.process.stdout is not None:
                    self.process.stdout.close()
        finally:
            self.directory.cleanup()
