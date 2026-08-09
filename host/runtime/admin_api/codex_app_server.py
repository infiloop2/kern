"""Stdio JSON-RPC client for the Codex app-server.

App-servers are spawned through the root-owned ``run-codex-app-server`` sudo
helper, which drops to the ``kern-agent`` user and points all traffic at
the network policy proxy. Codex persists its login and threads under the agent
home, so separate processes share state: status checks and logins use
short-lived servers, and each turn runs on a fresh server that resumes
its provider thread by id.

A device-code login only completes while the app-server that started it keeps
polling, so ``start_device_login`` parks its server in ``_parked_login``. The
status poller is the sole reader of that parked server: it drives the login
forward and records the completion on the parked record for the orchestrator
to capture. The parked server lives until the orchestrator captures the
completed login, a new login starts, or an operator reset closes it. Status
probes never close it: agent-side credentials can look active while an operator
flow is still pending.

The ``account/login/completed`` notification (and ``account/read``) carry no
ChatGPT account id on this app-server protocol version; the id lives only in the
login tokens the CLI just wrote, as a provider-signed ``chatgpt_account_id``
claim. So the moment the poller first observes a completed login it reads that
id through the root ``read-codex-account-id`` helper and stores it on the
parked login record. That read happens once, at completion; later retries only
look the stored id up. Reading once is what keeps the trust tight: the
agent-writable auth file is consulted only in the narrow window right after the
CLI writes it, never re-trusted on a later retry (see
``read_completed_device_login_account_id``).

The Codex app-server initialize request includes a fixed Kern client
version. Keep this stable unless Kern intentionally changes the client
contract it expects Codex to see during app-server initialization.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import json
import queue
import re
import subprocess
import threading
import time
from typing import IO, Any, Callable

from host.runtime.admin_api import agent_activity, thread_scope
from host.runtime.core.state import read_proxy_openai_account_id

DEFAULT_COMMAND = ["/usr/bin/sudo", "-n", "/usr/local/lib/kern-host/run-codex-app-server"]
DEFAULT_ACCOUNT_ID_COMMAND = ["/usr/bin/sudo", "-n", "/usr/local/lib/kern-host/read-codex-account-id"]
AGENT_CWD = "/mnt/kern-agent/agent-home"
ACCOUNT_ID_HELPER_TIMEOUT_SECONDS = 10
CLIENT_VERSION = "v1.0"
# Under the orchestrator's five-minute active recheck, so a scheduled recheck
# always revalidates, while the five-second pending poll never becomes a
# provider-traffic loop.
LIVE_VALIDATION_RETRY_SECONDS = 240
COMMAND_OUTPUT_EMIT_SECONDS = 0.75
COMMAND_OUTPUT_EMIT_BYTES = 24 * 1024
COMMAND_OUTPUT_MAX_EVENTS = 80
CODEX_STEER_TIMEOUT_SECONDS = 30
PROCESS_EXIT_TIMEOUT_SECONDS = 3


@dataclass
class _ParkedLogin:
    """The single parked device-login flow: its polling app-server, the login
    id it serves, and — once the poller observes completion — the trusted
    account id read at that moment (None records a failed read, which fails
    closed at capture)."""

    server: "CodexAppServer"
    login_id: str
    completed: bool = field(default=False)
    account_id: str | None = field(default=None)


_parked_login: _ParkedLogin | None = None
_login_lock = threading.Lock()

# The last live credential-validation failure: (status, error_message, recorded
# monotonic time). An awaiting_login verdict is final until an operator login
# completes or the linked account is reset; any other failure is retried after
# LIVE_VALIDATION_RETRY_SECONDS. In-memory on purpose: a restart revalidates
# once from scratch.
_live_validation_failure: tuple[str, str | None, float] | None = None


class CodexAppServerError(RuntimeError):
    pass


class CodexTimeout(CodexAppServerError):
    pass


class CodexTurnFinishing(CodexAppServerError):
    """A steer arrived after Codex published this turn's completion."""


@dataclass(frozen=True)
class CodexLogin:
    login_id: str
    verification_url: str
    user_code: str


