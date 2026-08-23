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
  cleared. Claude and Grok identities are additionally server-attested through
  their root account helpers, so agent-writable metadata is never what gets
  shown or pinned.
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
from host.runtime.agent_runtime import (
    agent_activity,
    # Kept as a public compatibility seam for callers/tests that patch the
    # credential client through orchestrator; provider_account_trust uses this
    # same module object after the domain split.
    bedrock_credentials,
    claude_code,
    codex_app_server,
    grok_agent,
    hermes_agent,
    script_runner,
)
from host.runtime.admin_api import github_credential, github_repo_audit
from host.runtime.admin_api.errors import ApiError
from host.runtime.agent_runtime import provider_account_trust
from host.runtime.agent_runtime.provider_account_trust import (
    CLAUDE_IDENTITY_ATTESTATION,
    CLAUDE_LIVE_PROBE_RETRY_SECONDS,
    OPENAI_OPERATOR_APPROVAL,
    XAI_OPERATOR_APPROVAL,
    ProviderAccountNotApproved,
    ProviderAccountTrustError,
    _BEDROCK_CONNECTION_LOCK,
    _TOKEN_ANCHORED,
    _active_account_value,
    _backfill_claude_usage,
    _capture_completed_token_login,
    _checked_claude_usage,
    _claude_anchor_is_server_attested,
    _claude_attestation,
    _claude_attestation_allowed,
    _claude_first_capture_approved,
    _completed_oauth_login,
    _current_oauth_login,
    _live_claude_status,
    _stamp_usage_checked_at,
    _string_field,
    _sync_runtime_proxy_pin_in,
    _trusted_active_account,
    _trusted_claude_account_id,
    _trusted_token_account_id,
    mark_oauth_login_completed,
    replace_and_validate_bedrock_credentials,
)
from host.runtime.agent_runtime.harness import ProviderSessionLost, ProviderTurnFinishing
from host.runtime.agent_runtime.harness_registry import HARNESSES, harness_adapter
from host.runtime.core.state import (
    read_claude_account,
    save_bedrock_account,
    save_claude_account,
    utc_now,
)

# Every runtime owns an independent ten-turn pool, so one busy runtime
# cannot take capacity from its peers. A message that would exceed the cap is
# rejected at admission; callers retry.
TURN_LIMIT_PER_RUNTIME = 10
EXECUTION_START_TIMEOUT_SECONDS = 10.0
RUNTIME_RECHECK_SECONDS = 300  # re-verify an active agent login this often (it can expire)
RUNTIME_PENDING_RECHECK_SECONDS = 5  # poll more often while loading / awaiting login
_MANAGED_PROVIDER_BY_RUNTIME = {
    runtime_type: adapter.managed_provider
    for runtime_type, adapter in HARNESSES.items()
    if adapter.managed_provider is not None
}
RUNTIME_LABELS = {
    runtime_type: adapter.label for runtime_type, adapter in HARNESSES.items()
}
OAUTH_RUNTIMES = tuple(
    runtime_type
    for runtime_type, adapter in HARNESSES.items()
    if adapter.oauth_key is not None
)
# oauth_logins rows key on the provider spelling, not the runtime type.
_OAUTH_KEY_BY_RUNTIME = {
    runtime_type: adapter.oauth_key
    for runtime_type, adapter in HARNESSES.items()
    if adapter.oauth_key is not None
}
DEACTIVATED_REASON = "agent runtime deactivated because its managed network provider is disabled"


