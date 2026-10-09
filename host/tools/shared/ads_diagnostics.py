"""Contained Ads failures, with provider evidence only in Host diagnostics."""
from __future__ import annotations

import html
import json
import re
import urllib.parse
from collections.abc import Sequence

from host.runtime.core import host_errors
from host.tools.shared.web import ProviderWarning, WebRequestError


MAX_ADS_ERROR_BYTES = 256 * 1024
_CREDENTIAL_KEYS = {"client_secret", "access_token", "refresh_token", "oauth_token", "oauth_signature", "appsecret_proof", "fb_exchange_token", "client_assertion", "password", "api_key", "apikey", "secret", "token", "key", "x-amz-signature", "x-amz-credential", "x-amz-security-token"}
_CREDENTIAL_ASSIGNMENT = re.compile(r"(?i)(?:\b(?:" + "|".join(re.escape(key) for key in sorted(_CREDENTIAL_KEYS | {"code"})) + r")\b['\"]?\s*[=:]|\bbearer\s+\S+|https?://[^/\s]+@)")


def _safe_provider_text(value: str) -> str:
    inspected = value
    for _ in range(3):
        inspected = html.unescape(urllib.parse.unquote(inspected))
        if _CREDENTIAL_ASSIGNMENT.search(inspected):
            return "[credential-bearing provider text omitted]"
    return value


class AdsProviderError(RuntimeError):
    """Keep a curated result separate from the provider's error response."""

    def __init__(self, message: str, operation: str, *, status: int = 0, body: bytes = b"", body_truncated: bool = False) -> None:
        super().__init__(message)
        self.operation = operation
        self.status = status
        self.body = body
        self.body_truncated = body_truncated


def _without_request_values(value: object, depth: int = 0) -> tuple[object, bool]:
    if depth >= 12:
        return "[nested diagnostic omitted]", True
    if isinstance(value, dict):
        excluded = {"trigger", "request", "requestParameters", "request_parameters", "request_params", "parameters", "input", "data", "metadata", "value", "values", "invalidValue"}
        selected: dict[str, object] = {}
        truncated = False
        for key, item in value.items():
            if key not in excluded and key.lower() not in _CREDENTIAL_KEYS:
                selected[key], omitted = _without_request_values(item, depth + 1)
                truncated = truncated or omitted
        return selected, truncated
    if isinstance(value, list):
        items = [_without_request_values(item, depth + 1) for item in value[:32]]
        return [item for item, _ in items], len(value) > 32 or any(omitted for _, omitted in items)
    return _safe_provider_text(value) if isinstance(value, str) else value, False


def ads_error_body(body: bytes) -> bytes:
    """Exclude request values and preserve a marker for structural truncation."""
    try:
        value = json.loads(body)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return b""
    if not isinstance(value, dict):
        return b""
    errors: dict[str, object] = {}
    truncated = value.get("diagnostic_truncated") is True
    for key in ("error", "error_description", "errors", "operation_errors", "partialFailureError"):
        if key in value:
            errors[key], omitted = _without_request_values(value[key])
            truncated = truncated or omitted
    if not errors:
        return b""
    if truncated:
        errors["diagnostic_truncated"] = True
    return json.dumps(errors, ensure_ascii=False).encode("utf-8", "replace")


def _google_error_context(body: bytes) -> dict[str, str | bool]:
    """Retain typed codes and correlation IDs independently of message length."""
    try:
        value = json.loads(body)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return {}
    if not isinstance(value, dict):
        return {}
    error = value.get("error") or value.get("partialFailureError")
    if not isinstance(error, dict):
        return {}
    result: dict[str, str | bool] = {}
    status = error.get("status")
    if isinstance(status, str) and re.fullmatch(r"[A-Z_]{1,64}", status, re.ASCII):
        result["google_status"] = status
    details = error.get("details")
    if not isinstance(details, list):
        return result
    codes: list[str] = []
    reasons: list[str] = []
    paths: list[str] = []
    paths_omitted = False
    metadata_omitted = len(details) > 32
    for detail in details[:32]:
        if not isinstance(detail, dict):
            continue
        detail_type = detail.get("@type")
        if detail_type == "type.googleapis.com/google.rpc.ErrorInfo":
            reason = detail.get("reason")
            if isinstance(reason, str) and re.fullmatch(r"[A-Z0-9_]{1,80}", reason, re.ASCII):
                reasons.append(reason)
        elif isinstance(detail_type, str) and re.fullmatch(r"type\.googleapis\.com/google\.ads\.googleads\.v[0-9]+\.errors\.GoogleAdsFailure", detail_type, re.ASCII):
            request_id = detail.get("requestId")
            if isinstance(request_id, str) and re.fullmatch(r"[a-zA-Z0-9_.-]{1,128}", request_id, re.ASCII):
                result.setdefault("google_request_id", request_id)
            errors = detail.get("errors")
            if isinstance(errors, list):
                metadata_omitted = metadata_omitted or len(errors) > 32
                for item in errors[:32]:
                    location = item.get("location") if isinstance(item, dict) else None
                    elements = location.get("fieldPathElements") if isinstance(location, dict) else None
                    if isinstance(elements, list) and (len(elements) > 8 or len(paths) >= 4):
                        paths_omitted = True
                    if isinstance(elements, list) and 0 < len(elements) <= 8 and len(paths) < 4:
                        parts: list[str] = []
                        for element in elements:
                            name = element.get("fieldName") if isinstance(element, dict) else None
                            index = element.get("index") if isinstance(element, dict) else None
                            if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]{0,63}", name, re.ASCII) or index is not None and (type(index) is not int or not 0 <= index <= 1000000):
                                parts = []
                                break
                            parts.append(name + (f"[{index}]" if index is not None else ""))
                        if parts:
                            paths.append(".".join(parts))
                    error_code = item.get("errorCode") if isinstance(item, dict) else None
                    if isinstance(error_code, dict):
                        for category, code in error_code.items():
                            if isinstance(category, str) and re.fullmatch(r"[a-zA-Z]{1,64}Error", category, re.ASCII) and isinstance(code, str) and re.fullmatch(r"[A-Z0-9_]{1,80}", code, re.ASCII):
                                codes.append(f"{category}:{code}")
    if codes:
        joined_codes = "; ".join(dict.fromkeys(codes))
        result["google_error_codes"] = joined_codes[:512]
        result["google_error_codes_truncated"] = len(joined_codes) > 512
    if reasons:
        joined_reasons = "; ".join(dict.fromkeys(reasons))
        result["google_error_reasons"] = joined_reasons[:512]
        result["google_error_reasons_truncated"] = len(joined_reasons) > 512
    if paths:
        joined = "; ".join(dict.fromkeys(paths))
        result["google_error_paths"] = joined[:512]
        result["google_error_paths_truncated"] = len(joined) > 512 or paths_omitted
    elif paths_omitted:
        result["google_error_paths_truncated"] = True
    if metadata_omitted:
        result["google_metadata_truncated"] = True
    return result


