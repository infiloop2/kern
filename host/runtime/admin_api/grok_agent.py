"""ACP (JSON-RPC over stdio) client for the Grok Build agent server.

Agent servers are spawned through the root-owned ``run-grok`` sudo helper,
which drops to the ``kern-agent`` user and points all traffic at the network
policy proxy. Grok persists its login and sessions under the agent home, so
separate processes share state: status checks and logins use short-lived
servers, and each turn will run on a fresh server that resumes its provider
session by id.

The same transport owns both the provider connection and one interactive turn.
Each Kern turn starts a fresh agent server, creates or loads the persisted ACP
session, submits one prompt, maps streamed ``session/update`` notifications to
Kern messages/activity, and accepts live steering through ``_x.ai/interject``.

Three things differ from the Codex app-server this is otherwise modelled on:

**The protocol is bidirectional.** ACP lets the agent call *the client* —
permission prompts, file reads, terminal control. Kern declares none of those
capabilities (the agent already has a shell on this host as ``kern-agent``, so
a client-side file API would only add a second, weaker path to the same files),
and an unanswered request would wedge the agent behind a reply that never
comes. Every inbound request is therefore answered with a JSON-RPC
``method not found`` rather than dropped.

**The login is a long-running request.** ACP ``authenticate`` does not return
until the operator finishes in a browser, so it cannot be awaited on the
request path. ``start_device_login`` writes it without waiting, reads the URL
to display from ``_x.ai/auth/get_url``, and parks the server; the status poller
is the sole reader of that parked server and observes the ``authenticate``
response whenever it lands. The parked server lives until the orchestrator
captures the completed login, a new login starts, or an operator reset closes
it. Status probes never close it: agent-side credentials can look active while
an operator flow is still pending.

**Entitlement is not the pinned value.** Build data-plane access can require the
account to belong to an xAI console team, which the account pin does not cover.
A ``permission-denied`` verdict is therefore classified as runtime ``error``
carrying the provider message, never ``awaiting_login`` — a fresh login cannot
fix an entitlement problem, and routing the operator into one is a dead end.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
import json
import queue
import secrets
import subprocess
import threading
import time
from typing import IO, Any, Callable, NoReturn
from urllib.parse import parse_qs, urlsplit

from host.runtime.admin_api import agent_activity, thread_scope

PRODUCTION_COMMAND = ["/usr/bin/sudo", "-n", "/usr/local/lib/kern-host/run-grok"]
DEFAULT_COMMAND = PRODUCTION_COMMAND
DEFAULT_ACCOUNT_COMMAND = ["/usr/bin/sudo", "-n", "/usr/local/lib/kern-host/read-grok-account"]
ACCOUNT_HELPER_TIMEOUT_SECONDS = 10
# The ACP major version this client speaks. A server that answers with a
# different major is refused rather than driven on guessed semantics.
PROTOCOL_VERSION = 1
# The one auth method Kern uses: the Grok subscription login. An API-key login
# would bill a metered console balance instead of the operator's subscription,
# and api.x.ai is denied by the network integration, so it is not offered.
AUTH_METHOD_ID = "grok.com"
# The only login mode this host can complete: xAI shows the operator a code in
# their own browser and polls for approval, so nothing has to listen here.
DEVICE_LOGIN_MODE = "device"
# xAI's ACP extension methods. The leading underscore is ACP's marker for a
# non-standard method and is part of the name on the wire: without it the
# server answers -32601, and its own error text echoes the stripped form back
# ("unknown ACP extension method: x.ai/..."). Verified against grok 1.0.5.
AUTH_INFO_METHOD = "_x.ai/auth/info"
CHECK_SUBSCRIPTION_METHOD = "_x.ai/auth/check_subscription"
GET_URL_METHOD = "_x.ai/auth/get_url"
BILLING_METHOD = "_x.ai/billing"
INTERJECT_METHOD = "_x.ai/interject"
SESSION_NEW_METHOD = "session/new"
SESSION_LOAD_METHOD = "session/load"
SESSION_PROMPT_METHOD = "session/prompt"
SESSION_CANCEL_METHOD = "session/cancel"
CLIENT_NAME = "kern-host"
CLIENT_VERSION = "v1.0"
AGENT_CWD = "/mnt/kern-agent/agent-home"
TURN_TIMEOUT_SECONDS = 45 * 60
SESSION_SETUP_TIMEOUT_SECONDS = 60
PROMPT_POLL_SECONDS = 0.25
# Under the orchestrator's five-minute active recheck, so a scheduled recheck
# always revalidates, while the five-second pending poll never becomes a
# provider-traffic loop.
LIVE_VALIDATION_RETRY_SECONDS = 240
PROCESS_EXIT_TIMEOUT_SECONDS = 3
JSONRPC_METHOD_NOT_FOUND = -32601
# Grok Build uses these provider-returned team policy reasons for its own
# ``is_zdr_team()`` decision. Keep the same narrow interpretation here:
# coding-data opt-out is a separate control and must not be presented as ZDR.
_ZDR_TEAM_BLOCKED_REASONS = frozenset(
    {"BLOCKED_REASON_NO_LOGS", "BLOCKED_REASON_NO_LOGS_MODERATED"}
)


@dataclass
class _ParkedLogin:
    """The single parked login flow: its polling agent server, the id of the
    in-flight ``authenticate`` request, the login id the admin API keys on, and
    — once the poller observes completion — the trusted account id reported by
    that exact authenticated ACP server."""

    server: "GrokAcpServer"
    login_id: str
    authenticate_id: int
    authorization_completed: bool = field(default=False)
    completed: bool = field(default=False)
    account_id: str | None = field(default=None)
    failure: str | None = field(default=None)


_parked_login: _ParkedLogin | None = None
_login_lock = threading.Lock()

# The last live entitlement-validation failure: (status, error_message,
# recorded monotonic time). An awaiting_login verdict is final until an
# operator login completes or the linked account is reset; any other failure is
# retried after LIVE_VALIDATION_RETRY_SECONDS. In-memory on purpose: a restart
# revalidates once from scratch.
_live_validation_failure: tuple[str, str | None, float] | None = None


class GrokAgentError(RuntimeError):
    pass


class GrokLoginAlreadyAuthenticated(GrokAgentError):
    """A new device flow cannot start while Grok still has a credential."""


class GrokTimeout(GrokAgentError):
    pass


class GrokSessionNotFoundError(GrokAgentError):
    """The recorded provider session no longer exists."""


class GrokTurnFinishing(GrokAgentError):
    """The provider completed before a live steer could be accepted."""


@dataclass(frozen=True)
class GrokLogin:
    login_id: str
    login_url: str
    user_code: str | None


def _user_code_from_url(login_url: str) -> str | None:
    """The device code xAI embeds in the verification URL's query string."""
    try:
        query = parse_qs(urlsplit(login_url).query)
    except ValueError:
        return None
    for key in ("user_code", "userCode", "code"):
        values = query.get(key)
        if values and isinstance(values[0], str) and values[0].strip():
            return values[0].strip()
    return None


