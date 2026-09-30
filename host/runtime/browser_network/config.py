"""Validated Browser settings backed by Postgres; secrets are write-only to callers."""
from __future__ import annotations

import re
import secrets
from typing import Any

from host.runtime.browser.client import BrowserError
from host.runtime.browser.storage import Store
from host.runtime.core import pgclient, secretbox


LOCATIONS = {
    "new_york": {"label": "New York", "country": "us", "city": "new_york", "locale": "en-US", "timezone": "America/New_York"},
    "london": {"label": "London", "country": "gb", "city": "london", "locale": "en-GB", "timezone": "Europe/London"},
}


class Settings:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.value: dict[str, str] = {"mode": ""}
        self.reload()

    def reload(self) -> None:
        try:
            value = self.store.load_settings()
            self.validate(value)
            self.value = value
        except (ValueError, OSError, BrowserError, pgclient.Error, secretbox.SecretBoxError):
            # A missing/unreadable database never silently enables Direct.
            self.value = {"mode": ""}

    @staticmethod
    def validate(value: dict[str, Any]) -> None:
        if not isinstance(value, dict):
            raise BrowserError("Browser connection settings must be an object.")
        mode = value.get("mode")
        fields = {"direct": {"mode"}, "decodo": {
            "mode", "username", "password", "location", "session"}}
        if not isinstance(mode, str) or mode not in fields or set(value) != fields[mode]:
            raise BrowserError("Choose Direct or Decodo Residential and complete its fields.")
        if any(not isinstance(v, str) or len(v) > 1024 or any(ord(c) < 32 or ord(c) > 126 for c in v) for v in value.values()):
            raise BrowserError("Connection settings must contain at most 1,024 printable ASCII characters per field.")
        if mode == "decodo":
            if not re.fullmatch(r"[a-zA-Z0-9_]{1,128}", value["username"]) or not value["password"]:
                raise BrowserError("Enter your Residential proxy username without targeting parameters, and its proxy password.")
            if value["location"] not in LOCATIONS:
                raise BrowserError("Choose New York or London for the Browser location.")
            if not re.fullmatch(r"[a-f0-9]{24}", value["session"]):
                raise BrowserError("Invalid Browser proxy session.")

    def prepare(self, body: dict[str, Any]) -> dict[str, str]:
        value = dict(body)
        if value.get("mode") == "decodo":
            if "session" in value:
                raise BrowserError("The Browser proxy session is managed by Kern.")
            for field in ("username", "location"):
                if isinstance(value.get(field), str):
                    value[field] = value[field].strip()
            if isinstance(value.get("username"), str):
                value["username"] = value["username"].removeprefix("user-")
            old = self.value
            if not value.get("password") and old.get("mode") == "decodo" and value.get("username") == old.get("username"):
                value["password"] = old.get("password", "")
            same_route = all(value.get(key) == old.get(key) for key in ("mode", "username", "location"))
            value["session"] = old["session"] if same_route else secrets.token_hex(12)
        self.validate(value)
        return value

    def proxy_username(self) -> str:
        value = self.value
        location = LOCATIONS[value["location"]]
        return (f"user-{value['username']}-country-{location['country']}-city-{location['city']}"
                f"-session-{value['session']}-sessionduration-1440")

    def save(self, value: dict[str, str]) -> None:
        self.store.save_settings(value)
        self.value = value

    def public(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "locations": [{"id": key, "label": value["label"]} for key, value in LOCATIONS.items()],
            "has_password": bool(self.value.get("password")),
            **{key: value for key, value in self.value.items() if key not in {"password", "session"}},
        }
        if not self.value["mode"]:
            result["error"] = "Browser connection settings are unavailable or invalid. Retry, or save a connection to resume Browser activity."
        elif self.value["mode"] == "decodo":
            # Derived browser identity is read-only; callers save only a preset ID.
            result.update(LOCATIONS[self.value["location"]])
        return result
