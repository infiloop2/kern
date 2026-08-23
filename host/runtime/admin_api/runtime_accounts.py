"""Provider login flows, connected credentials, and account responses."""

from __future__ import annotations

from http import HTTPStatus
import re
import threading
import time
from typing import Any, Callable, NamedTuple

from host.network_integrations.bedrock.manifest import SUPPORTED_REGIONS as BEDROCK_REGIONS
from host.runtime.agent_runtime import (
    bedrock_credentials,
    claude_code,
    codex_app_server,
    grok_agent,
    orchestrator,
)
from host.runtime.admin_api.agent_files import helper_error_message as _helper_error_message
from host.runtime.admin_api.errors import ApiError
from host.runtime.admin_api.threads import _account_response_metadata
from host.runtime.core import state
from host.runtime.core.root_helpers import HelperTimedOut, run_root_helper as _run_root_helper
from host.runtime.core.state import read_claude_account, read_openai_account, read_xai_account

OAUTH_LOGIN_LOCK_TIMEOUT_SECONDS = 5
OAUTH_LOGIN_STATUSES = ("awaiting_login", "error")
AGENT_AUTH_CLEAR_HELPER_TIMEOUT_SECONDS = 10
AGENT_AUTH_CLEAR_HELPER_COMMAND = [
    "/usr/bin/sudo", "-n", "/usr/local/lib/kern-host/clear-agent-auth"
]
OAUTH_LOGIN_LOCK = threading.Lock()


def _minutes_from_now(minutes: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + minutes * 60))


def _mint_codex_login() -> tuple[dict[str, str], dict[str, str]]:
    login = codex_app_server.start_device_login()
    response = {
        "status": "awaiting_login",
        "device_code": login.user_code,
        "login_url": login.verification_url,
        "expires_at": _minutes_from_now(10),
    }
    return response, response | {"login_id": login.login_id}


def _mint_claude_login() -> tuple[dict[str, str], dict[str, str]]:
    login = claude_code.start_oauth_login()
    response = {
        "status": "awaiting_code",
        "login_url": login.login_url,
        "expires_at": _minutes_from_now(10),
    }
    return response, response


def _mint_grok_login() -> tuple[dict[str, str], dict[str, str]]:
    """Start the Grok device login.

    This is the Codex shape, not the Claude one: xAI shows the operator a code
    in their browser and polls for approval itself, so there is no completion
    endpoint to call back into. The status poller observes the resolved
    ``authenticate`` on the parked server and captures the anchor from there.
    The code is embedded in the verification URL's query string rather than
    returned as a field, and the adapter lifts it out.
    """
    try:
        login = grok_agent.start_device_login()
    except grok_agent.GrokLoginAlreadyAuthenticated:
        # A browser flow may have written the credential just before its
        # parked completion server was lost. Grok then has no anchor to trust,
        # but refuses to issue another URL while that credential remains.
        # Clear it through the same root helper as an operator reset and retry
        # once, so every accepted anchor still comes from a fresh browser flow.
        account = state.read_xai_account()
        if account.get("operator_approval") == orchestrator.XAI_OPERATOR_APPROVAL:
            raise ApiError(
                HTTPStatus.CONFLICT,
                "Disconnect the linked Grok account before signing in again",
            )
        _clear_local_agent_auth("grok")
        login = grok_agent.start_device_login()
    response = {
        "status": "awaiting_login",
        "device_code": login.user_code or "",
        "login_url": login.login_url,
        "expires_at": _minutes_from_now(10),
    }
    # login_id is persisted but never returned: it is what scopes the anchor
    # capture and the parked-server close to this exact login.
    return response, response | {"login_id": login.login_id}


class _OAuthLoginFlow(NamedTuple):
    """One runtime's login flow: the codex and claude endpoints are the same
    machine, differing only in these fields. mint returns (public response,
    persisted record); close tears down a login whose gate re-check lost."""

    runtime_type: str
    # oauth_logins keys on the provider spelling ('claude'), not the runtime
    # type ('claude_code'); orchestrator.mark_oauth_login_completed keys the
    # same way.
    oauth_key: str
    display: str
    provider: str
    response_keys: tuple[str, ...]
    mint: Callable[[], tuple[dict[str, str], dict[str, str]]]
    close: Callable[[], None]
    # Whether a persisted row still has the process that can complete it, for
    # flows where one is required. None means the row stands on its own.
    parked: Callable[[], bool] | None = None


