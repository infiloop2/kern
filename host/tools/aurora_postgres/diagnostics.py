"""Aurora failure metadata for Host diagnostics, without database contents.

Only fixed labels, known AWS error codes and validated request IDs are logged.
Never pass provider exceptions to the reporter: their messages, bodies and
causes may include SQL, parameter values, credentials or database rows.
"""
from __future__ import annotations

import json
import socket
import ssl
import urllib.error
from typing import NoReturn

from host.runtime.core import host_errors
from host.tools.shared.web import RESPONSE_TOO_LARGE_MESSAGE, WebRequestError
from host.tools.shared.inputs import ToolInputValidationError


class ResponseValidationError(ToolInputValidationError):
    """A malformed provider result, distinct from rejected action input."""


def invalid_response(message: str) -> NoReturn:
    raise ResponseValidationError(message)


AWS_CODES = frozenset({
    "AccessDeniedException", "BadRequestException", "DatabaseErrorException",
    "DatabaseNotFoundException", "DatabaseResumingException", "DatabaseUnavailableException",
    "ForbiddenException", "HttpEndpointNotEnabledException", "InternalServerErrorException",
    "InvalidSecretException", "SecretsErrorException", "ServiceUnavailableError",
    "StatementTimeoutException", "TransactionNotFoundException", "UnsupportedResultException",
    "ValidationException", "ThrottlingException", "UnrecognizedClientException",
    "InvalidSignatureException", "IncompleteSignatureException", "SignatureDoesNotMatch",
    "ExpiredTokenException", "InvalidClientTokenId", "MissingAuthenticationTokenException",
    "RequestExpired", "RequestTimeTooSkewed",
})


def aws_error_code(exc: WebRequestError) -> str:
    try:
        body = json.loads(exc.body)
    except (ValueError, UnicodeError, RecursionError):
        return "unavailable"
    if not isinstance(body, dict):
        return "unavailable"
    code = body.get("__type", body.get("code", ""))
    if not isinstance(code, str) or not code:
        return "unavailable"
    code = code.rsplit("#", 1)[-1]
    return code if code in AWS_CODES else "unrecognized"


def failure_type(exc: WebRequestError) -> str:
    if exc.status:
        return "http_error"
    if str(exc) == RESPONSE_TOO_LARGE_MESSAGE:
        return "response_too_large"
    cause: object = exc.__cause__
    if isinstance(cause, urllib.error.URLError):
        cause = cause.reason
    if isinstance(cause, ssl.SSLError):
        return "tls_error"
    if isinstance(cause, TimeoutError):
        return "timeout"
    if isinstance(cause, socket.gaierror):
        return "dns_error"
    if isinstance(cause, ConnectionError):
        return "connection_error"
    return "transport_error"


def report_failure(*, operation: str, role: str, phase: str, correlation_id: str,
                   failure: str, elapsed_ms: int | None = None, exc: WebRequestError | None = None) -> None:
    context: dict[str, str | int | bool] = {
        "operation": operation if operation in {"begin", "execute", "commit", "rollback"} else "unknown",
        "role": role if role in {"reader", "writer", "ddl", "dropper"} else "unknown",
        "phase": phase if phase in {"begin", "transaction_setup", "role_check", "statement", "commit", "rollback"} else "unknown",
        "failure_type": failure,
        "correlation_id": correlation_id,
        # Zero means no status was recorded, not that AWS returned HTTP 0.
        "http_status": exc.status if exc else 0,
    }
    if elapsed_ms is not None:
        context["elapsed_ms"] = elapsed_ms
    if exc is not None:
        context.update(aws_error_code=aws_error_code(exc), aws_request_id=exc.request_id,
                       error_body_truncated=exc.body_truncated)
    host_errors.report_warning("tools.aurora_postgres", "Aurora Data API operation failed.",
                               context=context, kind="provider_failure")
