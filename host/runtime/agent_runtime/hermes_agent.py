"""Hermes runtime adapter (AWS Bedrock inference).

Hermes (NousResearch's hermes-agent) runs on AWS Bedrock. Its supported
automation surface here is the headless one-query API behind a stdin adapter:
one process per prompt, quiet output,
approvals disabled (the OS/proxy boundary is the enforcement), fixed
terminal/file/bundled-tools toolsets with the host MCP shim connected, and
reported/resumed session ids. The launcher pins the provider and environment;
this adapter supplies only the prompt, model, and session selection.

Hermes has no mid-turn steering channel in this mode. Each API turn maps to
exactly one Hermes process and model turn; later input starts a new turn on
the same thread and resumes its stored Hermes session. The provider's
credential surface (operator paste and STS attestation) is owned by
``host.runtime.agent_runtime.bedrock_credentials``.
"""

from __future__ import annotations

import json
import queue
import re
import secrets
import subprocess
import threading
import time
from typing import Any, Callable

from host.runtime.agent_runtime import agent_activity, bedrock_credentials, thread_scope
from host.runtime.agent_runtime.harness import subprocess_cwd

DEFAULT_COMMAND = ["/usr/bin/sudo", "-n", "/usr/local/lib/kern-host/run-hermes"]
AGENT_CWD = "/mnt/kern-agent/agent-home"
# Bounded by Hermes's own agent.max_turns; the wait is generous because one
# prompt can run many tool-using turns.
TURN_TIMEOUT_SECONDS = 45 * 60
PROCESS_EXIT_TIMEOUT_SECONDS = 3
# The captured id re-enters the CLI as the --resume value, so it must never
# look like a flag: require a leading alphanumeric.
SESSION_ID_RE = re.compile(r"^session_id:\s*([A-Za-z0-9][\S]*)\s*$", re.MULTILINE)
# Kept byte-for-byte in sync with the launcher wrapper's
# ``hermes-stdin.py:ACTIVITY_LINE_PREFIX``. Hermes runs quiet, so its stdout is
# the answer text; the wrapper interleaves one activity record per line behind
# this Record-Separator sentinel. To keep the activity channel out of reach of
# the answer text — which is model-controlled and could otherwise reproduce a
# bare sentinel to forge a card or get itself dropped from the response — the
# host mints a fresh random nonce per turn (``--activity-nonce``) and only a
# line carrying that exact secret is treated as activity. The model never sees
# the nonce, so its answer cannot forge the frame (a same-user shell reading
# the process argv is out of scope here, as everywhere: the OS/proxy boundary
# is the enforcement).
ACTIVITY_LINE_PREFIX = "\x1ekern-activity "


def _activity_marker(nonce: str) -> str:
    """The full per-turn line prefix a wrapper activity record carries."""
    return f"{ACTIVITY_LINE_PREFIX}{nonce} "
# The orchestrator talks to every provider module through one contract; the
# Bedrock connection satisfies the account side of it.
account_status = bedrock_credentials.account_status


class HermesAgentError(RuntimeError):
    pass