_OAUTH_LOGIN_FLOWS = {
    "codex": _OAuthLoginFlow(
        runtime_type="codex",
        oauth_key="codex",
        display="Codex",
        provider="OpenAI",
        response_keys=("status", "device_code", "login_url", "expires_at"),
        mint=_mint_codex_login,
        close=lambda: codex_app_server.close_login_server(),
    ),
    "claude_code": _OAuthLoginFlow(
        runtime_type="claude_code",
        oauth_key="claude",
        display="Claude",
        provider="Claude",
        response_keys=("status", "login_url", "expires_at"),
        mint=_mint_claude_login,
        close=lambda: claude_code.close_login_process(),
    ),
    "grok": _OAuthLoginFlow(
        runtime_type="grok",
        oauth_key="grok",
        display="Grok",
        provider="xAI",
        response_keys=("status", "device_code", "login_url", "expires_at"),
        mint=_mint_grok_login,
        close=lambda: grok_agent.close_login_server(),
        parked=lambda: grok_agent.login_server_parked(),
    ),
}


def _require_oauth_login_available(flow: _OAuthLoginFlow) -> None:
    if not orchestrator.runtime_network_enabled(flow.runtime_type):
        raise ApiError(
            HTTPStatus.CONFLICT,
            f"{flow.display} OAuth login is unavailable while {flow.provider} provider access is disabled",
        )
    if orchestrator.runtime_status(flow.runtime_type) not in OAUTH_LOGIN_STATUSES:
        raise ApiError(
            HTTPStatus.CONFLICT,
            f"{flow.display} OAuth login is only available while awaiting_login or in error",
        )


def _start_oauth_login(flow: _OAuthLoginFlow) -> dict[str, str]:
    if not OAUTH_LOGIN_LOCK.acquire(timeout=OAUTH_LOGIN_LOCK_TIMEOUT_SECONDS):
        raise ApiError(HTTPStatus.CONFLICT, f"{flow.display} OAuth login is already starting")
    try:
        _require_oauth_login_available(flow)
        oauth = state.oauth_login(flow.oauth_key)
        if oauth and flow.parked is not None and not flow.parked():
            # The row outlived the process that can complete it. Grok's device
            # flow is driven by the CLI holding the long-running authenticate
            # request, and an admin API restart stops that scope through
            # BindsTo, so returning the row would hand the operator a code
            # nobody is exchanging until it expires. Drop it and mint again.
            with state.mutation() as cur:
                state.set_oauth_login(cur, flow.oauth_key, None)
            oauth = None
        if oauth:
            return {key: oauth[key] for key in flow.response_keys}
        response, persisted = flow.mint()
        with state.mutation() as cur:
            # Re-check the gate inside the mutation: a policy disable or a
            # completed refresh that raced the slow mint must not park a
            # fresh login process, so the loser closes it here.
            try:
                _require_oauth_login_available(flow)
            except ApiError:
                flow.close()
                raise
            state.set_oauth_login(cur, flow.oauth_key, persisted)
        return response
    finally:
        OAUTH_LOGIN_LOCK.release()


def _current_oauth_login_response(flow: _OAuthLoginFlow) -> dict[str, str]:
    _require_oauth_login_available(flow)
    oauth = state.oauth_login(flow.oauth_key)
    # A row whose driving process is gone is not a login in progress. Report it
    # as absent rather than handing back a code nobody is exchanging; the row
    # itself is cleared by the next start, which is the mutating path.
    if oauth and flow.parked is not None and not flow.parked():
        oauth = None
    if not oauth:
        raise ApiError(HTTPStatus.NOT_FOUND, f"{flow.display} OAuth login has not been started")
    return {key: oauth[key] for key in flow.response_keys}


def start_codex_oauth_login() -> dict[str, str]:
    return _start_oauth_login(_OAUTH_LOGIN_FLOWS["codex"])


def current_codex_oauth_login() -> dict[str, str]:
    return _current_oauth_login_response(_OAUTH_LOGIN_FLOWS["codex"])


