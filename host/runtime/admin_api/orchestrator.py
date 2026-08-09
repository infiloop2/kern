"""Agent runtime orchestration: admission and execution of live turns, plus
the background poller that keeps the cached runtime status fresh. The admin
API delegates here; route handlers translate the raised ApiErrors.

Concurrency model: there is no queue. A message for an idle thread starts a
turn immediately when its runtime has capacity (``TURN_LIMIT_PER_RUNTIME``
concurrent turns per runtime) and is rejected otherwise; a message for a
thread with a live turn is synchronously delivered into that turn as a steer
and recorded only after the provider transport acknowledges it. Every turn
runs on a fresh runtime process: Codex turns resume their provider thread by
id on a new app-server; Claude Code and Hermes turns resume by recorded
session id. Turns on the same user thread are serialized by the live-turn
fence, while turns on different threads run in parallel.

How the synchronization fits together:

- ``state.mutation()`` guards each durable check-and-write cycle.
  ``_LIVE_LOCK`` guards only the small in-memory registry of admitted
  executions. Each execution's ``delivery_lock`` orders steering, provider
  callbacks, stop, and durable lifecycle finalization for that one thread.
  Those paths take the delivery lock before a mutation; admission never takes
  a delivery lock, and no path takes ``_LIVE_LOCK`` while holding one. Starting,
  running, interrupting, and closing provider processes happen outside these
  locks.
  ``_REFRESH_LOCKS`` sits outside this set: it serializes
  ``refresh_runtime_status`` per runtime, is deliberately held across slow
  provider probes, and is never acquired while holding (or by) anything else.
- Provider account trust is anchored in the database, not in locks: the
  stored provider account row is the operator-approved anchor. It is written
  only inside the refresh commit mutation (first capture requires an
  unexpired operator OAuth login; afterwards the probed account id must
  match) and only an operator reset (``reset_linked_account``) clears it.
  The anchor check, the anchor save, the proxy pin write, and the reset clear
  all run inside mutations, so whichever of a refresh and a reset commits
  second sees the other's state — a stale probe result can never re-approve
  an account (or republish a pin for one) that a concurrent reset just
  cleared. Claude identity is additionally server-attested: whenever the
  probed token hash differs from the anchored one, the account uuid comes
  from api.anthropic.com for that token (via the root helper), so
  agent-writable metadata is never what gets anchored.
- A user thread is busy while it has a ``_LIVE`` entry. Its private execution
  phase is ``STARTING``, ``RUNNING``, ``FINISHING``, or ``CLOSED``. Admission
  publishes the entry after its database commit and the owning execution
  thread removes it only after process teardown succeeds. Messages during the
  short STARTING/FINISHING windows receive a retryable conflict rather than
  being queued.
- A steer is accepted only in RUNNING. Provider acknowledgement happens before
  the user-message commit, so a failed post-ack commit is deliberately
  ambiguous and a caller retry may duplicate the steer. A provider rejection
  after RUNNING is a terminal transport failure, not a startup retry.
- Stop and normal completion finalize the database first, move to FINISHING,
  and request a non-blocking interrupt when needed. The one execution thread
  owns bounded close/reap and moves to CLOSED. A cleanup failure retains the
  live fence so a new process can never race a surviving old scope.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from functools import partial
from http import HTTPStatus
import threading
import time
from typing import Any, Callable

from host.config import AGENT_RUNTIMES
from host.runtime.core import host_errors, network_policy, state
from host.runtime.admin_api import (
    agent_activity,
    bedrock_credentials,
    claude_code,
    codex_app_server,
    github_credential,
    github_repo_audit,
    hermes_agent,
)
from host.runtime.admin_api.errors import ApiError
from host.runtime.core.state import (
    read_claude_account,
    read_openai_account,
    save_bedrock_account,
    save_claude_account,
    save_openai_account,
    utc_now,
)

# Every runtime owns an independent three-turn pool, so one busy runtime
# cannot take capacity from its peers. A message that would exceed the cap is
# rejected at admission; callers retry.
TURN_LIMIT_PER_RUNTIME = 3
EXECUTION_START_TIMEOUT_SECONDS = 10.0
RUNTIME_RECHECK_SECONDS = 300  # re-verify an active agent login this often (it can expire)
RUNTIME_PENDING_RECHECK_SECONDS = 5  # poll more often while loading / awaiting login
# Live Claude probe results younger than this are reused by the five-second
# non-active poll; under RUNTIME_RECHECK_SECONDS so the scheduled five-minute
# recheck always probes.
CLAUDE_LIVE_PROBE_RETRY_SECONDS = 240
_MANAGED_PROVIDER_BY_RUNTIME = {
    "codex": "openai",
    "claude_code": "claude",
    "hermes": "bedrock",
}
CLAUDE_IDENTITY_ATTESTATION = "anthropic_oauth_profile"
OPENAI_OPERATOR_APPROVAL = "codex_device_login"
RUNTIME_LABELS = {"codex": "Codex", "claude_code": "Claude Code", "hermes": "Hermes"}
OAUTH_RUNTIMES = ("codex", "claude_code")
DEACTIVATED_REASON = "agent runtime deactivated because its managed network provider is disabled"


class ExecutionPhase(str, Enum):
    STARTING = "starting"
    RUNNING = "running"
    FINISHING = "finishing"
    CLOSED = "closed"


@dataclass
class _Turn:
    """One live execution from durable admission through process teardown."""

    runtime_type: str
    thread_id: str
    model: str
    effort: str
    # Durable storage-only scope for this execution. It is attached to thread
    # events so reused provider activity ids cannot collide across processes.
    run_number: int
    phase: ExecutionPhase = ExecutionPhase.STARTING
    # The runtime adapter is published before it starts. Its own lifecycle
    # lock makes ``interrupt`` safe before, during, or after process spawn.
    server: Any = None
    # Steer delivery, provider callbacks, stop, and lifecycle transitions
    # share this lock. Slow process work always happens outside it.
    delivery_lock: threading.Lock = field(default_factory=threading.Lock)
    # The adapter publishes only a provider-confirmed, non-empty resumable id.
    provider_session_id: str | None = None
    startup_timer: threading.Timer | None = None


# runtime/thread key -> live turn. An entry exists from admission until the
# process close completes, so per-user-thread serialization runs through it.
_LIVE: dict[str, _Turn] = {}
_LIVE_LOCK = threading.Lock()
# Cached provider status, in process memory on purpose: it is derived health,
# re-computed from the provider CLIs within seconds of startup, so
# persisting it would only serve stale answers across restarts (a fresh
# process reports "loading" until the first poll). Writers replace whole
# records under _RUNTIME_STATUS_LOCK and never hold it around database work.
# readers take the current record lock-free (records are never mutated in
# place), so no path holds this lock while entering state.mutation() and the
# lock graph stays acyclic.
_RUNTIME_STATUSES: dict[str, dict[str, str]] = {}
_RUNTIME_STATUS_LOCK = threading.Lock()
# One in-flight refresh per runtime.
_REFRESH_LOCKS: dict[str, Any] = {runtime_type: threading.Lock() for runtime_type in AGENT_RUNTIMES}
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


def runtime_status(runtime_type: str) -> str:
    return runtime_status_record(runtime_type)["status"]


def runtime_status_record(runtime_type: str) -> dict[str, str]:
    return _RUNTIME_STATUSES.get(runtime_type, {"status": "loading"})


def all_runtime_status_records() -> dict[str, dict[str, str]]:
    return {runtime_type: runtime_status_record(runtime_type) for runtime_type in AGENT_RUNTIMES}


def _set_runtime_status(runtime_type: str, status: str, error_message: str | None = None) -> None:
    """Replace the provider status record."""
    record = {"status": status}
    if error_message is not None:
        record["error_message"] = error_message
    with _RUNTIME_STATUS_LOCK:
        _RUNTIME_STATUSES[runtime_type] = record


def agent_runtime_status() -> dict[str, Any]:
    # Reads only cached, in-memory status — never spawns an agent process — so
    # the request path (and /v1/health) is always fast. A background thread
    # keeps the status fresh.
    statuses = all_runtime_status_records()
    with _LIVE_LOCK:
        live = [(turn.runtime_type, turn.thread_id) for turn in _LIVE.values()]
    runtimes = []
    for runtime_type in sorted(AGENT_RUNTIMES):
        record = statuses.get(runtime_type, {})
        status = str(record.get("status", "loading"))
        active = sorted(thread_id for live_runtime, thread_id in live if live_runtime == runtime_type)
        response = {"type": runtime_type, "status": status, "active_thread_ids": active}
        error_message = record.get("error_message")
        if status == "error" and error_message:
            response["error_message"] = error_message
        runtimes.append(response)
    return {"runtimes": runtimes}


def refresh_runtime_status(runtime_type: str, *, force_provider_probe: bool = False) -> str:
    """Re-derive the agent runtime status and cache it in memory. Runs the
    provider check outside the state transaction so a slow runtime process
    never blocks requests. Serialized per provider connection by
    _REFRESH_LOCKS."""
    with _REFRESH_LOCKS[runtime_type]:
        return _refresh_runtime_status_serialized(
            runtime_type, force_provider_probe=force_provider_probe
        )


def _refresh_runtime_status_serialized(runtime_type: str, *, force_provider_probe: bool = False) -> str:
    if not runtime_network_enabled(runtime_type):
        return _mark_runtime_deactivated(runtime_type)
    provider = _provider_module(runtime_type)
    try:
        if runtime_type == "codex" and force_provider_probe:
            status, error_message, account = provider.account_status(force_provider_probe=True)
        else:
            status, error_message, account = provider.account_status()
    except Exception as exc:
        host_errors.report_unexpected(
            "orchestrator.runtime_status",
            exc,
            context={"agent_runtime": runtime_type},
        )
        status, error_message, account = "error", f"unexpected error checking {runtime_type}: {exc!r}", None
    if runtime_type == "claude_code" and status == "active" and isinstance(account, dict):
        status, error_message, account = _live_claude_status(account, force_probe=force_provider_probe)
    # The status poll is the sole reader of the parked login server, so it has
    # now recorded any completed-login notification. Capture the first trusted
    # anchor here, in the same refresh, so this refresh's commit publishes the
    # proxy pin the moment the login lands instead of a poll cycle later.
    _capture_completed_codex_login(runtime_type)
    account_value = _active_account_value(runtime_type, status, account)
    attested: dict[str, Any] | None = None
    attest_error: str | None = None
    if runtime_type == "claude_code" and status == "active" and account_value:
        attested, attest_error = _claude_attestation(account_value)
    deactivated = False
    became_nonactive = False
    codex_login_to_close: str | None = None
    after_commit: list[Callable[[], None]] = []
    with state.mutation(after_commit=after_commit) as cur:
        if not runtime_network_enabled(runtime_type):
            # The one policy re-check, inside the mutation: a policy disable
            # that landed while the slow probe ran must not be overwritten by
            # the probe's stale result. The deactivated status, the OAuth
            # clear, and the pin clear commit in this same transaction.
            _mark_runtime_deactivated_in(cur, after_commit, runtime_type)
            deactivated = True
        else:
            if status == "active" and runtime_type in OAUTH_RUNTIMES:
                # The anchor check, the anchor save, and the pin write below
                # share this mutation, so they serialize with an operator
                # reset: a slow probe that started before the reset sees the
                # anchor already cleared here and cannot re-approve the old
                # account or republish its pin.
                try:
                    account_value = _trusted_active_account(cur, runtime_type, account_value, attested, attest_error)
                except ProviderAccountNotApproved:
                    status, error_message, account_value = "awaiting_login", None, None
                except ProviderAccountTrustError as exc:
                    status, error_message, account_value = "error", str(exc), None
            previous = runtime_status(runtime_type)
            cached_error = error_message if status == "error" and error_message else None
            after_commit.append(
                partial(_set_runtime_status, runtime_type, status, cached_error)
            )
            became_nonactive = previous == "active" and status != "active"
            if status == "active":
                # The device code is spent (or moot) once the account is active.
                # Without this, a later session expiry would resurface the stale
                # record instead of letting the operator start a fresh login.
                if runtime_type == "codex":
                    completed_login = state.oauth_login("codex", cur)
                    codex_login_to_close = _string_field(completed_login, "login_id") if completed_login else None
                if runtime_type == "claude_code":
                    state.set_oauth_login(cur, "claude", None)
                    save_claude_account(account_value, cur)
                elif runtime_type == "hermes":
                    # The Bedrock account row is written once at credential
                    # submission and only cleared by a disconnect; the status
                    # refresh has nothing to store for it.
                    pass
                else:
                    state.set_oauth_login(cur, "codex", None)
                    _stamp_usage_checked_at(account_value, "codex_usage", utc_now())
                    save_openai_account(account_value, cur)
            if runtime_type in OAUTH_RUNTIMES:
                _sync_runtime_proxy_pin_in(cur, runtime_type, account_value if status == "active" else None)
            if runtime_type in OAUTH_RUNTIMES and previous == "awaiting_login" and status == "active":
                state.append_agent_event(cur, "agent_runtime.login_completed", None, {"agent_runtime": runtime_type})
            if previous != "active" and status == "active":
                state.append_agent_event(
                    cur,
                    "agent_runtime.active",
                    None,
                    {"agent_runtime": runtime_type},
                )
    if deactivated:
        _stop_runtime_processes(runtime_type, DEACTIVATED_REASON)
        return "deactivated"
    if became_nonactive:
        label = RUNTIME_LABELS.get(runtime_type, runtime_type)
        reason = (
            f"{label} runtime became unavailable: {error_message}"
            if status == "error" and error_message
            else f"{label} runtime became {status}"
        )
        _stop_runtime_processes(runtime_type, reason)
    if status == "active":
        # The login flow (first login or reauth) has landed, so its parked
        # device-login server is done. Close the one for this login id, scoped so
        # a login started meanwhile survives, or later status checks would keep
        # polling the leftover login process instead of short-lived servers.
        if codex_login_to_close:
            codex_app_server.close_completed_login_server(codex_login_to_close)
        if runtime_type == "claude_code":
            _backfill_claude_usage(account_value)
    return status


def _capture_completed_codex_login(runtime_type: str) -> None:
    """Persist the first trusted OpenAI anchor once the login has completed.

    Runs right after the status poll, which is the sole reader of the parked
    device-code app-server and has therefore recorded the successful
    account/login/completed notification. A stored OAuth row means the operator
    saw a device code, not that the login completed, so capture still requires
    that completion for the exact login id; the account id itself is read from
    the provider-signed login tokens promptly after completion (see
    read_completed_device_login_account_id). The surrounding refresh publishes
    the proxy pin when it commits, right after this capture.
    """
    if runtime_type != "codex":
        return
    if _trusted_openai_account_id(read_openai_account()):
        return  # anchor already established; nothing to capture
    login = _current_oauth_login("codex")
    login_id = _string_field(login, "login_id") if login else None
    if not login_id:
        return
    try:
        account_id = codex_app_server.read_completed_device_login_account_id(login_id)
    except codex_app_server.CodexAppServerError:
        # A helper hiccup must not fail the refresh; the poller already
        # classified the runtime state and the next refresh retries the capture.
        return
    if account_id:
        _capture_completed_codex_oauth_login(login_id, account_id)


def _capture_completed_codex_oauth_login(login_id: str, account_id: str) -> bool:
    """Persist the first OpenAI anchor only for the completed device-code flow."""
    with state.mutation() as cur:
        trusted_account_id = _trusted_openai_account_id(read_openai_account(cur))
        if trusted_account_id:
            return trusted_account_id == account_id
        login = _current_oauth_login("codex", cur)
        current_login_id = _string_field(login, "login_id") if login else None
        if login is None or current_login_id != login_id:
            return False
        state.set_oauth_login(cur, "codex", login | {"status": "completed"})
        save_openai_account(_with_openai_operator_approval({"account_id": account_id}), cur)
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
    proxy header guard. Claude's identity is server-attested: the anchor is
    the account uuid api.anthropic.com reports for the token itself, checked
    whenever the token changes, so agent-writable metadata is never trusted.
    """
    if not account:
        raise ProviderAccountTrustError(f"{runtime_type} reported active without account metadata")
    if runtime_type == "claude_code":
        return _trusted_claude_account(cur, account, attested, attest_error)
    return _trusted_openai_account(cur, account)


