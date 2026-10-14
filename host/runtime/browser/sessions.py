"""Four thread-owned slots: one operator login and three independent tool calls."""
from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
import threading
from typing import Any

from host.constants import BROWSER_DEBUG_PORT, BROWSER_SESSION_COUNT
from host.runtime.browser.accounts import Accounts
from host.runtime.browser.client import BrowserActionNotStarted
from host.runtime.browser.storage import Store
from host.runtime.browser_network.relay import Network


class Sessions:
    def __init__(self, store: Store | None = None, factory: Any = None) -> None:
        self.store = store if store is not None else Store()
        self.network = Network(self.store)
        self.factory = factory
        self.workers = [ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"browser-{slot}")
                        for slot in range(BROWSER_SESSION_COUNT)]
        self.lock = threading.Condition()
        self.busy = [0] * BROWSER_SESSION_COUNT
        self.accounts: dict[str, int] = {}
        self.closed = False
        self.operator = self.workers[0].submit(self.new_accounts, 0).result()

    def new_accounts(self, slot: int) -> Accounts:
        return Accounts(self.store, self.factory, network=self.network, debug_port=BROWSER_DEBUG_PORT + slot)

    def submit(self, operation: str, body: dict[str, Any], *, agent_action: bool = False) -> Future[Any]:
        # Admission and account reservation are atomic. Reject before executing
        # anything when full; approved actions must never wait in a hidden queue.
        with self.lock:
            if not agent_action and operation != "cancel":
                # Preserve the short admission wait for popup polling racing
                # expiry maintenance or another brief operator request.
                self.lock.wait_for(lambda: self.closed or not self.busy[0], timeout=1)
            if self.closed:
                raise BrowserActionNotStarted()
            slot = next((s for s in range(1, BROWSER_SESSION_COUNT) if not self.busy[s]), None) if agent_action else 0
            if slot is None or (self.busy[slot] and operation != "cancel"):
                raise BrowserActionNotStarted()
            key = body.get("account_id")
            account = key if isinstance(key, str) else ""
            owner = self.accounts.get(account)
            if owner is not None and (agent_action or owner != 0):
                raise BrowserActionNotStarted()
            if account:
                self.accounts[account] = slot
            # Operator Cancel can queue behind its current frame; no tool job
            # ever uses this worker. Its lease is validated when it runs.
            self.busy[slot] += 1
            try:
                return self.workers[slot].submit(self.run, slot, operation, body, agent_action)
            except Exception:
                self.busy[slot] -= 1
                if account and owner is None:
                    self.accounts.pop(account, None)
                raise

    def run(self, slot: int, operation: str, body: dict[str, Any], agent_action: bool) -> Any:
        profiles = self.operator if slot == 0 else None
        try:
            if profiles is None:
                profiles = self.new_accounts(slot)
            if operation == "screenshot_url":
                return profiles.screenshot(body)
            return profiles.dispatch(operation, body, agent_action=agent_action)
        finally:
            try:
                if slot and profiles is not None:
                    for profile in profiles.profiles.values():
                        profile.close()
            finally:
                with self.lock:
                    for key, owner in list(self.accounts.items()):
                        if owner == slot:
                            del self.accounts[key]
                    if slot == 0:
                        for key, profile in self.operator.profiles.items():
                            if profile.browser or profile.lease:
                                self.accounts[key] = 0
                    self.busy[slot] -= 1
                    self.lock.notify_all()

    def expire(self) -> None:
        with self.lock:
            if self.closed or self.busy[0]:
                return
            self.busy[0] += 1
            self.workers[0].submit(self.maintain)

    def maintain(self) -> None:
        try:
            self.operator.expire()
        finally:
            with self.lock:
                for key, owner in list(self.accounts.items()):
                    if owner == 0:
                        profile = self.operator.profiles.get(key)
                        if profile is None or not (profile.browser or profile.lease):
                            del self.accounts[key]
                self.busy[0] -= 1
                self.lock.notify_all()

    def close(self) -> None:
        with self.lock:
            if self.closed:
                return
            self.closed = True
            self.lock.notify_all()
        for worker in self.workers[1:]:
            worker.shutdown(wait=True)
        try:
            self.workers[0].submit(self.close_operator).result()
        finally:
            self.workers[0].shutdown(wait=True)
            self.network.disconnect()

    def close_operator(self) -> None:
        for profile in [*self.operator.profiles.values(), *self.operator.pending.values()]:
            profile.close()