def start_claude_oauth_login() -> dict[str, str]:
    return _start_oauth_login(_OAUTH_LOGIN_FLOWS["claude_code"])


def current_claude_oauth_login() -> dict[str, str]:
    return _current_oauth_login_response(_OAUTH_LOGIN_FLOWS["claude_code"])


def start_grok_oauth_login() -> dict[str, str]:
    return _start_oauth_login(_OAUTH_LOGIN_FLOWS["grok"])


def current_grok_oauth_login() -> dict[str, str]:
    return _current_oauth_login_response(_OAUTH_LOGIN_FLOWS["grok"])


def complete_claude_oauth_login(body: Any) -> dict[str, str]:
    if not isinstance(body, dict) or not isinstance(body.get("code"), str) or not body["code"].strip():
        raise ApiError(HTTPStatus.BAD_REQUEST, "code must be a non-empty string")
    if not orchestrator.runtime_network_enabled("claude_code"):
        raise ApiError(HTTPStatus.CONFLICT, "Claude OAuth login is unavailable while Claude provider access is disabled")
    try:
        claude_code.complete_oauth_login(body["code"])
    except claude_code.ClaudeCodeError as exc:
        raise ApiError(HTTPStatus.CONFLICT, str(exc)) from exc
    orchestrator.mark_oauth_login_completed("claude", _claude_completed_token_hash())
    status = orchestrator.refresh_runtime_status("claude_code")
    if status != "active":
        # The pending login record must survive until the refresh above: it is
        # the operator-approval window that lets the refresh capture the first
        # trusted account. On an active result the refresh clears it itself.
        with state.mutation() as cur:
            state.set_oauth_login(cur, "claude", None)
    return {"status": "accepted"}


def _claude_completed_token_hash() -> str | None:
    """Bind the operator approval to the token the login just wrote: first
    capture requires attesting this exact token, so agent credentials swapped
    after completion do not inherit the approval. If the read fails, the
    completion refresh cannot capture a first trusted account and the
    non-active completion path clears the spent login so the operator can
    retry."""
    try:
        account = claude_code.read_claude_account()
    except claude_code.ClaudeCodeError:
        return None
    value = account.get("access_token_sha256") if account else None
    return value if isinstance(value, str) and value else None


# Long-term IAM user access key ids only (AKIA prefix, 20 characters).
# Temporary session credentials (ASIA...) need an X-Amz-Security-Token the
# proxy deliberately denies, so rejecting them here with a clear message
# beats the generic STS failure they would otherwise hit.
BEDROCK_ACCESS_KEY_ID_RE = re.compile(r"^AKIA[0-9A-Z]{16}$")


def connect_bedrock_credentials(body: Any) -> dict[str, str]:
    """Store the operator-pasted AWS key pair and region as one connection.

    Only this operator API
    writes that row, so the stored credential is the approval. The request
    synchronously attests the key even while Bedrock is disabled; a failed
    candidate is never stored and leaves any previous validated connection
    unchanged."""
    if not isinstance(body, dict):
        raise ApiError(HTTPStatus.BAD_REQUEST, "request body must be an object")
    unexpected = sorted(set(body) - {"access_key_id", "secret_access_key", "region"})
    if unexpected:
        raise ApiError(HTTPStatus.BAD_REQUEST, "unexpected request fields: " + ", ".join(unexpected))
    access_key_id = body.get("access_key_id")
    secret_access_key = body.get("secret_access_key")
    region = body.get("region")
    if not isinstance(access_key_id, str) or not access_key_id.strip():
        raise ApiError(HTTPStatus.BAD_REQUEST, "access_key_id must be a non-empty string")
    if not BEDROCK_ACCESS_KEY_ID_RE.fullmatch(access_key_id.strip()):
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            "access_key_id must be a long-term IAM access key id (20 characters, AKIA prefix); "
            "temporary session credentials (ASIA...) are not supported — create a long-term "
            "access key for a dedicated IAM user instead",
        )
    if not isinstance(secret_access_key, str) or not secret_access_key.strip():
        raise ApiError(HTTPStatus.BAD_REQUEST, "secret_access_key must be a non-empty string")
    if region not in BEDROCK_REGIONS:
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            "region must be one of " + ", ".join(BEDROCK_REGIONS),
        )
    try:
        status, error_message = orchestrator.replace_and_validate_bedrock_credentials(
            access_key_id.strip(),
            secret_access_key.strip(),
            region,
        )
    except bedrock_credentials.BedrockCredentialsError as exc:
        raise ApiError(HTTPStatus.BAD_REQUEST, str(exc)) from exc
    if status != "active":
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            error_message or "AWS credential validation failed",
        )
    if not orchestrator.runtime_network_enabled("hermes"):
        return {"status": "accepted"}
    # Runtime refresh reads the validated row without another AWS call. The
    # proxy reads that same row directly.
    orchestrator.refresh_runtime_status("hermes")
    return {"status": "accepted"}


