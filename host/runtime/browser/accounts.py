"""Saved account lifecycle, operator leases, and action usage. Worker-thread only."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import tempfile
import time
from typing import Any

from host.runtime.browser.providers import PROVIDERS
from host.runtime.browser.actions import ACTIONS
from host.runtime.browser.browser import Browser, WIDTH, HEIGHT
from host.runtime.browser.client import BrowserError

LEASE_SECONDS = 600


class Profile:
    def __init__(self, directory: Path, provider_name: str, factory: Any = Browser) -> None:
        self.provider = PROVIDERS[provider_name]
        self.directory = directory
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.file = directory / "state.json"
        self.data: dict[str, Any] = {"provider_identifier": "", "state": "needs_attention", "checked_at": "", "usage": {}}
        if self.file.exists():
            self.data = json.loads(self.file.read_text())
        self.browser: Any = None
        self.factory = factory
        self.lease = ""
        self.expires = 0.0

    def save(self) -> None:
        temporary = self.file.with_suffix(".tmp")
        with temporary.open("w") as stream:
            os.chmod(temporary, 0o600)
            json.dump(self.data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.file)
        fd = os.open(self.directory, os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def close(self) -> None:
        browser, self.browser = self.browser, None
        if browser:
            try:
                browser.save_state()
            finally:
                browser.close()

    def launch(self) -> Any:
        if self.browser is None:
            self.browser = self.factory(self.directory / "auth.json", self.provider.login_url)
        return self.browser

    def status(self) -> dict[str, Any]:
        return {"provider": self.provider.name, "provider_identifier": self.data["provider_identifier"],
                "state": self.data["state"], "checked_at": self.data["checked_at"]}

    def check_lease(self, body: dict[str, Any]) -> dict[str, Any]:
        token = body.get("lease")
        if not self.lease or not isinstance(token, str) or not secrets.compare_digest(token, self.lease):
            raise BrowserError("Browser control expired or belongs to another window. Open browser again.")
        return {key: value for key, value in body.items() if key != "lease"}

    def dispatch(self, operation: str, body: dict[str, Any]) -> dict[str, Any]:
        if self.lease and time.monotonic() > self.expires:
            self.lease = ""
            self.close()
        if operation == "status" and not body:
            return self.status()
        if operation == "check" and not body:
            if self.lease:
                raise BrowserError("Close the browser popup before checking the login.")
            if not self.data["provider_identifier"]:
                return self.status()
            self.data["state"] = "needs_attention"
            try:
                live_account = self.provider.verify_account(self.launch().page)
                self.data["state"] = "connected" if live_account == self.data["provider_identifier"] else "needs_attention"
            except BrowserError:
                self.data["state"] = "needs_attention"
            finally:
                self.close()
                self.data["checked_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                self.save()
            return self.status()
        if operation == "open" and not body:
            if self.lease:
                raise BrowserError("Another window controls this browser. Close it or wait ten minutes for control to expire.")
            self.data["state"] = "needs_attention"
            self.save()
            self.launch()
            self.lease = secrets.token_urlsafe(32)
            self.expires = time.monotonic() + LEASE_SECONDS
            return {"site": self.provider.login_url, "lease": self.lease, "width": WIDTH, "height": HEIGHT}
        if operation in {"frame", "input", "save", "cancel"}:
            payload = self.check_lease(body)
            if operation == "input":
                self.expires = time.monotonic() + LEASE_SECONDS
                self.launch().input(payload)
                return {"ok": True}
            if operation == "frame" and not payload:
                return {"image": self.launch().frame(), "origin": self.launch().origin()}
            if operation == "cancel" and not payload:
                self.close()
                self.lease = ""
                return self.status()
            if operation == "save" and not payload:
                account = self.provider.verify_account(self.launch().page)
                account = self.provider.validate_identifier(account)
                if self.data["provider_identifier"] and account != self.data["provider_identifier"]:
                    self.data["state"] = "needs_attention"
                    self.data["checked_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                    self.save()
                    raise BrowserError("This connection belongs to another account. Sign back in to the saved account or connect a new account.")
                self.close()
                self.data["provider_identifier"] = account
                self.data["state"] = "connected"
                self.data["checked_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                self.save()
                self.lease = ""
                return self.status()
        if operation == "disconnect" and not body:
            browser, self.browser = self.browser, None
            if browser:
                browser.close()
            self.lease = ""
            return {"ok": True}
        if operation in ACTIONS:
            return ACTIONS[operation](self, body)
        raise BrowserError("Unsupported browser operation or fields.")



class Accounts:
    """Admin-owned accounts and one temporary login; browser files are internal."""
    def __init__(self, directory: Path, factory: Any = Browser) -> None:
        self.directory = directory
        self.factory = factory
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.profiles: dict[str, Profile] = {}
        self.pending: dict[str, Profile] = {}
        for entry in directory.iterdir():
            if re.fullmatch(r"login_[a-f0-9]{32}", entry.name):
                shutil.rmtree(entry)  # Incomplete logins never survive restart.
            elif re.fullmatch(r"acct_[a-f0-9]{32}", entry.name) and (entry / "provider").is_file():
                profile = Profile(entry, (entry / "provider").read_text(), factory)
                if profile.data["provider_identifier"]:
                    self.profiles[entry.name] = profile
                else:
                    shutil.rmtree(entry)

    def expire(self) -> None:
        for key, profile in list(self.pending.items()):
            if time.monotonic() > profile.expires:
                profile.close()
                shutil.rmtree(profile.directory)
                del self.pending[key]
        for profile in self.profiles.values():
            if profile.lease and time.monotonic() > profile.expires:
                profile.lease = ""
                profile.close()

    def dispatch(self, operation: str, body: dict[str, Any], *, agent_action: bool = False) -> dict[str, Any]:
        self.expire()
        if operation == "ready" and not body:
            with tempfile.TemporaryDirectory() as temporary:
                browser = self.factory(Path(temporary) / "auth.json", "about:blank")
                try:
                    browser.frame()
                finally:
                    browser.close()
            return {"ready": True}
        if operation == "list" and not body:
            return {"accounts": [{"account_id": key, **profile.status()} for key, profile in self.profiles.items()]}
        if operation == "create" and set(body) == {"provider"}:
            if not isinstance(body["provider"], str) or body["provider"] not in PROVIDERS:
                raise BrowserError("Unsupported browser provider.")
            if self.pending or any(profile.lease for profile in self.profiles.values()):
                raise BrowserError("Another login is open. Close it or wait ten minutes for it to expire.")
            if len(self.profiles) >= 5:
                raise BrowserError("Up to 5 browser accounts can be saved. Disconnect an unused account first.")
            key = "login_" + secrets.token_hex(16)
            profile = Profile(self.directory / key, body["provider"], self.factory)
            (profile.directory / "provider").write_text(profile.provider.name)
            profile.expires = time.monotonic() + LEASE_SECONDS
            self.pending[key] = profile
            return {"login_id": key}
        pending = "login_id" in body
        key_value = body.get("login_id" if pending else "account_id")
        collection = self.pending if pending else self.profiles
        if not isinstance(key_value, str) or key_value not in collection:
            raise BrowserError("Select a saved browser account or start a new login.")
        key = key_value
        profile = collection[key]
        if pending and (agent_action or operation not in {"open", "frame", "input", "save", "cancel"}):
            raise BrowserError("Finish the operator login before using this account.")
        if agent_action and profile.lease:
            raise BrowserError("Browser account is open for operator control. Save and close it before using agent actions.")
        if agent_action and profile.data["state"] != "connected":
            raise BrowserError("Browser login needs attention. Check this account before using agent actions.")
        if (operation in {"open", "check"} or operation in ACTIONS) and any(other.lease for other in [*self.profiles.values(), *self.pending.values()] if other is not profile):
            raise BrowserError("Another website is open. Close its browser window first.")
        payload = {name: value for name, value in body.items() if name != ("login_id" if pending else "account_id")}
        result = profile.dispatch(operation, payload)
        if pending and operation == "save":
            account_id = "acct_" + key.removeprefix("login_")
            destination = self.directory / account_id
            profile.directory.rename(destination)
            fd = os.open(self.directory, os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            profile.directory, profile.file = destination, destination / "state.json"
            self.profiles[account_id] = profile
            del self.pending[key]
            return {"account_id": account_id, **result}
        if pending and operation == "cancel":
            shutil.rmtree(profile.directory)
            del self.pending[key]
            return {"ok": True}
        if operation == "disconnect":
            shutil.rmtree(profile.directory)
            del self.profiles[key]
        if operation in {"save", "check", "cancel"}:
            return {"account_id": key, **result}
        return result