def report_ads_failure(
    tool_id: str, action_id: str, exc: BaseException, *, phase: str,
    confirmed: Sequence[str] = (),
) -> None:
    """Report once at the catch that returns the failed action.

    Never inspect input, credentials, headers or locals. Error envelopes can
    contain provider text, so they belong only in authenticated diagnostics.
    Preserve the original HTTP cause across curated/reconnect exceptions.
    """
    host_errors.report_warning(f"tools.{tool_id}", exc,
        context=ads_failure_context(tool_id, action_id, exc, phase=phase, confirmed=confirmed), kind="provider_failure")


def ads_failure_context(
    tool_id: str, action_id: str, exc: BaseException, *, phase: str,
    confirmed: Sequence[str] = (),
) -> dict[str, str | int | bool]:
    """Build the same bounded context for warnings emitted by the host boundary."""
    try:
        return _ads_failure_context(tool_id, action_id, exc, phase=phase, confirmed=confirmed)
    except Exception:
        return {"tool_id": tool_id, "action_id": action_id, "phase": phase, "diagnostic_incomplete": True}


def _ads_failure_context(
    tool_id: str, action_id: str, exc: BaseException, *, phase: str,
    confirmed: Sequence[str],
) -> dict[str, str | int | bool]:
    context: dict[str, str | int | bool] = {
        "tool_id": tool_id, "action_id": action_id, "phase": phase,
        "provider": {"x_ads": "X Ads", "google_ads": "Google Ads", "instagram_ads": "Meta Marketing API"}[tool_id],
    }
    if confirmed:
        resources = "; ".join(confirmed).encode("utf-8")
        context["confirmed_resources"] = resources[:512].decode("utf-8", "ignore")
        context["confirmed_resources_truncated"] = len(resources) > 512
    current: BaseException | None = exc
    seen: set[int] = set()
    body = b""
    transport_truncated = False
    while current is not None and id(current) not in seen and len(seen) < 16:
        seen.add(id(current))
        if isinstance(current, (AdsProviderError, WebRequestError, ProviderWarning)):
            transport_truncated = transport_truncated or current.body_truncated
            context.setdefault("http_status", current.status)
            if current.operation:
                context.setdefault("operation", current.operation)
            candidate = current.response_body.encode("utf-8") if isinstance(current, ProviderWarning) else current.body
            if not body and candidate:
                body = candidate
        current = current.__cause__ or current.__context__
    if transport_truncated:
        context["provider_response_truncated"] = True
    if body:
        if tool_id == "google_ads":
            context.update(_google_error_context(body))
        # Exclude successful data and echoed request parameters when the
        # provider includes them alongside an error in its JSON envelope.
        body = ads_error_body(body)
        if body:
            remaining = body.decode("utf-8", "replace")
            for key, value in context.items():
                if isinstance(value, str):
                    context[key] = value.encode("utf-8")[:512].decode("utf-8", "ignore")
            context["provider_response_truncated"] = True
            # Each diagnostic context scalar is capped at 512 UTF-8 bytes.
            for index in range(4):
                chunk = remaining.encode("utf-8")[:512].decode("utf-8", "ignore")
                key = "provider_response" + (f"_{index + 1}" if index else "")
                # JSON escaping can triple the size of non-ASCII text. Fit
                # the collector's aggregate context limit before emission.
                while chunk and len(json.dumps({**context, key: chunk}).encode("utf-8")) > 4096:
                    chunk = chunk[:-1]
                if chunk:
                    context[key] = chunk
                    remaining = remaining[len(chunk):]
                else:
                    break
            context["provider_response_truncated"] = transport_truncated or bool(remaining) or json.loads(body).get("diagnostic_truncated") is True
    if transport_truncated and "provider_response" not in context:
        context["provider_response_unavailable"] = "transport body limit"
    return context
