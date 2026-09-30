"""In-memory persistence for offline Browser lifecycle and Chromium fixtures."""
from copy import deepcopy
from host.runtime.browser.storage import Store


class MemoryStore(Store):
    def __init__(self):
        self.settings = {"mode": "direct"}
        self.saved = {}
        self.snapshots = {}

    def load_settings(self):
        return deepcopy(self.settings)

    def save_settings(self, value):
        self.settings = deepcopy(value)

    def accounts(self):
        return deepcopy(self.saved)

    def auth(self, account_id):
        return deepcopy(self.snapshots[account_id])

    def save_account(self, account_id, provider, data, auth=None):
        self.saved[account_id] = (provider, deepcopy(data))
        if auth is not None:
            self.snapshots[account_id] = deepcopy(auth)

    def delete_account(self, account_id):
        self.saved.pop(account_id)
        self.snapshots.pop(account_id)
