"""X connection identity: the signed-in account's lowercase handle."""
from __future__ import annotations

import re
from typing import Any
from host.runtime.browser.client import BrowserError

name = "x"
login_url = "https://x.com/"
ACCOUNT_TIMEOUT_MS = 30000
HANDLE = re.compile(r"[A-Za-z0-9_]{1,15}")


class PageNotReady(BrowserError):
    """No usable account or explicit sign-in state appeared within the wait."""


class LoginRequired(BrowserError):
    """X explicitly displayed login or an account-access challenge."""


# Return only known states and the profile href, never page text. An absent
# profile link is inconclusive: X may still be loading its client application.
ACCOUNT_STATE = """() => {
    if (location.origin !== 'https://x.com') return false;
    const visible = element => {
        const style = getComputedStyle(element);
        return style.display !== 'none' && style.visibility !== 'hidden'
            && element.getClientRects().length > 0;
    };
    if (location.pathname.startsWith('/account/access')) return {state: 'challenge'};
    if (location.pathname.startsWith('/i/flow/login') ||
        Array.from(document.querySelectorAll('input[autocomplete="username"], input[type="password"]')).some(visible))
        return {state: 'login'};
    const profiles = Array.from(document.querySelectorAll('[data-testid="AppTabBar_Profile_Link"]')).filter(visible);
    if (profiles.length === 1)
        return {state: 'profile', href: profiles[0].getAttribute('href')};
    return false;
}"""


def validate_identifier(value: object) -> str:
    if not isinstance(value, str) or not HANDLE.fullmatch(value):
        raise BrowserError("The selected account has no verified X handle.")
    return value.lower()


def verify_account(page: Any, *, timeout: int = ACCOUNT_TIMEOUT_MS) -> str:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError  # type: ignore[import-not-found]
    try:
        result = page.wait_for_function(ACCOUNT_STATE, timeout=timeout)
    except PlaywrightTimeoutError:
        raise PageNotReady("X did not finish loading a verifiable account. Login status is unknown.") from None
    try:
        state = result.json_value()
    finally:
        result.dispose()
    if state["state"] == "challenge":
        raise LoginRequired("X requires an account check. Open the browser to complete it.")
    if state["state"] == "login":
        raise LoginRequired("X requires sign-in. Open the browser to sign in.")
    href = state.get("href") or ""
    handle = href.removeprefix("/")
    if not HANDLE.fullmatch(handle):
        raise BrowserError("X displayed an unrecognized account link. Open the browser to check the account.")
    return handle.lower()
