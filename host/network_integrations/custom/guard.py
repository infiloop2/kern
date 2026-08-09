"""Request decisions for operator-configured custom domains.

The operator names each domain, its methods, and its path guards. That is the
boundary, and it is the whole boundary: adding a custom domain means trusting
that destination with anything the agent can reach.

The host deliberately inspects nothing inside these requests. It cannot — an
operator API has no known client, no known header set, and no knowable URL
grammar, so any content rule here is guesswork that denies legitimate traffic
(opaque session cookies, entity-tag preconditions, idempotency keys) without
stopping a determined sender. Request bodies were never scanned either, so on
any write-capable domain the guard was never a boundary in the first place.
Managed integrations are different and are guarded accordingly: their clients
and destinations are known, so their headers are held to a shape and their URL
values are scanned.
"""

from __future__ import annotations

from host.network_integrations.base import AccountAttestor
from host.network_integrations.custom.manifest import CustomIntegration, rule_for_host
from host.runtime.core.network_policy import route_allowed


def host_allowed(config: CustomIntegration, host: str) -> bool:
    rule = rule_for_host(config, host)
    return bool(rule and rule.allow_http_methods)


def websocket_allowed(config: CustomIntegration, host: str) -> bool:
    rule = rule_for_host(config, host)
    return bool(rule and rule.allow_websocket)


def request_denied(
    config: CustomIntegration,
    method: str,
    host: str,
    path: str,
    query: str,
    headers: list[tuple[str, str]],
    body: bytes,
    account_attestor: AccountAttestor | None = None,
) -> str | None:
    del headers, body, account_attestor
    rule = rule_for_host(config, host)
    if rule is None or not route_allowed(
        method, path, query, rule.allow_http_methods, rule.path_guards
    ):
        return "network_policy_denied"
    return None