def _trusted_openai_account(cur: Any, account: dict[str, Any]) -> dict[str, Any]:
    account_id = _string_field(account, "account_id")
    if not account_id:
        raise ProviderAccountTrustError("OpenAI account id is not available")
    trusted_account_id = _trusted_openai_account_id(read_openai_account(cur))
    if trusted_account_id:
        if account_id != trusted_account_id:
            raise ProviderAccountTrustError("OpenAI account changed; reset the linked account under Home > Integrations in the admin UI")
        return _with_openai_operator_approval(account)
    raise ProviderAccountNotApproved("OpenAI account is not operator-approved; start OAuth login from the admin UI")


def _trusted_openai_account_id(account: dict[str, Any]) -> str | None:
    if _string_field(account, "operator_approval") != OPENAI_OPERATOR_APPROVAL:
        return None
    return _string_field(account, "account_id")


def _with_openai_operator_approval(account: dict[str, Any]) -> dict[str, Any]:
    approved = dict(account)
    approved["operator_approval"] = OPENAI_OPERATOR_APPROVAL
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

    Mirrors _trusted_openai_account_id: the operator-approval marker is the
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
    state.save_proxy_openai_account_id(account_id, cur)


def _string_field(value: dict[str, Any], key: str) -> str | None:
    field = value.get(key)
    return field if isinstance(field, str) and field else None


