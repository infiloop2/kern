"""X connection identity: the signed-in account's lowercase handle."""
from __future__ import annotations

import re
from typing import Any
from host.runtime.browser.client import BrowserError

name = "x"
login_url = "https://x.com/"
HANDLE = re.compile(r"[A-Za-z0-9_]{1,15}")


def validate_identifier(value: object) -> str:
    if not isinstance(value, str) or not HANDLE.fullmatch(value):
        raise BrowserError("The selected account has no verified X handle.")
    return value.lower()


def verify_account(page: Any, *, timeout: int = 10000) -> str:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError  # type: ignore[import-not-found]
    try:
        href = page.get_by_test_id("AppTabBar_Profile_Link").get_attribute("href", timeout=timeout) or ""
    except PlaywrightTimeoutError:
        raise BrowserError("X sign-in could not be confirmed. Open the browser to check the account.") from None
    handle = href.removeprefix("/")
    if not HANDLE.fullmatch(handle):
        raise BrowserError("Sign in to X, then return to Home before saving the session.")
    return handle.lower()
