"""Request decisions for the npm package integration.

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
    "registry.npmjs.org": (("GET", "HEAD"), ()),
    "nodejs.org": (("GET", "HEAD"), (r"^/dist(?:/.*)?$",)),
}

# Public package names are normally scanned because registry lookup paths can
# encode attacker-readable data. This exact dependency name is a false positive
# for the guard's credential-language rule; keep the exception static and scan
# its query string normally.
PUBLIC_PACKAGE_PATH_EXCEPTIONS = frozenset({"/token-types"})


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
    if "/-/" in path:
        # Tarball URLs (/<pkg>/-/<pkg>-<version>.tgz) are provider-returned after
        # metadata resolution, not agent-authored names.
        return None
    if path in PUBLIC_PACKAGE_PATH_EXCEPTIONS:
        return request_param_denial("", query)
    return request_param_denial(path, query)