class HermesSession:
    """Owns at most one running Hermes chat process.

    start() exists to satisfy the orchestrator's server contract; each prompt
    spawns its own process in run() because the headless chat CLI is
    single-shot and sessions are persisted on disk under the agent home.
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
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[str] | None = None
        self._closed = False
        # _stream_process sets this as soon as it sees the CLI's session_id
        # stderr line, ahead of the process exiting — a kill (proc.kill(),
        # raising before _run_prompt's own end-of-process parse ever runs)
        # still leaves the orchestrator able to read it and persist the
        # thread mapping. None if the CLI has not reported one yet, which is
        # possible right up to end of turn: unlike Codex's threadId or
        # Claude's session_id, this adapter has no confirmation of how early
        # the vendored Hermes CLI actually writes the line.
        self.last_known_session_id: str | None = None

    def start(self, init_timeout: float = 60.0) -> None:
        return

    def close(self) -> None:
        with self._lock:
            self._closed = True
            proc = self._proc
        if proc is not None and proc.poll() is None:
            # Best-effort signal only: the production launcher runs as root, so
            # this unprivileged kill fails with EPERM and the root scope
            # teardown below is the real kill; a same-user command (tests) just
            # dies here. A signal failure must never escape close() — the
            # orchestrator keeps a thread fenced when close() raises.
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=PROCESS_EXIT_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                pass
        # Last resort after the launcher signal above: a killed turn leaves the
        # runtime's own descendants (a shell still in a long command) in this
        # thread's systemd scope, keeping the scope's cgroup — and its name —
        # alive so the next turn on this thread cannot recreate it. It runs as
        # root, so it frees the scope even when the signal above could not, and
        # close() returns only once the whole cgroup is gone; a clean exit
        # already emptied it, so this is then a no-op. The orchestrator fences
        # the thread on exactly this contract.
        thread_scope.stop_thread_scope(self._thread_id, self._command, DEFAULT_COMMAND)

    def interrupt(self) -> None:
        """Interrupt a turn without waiting for process/scope teardown."""
        with self._lock:
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

    def run(
        self,
        input_message: str,
        session_id: str | None,
        model: str,
        effort: str,
        on_message: Callable[[str | dict[str, Any]], None],
    ) -> tuple[str, str]:
        # Hermes exposes no mid-turn steering channel. The API rejects steers
        # before they reach this shared runtime contract, so one turn always
        # remains one process and one model turn.
        del effort
        from host.runtime.core import state

        region = state.read_bedrock_region()
        if not region:
            raise HermesAgentError("the AWS Bedrock integration has no configured region")
        result_session_id, last_message = self._run_prompt(
            region, input_message, session_id, model, on_message
        )
        on_message(last_message)
        return result_session_id, last_message

    def _run_prompt(
        self,
        region: str,
        prompt: str,
        session_id: str | None,
        model: str,
        on_activity: Callable[[dict[str, Any]], None],
    ) -> tuple[str, str]:
        # Per-turn secret that frames the wrapper's activity lines so the
        # model-controlled answer text can never be mistaken for (or forge) an
        # activity record.
        nonce = secrets.token_hex(16)
        argv = [*self._command, f"region={region}"]
        if self._thread_id is not None:
            argv.extend(["--thread-scope", self._thread_id])
        argv.extend(["--model", model, "--activity-nonce", nonce])
        if session_id:
            argv.extend(["--resume", session_id])
        with self._lock:
            if self._closed:
                raise HermesAgentError("Hermes turn was closed")
            # No operator credential crosses this boundary: the launcher
            # injects the dummy Bedrock routing identity and the network
            # proxy re-signs each allowed request with the operator's real key.
            self._proc = subprocess.Popen(
                argv,
                cwd=_subprocess_cwd(self._command),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            proc = self._proc
        try:
            stdout, stderr = self._stream_process(
                proc, prompt, on_activity, _activity_marker(nonce)
            )
        except subprocess.TimeoutExpired as exc:
            self.close()
            raise HermesAgentError("Hermes turn timed out") from exc
        finally:
            with self._lock:
                self._proc = None
        if self._closed:
            raise HermesAgentError("Hermes turn was closed")
        if proc.returncode != 0:
            detail = (stderr or stdout or "").strip()[:500]
            raise HermesAgentError(detail or f"Hermes exited with status {proc.returncode}")
        # --pass-session-id prints the session line to stderr; the answer text
        # is stdout.
        match = SESSION_ID_RE.search(stderr or "") or SESSION_ID_RE.search(stdout or "")
        new_session_id = match.group(1) if match else session_id
        if not new_session_id:
            raise HermesAgentError("Hermes did not report a session id")
        answer = _answer_text(stdout)
        if not answer:
            raise HermesAgentError("Hermes returned no answer text")
        self.last_known_session_id = new_session_id
        if self._on_session_id is not None:
            self._on_session_id(new_session_id)
        return new_session_id, answer

    def _stream_process(
        self,
        proc: subprocess.Popen[str],
        prompt: str,
        on_activity: Callable[[dict[str, Any]], None],
        activity_marker: str,
    ) -> tuple[str, str]:
        """Feed the prompt and stream stdout, splitting live activity records
        from the answer text.

        Hermes runs quiet, so stdout carries the answer interleaved with the
        wrapper's sentinel-framed activity lines. Each activity line is
        validated and emitted through ``on_activity`` as it arrives — from this
        one driving thread, never a reader thread, so persistence stays
        serialized as it is for Codex and Claude Code. The plain lines and the
        full stderr are returned so the existing session-id, answer, and
        exit-status handling is unchanged. Raises ``subprocess.TimeoutExpired``
        (handled by the caller as a killed, timed-out turn) if the stream does
        not finish within ``TURN_TIMEOUT_SECONDS``. stdin, stdout, and stderr
        are pumped on separate threads so a large prompt or a full pipe cannot
        deadlock the turn."""
        stderr_parts: list[str] = []
        lines: queue.Queue[str | None] = queue.Queue()
        ready_errors: list[Exception] = []

        def pump_stdout() -> None:
            try:
                assert proc.stdout is not None
                for line in proc.stdout:
                    lines.put(line)
            except (OSError, ValueError):
                # The timeout/kill path closes stdout from the driving thread
                # while this read blocks; that surfaces as a closed-file read,
                # not a real error. The sentinel below still ends the loop.
                pass
            finally:
                lines.put(None)

        def pump_stderr() -> None:
            try:
                assert proc.stderr is not None
                for line in proc.stderr:
                    stderr_parts.append(line)
                    if self.last_known_session_id is None:
                        match = SESSION_ID_RE.search(line)
                        if match:
                            self.last_known_session_id = match.group(1)
            except (OSError, ValueError):
                pass

        def pump_stdin() -> None:
            # Hermes reads the whole prompt from stdin before it works, so a
            # closed pipe or a dead process (kill mid-write) is expected, not
            # an error to surface.
            try:
                assert proc.stdin is not None
                proc.stdin.write(prompt)
                proc.stdin.close()
                if self._on_ready is not None and not self._on_ready():
                    ready_errors.append(HermesAgentError("Hermes execution stopped during startup"))
                    self.interrupt()
            except Exception as exc:  # callback failures must reach the execution owner
                ready_errors.append(exc)

        stderr_thread = threading.Thread(target=pump_stderr, daemon=True)
        for target in (pump_stdout, pump_stdin):
            threading.Thread(target=target, daemon=True).start()
        stderr_thread.start()

        plain: list[str] = []
        deadline = time.monotonic() + TURN_TIMEOUT_SECONDS
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(proc.args, TURN_TIMEOUT_SECONDS)
                try:
                    line = lines.get(timeout=min(1.0, remaining))
                except queue.Empty:
                    continue
                if line is None:
                    break
                if line.startswith(activity_marker):
                    record = _activity_from_line(line, activity_marker)
                    if record is not None:
                        on_activity(record)
                else:
                    plain.append(line)
            # stdout reached EOF; give the process the rest of the budget to
            # exit and stderr to drain so returncode and the session-id line
            # are ready.
            proc.wait(timeout=max(0.0, deadline - time.monotonic()))
            stderr_thread.join(timeout=5)
            if ready_errors:
                raise ready_errors[0]
        finally:
            # communicate() used to own pipe teardown; do it here so a normal
            # or timed-out turn never leaks the stdio descriptors.
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
        return "".join(plain), "".join(stderr_parts)


def run_turn(
    server: HermesSession,
    input_message: str,
    session_id: str | None,
    model: str,
    effort: str,
    on_message: Callable[[str | dict[str, Any]], None],
) -> tuple[str, str]:
    return server.run(
        input_message,
        session_id,
        model,
        effort,
        on_message,
    )


def _answer_text(stdout: str | None) -> str:
    """The final answer: stdout minus the session line and blank edges.

    Activity lines are stripped upstream in ``_stream_process`` before they
    reach here, so this only has to drop the reported session-id line."""
    lines = [
        line for line in (stdout or "").splitlines()
        if not SESSION_ID_RE.fullmatch(line.strip())
    ]
    return "\n".join(lines).strip()


def _activity_from_line(line: str, activity_marker: str) -> dict[str, Any] | None:
    """Validate one nonce-framed activity line from the launcher wrapper.

    The caller has already matched ``activity_marker`` (the per-turn secret
    prefix). The wrapper runs as the untrusted agent user, so its records are
    re-validated and bounded here at the host boundary — a malformed or
    out-of-contract line is dropped rather than shown or persisted."""
    payload = line[len(activity_marker):].strip()
    try:
        record = json.loads(payload)
    except (ValueError, TypeError):
        return None
    return agent_activity.normalize_record(record)


def _subprocess_cwd(command: list[str]) -> str | None:
    # In production, the admin API cannot traverse the agent user's private
    # 0700 home. The sudo helper starts as root, cds there, and then drops to
    # kern-agent. Custom test commands still run from AGENT_CWD.
    return subprocess_cwd(command, DEFAULT_COMMAND, AGENT_CWD)