def current_bedrock_credentials() -> dict[str, Any]:
    """Return non-secret metadata for the validated Bedrock credential."""
    access_key_id = state.read_bedrock_access_key_id()
    response: dict[str, Any] = {"connected": access_key_id is not None}
    if access_key_id is not None:
        response["access_key_id"] = access_key_id
        region = state.read_bedrock_region()
        if region is not None:
            response["region"] = region
    return response


def disconnect_bedrock_credentials() -> dict[str, str]:
    """Delete the AWS connection and stop Hermes."""
    orchestrator.disconnect_bedrock_connection()
    return {"status": "accepted"}


def reset_linked_account(body: Any) -> dict[str, str]:
    """Delete the linked-account guard: the operator-approved anchor, its
    proxy pin, pending OAuth approval, local agent auth files, and old runtime
    processes. Callable in any runtime status."""
    if not isinstance(body, dict) or body.get("agent_runtime") not in OAUTH_RUNTIME_TYPES:
        raise ApiError(HTTPStatus.BAD_REQUEST, "agent_runtime must be one of " + ", ".join(OAUTH_RUNTIME_TYPES))
    runtime_type = body["agent_runtime"]
    orchestrator.reset_linked_account(runtime_type)
    try:
        _clear_local_agent_auth(runtime_type)
    except ApiError:
        orchestrator.refresh_runtime_status(runtime_type)
        raise
    orchestrator.refresh_runtime_status(runtime_type)
    return {"status": "accepted"}


# The clear-agent-auth helper is named for the provider whose files it
# removes, not for the runtime that uses them.
_AGENT_AUTH_HELPER_RUNTIMES = {"codex": "codex", "claude_code": "claude", "grok": "grok"}


def _clear_local_agent_auth(runtime_type: str) -> None:
    helper_runtime = _AGENT_AUTH_HELPER_RUNTIMES[runtime_type]
    try:
        proc = _run_root_helper(
            [*AGENT_AUTH_CLEAR_HELPER_COMMAND, helper_runtime], AGENT_AUTH_CLEAR_HELPER_TIMEOUT_SECONDS
        )
    except HelperTimedOut as exc:
        message = (
            f"{runtime_type} reset helper could not be terminated; retry reset"
            if exc.could_not_terminate
            else f"{runtime_type} reset timed out clearing local auth files; retry reset"
        )
        raise ApiError(HTTPStatus.CONFLICT, message) from exc
    if proc.returncode != 0:
        detail = _helper_error_message(proc.stdout, proc.stderr)
        message = f"{runtime_type} reset failed clearing local auth files; retry reset"
        if detail:
            message = f"{message}: {detail}"
        raise ApiError(HTTPStatus.CONFLICT, message)


AGENT_RUNTIME_TYPES = ("codex", "claude_code", "grok", "hermes")
OAUTH_RUNTIME_TYPES = ("codex", "claude_code", "grok")


def current_agent_accounts() -> dict[str, Any]:
    statuses = orchestrator.all_runtime_status_records()
    return {
        "accounts": [
            _current_agent_account(statuses, "codex"),
            _current_agent_account(statuses, "claude_code"),
            _current_agent_account(statuses, "grok"),
            _current_bedrock_account(statuses),
        ]
    }