class GrokAcpServer:
    """One ACP transport over the agent server's stdio.

    Ordinary calls and notification reads share one response consumer, exactly
    as the Codex transport does. ``begin_call`` additionally starts a
    long-running request whose response is delivered to a dedicated waiter, so
    the login's ``authenticate`` can stay in flight across many status polls
    without owning the transport.
    """

    def __init__(
        self,
        command: list[str] | None = None,
        thread_id: str | None = None,
        on_ready: Callable[[], bool] | None = None,
        on_session_id: Callable[[str], None] | None = None,
    ) -> None:
        self._command = command or DEFAULT_COMMAND
        self._on_ready = on_ready
        self._on_session_id = on_session_id
        # Every production server needs a named scope so close() can reap its
        # entire cgroup if the CLI ignores stdin EOF. Turns use their host
        # thread id; short-lived status and login servers get a unique control
        # id. Custom test commands keep the old no-scope behavior unless their
        # caller supplied an explicit thread id.
        production = self._command[: len(PRODUCTION_COMMAND)] == PRODUCTION_COMMAND
        self._thread_id = thread_id or (
            f"grok-probe-{secrets.token_hex(8)}" if production else None
        )
        if self._thread_id is not None:
            self._command = [*self._command, "--thread-scope", self._thread_id]
        self._next_id = 1
        self._proc: subprocess.Popen[str] | None = None
        self._lifecycle_lock = threading.Lock()
        self._closed = False
        self._messages: queue.Queue[dict[str, Any]] = queue.Queue()
        self._pending: deque[dict[str, Any]] = deque()
        self._stderr_tail: deque[str] = deque(maxlen=20)
        self._io_lock = threading.RLock()
        self._stdin_lock = threading.Lock()
        self._response_waiters_lock = threading.Lock()
        self._response_waiters: dict[int, queue.Queue[dict[str, Any]]] = {}
        self._response_sequence = 0
        self._response_sequences: dict[int, int] = {}
        self._turn_lock = threading.Lock()
        self._active_session_id: str | None = None
        self._active_prompt_id: int | None = None
        self._accepting_steers = False
        self._last_session_id: str | None = None
        # What the server reported at initialize: the model catalog and agent
        # capabilities. Read by the runtime's session options work; kept here so
        # one probe answers both status and catalog questions.
        self.initialize_result: dict[str, Any] = {}

    def __enter__(self) -> "GrokAcpServer":
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def start(self, init_timeout: float = 60.0) -> None:
        with self._lifecycle_lock:
            if self._closed:
                raise GrokAgentError("Grok agent server was interrupted before startup")
            try:
                self._proc = subprocess.Popen(
                    self._command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            except OSError as exc:
                raise GrokAgentError(f"failed to start Grok agent server command: {exc}") from exc
        assert self._proc.stdout is not None and self._proc.stderr is not None
        threading.Thread(target=self._read_stdout, args=(self._proc.stdout,), daemon=True).start()
        threading.Thread(target=self._read_stderr, args=(self._proc.stderr,), daemon=True).start()
        result = self.call("initialize", _client_info(), timeout=init_timeout)
        self.initialize_result = result if isinstance(result, dict) else {}
        negotiated = self.initialize_result.get("protocolVersion")
        # A major-version mismatch means the method names and payloads below are
        # no longer the ones this client was written against. Refuse rather than
        # drive a protocol Kern has not reviewed.
        if type(negotiated) is not int or negotiated != PROTOCOL_VERSION:
            raise GrokAgentError(
                f"Grok agent server speaks ACP protocol {negotiated}, "
                f"but this host is pinned to {PROTOCOL_VERSION}"
            )

    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def last_known_session_id(self) -> str | None:
        return self._last_session_id

    def close(self) -> None:
        with self._turn_lock:
            self._accepting_steers = False
        with self._lifecycle_lock:
            self._closed = True
            proc = self._proc
        try:
            if proc is not None:
                # Closing stdin signals EOF, the agent server's normal shutdown
                # path. This is the reliable lever: the process is spawned
                # through sudo and may run as root/agent, so an unprivileged
                # signal can fail with EPERM — the kill below is a best-effort
                # fallback only.
                if proc.stdin is not None:
                    try:
                        proc.stdin.close()
                    except OSError:
                        pass
                try:
                    proc.wait(timeout=PROCESS_EXIT_TIMEOUT_SECONDS)
                except subprocess.TimeoutExpired:
                    try:
                        proc.kill()
                    except OSError:
                        pass
                # stdout and stderr are deliberately not closed here: each
                # reader thread owns its pipe and releases it at EOF. A buffered
                # stream's close() blocks on the same lock the reader holds
                # across its blocking read.
        finally:
            # Last resort after the clean stdin-EOF shutdown above: guarantee
            # the scope cgroup is gone even if a child outlived it. A clean
            # shutdown already emptied it, so this is then a no-op.
            thread_scope.stop_thread_scope(
                self._thread_id, self._command, PRODUCTION_COMMAND
            )

    def interrupt(self) -> None:
        """Cancel the active ACP prompt, then interrupt its process scope."""
        with self._turn_lock:
            self._accepting_steers = False
            session_id = self._active_session_id
        if session_id and self.alive():
            try:
                self.notify(SESSION_CANCEL_METHOD, {"sessionId": session_id})
            except GrokAgentError:
                pass
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
        thread_scope.interrupt_thread_scope(
            self._thread_id, self._command, PRODUCTION_COMMAND
        )

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
                if not isinstance(message, dict):
                    continue
                request_id = message.get("id")
                if message.get("method") is not None:
                    if request_id is not None:
                        # An agent -> client request. Kern declares no client
                        # capabilities, so there is nothing to serve; answering
                        # keeps the agent from blocking on a reply forever.
                        self._reject_request(request_id, str(message.get("method")))
                        continue
                    self._messages.put(message)
                    continue
                waiter = None
                if isinstance(request_id, int):
                    with self._response_waiters_lock:
                        waiter = self._response_waiters.get(request_id)
                    if waiter is not None:
                        with self._response_waiters_lock:
                            self._response_sequence += 1
                            self._response_sequences[request_id] = self._response_sequence
                        try:
                            waiter.put_nowait(message)
                        except queue.Full:
                            pass
                        else:
                            continue
                self._messages.put(message)

    def _reject_request(self, request_id: Any, method: str) -> None:
        try:
            with self._stdin_lock:
                proc = self._proc
                if proc is None or proc.stdin is None or proc.poll() is not None:
                    return
                proc.stdin.write(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "error": {
                                "code": JSONRPC_METHOD_NOT_FOUND,
                                "message": f"{method} is not available: this client serves no agent requests",
                            },
                        }
                    )
                    + "\n"
                )
                proc.stdin.flush()
        except (OSError, ValueError):
            # A closed or dying transport needs no reply.
            pass

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

    def call(self, method: str, params: dict[str, Any], *, timeout: float = 60.0) -> Any:
        with self._io_lock:
            with self._stdin_lock:
                request_id = self._write_request_locked(method, params)
            deadline = time.monotonic() + timeout
            # Notifications that arrive before our response are kept for
            # read_message(). The I/O lock gives exactly one consumer
            # ownership of both queues while it correlates this response. The
            # deadline belongs to the whole call: unrelated messages must not
            # reset it and keep a missing response alive indefinitely.
            while True:
                message = self._next_message(deadline - time.monotonic())
                if message.get("id") != request_id:
                    self._pending.append(message)
                    continue
                error = message.get("error")
                if error is not None:
                    raise GrokAgentError(_error_message(error))
                return message.get("result")

    def notify(self, method: str, params: dict[str, Any]) -> None:
        """Write one JSON-RPC notification and synchronously flush it."""
        with self._stdin_lock:
            proc = self._require_proc()
            assert proc.stdin is not None
            try:
                proc.stdin.write(
                    json.dumps({"jsonrpc": "2.0", "method": method, "params": params})
                    + "\n"
                )
                proc.stdin.flush()
            except (OSError, ValueError) as exc:
                raise GrokAgentError("Grok agent server transport closed") from exc

    def run(
        self,
        input_message: str,
        session_id: str | None,
        model: str,
        effort: str,
        on_message: Callable[[str | dict[str, Any]], None],
    ) -> tuple[str, str]:
        """Create/load one ACP session and drive one streamed prompt."""
        active_session_id = self._prepare_session(session_id, model, effort)
        self._last_session_id = active_session_id
        if self._on_session_id is not None:
            self._on_session_id(active_session_id)

        prompt_id = self.begin_call(
            SESSION_PROMPT_METHOD,
            {
                "sessionId": active_session_id,
                "prompt": [{"type": "text", "text": input_message}],
            },
        )
        with self._turn_lock:
            if self._closed:
                raise GrokAgentError("Grok turn was closed")
            self._active_session_id = active_session_id
            self._active_prompt_id = prompt_id
            self._accepting_steers = True
        if self._on_ready is not None and not self._on_ready():
            self.interrupt()
            raise GrokAgentError("Grok execution stopped during startup")

        message_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_states: dict[str, dict[str, Any]] = {}
        deadline = time.monotonic() + TURN_TIMEOUT_SECONDS
        response: dict[str, Any] | None = None
        try:
            while response is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise GrokTimeout("Grok turn timed out")
                try:
                    notification = self.read_message(
                        timeout=min(PROMPT_POLL_SECONDS, remaining)
                    )
                except GrokTimeout:
                    pass
                except GrokAgentError:
                    response = self.poll_response(prompt_id)
                    if response is None:
                        raise
                else:
                    _consume_turn_notification(
                        notification,
                        active_session_id,
                        message_parts,
                        reasoning_parts,
                        tool_states,
                        on_message,
                    )
                # Read notifications before checking the response waiter. ACP
                # writes the final chunks ahead of the prompt response; this
                # ordering prevents a fast response from dropping its last
                # answer bytes or completion activity.
                response = response or self.poll_response(prompt_id)
            # The stdout reader routes the response to a separate waiter. It
            # may therefore become visible while earlier notifications are
            # still sitting in the FIFO. Once the response is present, every
            # preceding wire message has already been enqueued; drain that
            # finite tail before finalizing the answer.
            for notification in self._take_queued_messages():
                _consume_turn_notification(
                    notification,
                    active_session_id,
                    message_parts,
                    reasoning_parts,
                    tool_states,
                    on_message,
                )
        finally:
            with self._turn_lock:
                self._accepting_steers = False
                self._active_prompt_id = None

        error = response.get("error") if isinstance(response, dict) else None
        if error is not None:
            raise GrokAgentError(_error_message(error))
        result = response.get("result") if isinstance(response, dict) else None
        if not isinstance(result, dict):
            raise GrokAgentError("Grok returned an invalid prompt response")
        stop_reason = result.get("stopReason")
        if isinstance(stop_reason, str) and stop_reason not in {
            "end_turn",
            "max_tokens",
            "stop_sequence",
        }:
            raise GrokAgentError(f"Grok turn stopped with reason {stop_reason}")
        if reasoning_parts:
            on_message(
                agent_activity.activity(
                    "grok",
                    "reasoning",
                    "reasoning",
                    "completed",
                    "Reasoning",
                    detail="".join(reasoning_parts),
                )
            )
        answer = agent_activity.clean_text("".join(message_parts)).strip()
        if not answer:
            raise GrokAgentError("Grok returned no answer text")
        on_message(answer)
        return active_session_id, answer

    def _prepare_session(self, session_id: str | None, model: str, effort: str) -> str:
        mcp_servers: list[dict[str, Any]] = []
        if session_id:
            try:
                result = self.call(
                    SESSION_LOAD_METHOD,
                    {
                        "sessionId": session_id,
                        "cwd": AGENT_CWD,
                        "mcpServers": mcp_servers,
                    },
                    timeout=SESSION_SETUP_TIMEOUT_SECONDS,
                )
            except GrokAgentError as exc:
                if _missing_session_error(str(exc)):
                    raise GrokSessionNotFoundError(
                        "the saved Grok session no longer exists; send the message again to start a fresh session"
                    ) from exc
                raise
            # session/load replays provider history before returning. Kern has
            # already persisted that history, so discard every queued replay
            # before submitting the new prompt.
            self._discard_pending_messages()
            loaded_id = _session_id_from_result(result) or session_id
            return loaded_id

        result = self.call(
            SESSION_NEW_METHOD,
            {
                "cwd": AGENT_CWD,
                "mcpServers": mcp_servers,
                "_meta": {
                    "modelId": model,
                    "reasoningEffort": effort,
                    # Kern has no interactive permission channel. The host OS,
                    # proxy and tool approval gates are the enforcement layer.
                    "yoloMode": True,
                },
            },
            timeout=SESSION_SETUP_TIMEOUT_SECONDS,
        )
        created_id = _session_id_from_result(result)
        if created_id is None:
            raise GrokAgentError("Grok did not report a session id")
        return created_id

    def _discard_pending_messages(self) -> None:
        self._take_queued_messages()

    def _take_queued_messages(self) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        with self._io_lock:
            while self._pending:
                messages.append(self._pending.popleft())
            while True:
                try:
                    messages.append(self._messages.get_nowait())
                except queue.Empty:
                    return messages

    def steer(self, text: str) -> None:
        with self._turn_lock:
            if (
                not self._accepting_steers
                or not self._active_session_id
                or self._active_prompt_id is None
            ):
                raise GrokTurnFinishing("Grok turn is finishing")
            session_id = self._active_session_id
            prompt_id = self._active_prompt_id
        interjection_id: int | None = None
        # Both long-running requests are sequenced by the one stdout reader.
        # Once the prompt response wins that race, no later interjection
        # send, acknowledgement, rejection, or timeout can make the message
        # part of the completed turn. Convert every such outcome to the
        # retryable finishing signal so the orchestrator never records a lost
        # steer.
        def reject_if_prompt_finished() -> None:
            with self._response_waiters_lock:
                prompt_sequence = self._response_sequences.get(prompt_id)
                interjection_sequence = (
                    self._response_sequences.get(interjection_id)
                    if interjection_id is not None
                    else None
                )
            if prompt_sequence is None or (
                interjection_sequence is not None
                and interjection_sequence < prompt_sequence
            ):
                return
            with self._turn_lock:
                if self._active_prompt_id == prompt_id:
                    self._accepting_steers = False
            raise GrokTurnFinishing("Grok turn is finishing")

        try:
            interjection_id = self.begin_call(
                INTERJECT_METHOD,
                {
                    "sessionId": session_id,
                    "text": text,
                    "interjectionId": f"kern-{secrets.token_hex(8)}",
                },
            )
            response = self.wait_response(interjection_id, timeout=30)
        except (GrokAgentError, OSError, ValueError) as exc:
            reject_if_prompt_finished()
            if isinstance(exc, GrokAgentError):
                raise
            raise GrokAgentError("Grok agent server transport closed") from exc
        reject_if_prompt_finished()
        error = response.get("error")
        if error is not None:
            raise GrokAgentError(_error_message(error))
        result = response.get("result")
        interjection = result.get("result") if isinstance(result, dict) else None
        if (
            not isinstance(interjection, dict)
            or interjection.get("status") != "queued"
        ):
            raise GrokAgentError("Grok did not accept the interjection")

    def begin_call(self, method: str, params: dict[str, Any]) -> int:
        """Start a long-running request and return its id without waiting.

        The response is routed to a dedicated waiter, so it can be collected
        later by ``poll_response`` without competing for the transport.
        """
        waiter: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        with self._stdin_lock:
            request_id = self._next_id
            self._next_id += 1
            with self._response_waiters_lock:
                self._response_waiters[request_id] = waiter
            try:
                self._write_request_with_id_locked(request_id, method, params)
            except Exception:
                with self._response_waiters_lock:
                    self._response_waiters.pop(request_id, None)
                raise
        return request_id

    def poll_response(self, request_id: int) -> dict[str, Any] | None:
        """The response to ``begin_call``, or None while it is still in flight."""
        with self._response_waiters_lock:
            waiter = self._response_waiters.get(request_id)
        if waiter is None:
            return None
        try:
            message = waiter.get_nowait()
        except queue.Empty:
            return None
        with self._response_waiters_lock:
            self._response_waiters.pop(request_id, None)
        with self._turn_lock:
            if self._active_prompt_id == request_id:
                self._accepting_steers = False
        return message

    def wait_response(
        self, request_id: int, *, timeout: float = 60.0
    ) -> dict[str, Any]:
        """Wait for a request started by ``begin_call`` without reading updates."""
        deadline = time.monotonic() + timeout
        while True:
            response = self.poll_response(request_id)
            if response is not None:
                return response
            if time.monotonic() >= deadline:
                raise GrokTimeout("timed out waiting for the Grok agent server")
            self._require_proc()
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

    def read_message(self, *, timeout: float = 60.0) -> dict[str, Any]:
        with self._io_lock:
            if self._pending:
                return self._pending.popleft()
            return self._next_message(timeout)

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
            json.dumps(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            )
            + "\n"
        )
        proc.stdin.flush()

    def _next_message(self, timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._require_proc()
                raise GrokTimeout("timed out waiting for the Grok agent server")
            try:
                return self._messages.get(timeout=min(0.25, remaining))
            except queue.Empty:
                self._require_proc()

    def _require_proc(self) -> subprocess.Popen[str]:
        proc = self._proc
        if proc is None:
            raise GrokAgentError("Grok agent server was not started")
        returncode = proc.poll()
        if returncode is not None:
            raise GrokAgentError(f"Grok agent server exited with status {returncode}")
        return proc


def _client_info() -> dict[str, Any]:
    """The initialize payload.

    Every client capability is declined on purpose. The agent runs on this host
    as ``kern-agent`` with its own shell and file access, so a client-side file
    or terminal API would add a second path to the same files that bypasses the
    OS boundary rather than reinforcing it, and permission prompts have no
    operator to answer them on an autonomous host.
    """
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "clientCapabilities": {
            "fs": {"readTextFile": False, "writeTextFile": False},
            "terminal": False,
        },
        "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
    }


