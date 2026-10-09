"""LinkedIn session identity and closed public-profile URL inputs."""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from host.runtime.browser.client import BrowserError

name = "linkedin"
login_url = "https://www.linkedin.com/feed/"


def profile_url(value: object) -> str:
    if not isinstance(value, str) or len(value) > 240 or any(ord(c) < 33 or ord(c) == 127 for c in value):
        raise BrowserError("Provide a LinkedIn HTTPS profile URL: https://www.linkedin.com/in/profile-name/.")
    try:
        parts = urlsplit(value)
    except ValueError:
        raise BrowserError("Provide a valid LinkedIn HTTPS profile URL.") from None
    match = re.fullmatch(r"/in/([A-Za-z0-9_-]{1,200})/?", parts.path)
    if (parts.scheme != "https" or parts.netloc not in {"linkedin.com", "www.linkedin.com"}
            or parts.query or parts.fragment or not match):
        raise BrowserError("Provide a LinkedIn HTTPS profile URL without credentials, query or fragment.")
    return "https://www.linkedin.com/in/" + match[1].lower() + "/"


def validate_identifier(value: object) -> str:
    return profile_url(value)


def verify_account(page: Any) -> str:
    from playwright.sync_api import expect  # type: ignore[import-not-found]
    # A profile visible in the page body is not evidence of the signed-in user.
    # Only the authenticated navigation's own Me menu is eligible.
    me = page.locator(".global-nav__me").filter(visible=True)
    expect(me).to_have_count(1, timeout=10000)
    button = me.get_by_role("button").filter(visible=True)
    expect(button).to_have_count(1, timeout=10000)
    button.click(timeout=10000)
    link = me.get_by_role("link", name=re.compile(r"^View [Pp]rofile$"))
    expect(link).to_have_count(1, timeout=10000)
    expect(link).to_be_visible(timeout=10000)
    href = link.get_attribute("href", timeout=10000) or ""
    account = profile_url("https://www.linkedin.com" + href if href.startswith("/in/") else href)
    button.click(timeout=10000)
    return account