def _stamp_usage_checked_at(account: dict[str, Any] | None, usage_key: str, checked_at: str) -> None:
    if not account:
        return
    usage = account.get(usage_key)
    if isinstance(usage, dict):
        usage["last_checked_at"] = checked_at


def _mark_runtime_deactivated(runtime_type: str) -> str:
    after_commit: list[Callable[[], None]] = []
    with state.mutation(after_commit=after_commit) as cur:
        _mark_runtime_deactivated_in(cur, after_commit, runtime_type)
    _stop_runtime_processes(runtime_type, DEACTIVATED_REASON)
    return "deactivated"


def _mark_runtime_deactivated_in(
    cur: Any,
    after_commit: list[Callable[[], None]],
    runtime_type: str,
) -> None:
    previous = runtime_status(runtime_type)
    after_commit.append(partial(_set_runtime_status, runtime_type, "deactivated"))
    _clear_oauth_login_in(cur, runtime_type)
    if runtime_type in OAUTH_RUNTIMES:
        _sync_runtime_proxy_pin_in(cur, runtime_type, None)
    if previous != "deactivated":
        state.append_agent_event(
            cur,
            "agent_runtime.deactivated",
            None,
            {"agent_runtime": runtime_type},
        )


def _clear_oauth_login_in(cur: Any, runtime_type: str) -> None:
    # Hermes has no OAuth flow (its credential lives encrypted
    # in the database), so there is no login record to clear for them.
    if runtime_type == "hermes":
        return
    state.set_oauth_login(cur, "claude" if runtime_type == "claude_code" else "codex", None)