def _error_message(error: Any) -> str:
    if isinstance(error, dict):
        message = error.get("message")
        detail = error.get("data")
        parts = [part for part in (message, detail) if isinstance(part, str) and part.strip()]
        if parts:
            return ": ".join(part.strip() for part in parts)
    return "Grok agent server request failed"


def _session_id_from_result(result: Any) -> str | None:
    if not isinstance(result, dict):
        return None
    value = result.get("sessionId") or result.get("session_id")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _missing_session_error(message: str) -> bool:
    lowered = message.strip().lower()
    return "session not found" in lowered or "no session found" in lowered


def _content_text(content: Any) -> str:
    if isinstance(content, dict):
        text = content.get("text")
        return agent_activity.clean_text(text) if isinstance(text, str) else ""
    return ""


def _consume_turn_notification(
    message: Any,
    session_id: str,
    message_parts: list[str],
    reasoning_parts: list[str],
    tool_states: dict[str, dict[str, Any]],
    on_message: Callable[[str | dict[str, Any]], None],
) -> None:
    """Map one ACP update without letting provider-owned shapes fail a turn."""
    try:
        if not isinstance(message, dict) or message.get("method") not in {
            "session/update",
            "_x.ai/session/update",
        }:
            return
        params = message.get("params")
        if not isinstance(params, dict) or params.get("sessionId") != session_id:
            return
        meta = params.get("_meta")
        if isinstance(meta, dict) and meta.get("isReplay") is True:
            return
        update = params.get("update")
        if not isinstance(update, dict):
            return
        update_type = update.get("sessionUpdate")
        if update_type == "agent_message_chunk":
            text = _content_text(update.get("content"))
            if text:
                message_parts.append(text)
            return
        if update_type == "agent_thought_chunk":
            text = _content_text(update.get("content"))
            if text:
                reasoning_parts.append(text)
                update = agent_activity.activity(
                    "grok",
                    "reasoning",
                    "reasoning",
                    "started",
                    "Reasoning",
                    # This chunk, not the trace so far. Each streamed update is
                    # persisted as its own event, so carrying the accumulated
                    # prefix stored the reasoning again on every chunk —
                    # quadratic in the length of the trace, for a turn that
                    # also writes the whole thing once more when it completes.
                    detail=text,
                )
                # Every chunk shares one activity id, and the reader merges a
                # lifecycle by replacing fields. Mark the detail append-only,
                # as Codex marks streamed command output, so the live card
                # accumulates the trace instead of showing the newest fragment
                # alone — usually mid-sentence.
                update["append_detail"] = True
                on_message(update)
            return
        if update_type in {"tool_call", "tool_call_update"}:
            tool_id = update.get("toolCallId") or update.get("tool_call_id")
            previous = tool_states.get(tool_id, {}) if isinstance(tool_id, str) else {}
            merged = {
                **previous,
                **{key: value for key, value in update.items() if value is not None},
            }
            if isinstance(tool_id, str) and tool_id:
                tool_states[tool_id] = merged
            activity = _tool_activity(merged)
            if activity is not None:
                on_message(activity)
            return
        if update_type == "plan":
            entries = update.get("entries") or update.get("plan")
            if entries is None:
                return
            completed = isinstance(entries, list) and bool(entries) and all(
                isinstance(entry, dict)
                and str(entry.get("status") or "").lower() in {"completed", "done"}
                for entry in entries
            )
            on_message(
                agent_activity.activity(
                    "grok",
                    "plan",
                    "plan",
                    "completed" if completed else "started",
                    "Plan",
                    detail=agent_activity.json_text(entries),
                )
            )
            return
        if update_type in {"task_backgrounded", "task_completed"}:
            snapshot = update.get("task_snapshot")
            snapshot_id = snapshot.get("task_id") if isinstance(snapshot, dict) else None
            task_id = str(
                update.get("task_id")
                or update.get("taskId")
                or snapshot_id
                or "background-task"
            )
            completed = update_type == "task_completed"
            title = update.get("command") or "Background task"
            on_message(
                agent_activity.activity(
                    "grok",
                    f"task:{task_id}",
                    "command",
                    "completed" if completed else "started",
                    title,
                    detail=update.get("cwd"),
                    output=agent_activity.json_text(update.get("task_snapshot"))
                    if completed
                    else None,
                    status="completed" if completed else "backgrounded",
                )
            )
    except Exception:
        # Provider progress is useful but non-authoritative. A malformed update
        # must never discard the final answer or fail the session lifecycle.
        return