# Runtimes with no managed provider connection to enable, log in to, or probe.
# They are available whenever the host is.
UNMANAGED_RUNTIMES = tuple(
    runtime_type
    for runtime_type, adapter in HARNESSES.items()
    if adapter.managed_provider is None
)
# Runtimes that map one turn to one process and expose no mid-turn channel, so
# a second message must wait for the turn instead of steering it.
NON_STEERABLE_RUNTIMES = tuple(
    runtime_type
    for runtime_type, adapter in HARNESSES.items()
    if not adapter.steerable
)


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
    provider = harness_adapter(runtime_type)
    if provider.collect_login_before_probe:
        provider.collect_login_completion()
        _capture_completed_token_login(runtime_type)
    try:
        status, error_message, account = provider.account_status(
            force_provider_probe=force_provider_probe
        )
    except Exception as exc:
        host_errors.report_warning(
            "orchestrator.runtime_status",
            exc,
            context={"agent_runtime": runtime_type},
        )
        status, error_message, account = "error", f"unexpected error checking {runtime_type}: {exc!r}", None
    if runtime_type == "claude_code" and status == "active" and isinstance(account, dict):
        status, error_message, account = _live_claude_status(account, force_probe=force_provider_probe)
    # The status poll is the sole reader of the parked login server, so it has
    # now recorded any completed login. Capture the first trusted anchor here,
    # in the same refresh, so this refresh's commit publishes the proxy pin the
    # moment the login lands instead of a poll cycle later.
    _capture_completed_token_login(runtime_type)
    account_value = _active_account_value(runtime_type, status, account)
    attested: dict[str, Any] | None = None
    attest_error: str | None = None
    if runtime_type == "claude_code" and status == "active" and account_value:
        attested, attest_error = _claude_attestation(account_value)
    deactivated = False
    became_nonactive = False
    login_to_close: str | None = None
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
            anchored = _TOKEN_ANCHORED.get(runtime_type)
            if anchored is not None:
                oauth_key = _OAUTH_KEY_BY_RUNTIME[runtime_type]
                completed_login = state.oauth_login(oauth_key, cur)
                # Grok's parked authenticate request can complete and bind the
                # first trusted anchor even when the following entitlement or
                # billing probe fails. That operator login is still spent: if
                # it remains in the database, Start login returns the completed
                # row forever instead of minting a retry. Codex completion is
                # discovered through a different path, so retain its existing
                # active-only cleanup rule.
                grok_login_completed = (
                    runtime_type == "grok"
                    and completed_login is not None
                    and _string_field(completed_login, "status") == "completed"
                )
                if status == "active" or grok_login_completed:
                    login_to_close = (
                        _string_field(completed_login, "login_id")
                        if completed_login
                        else None
                    )
                    state.set_oauth_login(cur, oauth_key, None)
            if status == "active":
                # The login record is spent (or moot) once the account is
                # active. Without this, a later session expiry would resurface
                # the stale record instead of letting the operator start a
                # fresh login.
                if anchored is not None:
                    _stamp_usage_checked_at(account_value, anchored.usage_key, utc_now())
                    anchored.save_account(account_value, cur)
                elif runtime_type == "claude_code":
                    state.set_oauth_login(cur, "claude", None)
                    save_claude_account(account_value, cur)
                else:
                    # The Bedrock account row is written once at credential
                    # submission and only cleared by a disconnect, and an
                    # unmanaged runtime has no account at all; the status
                    # refresh has nothing to store for either.
                    pass
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
    # The login flow (first login or reauth) has landed, so its parked login
    # server is done. Close the one for this login id, scoped so a login started
    # meanwhile survives. A completed Grok login is retired even when its
    # post-login provider probe failed; the immutable account anchor captured
    # from that exact flow remains available for the retry.
    anchored = _TOKEN_ANCHORED.get(runtime_type)
    if anchored is not None and login_to_close:
        anchored.close_completed_login_server(login_to_close)
    if status == "active":
        if runtime_type == "claude_code":
            _backfill_claude_usage(account_value)
    return status


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
    # Once the OAuth row commits away, any pending provider login has no
    # approval record and no useful future. Reap it after commit from every
    # deactivation path, including a policy change that races a provider probe.
    # Grok would otherwise keep polling in its named scope after xAI is off.
    after_commit.append(partial(_close_login_flow, runtime_type))
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
    oauth_key = _OAUTH_KEY_BY_RUNTIME.get(runtime_type)
    if oauth_key is None:
        return
    state.set_oauth_login(cur, oauth_key, None)


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
    for runtime_type in ("codex", "claude_code", "grok", "hermes"):
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
            host_errors.report_warning(
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
    _reset_linked_account_in_state(runtime_type)
    # The reset replaces the credential any remembered live-validation verdict
    # was about; the next login validates from scratch.
    provider_account_trust.reset_live_validation(runtime_type)
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
            _TOKEN_ANCHORED[runtime_type].save_account(None, cur)
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
        elif runtime_type == "grok":
            grok_agent.close_login_server()
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
    # Only the managed runtimes are polled. This loop exists to re-derive a
    # status that can change underneath the host — a login expiring, a token
    # rotating, a credential being revoked — and an unmanaged runtime has none
    # of that: its status comes from a constant, so a poll would re-publish an
    # unchangeable value and open an empty transaction to do it.
    # ``start_background_loops`` publishes those runtimes once instead.
    refresh_targets = ("codex", "claude_code", "grok", "hermes")
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
            host_errors.report_warning("orchestrator.runtime_status_loop", exc)
            time.sleep(RUNTIME_PENDING_RECHECK_SECONDS)
            continue
        sleep_for = min(max(0.0, due - time.monotonic()) for due in next_check_at.values())
        time.sleep(min(max(sleep_for, 0.1), RUNTIME_PENDING_RECHECK_SECONDS))


def start_background_loops() -> None:
    # Publish the unmanaged runtimes once, here, before anything slow runs.
    # This is the whole of their status lifecycle: they have no provider to
    # probe and no credential to expire, so there is nothing for the status
    # poller to re-derive later and they are deliberately absent from it.
    #
    # It happens first because the wait has a cost. A scheduled script
    # occurrence firing before this line would be admitted against a "loading"
    # status, and occurrences are attempted once by design, so it would fail
    # permanently for a runtime that is never unavailable. Taking the status
    # from the adapter keeps that module authoritative; the write is one
    # in-memory record, so it needs no database and cannot fail or wait.
    #
    # No ``agent_runtime.active`` event is recorded for these: that event marks
    # the transition into being usable, and a runtime that is usable from
    # process start never makes it.
    for runtime_type in UNMANAGED_RUNTIMES:
        status, _error, _account = harness_adapter(runtime_type).account_status()
        _set_runtime_status(runtime_type, status)
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
    adapter = harness_adapter(runtime_type)
    failure: ApiError | None = None
    server_to_interrupt: Any = None
    with turn.delivery_lock:
        if turn.phase != ExecutionPhase.RUNNING:
            raise _retryable_phase_error(turn.phase)
        if not adapter.steerable:
            label = adapter.label
            raise ApiError(
                HTTPStatus.CONFLICT,
                f"{label} cannot accept another message while running; wait for it to finish",
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
            except ProviderTurnFinishing:
                # The provider reader has already observed completion,
                # but the execution worker has not yet committed FINISHING.
                # Preserve that completion fence as a retryable phase instead
                # of recording a false terminal provider error.
                raise _retryable_phase_error(ExecutionPhase.FINISHING)
            except adapter.transport_errors as exc:
                # Once the adapter has declared RUNNING, "not ready" is no
                # longer a transient phase. It is a provider/transport failure
                # and owns the normal durable error -> FINISHING path.
                detail = str(exc)
                label = adapter.label
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
    adapter = harness_adapter(runtime_type)

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

    def finish_provider_turn(provider_session_id: str, output: str) -> int:
        """Atomically choose between a just-delivered steer and completion."""
        del output  # the final text already streamed as a thread.message event
        with turn.delivery_lock:
            # A driver using atomic completion normally consumes this counter in its event
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
        # A harness may rotate its credential independently. Converge the
        # proxy pin before its process starts; if the refresh makes the
        # runtime non-active, that transition stops this turn.
        if adapter.refresh_before_turn:
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
        try:
            new_provider_session_id, _output = adapter.run_turn(
                server,
                input_message,
                provider_session_id,
                turn.model,
                turn.effort,
                on_agent_message,
                finish_provider_turn,
            )
        except ProviderSessionLost as exc:
            if not provider_session_id:
                raise
            # The durable host thread is still valid, but this provider
            # session is known to be gone. Clear only this run's exact mapping
            # and surface the failed turn; the user's next send starts a fresh
            # session through the normal history handoff.
            with turn.delivery_lock:
                if turn.phase != ExecutionPhase.RUNNING:
                    return
                after_commit: list[Callable[[], None]] = []
                with state.mutation(after_commit=after_commit) as cur:
                    state.clear_thread_provider_session(
                        cur,
                        thread_id,
                        turn.run_number,
                        provider_session_id,
                    )
                    _record_turn_finished(
                        cur,
                        after_commit,
                        turn,
                        error_message=str(exc),
                    )
                turn.provider_session_id = None
            return
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
    # Compatibility/readability seam for account-specific code and tests.
    # Turn orchestration consumes the typed adapter directly.
    return harness_adapter(runtime_type or "codex").module


def _new_agent_server(
    runtime_type: str,
    thread_id: str,
    on_ready: Callable[[], bool],
    on_session_id: Callable[[str], None],
) -> Any:
    # Every turn runs inside a scope named after its host thread. Turns on one
    # thread are serialized, and --collect removes the scope before the same
    # unit name can be used by its next turn.
    return harness_adapter(runtime_type).new_session(
        thread_id, on_ready, on_session_id
    )


def _live_key(runtime_type: str, thread_id: Any) -> str:
    return f"{runtime_type}:{thread_id}"


def runtime_network_enabled(runtime_type: str) -> bool:
    # An unmanaged runtime gates on nothing: it holds no provider credential
    # and reaches no provider endpoint. Whatever network its work performs is
    # policed by the ordinary per-integration policy, like any agent shell
    # command, so there is no runtime-level connection to enable or disable.
    if runtime_type in UNMANAGED_RUNTIMES:
        return True
    provider = _MANAGED_PROVIDER_BY_RUNTIME.get(runtime_type)
    integrations = network_policy.load_policy().get("network_integrations", {})
    if not provider or not isinstance(integrations, dict):
        return False
    integration = integrations.get(provider)
    return isinstance(integration, dict) and integration.get("enabled") is True