def reconcile_runtime_status_after_policy_change() -> None:
    """Synchronize cached runtime state after a policy update.

    Disabled runtimes are deactivated synchronously because that fails running
    turns and closes their processes; deactivation never probes, so it skips
    the provider-connection refresh serialization rather than wait out an in-flight
    slow probe (which re-checks the policy inside its own commit anyway).
    Enabled runtimes are refreshed in the background: a policy change may have
    re-enabled a runtime whose poller still has a stale long active-runtime
    deadline, but the network-policy request path must not block on provider
    CLI checks.
    """
    enabled: list[str] = []
    for runtime_type in ("codex", "claude_code", "hermes"):
        if not runtime_network_enabled(runtime_type):
            _mark_runtime_deactivated(runtime_type)
        else:
            enabled.append(runtime_type)
    if enabled:
        threading.Thread(target=_refresh_runtimes, args=(tuple(enabled),), daemon=True).start()


def _refresh_runtimes(runtime_types: tuple[str, ...]) -> None:
    for runtime_type in runtime_types:
        try:
            refresh_runtime_status(runtime_type)
        except Exception as exc:
            host_errors.report_unexpected(
                "orchestrator.initial_runtime_refresh",
                exc,
                context={"agent_runtime": runtime_type},
            )
            continue


def reset_linked_account(runtime_type: str) -> None:
    """Operator reset: delete the linked-account guard and stop old sessions.

    One mutation clears the trusted account anchor, its proxy pin, and any
    pending OAuth approval; a best-effort close of a parked login flow
    follows. Live runtime processes are closed and running turns are failed
    so no process from the old linked account keeps executing while the caller
    clears local auth files and refreshes status. A device login parked at
    the same instant is torn down with everything else (starting a login
    while resetting the account is contradictory; the operator just starts
    a fresh login)."""
    if runtime_type not in OAUTH_RUNTIMES:
        raise ValueError("linked-account reset is only available for OAuth runtimes")
    global _CLAUDE_LIVE_PROBE
    _reset_linked_account_in_state(runtime_type)
    # The reset replaces the credential any remembered live-validation verdict
    # was about; the next login validates from scratch.
    if runtime_type == "claude_code":
        _CLAUDE_LIVE_PROBE = None
    else:
        codex_app_server.clear_live_validation_failure()
    _close_login_flow(runtime_type)
    _stop_runtime_processes(runtime_type, "linked provider account was reset by the operator")


def _reset_linked_account_in_state(runtime_type: str) -> None:
    after_commit: list[Callable[[], None]] = []
    with state.mutation(after_commit=after_commit) as cur:
        next_status = "awaiting_login" if runtime_network_enabled(runtime_type) else "deactivated"
        after_commit.append(partial(_set_runtime_status, runtime_type, next_status))
        _clear_oauth_login_in(cur, runtime_type)
        if runtime_type == "claude_code":
            save_claude_account(None, cur)
        else:
            save_openai_account(None, cur)
        _sync_runtime_proxy_pin_in(cur, runtime_type, None)
        state.append_agent_event(cur, "agent_runtime.linked_account_reset", None, {"agent_runtime": runtime_type})