def _tool_activity(update: dict[str, Any]) -> dict[str, Any] | None:
    tool_id = update.get("toolCallId") or update.get("tool_call_id")
    if not isinstance(tool_id, str) or not tool_id:
        return None
    raw_kind = str(update.get("kind") or "tool").lower()
    kind = (
        "command"
        if raw_kind in {"execute", "exec", "terminal"}
        else "file_change"
        if raw_kind in {"edit", "delete", "move"}
        else "search"
        if raw_kind in {"search", "fetch"}
        else "tool"
    )
    status = str(update.get("status") or "pending")
    phase = "completed" if status.lower() in {
        "completed",
        "failed",
        "cancelled",
        "canceled",
    } else "started"
    title = update.get("title") or raw_kind.replace("_", " ").title() or "Tool"
    detail_value = update.get("rawInput") or update.get("raw_input") or update.get("locations")
    output_value = update.get("content") or update.get("rawOutput") or update.get("raw_output")
    return agent_activity.activity(
        "grok",
        f"tool:{tool_id}",
        kind,
        phase,
        title,
        detail=agent_activity.json_text(detail_value) if detail_value is not None else None,
        output=agent_activity.json_text(output_value) if output_value is not None else None,
        status=status,
    )


def run_turn(
    server: GrokAcpServer,
    input_message: str,
    session_id: str | None,
    model: str,
    effort: str,
    on_message: Callable[[str | dict[str, Any]], None],
) -> tuple[str, str]:
    return server.run(input_message, session_id, model, effort, on_message)


