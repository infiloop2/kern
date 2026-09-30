"""Custom-domain decisions, with an optional strict request-content guard.

The opt-in applies the standard parameter checks without exceptions. It is off
by default because arbitrary APIs may need credentials, opaque values, or large
payloads. This remains a heuristic, not a complete exfiltration boundary.
"""

from __future__ import annotations

from email.message import Message
import json
import urllib.parse

from host.network_integrations.base import AccountAttestor
from host.network_integrations.custom.manifest import CustomIntegration, rule_for_host
from host.param_guard import MAX_PARAMETER_BYTES, REASON_ENCODED_BLOB, REASON_TOO_LARGE, find_denial
from host.runtime.core.network_policy import route_allowed


def host_allowed(config: CustomIntegration, host: str) -> bool:
    rule = rule_for_host(config, host)
    return bool(rule and rule.allow_http_methods)


def websocket_allowed(config: CustomIntegration, host: str) -> bool:
    rule = rule_for_host(config, host)
    # Opaque frame contents cannot satisfy the request-content contract.
    return bool(rule and rule.allow_websocket and not rule.guard_request_content)


def _text_denial(value: str) -> str | None:
    """Check original text and percent-decoded forms, without exemptions."""
    while True:
        denial = find_denial(value)
        if denial is not None:
            return denial.reason
        try:
            decoded = urllib.parse.unquote(value, errors="strict")
        except UnicodeDecodeError:
            return REASON_ENCODED_BLOB
        if decoded == value:
            return None
        value = decoded


def _body_denial(headers: list[tuple[str, str]], body: bytes) -> str | None:
    # Bound before parsing; never truncate or let many small fields evade G1.
    if len(body) > MAX_PARAMETER_BYTES:
        return REASON_TOO_LARGE
    if not body:
        return None
    content_type = Message()
    for name, value in headers:
        if name.lower() == "content-encoding" and value.strip().lower() not in {"", "identity"}:
            return "custom_content_uninspectable"
        if name.lower() == "content-type":
            content_type["content-type"] = value
    media_type = str(content_type.get("content-type", "text/plain")).split(";", 1)[0].strip().lower()
    if content_type.get_content_charset("utf-8") not in {"utf-8", "us-ascii"}:
        return "custom_content_uninspectable"
    declared_json = media_type == "application/json" or (media_type.startswith("application/") and media_type.endswith("+json"))
    if not declared_json and media_type not in {"text/plain", "application/x-www-form-urlencoded"}:
        return "custom_content_uninspectable"
    try:
        text = body.decode("utf-8", errors="strict")
        # MIME is agent-controlled. A receiver can parse JSON regardless of
        # the header, so JSON-shaped bodies must decode (or fail closed).
        if declared_json or text.lstrip().startswith(("{", "[", '"')):
            # Preserve duplicate object keys and numeric spellings. Inspect
            # decoded keys/values, not JSON punctuation or escaped spellings.
            pending = [json.loads(text, object_pairs_hook=list, parse_int=str, parse_float=str)]
            values = []
            while pending:
                item = pending.pop()
                if isinstance(item, (list, tuple)):
                    pending.extend(reversed(item))
                else:
                    values.append(str(item))
            text = ": ".join(values)
        else:
            pairs = urllib.parse.parse_qsl(text, keep_blank_values=True, errors="strict")
            text = "\n".join(f"{name}: {value}" for name, value in pairs)
    except (UnicodeError, ValueError, RecursionError):
        return "custom_content_uninspectable"
    return _text_denial(text)


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
    del account_attestor
    rule = rule_for_host(config, host)
    if rule is None or not route_allowed(
        method, path, query, rule.allow_http_methods, rule.path_guards
    ):
        return "network_policy_denied"
    if not rule.guard_request_content:
        return None
    if any(name.lower() == "upgrade" and value.lower() == "websocket" for name, value in headers):
        return "websocket_not_allowed"
    url = f"https://{host}{path}" + ("?" + query if query else "")
    denial = _text_denial(url)
    if denial:
        return denial
    # Query '+' uses form semantics; path '+' stays literal. The original URL
    # scan above already validates every percent escape as UTF-8.
    url = f"https://{host}{path}" + ("?" + urllib.parse.unquote_plus(query) if query else "")
    return (
        _text_denial(url)
        or _text_denial("\n".join(f"{name}: {value}" for name, value in headers))
        or _body_denial(headers, body)
    )