def disconnect_bedrock_connection() -> None:
    """Disconnect the AWS connection and stop Hermes work."""
    with _BEDROCK_CONNECTION_LOCK:
        after_commit: list[Callable[[], None]] = []
        with state.mutation(after_commit=after_commit) as cur:
            save_bedrock_account(None, cur)
            state.delete_bedrock_credential(cur)
            cur.execute("SELECT 1 FROM managed_integrations WHERE integration = 'bedrock'")
            enabled = cur.fetchone() is not None
            next_status = "awaiting_login" if enabled else "deactivated"
            after_commit.append(partial(_set_runtime_status, "hermes", next_status))
            state.append_agent_event(
                cur,
                "agent_runtime.linked_account_reset",
                None,
                {"agent_runtime": "hermes"},
            )
        _stop_runtime_processes(
            "hermes",
            "AWS Bedrock connection was reset by the operator",
        )


def _close_login_flow(runtime_type: str) -> None:
    # Best-effort: the pending OAuth record is already gone, so a parked login
    # process that resists closing is inert; never fail the caller over it.
    try:
        if runtime_type == "codex":
            codex_app_server.close_login_server()
        elif runtime_type == "claude_code":
            claude_code.close_login_process()
        # Hermes has no login process to close.
    except Exception:
        pass


def _stop_runtime_processes(runtime_type: str, reason: str) -> None:
    interrupted: list[Any] = []
    with _LIVE_LOCK:
        turns = [turn for turn in _LIVE.values() if turn.runtime_type == runtime_type]
    for turn in turns:
        after_commit: list[Callable[[], None]] = []
        with turn.delivery_lock, state.mutation(after_commit=after_commit) as cur:
            if turn.phase not in (ExecutionPhase.STARTING, ExecutionPhase.RUNNING):
                continue
            _record_turn_finished(
                cur,
                after_commit,
                turn,
                error_message=reason,
            )
            if turn.server is not None:
                interrupted.append(turn.server)
    for server in interrupted:
        _interrupt_turn(server)


def runtime_status_loop() -> None:
    refresh_targets = ("codex", "claude_code", "hermes")
    next_check_at = {runtime_type: 0.0 for runtime_type in refresh_targets}
    while True:
        now = time.monotonic()
        try:
            for runtime_type in refresh_targets:
                if now < next_check_at[runtime_type]:
                    continue
                status = refresh_runtime_status(runtime_type)
                delay = RUNTIME_RECHECK_SECONDS if status == "active" else RUNTIME_PENDING_RECHECK_SECONDS
                next_check_at[runtime_type] = time.monotonic() + delay
        except Exception as exc:
            # Keep the loop alive; retry soon because the failed refresh did
            # not update that runtime's cached state.
            host_errors.report_unexpected("orchestrator.runtime_status_loop", exc)
            time.sleep(RUNTIME_PENDING_RECHECK_SECONDS)
            continue
        sleep_for = min(max(0.0, due - time.monotonic()) for due in next_check_at.values())
        time.sleep(min(max(sleep_for, 0.1), RUNTIME_PENDING_RECHECK_SECONDS))


def start_background_loops() -> None:
    # Converge GitHub credentials before any turn can be admitted: after a
    # restart the persisted App installation token may already be expired, and
    # a turn's first git/gh call must not run against a stale token file.
    # This blocks startup by at most one mint (~1s) and does not hold up the
    # other runtimes.
    try:
        github_credential.reconcile()
        converged = True
    except Exception:
        # reconcile() records its own failures; reaching here means it could
        # not even run (the database briefly unavailable during startup), so
        # the refresh loop retries quickly rather than waiting a full cycle.
        converged = False
    threading.Thread(target=runtime_status_loop, daemon=True).start()
    threading.Thread(target=github_credential_refresh_loop, args=(converged,), daemon=True).start()


GITHUB_CREDENTIAL_REFRESH_CHECK_SECONDS = 300
GITHUB_CREDENTIAL_RETRY_SECONDS = 10


def github_credential_refresh_loop(converged: bool) -> None:
    # Converge the installed token file every cycle: App installation tokens
    # live one hour and are re-minted inside the refresh margin, and any
    # earlier failure (mint, install, disable-time removal) is retried here.
    # The initial convergence ran synchronously in start_background_loops
    # before any turn could be admitted; while a convergence attempt cannot
    # run at all (the database unavailable), retry on the short interval so
    # turns are not admitted against stale credential files for a full cycle.
    while True:
        time.sleep(GITHUB_CREDENTIAL_REFRESH_CHECK_SECONDS if converged else GITHUB_CREDENTIAL_RETRY_SECONDS)
        try:
            github_credential.reconcile()
            github_repo_audit.refresh()
            converged = True
        except Exception:
            converged = False


def live_thread_ids() -> set[str]:
    """Threads with an admitted execution that is not fully closed."""
    with _LIVE_LOCK:
        return {turn.thread_id for turn in _LIVE.values()}


def _retryable_phase_error(phase: ExecutionPhase) -> ApiError:
    if phase == ExecutionPhase.STARTING:
        return ApiError(
            HTTPStatus.CONFLICT,
            "the agent is starting; retry shortly",
        )
    return ApiError(
        HTTPStatus.CONFLICT,
        "the agent is finishing; retry shortly",
    )


