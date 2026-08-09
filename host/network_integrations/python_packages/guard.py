"""Request decisions for the Python package integration.

Headers are not inspected. These are first-party destinations that reflect
nothing back, so a header cannot reach an attacker; what the proxy removes is
what a header can *do* — the credential headers, since the agent holds no
credential this registry should receive — and the free text in User-Agent.
The requested package name is different: npm and PyPI publish per-package
download statistics, so it is a channel someone can actually read, and it
keeps the parameter guard.
"""

from __future__ import annotations

from host.network_integrations.base import (
    AccountAttestor,
    ManagedIntegration,
    fixed_user_agent,
    request_param_denial,
    strip_credentials,
)
from host.runtime.core.network_policy import route_allowed

ROUTES = {
    "pypi.org": (("GET", "HEAD"), (r"^/simple(?:/.*)?$", r"^/pypi/[^/]+/json$")),
    "files.pythonhosted.org": (("GET", "HEAD"), (r"^/packages(?:/.*)?$",)),
}


def host_allowed(config: ManagedIntegration, host: str) -> bool:
    del config
    return host.lower() in ROUTES


def rewrite_request_headers(
    config: object,
    method: str,
    host: str,
    path: str,
    query: str,
    headers: list[tuple[str, str]],
    body: bytes,
) -> list[tuple[str, str]]:
    """Drop identity-bearing headers and replace User-Agent with the host's
    own; forward everything else as the client sent it."""
    del config, method, host, path, query, body
    return fixed_user_agent(strip_credentials(headers))


def request_denied(
    config: ManagedIntegration,
    method: str,
    host: str,
    path: str,
    query: str,
    headers: list[tuple[str, str]],
    body: bytes,
    account_attestor: AccountAttestor | None = None,
) -> str | None:
    del config, headers, body, account_attestor
    route = ROUTES.get(host.lower())
    if not route or not route_allowed(method, path, query, *route):
        return "network_policy_denied"
    if host.lower() == "files.pythonhosted.org":
        # Download URLs come from the simple-index response: their hash segments
        # and filenames are provider-echoed, not agent-authored.
        return None
    return request_param_denial(path, query)
