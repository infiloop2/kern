"""Claude Code runtime adapter.

Claude Code does not expose Codex's stdio JSON-RPC app-server. The supported
automation surface is the CLI/Agent SDK: print mode, stream-json I/O, and
resumable sessions. This module wraps that process shape behind the same small
contract the orchestrator needs: account status, start/complete OAuth login,
run one turn, and close the running process for turn stops.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import queue
import re
import subprocess
import threading
import time
from typing import IO, Any, Callable

from host.runtime.admin_api import agent_activity, thread_scope

DEFAULT_COMMAND = ["/usr/bin/sudo", "-n", "/usr/local/lib/kern-host/run-claude-code"]
DEFAULT_ACCOUNT_COMMAND = ["/usr/bin/sudo", "-n", "/usr/local/lib/kern-host/read-claude-account"]
AGENT_CWD = "/mnt/kern-agent/agent-home"
# The bundled tools surface: Claude Code spawns the MCP shim as the agent
# user; the shim forwards to the host tools socket (see
# docs/architecture/tools/host-integration.md). --strict-mcp-config (below)
# makes this the only MCP server. With no tools enabled the shim lists
# nothing, so passing it unconditionally is harmless.
TOOLS_MCP_CONFIG = json.dumps(
    {
        "mcpServers": {
            "kern": {
                "command": "/usr/bin/python3",
                "args": ["-m", "host.runtime.agent_shim.mcp_shim"],
                "env": {"PYTHONPATH": "/opt/kern-host"},
            }
        }
    }
)
ACCOUNT_HELPER_TIMEOUT_SECONDS = 15
# The attest helper makes one HTTPS round trip (10s inside the helper) on top
# of the file read, so it gets a larger budget than the plain account read.
ATTEST_HELPER_TIMEOUT_SECONDS = 20
STATUS_TIMEOUT_SECONDS = 45
USAGE_TIMEOUT_SECONDS = 30
LOGIN_START_TIMEOUT_SECONDS = 30
# How long a steered turn waits, after a result that leaves sent user messages
# unaccounted for, before concluding the steer was merged into the turn that
# just ended. The pinned CLI folds a mid-turn user message into the running
# turn and emits a single result for both messages; only a steer that lands
# between turns starts its own turn, and that turn announces itself with local
# stream events (system init) within milliseconds, which disarms the deadline.
# Verified against the real CLI in both timings; the bound only has to beat
# the CLI's local event-loop latency, not any network round trip.
STEER_SETTLE_TIMEOUT_SECONDS = 10.0
PROCESS_EXIT_TIMEOUT_SECONDS = 3
LOGIN_URL_RE = re.compile(r"If the browser didn't open, visit: (https://\S+)")
# Usage lines are parsed one window per line: a window header, a percent, and
# an optional reset time. Each piece is matched independently so one odd line
# (or a missing reset) degrades to a partial snapshot instead of no snapshot.
# All captured values are bounded because the text comes from an agent-run CLI.
USAGE_WINDOW_RE = re.compile(
    r"^\s*Current\s+(session|week\s*\(([^()]{1,40})\))\s*:\s*(.+?)\s*$", re.IGNORECASE
)
USAGE_PERCENT_RE = re.compile(r"(\d{1,3}(?:\.\d{1,2})?)\s*%\s+used\b", re.IGNORECASE)
USAGE_RESETS_RE = re.compile(r"\bresets\s+(.{1,100}?)\s*$", re.IGNORECASE)
USAGE_RESET_RE = re.compile(r"^([A-Za-z]{3})\s+(\d{1,2}),\s+(\d{1,2})(?::(\d{2}))?(am|pm)\s+\(UTC\)$", re.IGNORECASE)
USAGE_RESET_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

_login_process: "ClaudeLoginProcess | None" = None
_login_lock = threading.Lock()


class ClaudeCodeError(RuntimeError):
    pass


class ClaudeAuthenticationError(ClaudeCodeError):
    pass


class ClaudeTimeout(ClaudeCodeError):
    pass


@dataclass(frozen=True)
class ClaudeLogin:
    login_url: str


class ClaudeCodeSession:
    """Owns at most one running Claude CLI process.

    start() exists to satisfy the orchestrator's server contract; the actual
    Claude process is spawned in run() because Claude's resumable CLI sessions
    are persisted on disk.
    """

    def __init__(
        self,
        command: list[str] | None = None,
        thread_id: str | None = None,
        on_ready: Callable[[], bool] | None = None,
        on_session_id: Callable[[str], None] | None = None,
    ) -> None:
        self._command = command or DEFAULT_COMMAND
        self._thread_id = thread_id
        self._on_ready = on_ready
        self._on_session_id = on_session_id
        # The orchestrator sets this only for an app-created turn. Claude's
        # append-system-prompt keeps it distinct from the app's current user
        # message and alongside the host's immutable CLAUDE.md instructions.
        self.app_instructions: str | None = None
        # Task turns run inside a systemd scope named after the host thread.
        # Keep the id separate from the command because the launcher's required
        # web-search decision must remain its first argument.
        self._proc: subprocess.Popen[str] | None = None
        self._messages: queue.Queue[dict[str, Any]] = queue.Queue()
        self._stderr_tail: deque[str] = deque(maxlen=20)
        self._stdin_lock = threading.Lock()
        # close() may win before run() reaches the CLI spawn. Keep that
        # terminal decision under the same lock as Popen/_proc publication so
        # a stopped turn can never create a process afterward.
        self._closed = False
        # Count only successfully flushed direct steers. The turn driver uses
        # the count to wait for Claude's result(s); message content never sits
        # in a host mailbox.
        self._delivered_steers = 0
        self._accepting_steers = False
        # Mirrors run()'s local result_session_id, but as an attribute so a
        # kill (which surfaces as an exception out of run(), discarding its
        # locals) still leaves the last session_id the CLI reported somewhere
        # the caller can read it. See last_known_session_id.
        self._last_session_id: str | None = None

    @property
    def last_known_session_id(self) -> str | None:
        """The most recent session_id the CLI reported this run, even if run()
        exited via an exception (e.g. the process was killed mid-turn) rather
        than a normal return. None until the CLI has reported one."""
        return self._last_session_id

    def start(self, init_timeout: float = 60.0) -> None:
        return

    def close(self) -> None:
        with self._stdin_lock:
            self._closed = True
            proc = self._proc
            self._accepting_steers = False
            if proc is not None and proc.stdin is not None:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
        try:
            if proc is not None:
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
                # stdout and stderr are deliberately not closed here. Each reader
                # thread owns its own pipe and closes it when the loop ends, because
                # a buffered stream's close() blocks on the very lock the reader
                # holds across its blocking read. Closing from this thread would
                # therefore hang for as long as the CLI stays alive and quiet on
                # that pipe — precisely the case this teardown exists for, a CLI
                # that outlived stdin EOF because background work is still running,
                # and stderr is usually silent for a whole turn. The scope teardown
                # below is what ends the process; the readers then see EOF and
                # release the pipes.
        finally:
            # Last resort after Claude Code's clean stdin-EOF shutdown above: the
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
        with self._stdin_lock:
            self._closed = True
            proc = self._proc
            self._accepting_steers = False
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

    def run(
        self,
        input_message: str,
        session_id: str | None,
        model: str,
        effort: str,
        on_message: Callable[[str | dict[str, Any]], None],
        finish_turn: Callable[[str, str], int] | None = None,
    ) -> tuple[str, str]:
        # State the operator's web-search decision to the launcher as its
        # required first argument; the launcher translates it into the WebSearch
        # deny (see host/bootstrap/helpers/run-claude-code.sh). The orchestrator
        # is the only side with a database role, so it reads the toggle here; the
        # proxy enforces the same toggle independently.
        from host.runtime.core import state

        enabled = state.read_claude_web_search()
        argv = [
            *self._command,
            f"web-search={'on' if enabled else 'off'}",
        ]
        if self._thread_id is not None:
            argv.extend(["--thread-scope", self._thread_id])
        argv.extend([
            "-p",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
            "--model",
            model,
            "--effort",
            effort,
            "--setting-sources",
            "user",
            # Deliberately no --safe-mode: the pinned CLI drops every
            # non-SDK MCP server in safe mode (verified empirically), which
            # would disable the bundled tools. --strict-mcp-config keeps the
            # MCP surface pinned to exactly the shim below, and the agent's
            # isolation comes from the OS boundaries and the installed
            # bypassPermissions user settings, not harness flags (see
            # docs/architecture/privilege-boundaries.md).
            "--strict-mcp-config",
            "--mcp-config",
            TOOLS_MCP_CONFIG,
        ])
        if self.app_instructions:
            argv.extend(["--append-system-prompt", self.app_instructions])
        if session_id:
            argv.extend(["--resume", session_id])
        self._messages = queue.Queue()
        self._stderr_tail.clear()
        with self._stdin_lock:
            if self._closed:
                raise ClaudeCodeError("Claude Code turn was closed")
            proc = subprocess.Popen(
                argv,
                cwd=_subprocess_cwd(self._command),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            self._proc = proc
            self._delivered_steers = 0
            self._accepting_steers = False
        assert proc.stdout is not None and proc.stderr is not None
        threading.Thread(target=self._read_stdout, args=(proc.stdout,), daemon=True).start()
        threading.Thread(target=self._read_stderr, args=(proc.stderr,), daemon=True).start()
        with self._stdin_lock:
            self._send_user_message_locked(input_message)
            # A direct steer cannot overtake the turn's initial message.
            self._accepting_steers = True
        if self._on_ready is not None and not self._on_ready():
            raise ClaudeCodeError("Claude Code execution stopped during startup")
        outstanding_user_messages = 1
        last_message = ""
        result_session_id = session_id
        self._last_session_id = session_id
        final: str | None = None
        settle_deadline: float | None = None

        def observe_delivered_steers(count: int | None = None) -> int:
            nonlocal outstanding_user_messages, settle_deadline
            delivered = self.take_delivered_steers() if count is None else count
            if delivered:
                outstanding_user_messages += delivered
                settle_deadline = None
            return delivered

        def finish_or_observe_late_steers() -> bool:
            """Return true once the caller has atomically finished the turn.

            The orchestrator callback shares the live turn's delivery lock
            with the API. It either records completion or returns the number
            of direct steers that committed immediately before that boundary.
            """
            if not result_session_id:
                raise ClaudeCodeError("Claude result did not include a session_id")
            assert final is not None
            delivered = (
                self.take_delivered_steers()
                if finish_turn is None
                else finish_turn(result_session_id, final)
            )
            if not delivered:
                return True
            observe_delivered_steers(delivered)
            return False

        while True:
            observe_delivered_steers()
            try:
                message = self._messages.get(timeout=1.0)
            except queue.Empty:
                if settle_deadline is not None and time.monotonic() >= settle_deadline:
                    # A result left sent user messages unaccounted for and the
                    # CLI has stayed idle since: the steer was merged into the
                    # turn that just ended, its result already covers every
                    # message, and no further result is coming. The atomic
                    # finish callback still gets the final say: a steer that
                    # arrived at this boundary must be delivered instead.
                    if finish_or_observe_late_steers():
                        assert result_session_id is not None
                        assert final is not None
                        return result_session_id, final
                self._require_proc()
                continue
            message_type = message.get("type")
            if message_type in ("assistant", "user") or (
                message_type == "system" and message.get("subtype") == "init"
            ):
                # Definite turn activity (a direct steer's turn announces
                # itself with a system init, then assistant/user events): its
                # own result will settle the count, so stop the clock entirely.
                settle_deadline = None
            elif settle_deadline is not None and message_type != "result":
                # Ambient chatter (stray system notifications, rate-limit
                # events) is not a new turn: push the deadline back rather
                # than disarming it, so an idle-but-noisy stream still settles.
                settle_deadline = time.monotonic() + STEER_SETTLE_TIMEOUT_SECONDS
            reported_session_id = message.get("session_id")
            if isinstance(reported_session_id, str):
                result_session_id = reported_session_id
                self._last_session_id = reported_session_id
                # A system/init frame may identify a newly allocated but still
                # empty session. Publish only once the provider reports actual
                # turn activity or a result for the submitted message.
                if (
                    self._on_session_id is not None
                    and not (
                        message_type == "system"
                        and message.get("subtype") == "init"
                    )
                ):
                    self._on_session_id(reported_session_id)
            if message.get("type") == "assistant":
                text = _assistant_text(message)
                if text:
                    last_message = text
                _emit_claude_content(message, on_message)
            elif message.get("type") == "user":
                _emit_claude_tool_results(message, on_message)
            elif message.get("type") != "result":
                _emit_claude_stream_status(message, on_message)
            if message.get("type") == "result":
                if message.get("subtype") != "success" or message.get("is_error"):
                    error = agent_activity.clean_text(
                        message.get("result")
                        or message.get("subtype")
                        or "Claude turn failed"
                    )
                    raise ClaudeCodeError(error)
                outstanding_user_messages = max(0, outstanding_user_messages - 1)
                final = agent_activity.clean_text(
                    message.get("result") or last_message or "Task completed."
                )
                if not outstanding_user_messages and not finish_or_observe_late_steers():
                    # The atomic boundary observed a direct steer, so its own
                    # result (or a merged result followed by the settle
                    # timeout) is still outstanding.
                    settle_deadline = time.monotonic() + STEER_SETTLE_TIMEOUT_SECONDS
                    continue
                if outstanding_user_messages:
                    # Either a direct steer's turn is still running (its events
                    # disarm the deadline above) or the steer was merged and
                    # this result is already final; wait for the stream to
                    # settle instead of forever.
                    settle_deadline = time.monotonic() + STEER_SETTLE_TIMEOUT_SECONDS
                    continue
                assert result_session_id is not None
                return result_session_id, final

    def _send_user_message_locked(self, text: str) -> None:
        proc = self._require_proc()
        assert proc.stdin is not None
        proc.stdin.write(json.dumps({
            "type": "user",
            "message": {"role": "user", "content": text},
            "parent_tool_use_id": None,
        }) + "\n")
        proc.stdin.flush()

    def steer(self, text: str) -> None:
        """Synchronously flush one user message into the live Claude CLI."""
        with self._stdin_lock:
            if not self._accepting_steers:
                raise ClaudeCodeError("Claude Code turn is not ready for steering")
            try:
                self._send_user_message_locked(text)
            except OSError as exc:
                raise ClaudeCodeError(f"Claude Code rejected the message: {exc}") from exc
            self._delivered_steers += 1

    def take_delivered_steers(self) -> int:
        with self._stdin_lock:
            delivered = self._delivered_steers
            self._delivered_steers = 0
            return delivered

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
                    self._messages.put(message)

    def _read_stderr(self, stream: IO[str]) -> None:
        with stream:
            for line in stream:
                stripped = line.strip()
                if stripped:
                    self._stderr_tail.append(stripped)

    def _require_proc(self) -> subprocess.Popen[str]:
        if self._proc is None or self._proc.poll() is not None:
            detail = "; ".join(self._stderr_tail)
            raise ClaudeCodeError(f"Claude Code process is not running{': ' + detail if detail else ''}")
        return self._proc


class ClaudeLoginProcess:
    def __init__(self, command: list[str] | None = None, start_timeout: float = LOGIN_START_TIMEOUT_SECONDS) -> None:
        self._command = command or DEFAULT_COMMAND
        self._start_timeout = start_timeout
        self._proc: subprocess.Popen[str] | None = None

    def start(self) -> ClaudeLogin:
        # Login runs no model turn; pass the launcher's required decision as
        # off (immaterial here, keeps the deny-by-default posture).
        self._proc = subprocess.Popen(
            [*self._command, "web-search=off", "auth", "login", "--claudeai"],
            cwd=_subprocess_cwd(self._command),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        assert self._proc.stdout is not None
        lines: queue.Queue[str | None] = queue.Queue()

        def read_stdout() -> None:
            assert self._proc is not None and self._proc.stdout is not None
            for line in self._proc.stdout:
                lines.put(line)
            lines.put(None)

        threading.Thread(target=read_stdout, daemon=True).start()
        output = ""
        deadline = time.monotonic() + self._start_timeout
        while time.monotonic() < deadline:
            remaining = max(0.0, deadline - time.monotonic())
            try:
                chunk = lines.get(timeout=min(0.5, remaining))
            except queue.Empty:
                if self._proc.poll() is not None:
                    raise ClaudeCodeError("Claude OAuth login exited before returning a login URL")
                continue
            if chunk is None:
                raise ClaudeCodeError("Claude OAuth login exited before returning a login URL")
            output += chunk
            match = LOGIN_URL_RE.search(output)
            if match:
                return ClaudeLogin(login_url=match.group(1))
        self.close()
        raise ClaudeTimeout("Claude OAuth login did not return a login URL in time")

    def complete(self, code: str) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise ClaudeCodeError("Claude OAuth login has not been started")
        proc.stdin.write(code.strip() + "\n")
        proc.stdin.flush()
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired as exc:
            raise ClaudeCodeError("Claude OAuth login did not complete after code submission") from exc
        if proc.returncode != 0:
            raise ClaudeCodeError("Claude OAuth login failed")

    def close(self) -> None:
        proc = self._proc
        if proc is None:
            return
        if proc.stdin is not None:
            try:
                proc.stdin.close()
            except OSError:
                pass
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        if proc.stdout is not None:
            proc.stdout.close()


def account_status() -> tuple[str, str | None, dict[str, Any] | None]:
    try:
        proc = subprocess.run(
            # No model turn here; the launcher requires the decision, and off
            # keeps the deny-by-default posture (immaterial for a status check).
            [*DEFAULT_COMMAND, "web-search=off", "auth", "status", "--json"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=STATUS_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "error", f"could not check Claude auth status: {exc!r}", None
    if proc.returncode != 0:
        return "awaiting_login", None, None
    try:
        status = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return "error", f"Claude auth status returned invalid JSON: {exc}", None
    if not isinstance(status, dict) or status.get("loggedIn") is not True:
        return "awaiting_login", None, None
    if status.get("authMethod") != "claude.ai":
        return "error", "Claude Code must be logged in with Claude.ai OAuth", None
    try:
        account = read_claude_account()
    except Exception as exc:
        return "error", f"could not read Claude account: {exc!r}", None
    if not account:
        return "error", "Claude auth is logged in but OAuth token metadata is unavailable", None
    _fill_claude_account_metadata(account, status)
    return "active", None, account


def read_claude_usage(command: list[str] | None = None) -> dict[str, Any]:
    usage_command = command or DEFAULT_COMMAND
    try:
        proc = subprocess.run(
            # /usage runs no agent turn; pass the launcher's required decision
            # as off (immaterial here, keeps the deny-by-default posture).
            [*usage_command, "web-search=off", "-p", "/usage", "--output-format", "json"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=USAGE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ClaudeCodeError(str(exc)) from exc
    if proc.returncode != 0:
        _raise_usage_probe_error("\n".join(part for part in (proc.stdout, proc.stderr) if part))
    try:
        value = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ClaudeCodeError(f"Claude usage probe returned invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ClaudeCodeError("Claude usage probe returned an invalid response")
    result = value.get("result")
    if value.get("is_error") is True or value.get("subtype") == "error":
        _raise_usage_probe_error(result if isinstance(result, str) else proc.stderr)
    if not isinstance(result, str):
        return {}
    return _parse_claude_usage_result(result)


def _raise_usage_probe_error(detail: Any) -> None:
    message = str(detail or "Claude usage probe failed").strip()[:500]
    normalized = message.lower()
    authentication_markers = (
        "failed to authenticate",
        "invalid authentication credentials",
        "invalid bearer",
        "oauth token has expired",
        "authentication_error",
        "api error: 401",
    )
    if any(marker in normalized for marker in authentication_markers):
        raise ClaudeAuthenticationError("Claude OAuth credentials are no longer valid")
    raise ClaudeCodeError(message)


def _parse_claude_usage_result(result: str, now: datetime | None = None) -> dict[str, Any]:
    """Extract usage windows from the CLI's human-readable /usage text.

    Recognizes ``Current session`` (``current_session_*``), ``Current week
    (all models)`` (``weekly_*``), and ``Current week (Fable)``
    (``fable_weekly_*``); other model-specific week lines are ignored. Windows
    parse independently and the reset time is optional per window, so a partial
    or drifted response yields whatever parsed instead of an empty snapshot;
    the first line per window wins."""
    captured_at = now or datetime.now(timezone.utc)
    usage: dict[str, Any] = {}
    for line in result.splitlines():
        window_match = USAGE_WINDOW_RE.match(line)
        if not window_match:
            continue
        week_label = window_match.group(2)
        if week_label is None:
            prefix = "current_session"
        elif week_label.strip().lower() == "all models":
            prefix = "weekly"
        elif week_label.strip().lower() == "fable":
            prefix = "fable_weekly"
        else:
            continue  # only the all-models and Fable weekly windows are tracked
        if f"{prefix}_used_percent" in usage:
            continue
        rest = window_match.group(3)
        percent_match = USAGE_PERCENT_RE.search(rest)
        if not percent_match:
            continue
        usage[f"{prefix}_used_percent"] = _percent_value(percent_match.group(1))
        resets_match = USAGE_RESETS_RE.search(rest)
        resets_at = _parse_usage_reset_at(resets_match.group(1), captured_at) if resets_match else None
        if resets_at is not None:
            usage[f"{prefix}_resets_at"] = resets_at
    return usage


def _parse_usage_reset_at(value: str, now: datetime) -> int | None:
    match = USAGE_RESET_RE.fullmatch(value)
    if not match:
        return None
    month = USAGE_RESET_MONTHS.get(match.group(1).lower())
    if month is None:
        return None
    hour12 = int(match.group(3))
    minute = int(match.group(4) or 0)
    if hour12 < 1 or hour12 > 12 or minute > 59:
        return None
    hour = hour12 % 12 + (12 if match.group(5).lower() == "pm" else 0)
    try:
        reset_at = datetime(now.year, month, int(match.group(2)), hour, minute, tzinfo=timezone.utc)
    except ValueError:
        return None
    # The provider omits the year. A December capture can legitimately report
    # an early-January reset, while a recent stale snapshot remains overdue.
    if reset_at < now - timedelta(days=183):
        try:
            reset_at = reset_at.replace(year=now.year + 1)
        except ValueError:
            return None
    return int(reset_at.timestamp())


def _percent_value(value: str) -> int | float:
    parsed = float(value)
    return int(parsed) if parsed.is_integer() else parsed


def read_claude_account(command: list[str] | None = None) -> dict[str, Any] | None:
    try:
        proc = subprocess.run(
            command or DEFAULT_ACCOUNT_COMMAND,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=ACCOUNT_HELPER_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ClaudeCodeError(f"could not read Claude account: {exc}") from exc
    if proc.returncode != 0:
        return None
    try:
        value = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ClaudeCodeError("Claude account helper returned invalid JSON") from exc
    return value if isinstance(value, dict) and value.get("access_token_sha256") else None


def read_attested_identity(
    command: list[str] | None = None, expected_token_sha256: str | None = None
) -> dict[str, Any]:
    """Server-attested identity of the agent's current Claude OAuth token.

    The root helper reads the agent credential file and asks
    api.anthropic.com/api/oauth/profile who the token belongs to, so the
    returned identity (account_uuid, email, organization_uuid, plus the
    token's access_token_sha256) is bound to the token by the provider, not
    by agent-writable metadata. Raises ClaudeCodeError when the token cannot
    be attested: missing credentials, unreachable endpoint, or a rejected
    token."""
    try:
        argv = list(command or [*DEFAULT_ACCOUNT_COMMAND, "--attest"])
        if expected_token_sha256:
            argv.extend(["--expected-token-sha256", expected_token_sha256])
        proc = subprocess.run(
            argv,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=ATTEST_HELPER_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ClaudeCodeError(f"could not attest Claude account: {exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise ClaudeCodeError(detail or "Claude account attestation failed")
    try:
        value = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ClaudeCodeError("Claude account attestation returned invalid JSON") from exc
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("account_uuid"), str)
        or not value["account_uuid"]
        or not isinstance(value.get("access_token_sha256"), str)
        or not value["access_token_sha256"]
    ):
        raise ClaudeCodeError("Claude account attestation response is incomplete")
    return value


def start_oauth_login() -> ClaudeLogin:
    global _login_process
    process = ClaudeLoginProcess()
    login = process.start()
    with _login_lock:
        if _login_process is not None:
            _login_process.close()
        _login_process = process
    return login


def complete_oauth_login(code: str) -> None:
    global _login_process
    with _login_lock:
        process = _login_process
    if process is None:
        raise ClaudeCodeError("Claude OAuth login has not been started")
    try:
        process.complete(code)
    finally:
        process.close()
        with _login_lock:
            if _login_process is process:
                _login_process = None


def close_login_process() -> None:
    global _login_process
    with _login_lock:
        process = _login_process
        _login_process = None
    if process is not None:
        process.close()


def run_turn(
    server: ClaudeCodeSession,
    input_message: str,
    session_id: str | None,
    model: str,
    effort: str,
    on_message: Callable[[str | dict[str, Any]], None],
    finish_turn: Callable[[str, str], int] | None = None,
) -> tuple[str, str]:
    return server.run(
        input_message,
        session_id,
        model,
        effort,
        on_message,
        finish_turn,
    )


def _subprocess_cwd(command: list[str]) -> str | None:
    # In production, the admin API cannot traverse the agent user's private
    # 0700 home. The sudo helper starts as root, cds there, and then drops to
    # kern-agent. Custom test commands still run from AGENT_CWD.
    return None if command == DEFAULT_COMMAND else AGENT_CWD


def _fill_claude_account_metadata(account: dict[str, Any], status: dict[str, Any]) -> None:
    if not account.get("email") and isinstance(status.get("email"), str) and status["email"].strip():
        account["email"] = status["email"].strip()
    if not account.get("organization_id") and isinstance(status.get("orgId"), str) and status["orgId"].strip():
        account["organization_id"] = status["orgId"].strip()
    # No account_id here on purpose: the trusted id always comes from the
    # stored anchor or a fresh server attestation (orchestrator._with_identity
    # replaces it on every active account), never from CLI output.
    plan_type = _extract_claude_plan_type(status)
    if plan_type:
        account["plan_type"] = plan_type


def _extract_claude_plan_type(status: dict[str, Any]) -> str | None:
    value = status.get("subscriptionType")
    if not isinstance(value, str):
        return None
    return value.strip() or None


def _assistant_text(message: dict[str, Any]) -> str:
    payload = message.get("message")
    if not isinstance(payload, dict):
        return ""
    content = payload.get("content")
    if not isinstance(content, list):
        return ""
    parts = [
        agent_activity.clean_text(block.get("text", ""))
        for block in content
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
    ]
    return "".join(parts)


def _message_content(message: dict[str, Any]) -> list[dict[str, Any]]:
    payload = message.get("message")
    if not isinstance(payload, dict):
        return []
    content = payload.get("content")
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict)]


def _claude_message_id(message: dict[str, Any], block: dict[str, Any], index: int) -> str:
    value = block.get("id") or block.get("tool_use_id") or message.get("uuid")
    if value:
        return str(value)
    payload = message.get("message")
    if isinstance(payload, dict) and payload.get("id"):
        return f"{payload['id']}:{index}"
    return f"claude:{index}:{time.monotonic_ns()}"


def _claude_tool_title(name: Any, block: dict[str, Any]) -> str:
    tool_name = str(name or "Tool")
    tool_input = block.get("input")
    if not isinstance(tool_input, dict):
        tool_input = {}
    if tool_name == "Bash":
        command = tool_input.get("command")
        return agent_activity.clip_text(command or "Shell command", agent_activity.ACTIVITY_SHORT_TEXT_BYTES)
    if tool_name in {"Read", "Write", "Edit", "Glob", "Grep"}:
        target = tool_input.get("file_path") or tool_input.get("path") or tool_input.get("pattern")
        return f"{tool_name}: {target}" if target else tool_name
    if tool_name in {"WebSearch", "WebFetch"}:
        return "Web search" if tool_name == "WebSearch" else "Fetch web page"
    return f"Tool: {tool_name}"


def _claude_tool_kind(name: Any) -> str:
    tool_name = str(name or "")
    if tool_name == "Bash":
        return "command"
    if tool_name in {"Write", "Edit"}:
        return "file_change"
    if tool_name in {"WebSearch", "WebFetch"}:
        return "search"
    return "tool"


def _emit_claude_content(
    message: dict[str, Any],
    on_message: Callable[[str | dict[str, Any]], None],
) -> None:
    """Emit text once plus semantic thinking/tool activity from one assistant message."""
    content = _message_content(message)
    text_emitted = False
    for index, block in enumerate(content):
        event: str | dict[str, Any] | None = None
        try:
            block_type = str(block.get("type") or "")
            if block_type == "text":
                if not text_emitted:
                    text_emitted = True
                    event = _assistant_text(message) or None
            else:
                activity_id = _claude_message_id(message, block, index)
                if block_type == "thinking":
                    event = agent_activity.activity(
                        "claude_code",
                        activity_id,
                        "reasoning",
                        "completed",
                        "Reasoning",
                        detail=block.get("thinking"),
                    )
                elif block_type in {"tool_use", "server_tool_use"}:
                    name = block.get("name")
                    tool_input = block.get("input")
                    event = agent_activity.activity(
                        "claude_code",
                        activity_id,
                        _claude_tool_kind(name),
                        "started",
                        _claude_tool_title(name, block),
                        detail=agent_activity.json_text(tool_input) if tool_input is not None else None,
                    )
        except Exception:
            # Provider progress is best-effort. One malformed block must not
            # abort the running turn or hide later valid blocks.
            continue
        if event is not None:
            # Deliberately outside the parser try: persistence failures are
            # host failures and must remain visible.
            on_message(event)


def _emit_claude_tool_results(
    message: dict[str, Any],
    on_message: Callable[[str | dict[str, Any]], None],
) -> None:
    for index, block in enumerate(_message_content(message)):
        try:
            block_type = str(block.get("type") or "")
            if block_type != "tool_result" and not block_type.endswith("_tool_result"):
                continue
            is_error = bool(block.get("is_error"))
            content = block.get("content")
            output = agent_activity.json_text(content) if isinstance(content, (dict, list)) else content
            event = agent_activity.activity(
                "claude_code",
                _claude_message_id(message, block, index),
                "tool",
                "completed",
                "Tool result",
                output=output,
                status="failed" if is_error else "completed",
            )
        except Exception:
            continue
        on_message(event)


def _emit_claude_stream_status(
    message: dict[str, Any],
    on_message: Callable[[str | dict[str, Any]], None],
) -> None:
    event: dict[str, Any] | None = None
    try:
        message_type = str(message.get("type") or "")
        subtype = str(message.get("subtype") or "")
        if message_type == "system" and subtype == "init":
            visible = {
                key: message[key]
                for key in ("model", "cwd", "tools", "permissionMode", "claude_code_version")
                if message.get(key) not in (None, "", [])
            }
            event = agent_activity.activity(
                "claude_code",
                _claude_message_id(message, {}, 0),
                "status",
                "completed",
                "Claude session initialized",
                detail=agent_activity.json_text(visible) if visible else None,
            )
        elif message_type == "tool_progress":
            tool_use_id = str(message.get("tool_use_id") or _claude_message_id(message, {}, 0))
            elapsed = message.get("elapsed_time_seconds")
            event = agent_activity.activity(
                "claude_code",
                tool_use_id,
                "tool",
                "started",
                "Tool progress",
                status=f"{elapsed}s" if elapsed is not None else "running",
            )
        elif message_type in {"tool_use_summary", "rate_limit_event", "auth_status"}:
            title = {
                "tool_use_summary": "Tool summary",
                "rate_limit_event": "Rate limit status",
                "auth_status": "Authentication status",
            }[message_type]
            event = agent_activity.activity(
                "claude_code",
                _claude_message_id(message, {}, 0),
                "status",
                "completed",
                title,
                detail=agent_activity.json_text({
                    key: value
                    for key, value in message.items()
                    if key not in {"type", "session_id", "uuid"}
                }),
            )
    except Exception:
        return
    if event is not None:
        on_message(event)