class CodexAppServer:
    """One app-server transport.

    Ordinary calls and notification reads share one response consumer. A
    synchronous ``turn/steer`` has a dedicated response waiter fed directly by
    the stdout reader, so activity processing cannot starve its acknowledgement.
    ``interrupt()`` is the prompt, non-blocking stop request; the owning
    execution thread later calls ``close()`` for authoritative process/scope
    cleanup.
    """

    def __init__(
        self,
        command: list[str] | None = None,
        thread_id: str | None = None,
        on_ready: Callable[[], bool] | None = None,
        on_session_id: Callable[[str], None] | None = None,
    ) -> None:
        self._command = command or DEFAULT_COMMAND
        # Kept for the kill path: close() stops this thread's systemd scope by
        # name. The launcher folds the id into _command below as the run flag.
        self._thread_id = thread_id
        self._on_ready = on_ready
        self._on_session_id = on_session_id
        # run_turn sets this as soon as the Codex threadId for this turn is
        # known — well before turn/start, let alone turn/completed — so a
        # kill (which surfaces run_turn's call()/read_message() as an
        # exception, discarding its locals) still leaves the orchestrator
        # able to read it and persist the thread mapping.
        self.last_known_session_id: str | None = None
        # Turns run inside a systemd scope named after the host thread:
        # the helper consumes this pair and turns it into systemd-run --unit,
        # Non-turn servers (status probes and logins) pass no thread id and
        # keep systemd's generated scope name.
        if thread_id is not None:
            self._command = [*self._command, "--thread-scope", thread_id]
        self._next_id = 1
        self._proc: subprocess.Popen[str] | None = None
        self._lifecycle_lock = threading.Lock()
        self._closed = False
        self._messages: queue.Queue[dict[str, Any]] = queue.Queue()
        self._pending: deque[dict[str, Any]] = deque()
        self._stderr_tail: deque[str] = deque(maxlen=20)
        self._io_lock = threading.RLock()
        self._stdin_lock = threading.Lock()
        self._steer_lock = threading.Lock()
        self._response_waiters_lock = threading.Lock()
        self._response_waiters: dict[int, queue.Queue[dict[str, Any]]] = {}
        # The stdout reader publishes this fence as soon as it observes
        # turn/completed. It remains set until this one-turn server is closed,
        # spanning the gap before the orchestrator durably records FINISHING.
        self._turn_completion_pending = threading.Event()
        self._active_thread_id: str | None = None
        self._active_turn_id: str | None = None

    def __enter__(self) -> "CodexAppServer":
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def start(self, init_timeout: float = 60.0) -> None:
        with self._lifecycle_lock:
            if self._closed:
                raise CodexAppServerError("Codex app-server was interrupted before startup")
            try:
                self._proc = subprocess.Popen(
                    self._command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            except OSError as exc:
                raise CodexAppServerError(f"failed to start Codex app-server command: {exc}") from exc
        assert self._proc.stdout is not None and self._proc.stderr is not None
        threading.Thread(target=self._read_stdout, args=(self._proc.stdout,), daemon=True).start()
        threading.Thread(target=self._read_stderr, args=(self._proc.stderr,), daemon=True).start()
        self.call("initialize", _client_info(), timeout=init_timeout)
        self.notify("initialized")

    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def close(self) -> None:
        with self._lifecycle_lock:
            self._closed = True
            proc = self._proc
        try:
            if proc is not None:
                # Closing stdin signals EOF, the app-server's normal shutdown path.
                # This is the reliable lever: the process is spawned through sudo and
                # may run as root/agent, so an unprivileged signal can fail with
                # EPERM — the kill below is a best-effort fallback only.
                if proc.stdin is not None:
                    try:
                        proc.stdin.close()
                    except OSError:
                        pass
                try:
                    proc.wait(timeout=PROCESS_EXIT_TIMEOUT_SECONDS)
                except subprocess.TimeoutExpired:
                    # Best-effort signal only: the production launcher runs as root,
                    # so this unprivileged kill fails with EPERM and the root scope
                    # teardown below is the real kill; a same-user command (tests)
                    # just dies here. A signal failure must never escape close() —
                    # the orchestrator keeps a thread fenced when close() raises.
                    try:
                        proc.kill()
                    except OSError:
                        pass
                    # The privileged scope teardown below owns the bounded,
                    # authoritative reap for the production root launcher.
                # stdout and stderr are deliberately not closed here: each reader
                # thread owns its pipe and releases it at EOF. A buffered stream's
                # close() blocks on the same lock the reader holds across its
                # blocking read, so closing them from this thread would hang for
                # as long as a process that outlived stdin EOF stays quiet on that
                # pipe — which is exactly when this teardown matters.
        finally:
            # Last resort after the app-server's clean stdin-EOF shutdown above: the
            # child reaping lives in the harness, but freeing the thread is the
            # host's invariant, so guarantee the scope cgroup is gone even if a child
            # outlived it. A clean shutdown already emptied it, so this is then a
            # no-op — the server is never killed abruptly ahead of its own shutdown.
            # It runs from a finally because it is the only kill that reaches a
            # process this unprivileged user cannot signal, and the orchestrator's
            # thread fence is only lifted once close() has run it.
            thread_scope.stop_thread_scope(self._thread_id, self._command, DEFAULT_COMMAND)

    def interrupt(self) -> None:
        """Interrupt a turn without waiting for process/scope teardown."""
        with self._lifecycle_lock:
            self._closed = True
            proc = self._proc
            if proc is not None and proc.stdin is not None:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
        thread_scope.interrupt_thread_scope(self._thread_id, self._command, DEFAULT_COMMAND)

    def _read_stdout(self, stream: IO[str]) -> None:
        # The reader owns its pipe: close() must not close it from another
        # thread (that blocks on the buffer lock this loop holds across every
        # read), so the stream is released here once the loop reaches EOF.
        with stream:
            for line in stream:
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(message, dict):
                    if message.get("method") == "turn/completed":
                        self._turn_completion_pending.set()
                    request_id = message.get("id")
                    waiter = None
                    if isinstance(request_id, int):
                        with self._response_waiters_lock:
                            waiter = self._response_waiters.get(request_id)
                    if waiter is not None:
                        try:
                            waiter.put_nowait(message)
                        except queue.Full:
                            pass
                        else:
                            continue
                    self._messages.put(message)

    def _read_stderr(self, stream: IO[str]) -> None:
        # Drain stderr (so the child never blocks on a full pipe) and keep the
        # tail for error reporting.
        with stream:
            for line in stream:
                stripped = line.strip()
                if stripped:
                    self._stderr_tail.append(stripped)

    def stderr_tail(self) -> str:
        return "\n".join(self._stderr_tail)

    def notify(self, method: str) -> None:
        with self._stdin_lock:
            proc = self._require_proc()
            assert proc.stdin is not None
            proc.stdin.write(json.dumps({"method": method}) + "\n")
            proc.stdin.flush()

    def call(self, method: str, params: dict[str, Any], *, timeout: float = 60.0) -> Any:
        with self._io_lock:
            with self._stdin_lock:
                request_id = self._write_request_locked(method, params)
            # Notifications that arrive before our response are kept for
            # read_message(). The I/O lock gives exactly one consumer
            # ownership of both queues while it correlates this response.
            while True:
                message = self._next_message(timeout)
                if message.get("id") != request_id:
                    self._pending.append(message)
                    continue
                if "error" in message:
                    raise CodexAppServerError(
                        message["error"].get("message", "Codex app-server request failed")
                    )
                return message.get("result")

    def read_message(self, *, timeout: float = 60.0) -> dict[str, Any]:
        with self._io_lock:
            if self._pending:
                return self._pending.popleft()
            return self._next_message(timeout)

    def set_active_turn(self, thread_id: str, turn_id: str) -> None:
        with self._steer_lock:
            with self._stdin_lock:
                self._active_thread_id = thread_id
                self._active_turn_id = turn_id

    def clear_active_turn(self) -> None:
        # Completion waits for a steer acknowledgement already in flight.
        # This preserves the original provider-ack-before-completion boundary
        # without making the steer compete with notification reads.
        with self._steer_lock:
            with self._stdin_lock:
                self._active_thread_id = None
                self._active_turn_id = None

    def steer(self, message: str) -> None:
        """Synchronously hand one message to the active Codex turn.

        A successful return is Codex's JSON-RPC acknowledgement. The caller
        deliberately records history only after this method returns.
        """
        with self._steer_lock:
            with self._stdin_lock:
                if self._turn_completion_pending.is_set():
                    raise CodexTurnFinishing("Codex turn is finishing")
                if self._active_thread_id is None or self._active_turn_id is None:
                    raise CodexAppServerError("Codex turn is not ready for steering")
                waiter: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
                request_id = self._next_id
                self._next_id += 1
                with self._response_waiters_lock:
                    self._response_waiters[request_id] = waiter
                try:
                    self._write_request_with_id_locked(
                        request_id,
                        "turn/steer",
                        {
                            "threadId": self._active_thread_id,
                            "expectedTurnId": self._active_turn_id,
                            "input": [{"type": "text", "text": message}],
                        },
                    )
                except OSError as exc:
                    with self._response_waiters_lock:
                        self._response_waiters.pop(request_id, None)
                    raise CodexAppServerError(f"Codex rejected the message: {exc}") from exc
                except Exception:
                    with self._response_waiters_lock:
                        self._response_waiters.pop(request_id, None)
                    raise
            try:
                response = self._wait_for_direct_response(
                    waiter,
                    timeout=CODEX_STEER_TIMEOUT_SECONDS,
                )
            finally:
                with self._response_waiters_lock:
                    self._response_waiters.pop(request_id, None)
            if "error" in response:
                if self._turn_completion_pending.is_set():
                    raise CodexTurnFinishing("Codex turn is finishing")
                error = response.get("error")
                detail = (
                    error.get("message", "Codex app-server request failed")
                    if isinstance(error, dict)
                    else "Codex app-server request failed"
                )
                raise CodexAppServerError(detail)

    def _write_request_locked(self, method: str, params: dict[str, Any]) -> int:
        """Allocate and write one request while the caller owns `_stdin_lock`."""
        request_id = self._next_id
        self._next_id += 1
        self._write_request_with_id_locked(request_id, method, params)
        return request_id

    def _write_request_with_id_locked(
        self,
        request_id: int,
        method: str,
        params: dict[str, Any],
    ) -> None:
        proc = self._require_proc()
        assert proc.stdin is not None
        proc.stdin.write(
            json.dumps({"id": request_id, "method": method, "params": params}) + "\n"
        )
        proc.stdin.flush()

    def _wait_for_direct_response(
        self,
        waiter: queue.Queue[dict[str, Any]],
        *,
        timeout: float,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._require_proc()
                raise CodexTimeout("timed out waiting for Codex app-server")
            try:
                return waiter.get(timeout=min(0.25, remaining))
            except queue.Empty:
                self._require_proc()

    def collect_completed_logins(self) -> set[str]:
        """Consume successful account/login/completed notifications, returning the
        login ids that completed. The notification carries no account id, so the
        trusted id is read separately (see read_completed_device_login_account_id)."""
        with self._io_lock:
            completed: set[str] = set()
            pending: deque[dict[str, Any]] = deque()
            while self._pending:
                message = self._pending.popleft()
                if message.get("method") == "account/login/completed":
                    params = message.get("params")
                    if isinstance(params, dict) and params.get("success") is True:
                        login_id = params.get("loginId")
                        if isinstance(login_id, str) and login_id:
                            completed.add(login_id)
                    continue
                pending.append(message)
            self._pending = pending
            return completed

    def _next_message(self, timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._require_proc()
                raise CodexTimeout("timed out waiting for Codex app-server")
            try:
                return self._messages.get(timeout=min(0.25, remaining))
            except queue.Empty:
                self._require_proc()

    def _require_proc(self) -> subprocess.Popen[str]:
        proc = self._proc
        if proc is None:
            raise CodexAppServerError("Codex app-server was not started")
        returncode = proc.poll()
        if returncode is not None:
            raise CodexAppServerError(f"Codex app-server exited with status {returncode}")
        return proc


def _client_info() -> dict[str, dict[str, str]]:
    return {"clientInfo": {"name": "kern-host", "version": CLIENT_VERSION}}


def account_status(*, force_provider_probe: bool = False) -> tuple[str, str | None, dict[str, Any] | None]:
    """Return (status, detail, account metadata). detail is set only for "error"."""
    # Bounded timeouts: only the background poller calls this, but a Codex
    # app-server that cannot start (e.g. its startup traffic is denied by a
    # restrictive policy) must not wedge the poller — it resolves to "error"
    # with a detail until conditions improve. The init timeout leaves room for
    # a cold Node start on a small instance.
    login_server = _current_login_server()
    if login_server is not None:
        return _login_server_status(login_server, force_provider_probe=force_provider_probe)

    server = CodexAppServer()
    try:
        server.start(init_timeout=45)
        return _account_status_from_server(server, force_provider_probe=force_provider_probe)
    except CodexAppServerError as exc:
        return _codex_status_error(exc, server)
    finally:
        server.close()


def _current_login_server() -> "CodexAppServer | None":
    with _login_lock:
        parked = _parked_login
    if parked is None:
        return None
    if parked.server.alive():
        return parked.server
    dead_server = _pop_parked(lambda p: p.server is parked.server)
    if dead_server is not None:
        dead_server.close()
    return None


def _login_server_status(
    server: "CodexAppServer", *, force_provider_probe: bool = False
) -> tuple[str, str | None, dict[str, Any] | None]:
    # The status poller is the only reader of the parked login server, so it also
    # drains the account/login/completed notifications that
    # read_completed_device_login_account_id later looks up. collect is
    # destructive, so record whatever completed before returning.
    status = _account_status_from_server(server, force_provider_probe=force_provider_probe)
    completed = server.collect_completed_logins()
    if completed:
        # Fresh credentials were just written, so the remembered verdict about
        # the previous credential no longer applies; revalidate from scratch.
        clear_live_validation_failure()
        # Capture the trusted account id now, at the moment completion is first
        # observed, so an agent that later swaps the (agent-writable) auth file
        # cannot get a different account anchored under the operator-approved
        # login id on a retry. A miss is recorded as None and fails closed at
        # capture, so the operator re-logs in rather than trusting whatever
        # tokens appear on a later cycle.
        try:
            account_id = read_codex_account_id()
        except CodexAppServerError:
            account_id = None
        with _login_lock:
            parked = _parked_login
            if parked is not None and parked.server is server and parked.login_id in completed:
                parked.completed = True
                parked.account_id = account_id
    return status


def _account_status_from_server(
    server: "CodexAppServer", *, force_provider_probe: bool = False
) -> tuple[str, str | None, dict[str, Any] | None]:
    try:
        result = server.call("account/read", {"refreshToken": False}, timeout=15)
        if not isinstance(result, dict):
            raise CodexAppServerError("Codex account/read returned invalid result")
        account = result.get("account")
        if account:
            account_id = read_codex_account_id()
            if account_id:
                account_metadata = _safe_account_metadata(account if isinstance(account, dict) else {})
                account_metadata["account_id"] = account_id
                try:
                    rate_limits = _safe_rate_limits_metadata(server.call("account/rateLimits/read", {}, timeout=15))
                except CodexAppServerError:
                    # An account without a proxy pin (agent-side credentials
                    # awaiting operator approval) cannot reach the guarded
                    # usage endpoint; that still classifies as a readable
                    # account, never a forced refresh: unpinned credentials
                    # settle through the operator-approval flow instead.
                    if _live_validation_failure is None and read_proxy_openai_account_id() is None:
                        rate_limits = {}
                    else:
                        return _validated_status_after_usage_failure(
                            server, account_id, force_provider_probe=force_provider_probe
                        )
                if rate_limits:
                    account_metadata["codex_usage"] = rate_limits
                return "active", None, account_metadata
            raise CodexAppServerError("Codex account/read returned an account without a supported account id")
        return "awaiting_login", None, None
    except CodexAppServerError as exc:
        return _codex_status_error(exc, server)


def _validated_status_after_usage_failure(
    server: "CodexAppServer", account_id: str, *, force_provider_probe: bool = False
) -> tuple[str, str | None, dict[str, Any] | None]:
    """Validate a pinned credential whose live usage read failed.

    The rate-limit read authenticates live, so its failure can mean the cached
    credential is stale. Ask Codex, which owns the refresh token, to validate
    or refresh through the unpinned auth endpoint before reporting connected.
    The verdict is remembered: automatic checks keep an authentication failure
    at awaiting_login until an operator login completes or the linked account
    is reset, and retry any other failure at most every
    LIVE_VALIDATION_RETRY_SECONDS. An explicit operator refresh bypasses that
    memory. Without it the five-second non-active poll would force a token
    refresh on every cycle."""
    global _live_validation_failure
    failure = _live_validation_failure
    if not force_provider_probe and failure is not None and (
        failure[0] == "awaiting_login" or time.monotonic() - failure[2] < LIVE_VALIDATION_RETRY_SECONDS
    ):
        return failure[0], failure[1], None
    try:
        refreshed = server.call("account/read", {"refreshToken": True}, timeout=15)
        if not isinstance(refreshed, dict):
            raise CodexAppServerError("Codex refreshed account/read returned invalid result")
        refreshed_account = refreshed.get("account")
        if not refreshed_account:
            _live_validation_failure = ("awaiting_login", None, time.monotonic())
            return "awaiting_login", None, None
        refreshed_account_id = read_codex_account_id()
        if not refreshed_account_id:
            raise CodexAppServerError(
                "Codex refreshed account/read returned an account without a supported account id"
            )
        if refreshed_account_id != account_id:
            raise CodexAppServerError("Codex account changed during credential refresh")
    except CodexAppServerError as exc:
        status, error_message, account = _codex_status_error(exc, server)
        _live_validation_failure = (status, error_message, time.monotonic())
        return status, error_message, account
    _live_validation_failure = None
    account_metadata = _safe_account_metadata(refreshed_account if isinstance(refreshed_account, dict) else {})
    account_metadata["account_id"] = refreshed_account_id
    return "active", None, account_metadata


def clear_live_validation_failure() -> None:
    """Forget the remembered live-validation verdict. Called when an operator
    login completes or the linked account is reset: both replace the credential
    the verdict was about."""
    global _live_validation_failure
    _live_validation_failure = None


def _codex_status_error(
    exc: CodexAppServerError,
    server: "CodexAppServer",
) -> tuple[str, str | None, dict[str, Any] | None]:
    message = str(exc).lower()
    # Only treat specific "not logged in" phrasings as awaiting_login, so a
    # real failure that merely mentions auth infrastructure (e.g. "could not
    # reach auth.openai.com", "authorization server unreachable") surfaces as
    # an error with its detail instead of an impossible login prompt.
    login_markers = ("not logged in", "logged out", "login required", "must log in",
                     "no account", "unauthorized", "401")
    if any(marker in message for marker in login_markers):
        return "awaiting_login", None, None
    return "error", _error_detail(str(exc), server.stderr_tail()), None


def _error_detail(message: str, stderr: str) -> str:
    if not stderr:
        return message
    if stderr in message:
        return message
    return f"{message}; app-server stderr: {stderr}"


def _safe_account_metadata(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    metadata: dict[str, Any] = {}
    email = _string_field(value, "email")
    if email:
        metadata["email"] = email
    plan_type = _string_field(value, "planType")
    if plan_type:
        metadata["plan_type"] = plan_type
    return metadata


def _string_field(value: dict[str, Any], key: str) -> str | None:
    item = value.get(key)
    if not isinstance(item, str):
        return None
    return item.strip() or None


# Only these scalar fields, per snapshot section, survive into metadata.
_RATE_LIMIT_WINDOW_FIELDS = (
    ("usedPercent", "used_percent"),
    ("windowDurationMins", "window_duration_mins"),
    ("resetsAt", "resets_at"),
)
_RATE_LIMIT_SECTIONS = (
    ("primary", _RATE_LIMIT_WINDOW_FIELDS),
    ("secondary", _RATE_LIMIT_WINDOW_FIELDS),
    ("credits", (("hasCredits", "has_credits"), ("unlimited", "unlimited"), ("balance", "balance"))),
)


def _safe_rate_limits_metadata(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    snapshot = value.get("rateLimits")
    rate_limits: dict[str, Any] = {}
    for key, fields in _RATE_LIMIT_SECTIONS:
        section = snapshot.get(key) if isinstance(snapshot, dict) else None
        if not isinstance(section, dict):
            continue
        safe: dict[str, Any] = {}
        for source_key, target_key in fields:
            item = section.get(source_key)
            if isinstance(item, str):
                item = item.strip() or None
            elif not isinstance(item, (bool, int, float)):
                item = None
            if item is not None:
                safe[target_key] = item
        if safe:
            rate_limits[key] = safe
    return {"rate_limits": rate_limits} if rate_limits else {}


def read_codex_account_id(command: list[str] | None = None) -> str | None:
    try:
        proc = subprocess.run(
            command or DEFAULT_ACCOUNT_ID_COMMAND,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=ACCOUNT_ID_HELPER_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CodexAppServerError(f"could not read Codex account id: {exc}") from exc
    if proc.returncode != 0:
        return None
    account_id = proc.stdout.strip()
    return account_id or None


def start_device_login() -> CodexLogin:
    global _parked_login
    server = CodexAppServer()
    server.start()
    try:
        result = server.call("account/login/start", {"type": "chatgptDeviceCode"}, timeout=30)
        if result.get("type") != "chatgptDeviceCode":
            raise CodexAppServerError("Codex did not return a device-code login flow")
    except BaseException:
        server.close()
        raise
    with _login_lock:
        old = _parked_login
        _parked_login = _ParkedLogin(server=server, login_id=result["loginId"])
    if old is not None:
        old.server.close()
    return CodexLogin(
        login_id=result["loginId"],
        verification_url=result["verificationUrl"],
        user_code=result["userCode"],
    )


def _pop_parked(match: Callable[[_ParkedLogin], bool]) -> "CodexAppServer | None":
    """Unpark and return the login server if the parked record matches, else
    None. The single record plus this one unpark path keeps the parked-login
    invariant structural: there is never more than one, and every close first
    proves it is closing the record it meant to."""
    global _parked_login
    with _login_lock:
        parked = _parked_login
        if parked is None or not match(parked):
            return None
        _parked_login = None
    return parked.server


def read_completed_device_login_account_id(login_id: str) -> str | None:
    """Return the completed operator device login's account id.

    A stored OAuth row means the operator saw a device code, not that the login
    completed. First-account capture therefore requires the successful
    account/login/completed notification for that exact login id, observed by the
    status poller on the parked login server. That notification carries no
    account id (nor does account/read) on this app-server protocol version, so
    the poller reads it through the root helper (the provider-signed
    chatgpt_account_id claim) the instant it first sees the completion and stores
    it on the parked record. This is a pure lookup of that captured id: a
    completion whose id read missed is stored as None and fails closed here, so a
    later agent swap of the auth file is never trusted. The residual swap window
    (between the CLI writing auth.json and the poller's capture) matches the
    Claude first-capture path, and the linked account is shown to the operator
    once pinned.
    """
    with _login_lock:
        parked = _parked_login
        if parked is None or parked.login_id != login_id or not parked.completed:
            return None
        account_id = parked.account_id
    if not account_id:
        raise CodexAppServerError("Codex completed login did not include a supported account id")
    return account_id


def close_login_server() -> None:
    server = _pop_parked(lambda parked: True)
    if server is not None:
        server.close()


def close_completed_login_server(login_id: str) -> None:
    """Close the parked login server for a captured login, unless a newer login
    has replaced it under a different login id."""
    server = _pop_parked(lambda parked: parked.login_id == login_id)
    if server is not None:
        server.close()


def run_turn(
    server: CodexAppServer,
    input_message: str,
    thread_id: str | None,
    model: str,
    effort: str,
    on_message: Callable[[str | dict[str, Any]], None],
) -> tuple[str, str]:
    """Run one turn to completion, emitting completed agent messages.

    Mid-turn input is sent synchronously through ``server.steer()`` by the
    request that submitted it; this driver owns only provider notifications.
    """
    if thread_id:
        try:
            thread = server.call(
                "thread/resume",
                # Codex 0.144.0 exposes effort only on turn/start; its
                # thread/resume schema accepts the sticky model and refreshed
                # developer instructions, but no effort.
                {
                    "threadId": thread_id,
                    "cwd": AGENT_CWD,
                    "model": model,
                    "developerInstructions": _developer_instructions(),
                },
                timeout=30,
            )["thread"]
        except CodexAppServerError:
            thread = _start_thread(server, model)
    else:
        thread = _start_thread(server, model)
    thread_id = str(thread["id"])
    turn = server.call(
        "turn/start",
        {
            "threadId": thread_id,
            "input": [{"type": "text", "text": input_message}],
            "model": model,
            "effort": effort,
        },
        timeout=30,
    )["turn"]
    # Only now, once the thread has accepted this turn's input, is the id worth
    # remembering. A resume that fell back to _start_thread above holds a brand
    # new empty thread, and the caller persists this attribute when a turn dies
    # — publishing it any earlier would let a turn/start failure (a rate limit,
    # an auth error) overwrite the thread's real history with an empty
    # conversation. A turn that never started has nothing worth resuming, so
    # leaving the previous mapping untouched is always the better trade.
    server.last_known_session_id = thread_id
    turn_id = turn["id"]
    server.set_active_turn(thread_id, turn_id)
    if server._on_session_id is not None:
        server._on_session_id(thread_id)
    if server._on_ready is not None and not server._on_ready():
        raise CodexAppServerError("Codex execution stopped during startup")
    current_parts: list[str] = []
    command_output_parts: dict[str, list[str]] = {}
    command_output_last_emit: dict[str, float] = {}
    command_output_emit_count: dict[str, int] = {}
    command_output_capped: set[str] = set()
    last_message = ""

    def emit_command_output(item_id: str, now: float) -> None:
        parts = command_output_parts.get(item_id) or []
        pending_output = "".join(parts)
        command_output_parts[item_id] = []
        if not pending_output or item_id in command_output_capped:
            return
        count = command_output_emit_count.get(item_id, 0)
        if count >= COMMAND_OUTPUT_MAX_EVENTS:
            output = agent_activity.TRUNCATION_SUFFIX
            command_output_capped.add(item_id)
        else:
            output = agent_activity.clip_text(
                pending_output,
                COMMAND_OUTPUT_EMIT_BYTES,
            )
            command_output_emit_count[item_id] = count + 1
        update = agent_activity.activity(
            "codex",
            item_id,
            "command",
            "started",
            "Command output",
            output=output,
            status="running",
        )
        update["append_output"] = True
        on_message(update)
        command_output_last_emit[item_id] = now

    while True:
        try:
            # There is no overall turn deadline; a stuck turn is abandoned
            # through POST /v1/threads/{thread_id}/stop.
            # Keep the ownership slice short so a synchronous steer request
            # can acquire the transport promptly.
            message = server.read_message(timeout=1.0)
        except CodexTimeout:
            continue
        if not isinstance(message, dict):
            continue
        method = message.get("method")
        params = message.get("params", {})
        if not isinstance(params, dict):
            continue
        if method == "item/agentMessage/delta":
            delta = params.get("delta")
            if isinstance(delta, str) and delta:
                current_parts.append(agent_activity.clean_text(delta))
        elif method == "item/commandExecution/outputDelta":
            item_id = str(params.get("itemId") or params.get("item_id") or "")
            delta = params.get("delta")
            if (
                item_id
                and isinstance(delta, str)
                and delta
                and item_id not in command_output_capped
            ):
                delta = agent_activity.clean_text(delta)
                parts = command_output_parts.setdefault(item_id, [])
                parts.append(delta)
                pending_output = "".join(parts)
                now = time.monotonic()
                if (
                    command_output_emit_count.get(item_id, 0)
                    >= COMMAND_OUTPUT_MAX_EVENTS
                    or item_id not in command_output_last_emit
                    or now - command_output_last_emit[item_id] >= COMMAND_OUTPUT_EMIT_SECONDS
                    or len(pending_output.encode()) >= COMMAND_OUTPUT_EMIT_BYTES
                ):
                    emit_command_output(item_id, now)
        elif method == "item/started":
            item = params.get("item", {})
            rich_activity = _codex_item_activity(item, "started")
            if rich_activity is not None:
                on_message(rich_activity)
        elif method == "item/completed":
            item = params.get("item", {})
            if not isinstance(item, dict):
                continue
            if item.get("type") == "agentMessage":
                item_text = item.get("text")
                last_message = (
                    agent_activity.clean_text(item_text)
                    if isinstance(item_text, str) and item_text
                    else "".join(current_parts)
                )
                current_parts = []
                if last_message:
                    on_message(last_message)
            else:
                item_id = str(item.get("id") or "")
                streamed_output = (
                    item_id in command_output_emit_count
                    or bool(command_output_parts.get(item_id))
                )
                if item_id and command_output_parts.get(item_id):
                    emit_command_output(item_id, time.monotonic())
                rich_activity = _codex_item_activity(item, "completed")
                if rich_activity is not None:
                    # Streaming already persisted this command output under the
                    # same activity id. Do not store the aggregated duplicate;
                    # Agent Chat keeps the accumulated output when the
                    # completion snapshot omits the field.
                    if streamed_output:
                        rich_activity.pop("output", None)
                    on_message(rich_activity)
        elif method == "turn/completed":
            for item_id in list(command_output_parts):
                if command_output_parts[item_id]:
                    emit_command_output(item_id, time.monotonic())
            turn = params.get("turn", {})
            if not isinstance(turn, dict):
                continue
            if turn.get("status") == "completed":
                # A few Codex completion paths end after delta notifications
                # without a matching item/completed. Flush that final message
                # through the same callback as every ordinary agent message;
                # the return value alone is not part of durable chat history.
                pending_message = "".join(current_parts)
                if pending_message:
                    last_message = pending_message
                    on_message(pending_message)
                server.clear_active_turn()
                return thread_id, last_message or "Task completed."
            error = turn.get("error") or {}
            if not isinstance(error, dict):
                error = {}
            server.clear_active_turn()
            raise CodexAppServerError(error.get("message", "Codex turn failed"))


def _codex_item_activity(item: Any, phase: str) -> dict[str, Any] | None:
    """Fail-soft boundary for provider-owned ThreadItem payloads."""
    try:
        return _codex_item_activity_unchecked(item, phase)
    except Exception:
        return None


def _codex_item_activity_unchecked(item: Any, phase: str) -> dict[str, Any] | None:
    """Normalize Codex ThreadItems without coupling Agent Chat to its schema."""
    if not isinstance(item, dict):
        return None
    item_type = str(item.get("type") or "")
    if item_type in {"", "userMessage", "hookPrompt", "agentMessage"}:
        return None
    activity_id = str(item.get("id") or f"{item_type}:{id(item)}")
    status = item.get("status")
    title = item_type
    kind = "status"
    detail: Any = None
    output: Any = None

    if item_type == "reasoning":
        kind, title = "reasoning", "Reasoning"
        detail = item.get("summary") or item.get("content")
    elif item_type == "plan":
        kind, title = "plan", "Plan"
        detail = item.get("text") or item.get("plan")
    elif item_type == "commandExecution":
        kind = "command"
        command = item.get("command") or item.get("commandLine") or "Command"
        title = agent_activity.clip_text(command, agent_activity.ACTIVITY_SHORT_TEXT_BYTES)
        cwd = item.get("cwd")
        detail = f"Working directory: {cwd}" if cwd else None
        output = item.get("aggregatedOutput") or item.get("output")
        if item.get("exitCode") is not None:
            status = f"exit {item['exitCode']}"
    elif item_type == "fileChange":
        kind, title = "file_change", "File changes"
        changes = item.get("changes") or item.get("files")
        detail = agent_activity.json_text(changes) if changes is not None else item.get("diff")
    elif item_type in {"mcpToolCall", "dynamicToolCall"}:
        kind = "tool"
        tool_name = item.get("tool") or item.get("name") or item.get("server") or "Tool"
        title = f"Tool: {tool_name}"
        arguments = item.get("arguments") or item.get("input")
        detail = agent_activity.json_text(arguments) if arguments is not None else None
        result = item.get("result") or item.get("output") or item.get("error")
        output = agent_activity.json_text(result) if result is not None else None
    elif item_type in {"collabAgentToolCall", "subAgentActivity"}:
        kind, title = "agent", "Sub-agent activity"
        detail = agent_activity.json_text(
            item.get("prompt") or item.get("receivers") or item.get("agents") or item
        )
    elif item_type == "webSearch":
        kind, title = "search", "Web search"
        detail = item.get("query") or item.get("queries")
    elif item_type == "imageView":
        kind, title = "image", "Viewed image"
        detail = item.get("path")
    elif item_type == "imageGeneration":
        kind, title = "image", "Generated image"
        detail = item.get("prompt")
        output = item.get("path") or item.get("output")
    elif item_type == "sleep":
        kind, title = "wait", "Waiting"
        detail = item.get("reason") or item.get("duration")
    elif item_type == "contextCompaction":
        kind, title = "status", "Compacted context"
    elif item_type == "enteredReviewMode":
        kind, title = "status", "Entered review mode"
    elif item_type == "exitedReviewMode":
        kind, title = "status", "Exited review mode"
    else:
        title = re.sub(r"(?<!^)(?=[A-Z])", " ", item_type).capitalize()
        detail = agent_activity.json_text(item)

    if isinstance(detail, (dict, list)):
        detail = agent_activity.json_text(detail)
    return agent_activity.activity(
        "codex",
        activity_id,
        kind,
        phase,
        title,
        detail=detail,
        output=output,
        status=str(status) if status is not None else None,
    )


def _start_thread(server: CodexAppServer, model: str) -> dict[str, Any]:
    return server.call(
        "thread/start",
        {
            "cwd": AGENT_CWD,
            "approvalPolicy": "never",
            "sandbox": "danger-full-access",
            # Effort is a turn/start field in the pinned app-server protocol,
            # not a thread/start field.
            "model": model,
            "developerInstructions": _developer_instructions(),
        },
        timeout=30,
    )["thread"]


def _developer_instructions() -> str:
    """Current host contract, refreshed on start and every resume."""
    return (
        "You are running inside Kern. Complete the operator task and "
        "return a concise final result."
    )