def account_status(
    *, force_provider_probe: bool = False
) -> tuple[str, str | None, dict[str, Any] | None]:
    """Return (status, detail, account metadata). detail is set only for "error"."""
    # Bounded timeouts: only the background poller calls this, but a Grok agent
    # server that cannot start (e.g. its startup traffic is denied by a
    # restrictive policy) must not wedge the poller — it resolves to "error"
    # with a detail until conditions improve. The init timeout leaves room for a
    # cold start on a small instance.
    login_server = _current_login_server()
    if login_server is not None:
        return _login_server_status(login_server, force_provider_probe=force_provider_probe)

    server = GrokAcpServer()
    try:
        server.start(init_timeout=45)
        return _account_status_from_server(server, force_provider_probe=force_provider_probe)
    except GrokAgentError as exc:
        return _grok_status_error(exc, server)
    finally:
        server.close()


def _current_login_server() -> "GrokAcpServer | None":
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
    server: "GrokAcpServer", *, force_provider_probe: bool = False
) -> tuple[str, str | None, dict[str, Any] | None]:
    """Drive the parked login forward, then report status from its server.

    The status poller is the only reader of the parked server, so it is also
    what observes the long-running ``authenticate`` response. Collect that
    first: a login that just completed should be visible to the capture in the
    same refresh that reports the resulting status.
    """
    _collect_parked_login(server)
    return _account_status_from_server(server, force_provider_probe=force_provider_probe)


def collect_login_completion() -> None:
    """Collect a parked login response before the guarded provider probe.

    The orchestrator uses the captured account id to publish the approved pin;
    only then can this provider's subscription and billing routes pass their
    account guard.
    """
    server = _current_login_server()
    if server is not None:
        _collect_parked_login(server)