def admit_turn(
    cur: Any,
    after_commit: list[Callable[[], None]],
    thread_id: str,
    runtime_type: str,
    model: str,
    effort: str,
    message: str,
    *,
    pre_message_activity: dict[str, Any] | None = None,
) -> _Turn:
    """Admit one new turn inside the caller's mutation.

    The service checks synchronous steering before entering this path and
    serializes sends per thread. Publishing through ``after_commit`` keeps the
    live fence and the initial durable events atomic from other senders'
    perspective.
    """
    label = RUNTIME_LABELS.get(runtime_type, runtime_type)
    with _LIVE_LOCK:
        existing = next((t for t in _LIVE.values() if t.thread_id == thread_id), None)
        if existing is not None:
            raise _retryable_phase_error(existing.phase)
        if not runtime_network_enabled(runtime_type):
            raise ApiError(
                HTTPStatus.CONFLICT,
                f"{label} runtime is deactivated; enable its provider under Home > Integrations",
            )
        status = runtime_status(runtime_type)
        if status != "active":
            raise ApiError(
                HTTPStatus.CONFLICT,
                f"{label} runtime is {status}; messages run only while it is active",
            )
        live = sum(1 for t in _LIVE.values() if t.runtime_type == runtime_type)
        if live >= TURN_LIMIT_PER_RUNTIME:
            raise ApiError(
                HTTPStatus.TOO_MANY_REQUESTS,
                f"{label} runtime is already running {TURN_LIMIT_PER_RUNTIME} concurrent threads; retry when one finishes",
            )
        run_number = state.start_thread_run(cur, thread_id)
        turn = _Turn(runtime_type, thread_id, model, effort, run_number)
        # Other admissions cannot interleave because the mutation lock is
        # still held when this callback registers the live fence.
        after_commit.append(partial(_publish_turn, turn))
    if pre_message_activity is not None:
        state.append_agent_event(
            cur,
            "thread.activity",
            thread_id,
            {"activity": pre_message_activity},
            run_number=run_number,
        )
    state.append_agent_event(
        cur,
        "thread.message",
        thread_id,
        {"message": message, "source": "user"},
        run_number=run_number,
    )
    return turn


def steer_live_turn(thread_id: str, runtime_type: str, message: str) -> bool:
    """Synchronously steer a live turn, returning False when it is idle.

    Provider acknowledgement happens before the durable user event. A database
    failure after acknowledgement is returned to the caller with no event; a
    retry may therefore deliver the same message twice by explicit design.
    """
    key = _live_key(runtime_type, thread_id)
    with _LIVE_LOCK:
        turn = _LIVE.get(key)
    if turn is None:
        return False
    failure: ApiError | None = None
    server_to_interrupt: Any = None
    with turn.delivery_lock:
        if turn.phase != ExecutionPhase.RUNNING:
            raise _retryable_phase_error(turn.phase)
        if runtime_type == "hermes":
            raise ApiError(
                HTTPStatus.CONFLICT,
                "Hermes cannot accept another message while running; wait for it to finish",
            )
        server = turn.server
        if server is None:
            detail = "runtime entered running state without a provider transport"
            after_commit: list[Callable[[], None]] = []
            with state.mutation(after_commit=after_commit) as cur:
                _record_turn_finished(cur, after_commit, turn, error_message=detail)
            failure = ApiError(HTTPStatus.BAD_GATEWAY, detail)
        else:
            try:
                server.steer(message)
            except codex_app_server.CodexTurnFinishing:
                # Codex's stdout reader has already observed turn/completed,
                # but the execution worker has not yet committed FINISHING.
                # Preserve that completion fence as a retryable phase instead
                # of recording a false terminal provider error.
                raise _retryable_phase_error(ExecutionPhase.FINISHING)
            except (codex_app_server.CodexAppServerError, claude_code.ClaudeCodeError) as exc:
                # Once the adapter has declared RUNNING, "not ready" is no
                # longer a transient phase. It is a provider/transport failure
                # and owns the normal durable error -> FINISHING path.
                detail = str(exc)
                label = RUNTIME_LABELS.get(runtime_type, runtime_type)
                after_commit = []
                with state.mutation(after_commit=after_commit) as cur:
                    _record_turn_finished(
                        cur,
                        after_commit,
                        turn,
                        error_message=f"{label} rejected the message: {detail}",
                    )
                server_to_interrupt = server
                failure = ApiError(
                    HTTPStatus.BAD_GATEWAY,
                    f"{label} rejected the message: {detail}",
                )
            else:
                with state.mutation() as cur:
                    state.touch_thread_session(cur, thread_id, utc_now())
                    state.append_agent_event(
                        cur,
                        "thread.message",
                        thread_id,
                        {"message": message, "source": "user"},
                        run_number=turn.run_number,
                    )
    if server_to_interrupt is not None:
        _interrupt_turn(server_to_interrupt)
    if failure is not None:
        raise failure
    return True


def _publish_turn(turn: _Turn) -> None:
    with _LIVE_LOCK:
        _LIVE[_live_key(turn.runtime_type, turn.thread_id)] = turn


def _mark_finishing(turn: _Turn, provider_session_id: str | None = None) -> None:
    """Publish FINISHING only after the durable lifecycle commit."""
    turn.phase = ExecutionPhase.FINISHING
    if provider_session_id:
        turn.provider_session_id = provider_session_id
    if turn.startup_timer is not None:
        turn.startup_timer.cancel()


def _publish_provider_session(turn: _Turn, provider_session_id: str) -> None:
    turn.provider_session_id = provider_session_id


