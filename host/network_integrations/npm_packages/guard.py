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

import json
import re
from datetime import datetime
from urllib.parse import unquote
import zlib

from host.network_integrations.base import (
    AccountAttestor,
    ManagedIntegration,
    fixed_user_agent,
    request_param_denial,
    strip_credentials,
)
from host.runtime.core.network_policy import route_allowed
from host.param_guard import find_denial

ROUTES = {
    "registry.npmjs.org": (("GET", "HEAD"), ()),
    "nodejs.org": (("GET", "HEAD"), (r"^/dist(?:/.*)?$",)),
}

AUDIT_PATH = "/-/npm/v1/security/advisories/bulk"
AUDIT_MAX_BYTES = 1024 * 1024
# npm's URL-friendly alphabet also covers legacy names. Scopes may start with
# punctuation; older npm clients also accept unscoped leading hyphens.
PACKAGE_NAME = re.compile(r"(?:@[A-Za-z0-9._~!()*'-]+/)?[A-Za-z0-9._~!()*'-]+")
VERSION_IDENTIFIER = re.compile(r"v?[0-9]+(?:\.[0-9]+){0,2}(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?")
VERSION_TIMESTAMP = re.compile(r"(?<=[.+-])(?:19|20)[0-9]{12}(?=[.+-]|$)")

# Public package names are normally scanned because registry lookup paths can
# encode attacker-readable data. These exact public dependency names are false
# positives for the guard's credential-language rule. Match decoded paths so
# npm's encoded scoped-package separator works; keep queries guarded normally.
PUBLIC_PACKAGE_PATH_EXCEPTIONS = frozenset({
    "/token-types",
    "/@aws-sdk/credential-provider-env",
    "/@aws-sdk/credential-provider-http",
    "/@aws-sdk/credential-provider-ini",
    "/@aws-sdk/credential-provider-login",
    "/@aws-sdk/credential-provider-node",
    "/@aws-sdk/credential-provider-process",
    "/@aws-sdk/credential-provider-sso",
    "/@aws-sdk/credential-provider-web-identity",
    "/@aws-sdk/token-providers",
    "/@smithy/credential-provider-imds",
})


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate package name")
        result[key] = value
    return result


def _version_guard_value(version: str) -> str:
    """Mask only valid YYYYMMDDhhmmss version components, not arbitrary IDs."""
    if VERSION_IDENTIFIER.fullmatch(version) is None:
        return version

    def timestamp(match: re.Match[str]) -> str:
        try:
            datetime.strptime(match[0], "%Y%m%d%H%M%S")
        except ValueError:
            return match[0]
        return "timestamp"

    return VERSION_TIMESTAMP.sub(timestamp, version)


def _audit_denial(headers: list[tuple[str, str]], body: bytes) -> str | None:
    """Inspect npm's JSON (normally gzip) without admitting arbitrary POST data."""
    if len(body) > AUDIT_MAX_BYTES:
        return "request_body_too_large"
    types = [value.strip().lower() for key, value in headers if key.lower() == "content-type"]
    encodings = [value.strip().lower() for key, value in headers if key.lower() == "content-encoding"]
    if len(types) != 1 or types[0] not in {"application/json", "application/json; charset=utf-8"}:
        return "network_policy_denied"
    if len(encodings) > 1 or (encodings and encodings[0] not in {"identity", "gzip"}):
        return "network_policy_denied"
    try:
        if encodings == ["gzip"]:
            # npm emits one gzip member with no filename/comment/extra fields.
            # Reject optional headers and concatenated/trailing data as well as
            # bounding decompression before allocating the full decoded body.
            if body[:4] != b"\x1f\x8b\x08\x00":
                return "network_policy_denied"
            decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
            body = decoder.decompress(body, AUDIT_MAX_BYTES + 1)
            if len(body) > AUDIT_MAX_BYTES or decoder.unconsumed_tail:
                return "request_body_too_large"
            if not decoder.eof or decoder.unused_data:
                return "network_policy_denied"
        payload = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeError, ValueError, RecursionError, zlib.error):
        return "network_policy_denied"
    if not isinstance(payload, dict):
        return "network_policy_denied"
    for package, versions in payload.items():
        if (PACKAGE_NAME.fullmatch(package) is None
                or package.startswith((".", "_"))
                or package.rsplit("/", 1)[-1].startswith(".")):
            return "network_policy_denied"
        if not isinstance(versions, list) or not versions:
            return "network_policy_denied"
        if "/" + package not in PUBLIC_PACKAGE_PATH_EXCEPTIONS:
            denial = request_param_denial("/" + package, "")
            if denial:
                return denial
        for version in versions:
            # Arborist forwards local/workspace version fields verbatim, even
            # when they are not strict SemVer (for example "1.0"). Keep the
            # string bound and content guard without imposing another parser.
            if not isinstance(version, str) or not version or len(version) > 128:
                return "network_policy_denied"
            value_denial = find_denial(version)
            if value_denial and value_denial.reason == "request_param_pii_denied":
                value_denial = find_denial(_version_guard_value(version))
            if value_denial:
                return value_denial.reason
    return None


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
    del config, account_attestor
    if method.upper() == "POST":
        if host.lower() != "registry.npmjs.org" or path != AUDIT_PATH or query:
            return "network_policy_denied"
        return _audit_denial(headers, body)
    route = ROUTES.get(host.lower())
    if not route or not route_allowed(method, path, query, *route):
        return "network_policy_denied"
    if "/-/" in path:
        # Tarball URLs (/<pkg>/-/<pkg>-<version>.tgz) are provider-returned after
        # metadata resolution, not agent-authored names.
        return None
    if host.lower() == "registry.npmjs.org" and unquote(path) in PUBLIC_PACKAGE_PATH_EXCEPTIONS:
        return request_param_denial("", query)
    return request_param_denial(path, query)