def _collect_parked_login(server: "GrokAcpServer") -> None:
    with _login_lock:
        parked = _parked_login
        if parked is None or parked.server is not server or parked.completed:
            return
        authenticate_id = parked.authenticate_id
        authorization_completed = parked.authorization_completed
    if not authorization_completed:
        response = server.poll_response(authenticate_id)
        if response is None:
            return
        error = response.get("error")
        if error is not None:
            with _login_lock:
                parked = _parked_login
                if parked is not None and parked.server is server:
                    parked.failure = _error_message(error)
            return
        with _login_lock:
            parked = _parked_login
            if parked is None or parked.server is not server:
                return
            parked.authorization_completed = True
        # Fresh credentials were just written, so the remembered verdict about
        # the previous credential no longer applies; revalidate from scratch.
        clear_live_validation_failure()
    # Capture the trusted account id from the exact ACP server whose
    # authenticate request just completed. The auth file is agent-writable, so
    # reading its token here would let another agent process swap in a foreign
    # credential during the operator's browser flow and get that account
    # approved. The completed server is the approval-bound source; later status
    # checks require the auth-file token claim to match this immutable anchor.
    try:
        info = server.call(AUTH_INFO_METHOD, {}, timeout=15)
    except GrokAgentError:
        # The authenticate response is consumed exactly once. Keep that fact
        # and retry this derived identity read on the next status poll instead
        # of terminally losing a successful browser flow.
        return
    account_id = _authenticated_account_id(info)
    if account_id is None:
        return
    with _login_lock:
        parked = _parked_login
        if parked is not None and parked.server is server:
            parked.completed = True
            parked.account_id = account_id


def _account_status_from_server(
    server: "GrokAcpServer", *, force_provider_probe: bool = False
) -> tuple[str, str | None, dict[str, Any] | None]:
    try:
        info = server.call(AUTH_INFO_METHOD, {}, timeout=15)
    except GrokAgentError as exc:
        return _grok_status_error(exc, server)
    if not _is_authenticated(info):
        return "awaiting_login", None, None
    try:
        account = read_grok_account()
    except GrokAgentError as exc:
        return "error", str(exc), None
    claimed_account_id = _string_field(account, "account_id") if account else None
    token_hash = _string_field(account, "access_token_sha256") if account else None
    if not account or not claimed_account_id or not token_hash:
        # Logged in per the agent, but the anchor value the proxy pins on is not
        # readable. Never report active without it: the pin would stay cleared
        # and every data-plane request would be denied under an "active" badge.
        return "error", "Grok is logged in but its account id is unavailable", None
    try:
        attested = read_attested_identity(
            token_hash, force=force_provider_probe
        )
    except GrokAgentError as exc:
        return "error", str(exc), None
    account_id = attested["account_id"]
    if account_id != claimed_account_id:
        return (
            "error",
            "xAI attested a different account than the Grok token claims",
            None,
        )
    metadata = _safe_account_metadata(info)
    reported_id = _string_field(metadata, "account_id")
    if reported_id and reported_id != account_id:
        # The provider-attested identity and the agent's own report disagree.
        # That is never a normal state, so fail closed rather than pick a winner.
        return (
            "error",
            "Grok reported a different account than xAI attested",
            None,
        )
    metadata["account_id"] = account_id
    metadata.pop("email", None)
    attested_email = attested.get("email")
    if attested_email:
        metadata["email"] = attested_email
    metadata["access_token_sha256"] = token_hash
    entitlement = _entitlement_status(server, force_provider_probe=force_provider_probe)
    if entitlement is not None:
        return entitlement
    usage = _read_usage(server)
    if usage:
        metadata["grok_usage"] = usage
    return "active", None, metadata


def _is_authenticated(info: Any) -> bool:
    if not isinstance(info, dict):
        return False
    # A logged-out server answers with an empty record rather than an error, so
    # presence of the principal is what distinguishes the two.
    return bool(
        _string_field(info, "principalId")
        or _string_field(info, "principal_id")
        or _string_field(info, "userId")
        or _string_field(info, "user_id")
    )


def _authenticated_account_id(info: Any) -> str | None:
    """The principal bound to one authenticated ACP server."""
    if not _is_authenticated(info):
        return None
    return _pick_string(info, "principalId", "principal_id", "userId", "user_id")


def _entitlement_status(
    server: "GrokAcpServer", *, force_provider_probe: bool = False
) -> tuple[str, str | None, dict[str, Any] | None] | None:
    """Re-check Build entitlement, or None when the account is entitled.

    Data-plane access can require the account to belong to an xAI console team,
    which is not the pinned value: the pin still matches after the team is
    deleted or the account removed from it, and the chat endpoint 403s. This
    probe is the only thing that notices, so its failure is reported as an
    error with the provider's own message — a fresh login cannot fix an
    entitlement problem.

    The check authenticates live, so its verdict is remembered: automatic checks
    keep an authentication failure at awaiting_login until an operator login
    completes or the linked account is reset, and retry any other failure at
    most every LIVE_VALIDATION_RETRY_SECONDS. An explicit operator refresh
    bypasses that memory. Without it the five-second non-active poll would
    generate provider traffic on every cycle.
    """
    global _live_validation_failure
    failure = _live_validation_failure
    if not force_provider_probe and failure is not None and (
        failure[0] == "awaiting_login" or time.monotonic() - failure[2] < LIVE_VALIDATION_RETRY_SECONDS
    ):
        return failure[0], failure[1], None
    try:
        server.call(CHECK_SUBSCRIPTION_METHOD, {}, timeout=15)
    except GrokTimeout as exc:
        _live_validation_failure = ("error", str(exc), time.monotonic())
        return "error", str(exc), None
    except GrokAgentError as exc:
        status, error_message, account = _grok_status_error(exc, server)
        _live_validation_failure = (status, error_message, time.monotonic())
        return status, error_message, account
    _live_validation_failure = None
    return None


def clear_live_validation_failure() -> None:
    """Forget the remembered entitlement verdict. Called when an operator login
    completes or the linked account is reset: both replace the credential the
    verdict was about."""
    global _live_validation_failure
    _live_validation_failure = None
    _ATTESTATION_FAILURES.clear()