def refresh_agent_runtime_accounts(body: Any) -> dict[str, Any]:
    runtime_types: tuple[str, ...]
    if body is None:
        runtime_types = AGENT_RUNTIME_TYPES
    elif isinstance(body, dict):
        runtime = body.get("agent_runtime")
        if runtime is None:
            runtime_types = AGENT_RUNTIME_TYPES
        elif isinstance(runtime, str) and runtime in AGENT_RUNTIME_TYPES:
            runtime_types = (runtime,)
        else:
            raise ApiError(HTTPStatus.BAD_REQUEST, "agent_runtime must be one of " + ", ".join(AGENT_RUNTIME_TYPES))
    else:
        raise ApiError(HTTPStatus.BAD_REQUEST, "request body must be an object")
    for runtime_type in runtime_types:
        force_probe = (
            runtime_type != "hermes"
            or orchestrator.runtime_network_enabled(runtime_type)
        )
        orchestrator.refresh_runtime_status(runtime_type, force_provider_probe=force_probe)
    return current_agent_accounts()


def _current_agent_account(statuses: dict[str, dict[str, Any]], runtime_type: str) -> dict[str, Any]:
    status = str(statuses.get(runtime_type, {}).get("status", "loading"))
    if runtime_type == "claude_code":
        response = {"agent_runtime": "claude_code", "provider": "claude", "status": status}
        account = read_claude_account()
        if account.get("identity_attestation") != orchestrator.CLAUDE_IDENTITY_ATTESTATION:
            account = {}
    elif runtime_type == "grok":
        response = {"agent_runtime": "grok", "provider": "xai", "status": status}
        account = read_xai_account()
        if account.get("operator_approval") != orchestrator.XAI_OPERATOR_APPROVAL:
            account = {}
    else:
        response = {"agent_runtime": "codex", "provider": "openai", "status": status}
        account = read_openai_account()
        if account.get("operator_approval") != orchestrator.OPENAI_OPERATOR_APPROVAL:
            account = {}
    return _account_response_tail(response, account, status, runtime_type)


def _current_bedrock_account(statuses: dict[str, dict[str, Any]]) -> dict[str, Any]:
    status = str(statuses.get("hermes", {}).get("status", "loading"))
    response: dict[str, Any] = {
        "provider": "bedrock",
        "agent_runtimes": ["hermes"],
        "status": status,
        # Live usage survives credential state on purpose: the counters record
        # month-to-date work already done, and reporting them costs one local
        # aggregate read.
        "bedrock_usage": _bedrock_live_usage(),
    }
    # Credential and display metadata are stored or cleared atomically, so the
    # account is meaningful only while the validated credential remains.
    account = state.read_bedrock_account() if state.read_bedrock_access_key_id() else {}
    return _account_response_tail(response, account, status, "bedrock")


def _account_response_tail(
    response: dict[str, Any],
    account: dict[str, Any],
    status: str,
    runtime_type: str,
) -> dict[str, Any]:
    if status == "active":
        response.update(_account_response_metadata(account, runtime_type))
        return response
    # The account anchor outlives sessions and deactivation; expose its
    # identity (never plan/usage) so the UI can show which account is linked
    # while the runtime is logged out or in error.
    for key in ("account_id", "email", "arn"):
        value = account.get(key)
        if isinstance(value, str) and value:
            response[key] = value
    return response


_BEDROCK_USAGE_TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
)


def _bedrock_live_usage() -> dict[str, Any]:
    """Month-to-date Bedrock usage.

    The proxy counts the token usage AWS reports in each allowed response and
    the USD it priced that response at, per model and UTC day. This
    sums the current month straight from those stored counters — the cost is
    the recorded figure, not re-priced at read time. It remains an estimate of
    what AWS will bill, not the bill itself: unmetered requests (``requests``
    minus ``metered_requests``) are surfaced instead of silently rounding the
    estimate down."""
    month_start = time.strftime("%Y-%m-01", time.gmtime())
    usage: dict[str, Any] = {
        "month_to_date": 0.0,
        "currency": "USD",
        "requests": 0,
        "metered_requests": 0,
        **{field: 0 for field in _BEDROCK_USAGE_TOKEN_FIELDS},
    }
    for row in state.read_bedrock_usage(month_start):
        usage["requests"] += row["requests"]
        usage["metered_requests"] += row["metered_requests"]
        usage["month_to_date"] += row["cost_usd"]
        for field in _BEDROCK_USAGE_TOKEN_FIELDS:
            usage[field] += row[field]
    usage["month_to_date"] = round(usage["month_to_date"], 4)
    return usage
