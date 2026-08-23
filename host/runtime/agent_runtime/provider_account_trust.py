"""Provider-account attestation, OAuth capture, and credential trust.

This module owns slow provider validation and the durable account anchors it
publishes. Turn admission and live process lifecycle remain in orchestrator.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, NamedTuple

from host.runtime.agent_runtime import (
    bedrock_credentials,
    claude_code,
    codex_app_server,
    grok_agent,
)
from host.runtime.agent_runtime.harness_registry import HARNESSES
from host.runtime.core import host_errors, state
from host.runtime.core.state import (
    read_claude_account,
    read_openai_account,
    read_xai_account,
    save_bedrock_account,
    save_claude_account,
    save_openai_account,
    save_xai_account,
    utc_now,
)

CLAUDE_LIVE_PROBE_RETRY_SECONDS = 240
CLAUDE_IDENTITY_ATTESTATION = "anthropic_oauth_profile"
OPENAI_OPERATOR_APPROVAL = "codex_device_login"
XAI_OPERATOR_APPROVAL = "grok_device_login"
# oauth_logins rows key on the provider spelling, not the runtime type.
_OAUTH_KEY_BY_RUNTIME = {
    runtime_type: adapter.oauth_key
    for runtime_type, adapter in HARNESSES.items()
    if adapter.oauth_key is not None
}
class _TokenAnchoredProvider(NamedTuple):
    """One runtime with a direct account-id anchor and proxy pin.

    Codex and Grok share the same stored shape and direct per-request proxy
    comparison. Grok additionally attests its current token before returning
    active metadata; Claude's opaque-token attestation owns its whole anchor
    lifecycle, so it keeps a separate orchestration path.
    """

    approval: str
    read_account: Callable[..., dict[str, Any]]
    save_account: Callable[..., None]
    save_proxy_account_id: Callable[..., None]
    usage_key: str
    # The account id captured when the operator's login completed, or None
    # while it has not. Raises ``error`` when the completion carried no usable
    # id, which fails the capture closed.
    read_completed_login_account_id: Callable[[str], str | None]
    clear_live_validation: Callable[[], None]
    close_completed_login_server: Callable[[str], None]
    error: type[Exception]


# The adapter entries below are deliberately written as lambdas rather than as
# direct function references: a reference captured here would bind the adapter
# function once, at import, so anything that later replaces the module
# attribute — including the tests that stand in for a provider CLI — would be
# silently ignored. The lambdas resolve through the module on every call.
_TOKEN_ANCHORED: dict[str, _TokenAnchoredProvider] = {
    "codex": _TokenAnchoredProvider(
        approval=OPENAI_OPERATOR_APPROVAL,
        read_account=lambda cur=None: read_openai_account(cur),
        save_account=lambda account, cur=None: save_openai_account(account, cur),
        save_proxy_account_id=lambda account_id, cur=None: state.save_proxy_openai_account_id(
            account_id, cur
        ),
        usage_key="codex_usage",
        read_completed_login_account_id=(
            lambda login_id: codex_app_server.read_completed_device_login_account_id(login_id)
        ),
        clear_live_validation=lambda: codex_app_server.clear_live_validation_failure(),
        close_completed_login_server=(
            lambda login_id: codex_app_server.close_completed_login_server(login_id)
        ),
        error=codex_app_server.CodexAppServerError,
    ),
    "grok": _TokenAnchoredProvider(
        approval=XAI_OPERATOR_APPROVAL,
        read_account=lambda cur=None: read_xai_account(cur),
        save_account=lambda account, cur=None: save_xai_account(account, cur),
        save_proxy_account_id=lambda account_id, cur=None: state.save_proxy_xai_account_id(
            account_id, cur
        ),
        usage_key="grok_usage",
        read_completed_login_account_id=(
            lambda login_id: grok_agent.read_completed_login_account_id(login_id)
        ),
        clear_live_validation=lambda: grok_agent.clear_live_validation_failure(),
        close_completed_login_server=(
            lambda login_id: grok_agent.close_completed_login_server(login_id)
        ),
        error=grok_agent.GrokAgentError,
    ),
}
# Credential validation is slow and happens before the database mutation. Keep
# connect and disconnect as ordered product actions so an older request cannot
# publish after a newer reset or replacement has completed.
_BEDROCK_CONNECTION_LOCK = threading.Lock()
# The last live Claude probe verdict, keyed by the probed token hash:
# {"token_hash", "status", "error_message", "usage", "at"}. An awaiting_login
# verdict is final for that token (recovery is an operator login, which mints a
# new token); active and error verdicts expire after
# CLAUDE_LIVE_PROBE_RETRY_SECONDS. Written only under the claude refresh lock;
# in memory on purpose so a restart revalidates once from scratch.
_CLAUDE_LIVE_PROBE: dict[str, Any] | None = None
# The last Claude token attestation, keyed by token hash: a token's identity
# never changes, so one successful fetch answers every recheck of that token
# (a runtime parked in account-mismatch error rechecks every five seconds).
# A failed fetch is retried after CLAUDE_LIVE_PROBE_RETRY_SECONDS.
_CLAUDE_ATTESTATION_MEMO: tuple[str, dict[str, Any] | None, str | None, float] | None = None
class ProviderAccountTrustError(RuntimeError):
    pass


class ProviderAccountNotApproved(ProviderAccountTrustError):
    """Active agent-side credentials with no operator-approved anchor.

    Not an error state: the runtime simply awaits an operator login. The proxy
    pin stays cleared, so the unapproved credentials cannot reach the provider
    in the meantime."""


def reset_live_validation(runtime_type: str) -> None:
    """Forget process-local validation after an operator account reset."""
    global _CLAUDE_LIVE_PROBE, _CLAUDE_ATTESTATION_MEMO
    if runtime_type == "claude_code":
        _CLAUDE_LIVE_PROBE = None
        _CLAUDE_ATTESTATION_MEMO = None
        return
    _TOKEN_ANCHORED[runtime_type].clear_live_validation()


def _capture_completed_token_login(runtime_type: str) -> None:
    """Record completion and persist the first trusted token-account anchor.

    Runs right after the status poll, which is the sole reader of the parked
    login server and has therefore recorded the provider's completion. A stored
    OAuth row means the operator saw a device code or login URL, not that the
    login completed, so capture still requires that completion for the exact
    login id; the account id itself is read from the provider-signed login
    tokens promptly after completion. The surrounding refresh publishes the
    proxy pin when it commits, right after this capture.
    """
    anchored = _TOKEN_ANCHORED.get(runtime_type)
    if anchored is None:
        return
    oauth_key = _OAUTH_KEY_BY_RUNTIME[runtime_type]
    stored_login = state.oauth_login(oauth_key)
    stored_login_id = _string_field(stored_login, "login_id") if stored_login else None
    stored_expiry = stored_login.get("expires_at") if stored_login else None
    was_expired = isinstance(stored_expiry, str) and stored_expiry <= utc_now()
    login = _current_oauth_login(oauth_key)
    login_id = _string_field(login, "login_id") if login else None
    if not login_id:
        # Scope the close to the exact expired approval. A new device flow may
        # start immediately after the old row is deleted; never let expiry
        # cleanup reap that replacement.
        if was_expired and stored_login_id:
            anchored.close_completed_login_server(stored_login_id)
        return
    account_id: str | None = None
    if runtime_type == "grok":
        try:
            account_id = anchored.read_completed_login_account_id(login_id)
        except anchored.error:
            with state.mutation() as cur:
                current = _current_oauth_login(oauth_key, cur)
                if current is not None and _string_field(current, "login_id") == login_id:
                    state.set_oauth_login(cur, oauth_key, None)
            anchored.close_completed_login_server(login_id)
            return
    try:
        if account_id is None:
            account_id = anchored.read_completed_login_account_id(login_id)
    except anchored.error:
        # A helper hiccup must not fail the refresh; the poller already
        # classified the runtime state and the next refresh retries the capture.
        return
    if account_id:
        _capture_completed_token_oauth_login(runtime_type, login_id, account_id)


def _capture_completed_token_oauth_login(runtime_type: str, login_id: str, account_id: str) -> bool:
    """Mark the exact login complete and persist its first account anchor."""
    anchored = _TOKEN_ANCHORED[runtime_type]
    oauth_key = _OAUTH_KEY_BY_RUNTIME[runtime_type]
    with state.mutation() as cur:
        login = _current_oauth_login(oauth_key, cur)
        current_login_id = _string_field(login, "login_id") if login else None
        if login is None or current_login_id != login_id:
            return False
        state.set_oauth_login(cur, oauth_key, login | {"status": "completed"})
        trusted_account_id = _trusted_token_account_id(anchored, anchored.read_account(cur))
        if trusted_account_id:
            return trusted_account_id == account_id
        anchored.save_account(
            _with_operator_approval(anchored, {"account_id": account_id}), cur
        )
        return True


def _active_account_value(runtime_type: str, status: str, account: Any) -> dict[str, Any] | None:
    if status != "active":
        return None
    if isinstance(account, dict):
        return account
    if runtime_type == "codex" and isinstance(account, str) and account:
        return {"account_id": account}
    return None


def _live_claude_status(
    account: dict[str, Any],
    *,
    force_probe: bool = False,
) -> tuple[str, str | None, dict[str, Any] | None]:
    """Validate a steady Claude credential through the CLI that owns refresh.

    First capture and an already-observed token rotation skip this probe because
    the root profile attestation below is their live validation. For a steady
    token, `/usage` authenticates through the agent proxy and gives Claude Code
    a chance to refresh. Detect a resulting hash change after either success or
    failure and pass the candidate onward for provider attestation before
    updating the admin-side rotation metadata. The proxy independently
    authorizes every old or rotated bearer against the approved account UUID.

    Each probe's verdict is memoized per token hash (_CLAUDE_LIVE_PROBE), so
    the probe itself runs at most once per CLAUDE_LIVE_PROBE_RETRY_SECONDS:
    turn-start refreshes and the five-second non-active poll reuse the verdict
    instead of generating provider traffic. An explicit operator refresh
    bypasses the memo. An awaiting_login verdict never expires on automatic
    checks; the token is rejected, and recovery is an operator login, account
    reset, or an operator-forced recheck that succeeds.
    """
    global _CLAUDE_LIVE_PROBE
    token_hash = _string_field(account, "access_token_sha256")
    stored = read_claude_account()
    if (
        not token_hash
        or not _trusted_claude_account_id(stored)
        or _string_field(stored, "access_token_sha256") != token_hash
    ):
        return "active", None, account
    memo = _CLAUDE_LIVE_PROBE
    if not force_probe and memo is not None and memo["token_hash"] == token_hash:
        if memo["status"] == "awaiting_login":
            return "awaiting_login", None, None
        if time.monotonic() - memo["at"] < CLAUDE_LIVE_PROBE_RETRY_SECONDS:
            if memo["status"] == "error":
                return "error", memo["error_message"], None
            refreshed = dict(account)
            if memo["usage"]:
                refreshed["claude_usage"] = dict(memo["usage"])
            return "active", None, refreshed
    return _probe_claude_status(account, token_hash)


def _probe_claude_status(
    account: dict[str, Any], token_hash: str
) -> tuple[str, str | None, dict[str, Any] | None]:
    global _CLAUDE_LIVE_PROBE
    usage: dict[str, Any] = {}
    probe_error: claude_code.ClaudeCodeError | None = None
    try:
        usage = claude_code.read_claude_usage()
    except claude_code.ClaudeCodeError as exc:
        probe_error = exc
    try:
        current = claude_code.read_claude_account()
    except claude_code.ClaudeCodeError as exc:
        return _memo_claude_probe(
            token_hash, "error", f"could not read Claude account after live authentication: {exc}"
        )
    if not current:
        return _memo_claude_probe(
            token_hash, "error", "Claude OAuth token metadata disappeared during live authentication"
        )
    refreshed = dict(account)
    refreshed.update(current)
    usage = _checked_claude_usage(usage)
    current_hash = _string_field(refreshed, "access_token_sha256")
    if current_hash != token_hash:
        # The CLI rotated the token during the probe: no verdict is memoized
        # for either hash. This refresh attests and commits the new token, and
        # its first scheduled recheck probes it.
        if usage:
            refreshed["claude_usage"] = usage
        return "active", None, refreshed
    if isinstance(probe_error, claude_code.ClaudeAuthenticationError):
        return _memo_claude_probe(token_hash, "awaiting_login", None)
    if probe_error is not None:
        return _memo_claude_probe(
            token_hash, "error", f"could not validate Claude authentication: {probe_error}"
        )
    if usage:
        refreshed["claude_usage"] = usage
    _memo_claude_probe(token_hash, "active", None, usage)
    return "active", None, refreshed


def _memo_claude_probe(
    token_hash: str, status: str, error_message: str | None, usage: dict[str, Any] | None = None
) -> tuple[str, str | None, dict[str, Any] | None]:
    global _CLAUDE_LIVE_PROBE
    _CLAUDE_LIVE_PROBE = {
        "token_hash": token_hash,
        "status": status,
        "error_message": error_message,
        "usage": dict(usage) if usage else {},
        "at": time.monotonic(),
    }
    return status, error_message, None


def _backfill_claude_usage(account: dict[str, Any] | None) -> None:
    """One usage read for an active token the steady probe has not covered.

    A first-capture or just-rotated token is validated by attestation, not the
    usage probe. Run usage once after that identity/metadata commit so the admin
    UI updates immediately instead of waiting for the next five-minute recheck.
    Metadata only: failures are ignored and never touch the runtime status; the
    next scheduled probe classifies auth state."""
    token_hash = _string_field(account, "access_token_sha256") if account else None
    if not token_hash or not account or "claude_usage" in account:
        return
    memo = _CLAUDE_LIVE_PROBE
    if memo is not None and memo["token_hash"] == token_hash:
        return  # this round's steady probe already answered for this token
    try:
        usage = claude_code.read_claude_usage()
    except claude_code.ClaudeCodeError:
        return
    usage = _checked_claude_usage(usage)
    _memo_claude_probe(token_hash, "active", None, usage)
    if not usage:
        return
    with state.mutation() as cur:
        stored = read_claude_account(cur)
        if _string_field(stored, "access_token_sha256") != token_hash:
            return  # the credential moved on; let its own refresh fetch usage
        stored["claude_usage"] = dict(usage)
        save_claude_account(stored, cur)


def _checked_claude_usage(usage: dict[str, Any]) -> dict[str, Any]:
    if not usage:
        return {}
    checked = dict(usage)
    checked["last_checked_at"] = utc_now()
    return checked


def replace_and_validate_bedrock_credentials(
    access_key_id: str,
    secret_access_key: str,
    region: str,
) -> tuple[str, str | None]:
    """Validate and replace the connection as one ordered operator action."""
    with _BEDROCK_CONNECTION_LOCK:
        return _replace_and_validate_bedrock_credentials(
            access_key_id,
            secret_access_key,
            region,
        )


def _replace_and_validate_bedrock_credentials(
    access_key_id: str,
    secret_access_key: str,
    region: str,
) -> tuple[str, str | None]:
    """Replace and synchronously validate the one Bedrock credential.

    The credential candidate exists only in the admin/root-helper process
    environments until the STS identity read succeeds. Both the credential and
    its region and identity metadata are then stored in one transaction. A
    rejected replacement leaves the previous validated connection unchanged.
    """
    credential = (access_key_id, secret_access_key)
    try:
        identity = bedrock_credentials.read_attested_identity(credential=credential)
    except bedrock_credentials.BedrockAuthenticationError as exc:
        return "error", f"AWS rejected the credential: {exc}"
    except bedrock_credentials.BedrockCredentialsError as exc:
        return "error", f"could not validate AWS credentials: {exc}"
    if _string_field(identity, "access_key_id") != access_key_id:
        return "error", "AWS validation returned a different access key id"
    account: dict[str, Any] = {"access_key_id": access_key_id}
    for key in ("account_id", "arn", "user_id"):
        field = _string_field(identity, key)
        if field:
            account[key] = field
    with state.mutation() as cur:
        state.save_bedrock_credential(access_key_id, secret_access_key, region, cur)
        save_bedrock_account(account, cur)
    return "active", None


def _claude_attestation(account: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """Attest the probed Claude token when its hash is not already anchored.

    The profile call is a network round trip, so it runs out here only when the
    token still needs attestation: a token recorded on a server-attested anchor
    was attested when it was first seen, and steady-state refreshes stay local.
    A token's attested identity never changes, so the result is memoized per
    token hash; a runtime parked in account-mismatch error therefore rechecks
    against the memo instead of refetching the profile every five seconds.
    The attestation itself is a read-only profile fetch — the trust
    decision that consumes it runs later, under the commit mutation, against
    the then-current anchor. Returns (attested identity, None) or (None,
    error message)."""
    global _CLAUDE_ATTESTATION_MEMO
    token_hash = _string_field(account, "access_token_sha256")
    if not token_hash:
        return None, None  # the trust check rejects the account outright
    if not _claude_attestation_allowed(token_hash):
        return None, None
    memo = _CLAUDE_ATTESTATION_MEMO
    if memo is not None and memo[0] == token_hash:
        if memo[1] is not None:
            return memo[1], None
        if time.monotonic() - memo[3] < CLAUDE_LIVE_PROBE_RETRY_SECONDS:
            return None, memo[2]
    try:
        attested = claude_code.read_attested_identity(expected_token_sha256=token_hash)
    except claude_code.ClaudeCodeError as exc:
        _CLAUDE_ATTESTATION_MEMO = (token_hash, None, str(exc), time.monotonic())
        return None, str(exc)
    _CLAUDE_ATTESTATION_MEMO = (token_hash, attested, None, time.monotonic())
    return attested, None


def _claude_attestation_allowed(token_hash: str) -> bool:
    stored = read_claude_account()
    trusted_account_id = _trusted_claude_account_id(stored)
    if trusted_account_id:
        # A steady-state token still on the anchor needs no re-attestation; a
        # rotated token does.
        return _string_field(stored, "access_token_sha256") != token_hash
    return _claude_first_capture_approved(token_hash)


def _claude_first_capture_approved(token_hash: str, cur: Any = None) -> bool:
    """First capture is approved only for the token the operator's completed
    login produced. Completion records sha256(accessToken) read right after
    the login helper finished, so agent credentials swapped after that moment
    do not inherit the approval (the remaining swap window is the milliseconds
    between the CLI writing the file and the completion read; the linked
    account is also shown to the operator once pinned). A completion whose
    hash read failed carries no usable first-capture approval."""
    login = _completed_oauth_login("claude", cur)
    if login is None:
        return False
    approved_hash = _string_field(login, "access_token_sha256")
    return approved_hash == token_hash


def _trusted_active_account(
    cur: Any,
    runtime_type: str,
    account: dict[str, Any] | None,
    attested: dict[str, Any] | None = None,
    attest_error: str | None = None,
) -> dict[str, Any]:
    """Validate a probed active account against the operator-approved anchor.

    The anchor is the stored account id: first captured only while an operator
    OAuth login is in flight, immutable afterwards until an operator reset
    clears it. OpenAI's anchor is additionally enforced per request by the
    proxy header guard. Claude and Grok identities are server-attested for each
    new token, so agent-writable metadata is never trusted as the displayed or
    pinned identity.
    """
    if not account:
        raise ProviderAccountTrustError(f"{runtime_type} reported active without account metadata")
    if runtime_type == "claude_code":
        return _trusted_claude_account(cur, account, attested, attest_error)
    return _trusted_token_account(cur, runtime_type, account)


# The provider name shown to the operator when their linked account is the
# problem. It names the account they would go and fix, not the runtime.
_ACCOUNT_PROVIDER_LABELS = {"codex": "OpenAI", "grok": "xAI"}


def _trusted_token_account(cur: Any, runtime_type: str, account: dict[str, Any]) -> dict[str, Any]:
    anchored = _TOKEN_ANCHORED[runtime_type]
    label = _ACCOUNT_PROVIDER_LABELS[runtime_type]
    account_id = _string_field(account, "account_id")
    if not account_id:
        raise ProviderAccountTrustError(f"{label} account id is not available")
    trusted_account_id = _trusted_token_account_id(anchored, anchored.read_account(cur))
    if trusted_account_id:
        if account_id != trusted_account_id:
            raise ProviderAccountTrustError(
                f"{label} account changed; reset the linked account under Home > Integrations in the admin UI"
            )
        return _with_operator_approval(anchored, account)
    raise ProviderAccountNotApproved(
        f"{label} account is not operator-approved; start OAuth login from the admin UI"
    )


def _trusted_token_account_id(
    anchored: _TokenAnchoredProvider, account: dict[str, Any]
) -> str | None:
    if _string_field(account, "operator_approval") != anchored.approval:
        return None
    return _string_field(account, "account_id")


def _with_operator_approval(
    anchored: _TokenAnchoredProvider, account: dict[str, Any]
) -> dict[str, Any]:
    approved = dict(account)
    approved["operator_approval"] = anchored.approval
    return approved


def _trusted_claude_account(
    cur: Any, account: dict[str, Any], attested: dict[str, Any] | None, attest_error: str | None
) -> dict[str, Any]:
    token_hash = _string_field(account, "access_token_sha256")
    if not token_hash:
        raise ProviderAccountTrustError("Claude account token is not available")
    stored = read_claude_account(cur)
    # Only a server-attested row is a trusted anchor; anything else re-captures
    # through the first-capture gate below, exactly like a fresh box.
    trusted_account_id = _trusted_claude_account_id(stored)
    if (
        trusted_account_id
        and _string_field(stored, "access_token_sha256") == token_hash
    ):
        # This exact token was attested when it was first anchored; its
        # identity comes from that attestation, never from the agent's local
        # metadata (which the probe may carry, forged or stale).
        return _with_identity(account, trusted_account_id, stored)
    if not trusted_account_id and not _claude_first_capture_approved(token_hash, cur):
        raise ProviderAccountNotApproved("Claude account is not operator-approved; start OAuth login from the admin UI")
    if attested is None:
        raise ProviderAccountTrustError(
            attest_error or "Claude account is not attested yet; retrying on the next refresh"
        )
    if _string_field(attested, "access_token_sha256") != token_hash:
        raise ProviderAccountTrustError("Claude token changed while attesting; retrying on the next refresh")
    attested_uuid = _string_field(attested, "account_uuid")
    if not attested_uuid:
        raise ProviderAccountTrustError("Claude account attestation has no account uuid")
    if trusted_account_id and attested_uuid != trusted_account_id:
        raise ProviderAccountTrustError("Claude account changed; reset the linked account under Home > Integrations in the admin UI")
    return _with_identity(account, attested_uuid, attested)


def _with_identity(account: dict[str, Any], account_id: str, source: dict[str, Any]) -> dict[str, Any]:
    """The probed account with its identity fields replaced by trusted ones
    (the stored anchor or a fresh attestation; attestations use the
    ``organization_uuid`` key, stored anchors ``organization_id``)."""
    merged = dict(account)
    merged["account_id"] = account_id
    email = _string_field(source, "email")
    if email:
        merged["email"] = email
    organization_id = _string_field(source, "organization_id") or _string_field(source, "organization_uuid")
    if organization_id:
        merged["organization_id"] = organization_id
    if _string_field(source, "account_uuid"):
        merged["identity_attestation"] = CLAUDE_IDENTITY_ATTESTATION
        merged["identity_attested_at"] = utc_now()
    elif _claude_anchor_is_server_attested(source):
        merged["identity_attestation"] = CLAUDE_IDENTITY_ATTESTATION
        attested_at = _string_field(source, "identity_attested_at")
        if attested_at:
            merged["identity_attested_at"] = attested_at
    return merged


def _claude_anchor_is_server_attested(account: dict[str, Any]) -> bool:
    return _string_field(account, "identity_attestation") == CLAUDE_IDENTITY_ATTESTATION


def _trusted_claude_account_id(account: dict[str, Any]) -> str | None:
    """The Claude anchor id, or None when the row is not a trusted anchor.

    Mirrors _trusted_token_account_id: the operator-approval marker is the
    server attestation, so a row without it is not an anchor and re-captures
    through a fresh operator login."""
    if not _claude_anchor_is_server_attested(account):
        return None
    return _string_field(account, "account_id")


def _current_oauth_login(key: str, cur: Any = None) -> dict[str, Any] | None:
    login = state.oauth_login(key, cur)
    expires_at = login.get("expires_at") if login else None
    if not isinstance(expires_at, str) or expires_at <= utc_now():
        if login is not None:
            if cur is not None:
                state.set_oauth_login(cur, key, None)
            else:
                with state.mutation() as fresh:
                    state.set_oauth_login(fresh, key, None)
        return None
    return login


def _completed_oauth_login(key: str, cur: Any = None) -> dict[str, Any] | None:
    login = _current_oauth_login(key, cur)
    if login is None or login.get("status") != "completed":
        return None
    return login


def mark_oauth_login_completed(key: str, access_token_sha256: str | None = None) -> bool:
    with state.mutation() as cur:
        login = _current_oauth_login(key, cur)
        if login is None:
            return False
        completed = login | {"status": "completed"}
        if access_token_sha256:
            completed["access_token_sha256"] = access_token_sha256
        state.set_oauth_login(cur, key, completed)
        return True


def _sync_runtime_proxy_pin_in(cur: Any, runtime_type: str, account: dict[str, Any] | None) -> None:
    """Write the runtime's proxy pin inside the caller's mutation, so pin and
    anchor/status commit in one transaction."""
    account_id = _string_field(account, "account_id") if account else None
    if runtime_type == "claude_code":
        state.save_proxy_claude_account_id(account_id, cur)
        return
    _TOKEN_ANCHORED[runtime_type].save_proxy_account_id(account_id, cur)


def _string_field(value: dict[str, Any], key: str) -> str | None:
    field = value.get(key)
    return field if isinstance(field, str) and field else None


def _stamp_usage_checked_at(account: dict[str, Any] | None, usage_key: str, checked_at: str) -> None:
    if not account:
        return
    usage = account.get(usage_key)
    if isinstance(usage, dict):
        usage["last_checked_at"] = checked_at