# An entitlement refusal names a permission problem the operator fixes at
# console.x.ai, not a login problem. Checked before the login markers below,
# because these messages often also mention authorization.
_ENTITLEMENT_MARKERS = (
    "permission-denied",
    "permission denied",
    "does not have permission",
    "update the permissions",
    "403",
)
# Only specific "not logged in" phrasings mean awaiting_login, so a real failure
# that merely mentions auth infrastructure (e.g. "could not reach auth.x.ai")
# surfaces as an error with its detail instead of an impossible login prompt.
_LOGIN_MARKERS = (
    "authentication required",
    "not logged in",
    "logged out",
    "login required",
    "must log in",
    "no auth method",
    "unauthorized",
    "401",
)


def _grok_status_error(
    exc: GrokAgentError,
    server: "GrokAcpServer",
) -> tuple[str, str | None, dict[str, Any] | None]:
    message = str(exc).lower()
    if any(marker in message for marker in _ENTITLEMENT_MARKERS):
        return "error", _error_detail(str(exc), server.stderr_tail()), None
    if any(marker in message for marker in _LOGIN_MARKERS):
        return "awaiting_login", None, None
    return "error", _error_detail(str(exc), server.stderr_tail()), None


def _error_detail(message: str, stderr: str) -> str:
    if not stderr:
        return message
    if stderr in message:
        return message
    return f"{message}; agent server stderr: {stderr}"