def _normalized_provider_session_id(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _provider_session_accepted(turn: _Turn, provider_session_id: str) -> None:
    """Persist a non-empty provider-confirmed resume id when it is learned."""
    accepted_session_id = _normalized_provider_session_id(provider_session_id)
    if accepted_session_id is None:
        raise ValueError("provider reported an empty session id")
    provider_session_id = accepted_session_id
    with turn.delivery_lock:
        if turn.phase == ExecutionPhase.CLOSED or turn.provider_session_id == provider_session_id:
            return
        after_commit: list[Callable[[], None]] = []
        with state.mutation(after_commit=after_commit) as cur:
            state.save_thread_provider_session(
                cur,
                turn.thread_id,
                turn.run_number,
                provider_session_id,
            )
            after_commit.append(
                partial(_publish_provider_session, turn, provider_session_id)
            )


def _provider_ready(turn: _Turn) -> bool:
    """Move STARTING to RUNNING once the initial message is accepted."""
    with turn.delivery_lock:
        if turn.phase != ExecutionPhase.STARTING:
            return False
        turn.phase = ExecutionPhase.RUNNING
        if turn.startup_timer is not None:
            turn.startup_timer.cancel()
        return True


def launch_turn(turn: _Turn, input_message: str, provider_session_id: str | None) -> None:
    """Run an admitted turn on its own thread. Called after the admitting
    mutation commits, so its user message and durable running state exist
    before the worker starts."""
    worker = threading.Thread(
        target=_run_turn, args=(turn, input_message, provider_session_id), daemon=True
    )
    try:
        worker.start()
    except Exception as exc:
        # Admission is already durable. Terminate that lifecycle synchronously
        # and release its capacity instead of leaving an unowned live fence.
        _finish_turn(turn, error_message=f"could not start turn worker: {exc}")
        _close_turn(turn, None)
        return
    timer = threading.Timer(
        EXECUTION_START_TIMEOUT_SECONDS,
        _starting_timed_out,
        args=(turn,),
    )
    timer.daemon = True
    with turn.delivery_lock:
        if turn.phase == ExecutionPhase.STARTING:
            turn.startup_timer = timer
            timer.start()


def _starting_timed_out(turn: _Turn) -> None:
    server: Any = None
    with turn.delivery_lock:
        if turn.phase != ExecutionPhase.STARTING:
            return
        after_commit: list[Callable[[], None]] = []
        with state.mutation(after_commit=after_commit) as cur:
            _record_turn_finished(
                cur,
                after_commit,
                turn,
                error_message="agent startup timed out",
            )
        server = turn.server
    if server is not None:
        _interrupt_turn(server)


def _run_turn(turn: _Turn, input_message: str, provider_session_id: str | None) -> None:
    runtime_type = turn.runtime_type
    thread_id = turn.thread_id
    provider = _provider_module(runtime_type)

    def on_agent_message(message: str | dict[str, Any]) -> None:
        event_type: str
        payload: dict[str, Any]
        refresh_recency = False
        if isinstance(message, dict):
            activity = agent_activity.normalize_record(message)
            if activity is None:
                return
            event_type = "thread.activity"
            payload = {"activity": activity}
        elif isinstance(message, str):
            text = agent_activity.clean_text(message)
            if not text:
                return
            event_type = "thread.message"
            payload = {"message": text, "source": "agent"}
            refresh_recency = True
        else:
            return
        # Provider callbacks can still arrive while a stop/deactivation is
        # tearing the process down. Commit the event before the terminal
        # boundary or, if termination won the lock, discard it.
        with turn.delivery_lock:
            if turn.phase != ExecutionPhase.RUNNING:
                return
            with state.mutation() as cur:
                if refresh_recency:
                    state.touch_thread_session(cur, thread_id, utc_now())
                state.append_agent_event(
                    cur,
                    event_type,
                    thread_id,
                    payload,
                    run_number=turn.run_number,
                )

    def finish_claude_turn(provider_session_id: str, output: str) -> int:
        """Atomically choose between a just-delivered steer and completion."""
        del output  # the final text already streamed as a thread.message event
        with turn.delivery_lock:
            # The Claude driver normally consumes this counter in its event
            # loop. Checking it once more under the delivery lock closes the
            # final steer-vs-completion boundary without storing any message.
            delivered = server.take_delivered_steers()
            if delivered:
                return delivered
            after_commit: list[Callable[[], None]] = []
            with state.mutation(after_commit=after_commit) as cur:
                if turn.phase != ExecutionPhase.RUNNING:
                    return 0
                _record_turn_finished(
                    cur,
                    after_commit,
                    turn,
                    provider_session_id=provider_session_id,
                )
            return 0

    # Everything from here is inside one try: the turn was admitted (its
    # events recorded) already, so ANY exception — including a failure to
    # create or start the server — must fail the turn; otherwise its thread
    # would stay fenced with no terminal event.
    server: Any = None
    try:
        # Claude's CLI rotates its bearer token independently. Converge the
        # proxy pin before its process starts; if the refresh makes the
        # runtime non-active, that transition stops this turn.
        if runtime_type == "claude_code":
            refresh_runtime_status(runtime_type)
            with turn.delivery_lock:
                active = turn.phase == ExecutionPhase.STARTING
            if not active:
                return
        with turn.delivery_lock:
            if turn.phase != ExecutionPhase.STARTING:
                return
        server = _new_agent_server(
            runtime_type,
            thread_id,
            partial(_provider_ready, turn),
            partial(_provider_session_accepted, turn),
        )
        # Publish before start so Stop can call the adapter's non-blocking,
        # start-safe interrupt. Each adapter prevents a process spawn after
        # interrupt has won its own lifecycle lock.
        with turn.delivery_lock:
            if turn.phase != ExecutionPhase.STARTING:
                return
            turn.server = server
        server.start()
        with turn.delivery_lock:
            if turn.phase not in (ExecutionPhase.STARTING, ExecutionPhase.RUNNING):
                return
        if runtime_type == "claude_code":
            new_provider_session_id, _output = provider.run_turn(
                server,
                input_message,
                provider_session_id,
                turn.model,
                turn.effort,
                on_agent_message,
                finish_claude_turn,
            )
        else:
            new_provider_session_id, _output = provider.run_turn(
                server,
                input_message,
                provider_session_id,
                turn.model,
                turn.effort,
                on_agent_message,
            )
        _finish_turn(turn, provider_session_id=new_provider_session_id)
    except Exception as exc:
        # The callback is the primary persistence path. The attribute is only
        # a defensive fallback for an adapter exception at the exact boundary
        # where it learned the id but its callback did not return.
        last_session_id = getattr(server, "last_known_session_id", None) if server is not None else None
        _finish_turn(turn, error_message=str(exc), provider_session_id=last_session_id)
    finally:
        _close_turn(turn, server)


def stop_thread_turn(thread_id: str) -> bool:
    """Stop the thread's live turn (the operator/app stop path). Returns False
    when the thread has no stoppable turn. The thread.stopped event and the
    lifecycle state commit in one mutation. The adapter is interrupted without
    waiting for teardown; the owning execution thread closes the process and
    removes the live fence."""
    with _LIVE_LOCK:
        turn = next((t for t in _LIVE.values() if t.thread_id == thread_id), None)
    if turn is None:
        return False
    after_commit: list[Callable[[], None]] = []
    with turn.delivery_lock, state.mutation(after_commit=after_commit) as cur:
        if turn.phase not in (ExecutionPhase.STARTING, ExecutionPhase.RUNNING):
            return False
        state.finish_thread_run(cur, thread_id, turn.run_number)
        state.append_agent_event(
            cur,
            "thread.stopped",
            thread_id,
            {},
            run_number=turn.run_number,
        )
        after_commit.append(partial(_mark_finishing, turn))
        server = turn.server
    if server is not None:
        _interrupt_turn(server)
    return True


def _interrupt_turn(server: Any) -> None:
    """Request prompt process interruption without waiting for teardown."""
    try:
        server.interrupt()
    except Exception:
        # The execution thread owns bounded close/verification and records a
        # cleanup error if the process cannot be reaped.
        pass


def _close_turn(turn: _Turn, server: Any) -> None:
    """Close the provider and release the live fence from its owning thread."""
    key = _live_key(turn.runtime_type, turn.thread_id)
    if turn.startup_timer is not None:
        turn.startup_timer.cancel()
    if server is not None:
        try:
            server.close()
        except Exception as exc:
            with turn.delivery_lock:
                if turn.phase != ExecutionPhase.CLOSED:
                    with state.mutation() as cur:
                        state.append_agent_event(
                            cur,
                            "thread.error",
                            turn.thread_id,
                            {"error_message": f"agent process cleanup failed: {exc}"},
                            run_number=turn.run_number,
                        )
            # Never run a new same-thread process while the old scope may live.
            return
    with turn.delivery_lock:
        turn.phase = ExecutionPhase.CLOSED
    with _LIVE_LOCK:
        if _LIVE.get(key) is turn:
            del _LIVE[key]


def _record_turn_finished(
    cur: Any,
    after_commit: list[Callable[[], None]],
    turn: _Turn,
    *,
    provider_session_id: str | None = None,
    error_message: str | None = None,
) -> None:
    """Finalize durable lifecycle state before process teardown begins."""
    accepted_session_id = (
        _normalized_provider_session_id(provider_session_id)
        or _normalized_provider_session_id(turn.provider_session_id)
    )
    if accepted_session_id and accepted_session_id != turn.provider_session_id:
        state.save_thread_provider_session(
            cur,
            turn.thread_id,
            turn.run_number,
            accepted_session_id,
        )
    state.touch_thread_session(cur, turn.thread_id, utc_now())
    if error_message is not None:
        state.append_agent_event(
            cur,
            "thread.error",
            turn.thread_id,
            {"error_message": error_message},
            run_number=turn.run_number,
        )
    state.finish_thread_run(cur, turn.thread_id, turn.run_number)
    after_commit.append(partial(_mark_finishing, turn, accepted_session_id))


def _finish_turn(
    turn: _Turn,
    *,
    provider_session_id: str | None = None,
    error_message: str | None = None,
) -> None:
    provider_session_id = _normalized_provider_session_id(provider_session_id)
    after_commit: list[Callable[[], None]] = []
    with turn.delivery_lock, state.mutation(after_commit=after_commit) as cur:
        if turn.phase in (ExecutionPhase.FINISHING, ExecutionPhase.CLOSED):
            # Defensive fallback only: adapters normally persist immediately
            # through their accepted-session callback.
            if (
                provider_session_id
                and turn.phase == ExecutionPhase.FINISHING
                and provider_session_id != turn.provider_session_id
            ):
                state.save_thread_provider_session(
                    cur,
                    turn.thread_id,
                    turn.run_number,
                    provider_session_id,
                )
                after_commit.append(
                    partial(_publish_provider_session, turn, provider_session_id)
                )
            return
        _record_turn_finished(
            cur,
            after_commit,
            turn,
            provider_session_id=provider_session_id,
            error_message=error_message,
        )


def _provider_module(runtime_type: str | None = None) -> Any:
    if runtime_type == "claude_code":
        return claude_code
    if runtime_type == "hermes":
        return hermes_agent
    return codex_app_server


def _new_agent_server(
    runtime_type: str,
    thread_id: str,
    on_ready: Callable[[], bool],
    on_session_id: Callable[[str], None],
) -> Any:
    # Every turn runs inside a scope named after its host thread. Turns on one
    # thread are serialized, and --collect removes the scope before the same
    # unit name can be used by its next turn.
    if runtime_type == "claude_code":
        return claude_code.ClaudeCodeSession(
            thread_id=thread_id,
            on_ready=on_ready,
            on_session_id=on_session_id,
        )
    if runtime_type == "hermes":
        return hermes_agent.HermesSession(
            thread_id=thread_id,
            on_ready=on_ready,
            on_session_id=on_session_id,
        )
    return codex_app_server.CodexAppServer(
        thread_id=thread_id,
        on_ready=on_ready,
        on_session_id=on_session_id,
    )


def _live_key(runtime_type: str, thread_id: Any) -> str:
    return f"{runtime_type}:{thread_id}"


def runtime_network_enabled(runtime_type: str) -> bool:
    provider = _MANAGED_PROVIDER_BY_RUNTIME.get(runtime_type)
    integrations = network_policy.load_policy().get("network_integrations", {})
    if not provider or not isinstance(integrations, dict):
        return False
    integration = integrations.get(provider)
    return isinstance(integration, dict) and integration.get("enabled") is True
