"""Provider contract and the explicitly supported connection types.

Add a provider module with these members, then register it here. Identifiers
are provider-defined strings: X verifies a handle, another provider may differ.
"""
from typing import Any, Protocol
from host.runtime.browser.providers import x


class Provider(Protocol):
    name: str
    login_url: str

    def verify_account(self, page: Any) -> str: ...
    def validate_identifier(self, value: object) -> str: ...


PROVIDERS: dict[str, Provider] = {"x": x}