def _safe_account_metadata(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    metadata: dict[str, Any] = {}
    account_id = _pick_string(value, "principalId", "principal_id", "userId", "user_id")
    if account_id:
        metadata["account_id"] = account_id
    email = _pick_string(value, "email")
    if email:
        metadata["email"] = email
    team_id = _pick_string(value, "teamId", "team_id")
    if team_id:
        metadata["team_id"] = team_id
    organization_id = _pick_string(value, "organizationId", "organization_id")
    if organization_id:
        metadata["organization_id"] = organization_id
    principal_type = _pick_string(value, "principalType", "principal_type")
    if principal_type:
        metadata["principal_type"] = principal_type
    # Newer Grok Build versions include this list in the authenticated info
    # response. Absence means unknown (for compatibility with older clients),
    # while a well-formed empty list is a definite inactive result.
    reasons = value.get("teamBlockedReasons")
    if reasons is None:
        reasons = value.get("team_blocked_reasons")
    if isinstance(reasons, list) and all(isinstance(reason, str) for reason in reasons):
        metadata["zdr_enabled"] = any(
            reason in _ZDR_TEAM_BLOCKED_REASONS for reason in reasons
        )
    # This account-backed choice is separate from ZDR. Grok Build exposes it
    # through /privacy and /settings, and uses it to gate coding-data retention
    # for product/model improvement. Preserve a missing field as unknown: old
    # pinned CLI versions may not report it, and "unknown" must never render as
    # an opt-out.
    coding_opt_out = value.get("codingDataRetentionOptOut")
    if coding_opt_out is None:
        coding_opt_out = value.get("coding_data_retention_opt_out")
    if isinstance(coding_opt_out, bool):
        metadata["coding_data_retention_opt_out"] = coding_opt_out
    return metadata


def _read_usage(server: "GrokAcpServer") -> dict[str, Any]:
    """The subscription usage snapshot for the top bar, or {} when unknown.

    Best-effort: a usage read that fails never changes the runtime's status. An
    account whose pin is not published yet (agent-side credentials awaiting
    operator approval) cannot reach the guarded billing route at all, which is
    an ordinary state rather than a fault.
    """
    try:
        billing = server.call(BILLING_METHOD, {}, timeout=15)
    except GrokAgentError:
        return {}
    return _safe_usage_metadata(billing)


def _safe_usage_metadata(value: Any) -> dict[str, Any]:
    """Normalize the billing snapshot.

    Deliberately returns {} when the usage percentage is absent. The provider
    omits it on a freshly-started period, and Grok's own client falls back to
    0.0 in that case — which would paint a permanently green "0% used" bar. Kern
    treats an absent percentage as *unknown* and omits the block entirely,
    the same rule it applies to Claude usage windows.
    """
    if not isinstance(value, dict):
        return {}
    config = value.get("config")
    config = config if isinstance(config, dict) else {}
    percent = _pick_number(
        config, "creditUsagePercent", "credit_usage_percent"
    )
    if percent is None:
        percent = _pick_number(value, "creditUsagePercent", "credit_usage_percent")
    if percent is None:
        return {}
    usage: dict[str, Any] = {"usage_percent": percent}
    period = config.get("currentPeriod")
    if not isinstance(period, dict):
        period = config.get("current_period")
    if isinstance(period, dict):
        period_type = _pick_string(period, "type")
        if period_type:
            normalized = period_type.rsplit("_", 1)[-1].lower()
            if normalized in {"weekly", "monthly", "daily"}:
                usage["period_type"] = normalized
        resets_at = _epoch(_pick_string(period, "end"))
        if resets_at is not None:
            usage["resets_at"] = resets_at
    tier = _pick_string(value, "subscriptionTier", "subscription_tier")
    if tier:
        usage["subscription_tier"] = tier
    on_demand = value.get("onDemandEnabled")
    if not isinstance(on_demand, bool):
        on_demand = value.get("on_demand_enabled")
    if isinstance(on_demand, bool):
        usage["on_demand_enabled"] = on_demand
    return usage


def _pick_string(value: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        item = value.get(key)
        if isinstance(item, str) and item.strip():
            return item.strip()
    return None


def _pick_number(value: dict[str, Any], *keys: str) -> int | float | None:
    """A numeric field, unwrapping the provider's ``{"val": n}`` money shape."""
    for key in keys:
        item = value.get(key)
        if isinstance(item, dict):
            item = item.get("val")
        if isinstance(item, bool):
            continue
        if isinstance(item, (int, float)):
            return item
    return None


def _epoch(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def _string_field(value: dict[str, Any] | None, key: str) -> str | None:
    if not isinstance(value, dict):
        return None
    item = value.get(key)
    return item if isinstance(item, str) and item else None


def _run_account_helper(argv: list[str]) -> subprocess.CompletedProcess[str]:
    """Run the root account helper. Raises only on failing to run it at all."""
    return subprocess.run(
        argv,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=ACCOUNT_HELPER_TIMEOUT_SECONDS,
    )


def read_grok_account(command: list[str] | None = None) -> dict[str, Any] | None:
    """The account identity from the login tokens, through the root helper.

    Returns None when no login exists. The account id is the token's own signed
    claim, never the agent-writable ``user_id`` field beside it, so the value
    the proxy pins on is the one xAI itself acts on.
    """
    try:
        proc = _run_account_helper(list(command or DEFAULT_ACCOUNT_COMMAND))
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GrokAgentError(f"could not read the Grok account: {exc}") from exc
    if proc.returncode == 2:
        return None  # no auth file yet
    if proc.returncode != 0:
        detail = proc.stderr.strip() or f"exit status {proc.returncode}"
        raise GrokAgentError(f"could not read the Grok account: {detail}")
    try:
        account = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise GrokAgentError(f"Grok account helper returned invalid JSON: {exc}") from exc
    if not isinstance(account, dict):
        raise GrokAgentError("Grok account helper returned an invalid response")
    return account


def start_device_login() -> GrokLogin:
    """Begin the operator's Grok login and park its server.

    ``authenticate`` does not return until the operator finishes in a browser,
    so it is written without waiting; the URL to display comes from
    ``x.ai/auth/get_url``, and the status poller collects the response later.
    """
    global _parked_login
    server = GrokAcpServer()
    try:
        server.start()
        authenticate_id = server.begin_call("authenticate", {"methodId": AUTH_METHOD_ID})
        result = server.call(GET_URL_METHOD, {}, timeout=30)
        login_url = (
            _pick_string(result, "auth_url", "authUrl", "url")
            if isinstance(result, dict)
            else None
        )
        if not login_url:
            try:
                info = server.call(AUTH_INFO_METHOD, {}, timeout=15)
            except GrokAgentError:
                info = None
            if _is_authenticated(info):
                raise GrokLoginAlreadyAuthenticated(
                    "Grok is already authenticated and cannot start a new device login"
                )
            raise GrokAgentError("Grok did not return a login URL")
        mode = _pick_string(result, "mode")
        if mode is not None and mode.lower() != DEVICE_LOGIN_MODE:
            # Only the device flow completes without a browser on this host. A
            # loopback flow would redirect to a port nothing here is listening
            # on, so it would hang rather than fail; refuse it by name.
            raise GrokAgentError(
                f"Grok returned an unsupported login mode {mode!r}; this host requires a device login"
            )
        # The code is carried in the URL's query rather than a field of its
        # own, and the operator needs it to read out loud from the browser.
        user_code = _user_code_from_url(login_url)
    except BaseException:
        server.close()
        raise
    # Random, not derived from the request id and a second. Both parts repeat:
    # a fresh server almost always reaches authenticate as request 2, and two
    # openings can land in the same second when a completed login is retired
    # and the operator immediately starts its replacement. A collision there is
    # not cosmetic -- the finishing refresh closes the parked server by login
    # id, so it would reap the replacement and leave its device code with
    # nothing driving it.
    login_id = f"grok-{secrets.token_hex(8)}"
    with _login_lock:
        old = _parked_login
        _parked_login = _ParkedLogin(
            server=server, login_id=login_id, authenticate_id=authenticate_id
        )
    if old is not None:
        old.server.close()
    return GrokLogin(login_id=login_id, login_url=login_url, user_code=user_code)


def _pop_parked(match: Callable[[_ParkedLogin], bool]) -> "GrokAcpServer | None":
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


# Attested identities, keyed by token hash. A token's identity does not change,
# and the status poll runs every few seconds, so the provider is asked once per
# credential rather than once per poll. In memory on purpose: a restart
# re-attests from scratch.
_ATTESTED_IDENTITY: dict[str, dict[str, str]] = {}
# Failed provider attestations, keyed by token hash: (error, recorded monotonic
# time). The status poll runs every five seconds and providers are checked
# sequentially, so retrying the ten-second helper timeout on every poll would
# stall every runtime after Grok. An operator refresh bypasses this memory.
_ATTESTATION_FAILURES: dict[str, tuple[str, float]] = {}


def read_attested_identity(
    expected_token_sha256: str, *, force: bool = False
) -> dict[str, str]:
    """Ask xAI who the agent's current token belongs to.

    The helper binds the provider response to the hash read just before it ran,
    so a rewritten auth file cannot make an established ACP session publish a
    different identity. As with Claude, an identity that cannot be attested is
    not reported active.
    """
    if not expected_token_sha256:
        raise GrokAgentError("the Grok token hash is unavailable")
    memo = _ATTESTED_IDENTITY.get(expected_token_sha256)
    if memo is not None:
        return memo
    failure = _ATTESTATION_FAILURES.get(expected_token_sha256)
    if (
        not force
        and failure is not None
        and time.monotonic() - failure[1] < LIVE_VALIDATION_RETRY_SECONDS
    ):
        raise GrokAgentError(failure[0])

    def fail(message: str) -> NoReturn:
        _ATTESTATION_FAILURES[expected_token_sha256] = (message, time.monotonic())
        raise GrokAgentError(message)

    try:
        proc = _run_account_helper(
            [*DEFAULT_ACCOUNT_COMMAND, "--attest"]
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        fail(f"could not attest the Grok account: {exc}")
    if proc.returncode != 0:
        detail = proc.stderr.strip() or f"exit status {proc.returncode}"
        fail(f"could not attest the Grok account: {detail}")
    try:
        value = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        fail(f"Grok account attestation returned invalid JSON: {exc}")
    if not isinstance(value, dict):
        fail("Grok account attestation returned an invalid response")
    if _string_field(value, "access_token_sha256") != expected_token_sha256:
        fail("the Grok token changed during account attestation")
    account_id = _string_field(value, "account_id")
    if not account_id:
        fail("Grok account attestation response is incomplete")
    identity = {"account_id": account_id}
    email = _string_field(value, "email")
    if email:
        identity["email"] = email
    _ATTESTATION_FAILURES.pop(expected_token_sha256, None)
    _ATTESTED_IDENTITY[expected_token_sha256] = identity
    return identity


def read_completed_login_account_id(login_id: str) -> str | None:
    """Return the completed operator login's account id.

    A stored OAuth row means the operator saw a login URL, not that the login
    completed. First-account capture therefore requires the ``authenticate``
    response for that exact login, observed by the status poller on the parked
    server. This is a pure lookup of the id captured from that same server;
    while its post-login identity is unavailable, capture remains pending.
    """
    with _login_lock:
        parked = _parked_login
        if parked is None or parked.login_id != login_id:
            return None
        if parked.failure:
            raise GrokAgentError(parked.failure)
        if not parked.completed:
            return None
        account_id = parked.account_id
    if not account_id:
        raise GrokAgentError("the completed Grok login did not include a usable account id")
    return account_id


def login_server_parked() -> bool:
    """Whether a live login server is still driving the parked device flow.

    Grok's device flow is only advanced by the CLI process that started it: it
    is the thing polling xAI and holding the long-running `authenticate`
    request. If that process is gone -- an admin API restart stops the scope
    through BindsTo -- the persisted login row names a code nobody is
    exchanging. Asking also reaps a parked server that has since died.
    """
    return _current_login_server() is not None


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
