"""Script runtime adapter: one static bash script from the agent home per turn.

This is the runtime for scheduled automation that needs no model — a backup, a
sync, a health check. It satisfies the same adapter contract as the model
runtimes (``account_status`` plus a session object with
``start``/``run``/``interrupt``/``close``), so a script run is an ordinary host
thread: it is admitted through the orchestrator, appears in run history, is
stoppable, and is torn down through the same per-thread systemd scope.

What differs is deliberate and small:

- The turn's message is the script's absolute path, not a prompt. The path
  contract lives in ``host.agent_scripts`` because the workspace validates the
  same spelling when a schedule is saved.
- There is no session to resume. Each run starts a fresh process, so the
  adapter reports no provider session id and every run is independent.
- There is no steering channel; the orchestrator rejects a second message
  while a script turn is live.
- The turn budget is fixed at ``SCRIPT_TIMEOUT_SECONDS``. The launcher applies
  the same limit to the scope, so this timeout is the fast path rather than
  the only defence.

The script's combined output is recorded as the turn's one agent message on
every path: a run that exits non-zero and a run abandoned at the timeout both
record what the script printed before failing the turn, because the output of a
run that went wrong is the output worth keeping. It is bounded as it is read
rather than afterwards, and decoded leniently, so neither a runaway loop nor a
stray non-UTF-8 byte can cost the admin API its memory or the run its history.
"""

from __future__ import annotations

import codecs
import io
import subprocess
import threading
from typing import Any, Callable, cast

from host.agent_scripts import AGENT_HOME, SCRIPT_TIMEOUT_SECONDS, script_path_error
from host.runtime.agent_runtime import thread_scope
from host.runtime.agent_runtime.harness import subprocess_cwd

DEFAULT_COMMAND = ["/usr/bin/sudo", "-n", "/usr/local/lib/kern-host/run-agent-script"]
AGENT_CWD = AGENT_HOME
TURN_TIMEOUT_SECONDS = SCRIPT_TIMEOUT_SECONDS
PROCESS_EXIT_TIMEOUT_SECONDS = 3
# The recorded message keeps the end of a long run: a script that fails does so
# at the end, and the tail is where its error is.
MAX_OUTPUT_CHARS = 4000
TRUNCATION_NOTICE = "[earlier output omitted]\n"
READ_CHUNK_BYTES = 64 * 1024
# How long the reader may keep draining after the process itself has exited.
OUTPUT_DRAIN_SECONDS = 5
# A failing run reports a short excerpt in the thread error; the full output is
# already recorded as the run's agent message.
MAX_ERROR_CHARS = 500


class ScriptRunError(RuntimeError):
    """A run that cannot produce a result.

    ``output`` carries whatever the script managed to print first, so every
    failure keeps its diagnostics — a script that reports where it got to and
    then hangs is exactly the run whose output is worth the most.
    """

    def __init__(self, message: str, output: str = "") -> None:
        super().__init__(message)
        self.output = output


def account_status() -> tuple[str, str | None, dict[str, Any]]:
    """The script runtime is always available.

    It runs no provider, holds no credential, and has nothing to log in to, so
    it satisfies the orchestrator's provider contract by reporting active with
    no account. Whether a given script can reach the network is decided by the
    ordinary network policy, exactly as it is for a model runtime's shell.
    """
    return "active", None, {}


class ScriptSession:
    """Owns at most one running script process.

    ``start()`` exists to satisfy the orchestrator's server contract; the
    process is spawned in ``run()`` because a script turn is single-shot.
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
        # Accepted for the shared adapter contract and never called: a script
        # run resumes nothing, so there is no provider session to persist.
        self._on_session_id = on_session_id
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[bytes] | None = None
        self._closed = False
        self.last_known_session_id: str | None = None

    def start(self, init_timeout: float = 60.0) -> None:
        return

    def close(self) -> None:
        with self._lock:
            self._closed = True
            proc = self._proc
        if proc is not None and proc.poll() is None:
            # Best-effort only, exactly as for Hermes: under the production
            # launcher the process is root-owned, so the scope teardown below
            # is the real kill. close() must never raise — the orchestrator
            # keeps a thread fenced when it does.
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=PROCESS_EXIT_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                pass
        # The output pipe is deliberately not touched here: the reader thread
        # owns it, and closing it from this side would block on that reader's
        # in-flight read. The kill above is what ends that read.
        #
        # A killed script leaves its own children (a sleep, a curl, a
        # background job) in this thread's scope, which keeps the scope name
        # alive. Freeing the whole cgroup here is what lets the next run of the
        # same schedule recreate it.
        thread_scope.stop_thread_scope(self._thread_id, self._command, DEFAULT_COMMAND)

    def interrupt(self) -> None:
        """Interrupt a run without waiting for process or scope teardown."""
        with self._lock:
            self._closed = True
            proc = self._proc
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
        # A script run carries no model selection and resumes no session; both
        # are part of the shared runtime contract and are recorded on the turn
        # for history, not consulted here.
        del session_id, model, effort
        script_path = input_message.strip()
        error = script_path_error(script_path)
        if error is not None:
            raise ScriptRunError(error)
        # Record what the script printed before deciding the turn's outcome, so
        # a failed run keeps its diagnostics on every path — including a run
        # abandoned mid-flight, which never returns a result at all.
        try:
            output, returncode = self._run_script(script_path)
        except ScriptRunError as exc:
            if exc.output:
                on_message(exc.output)
            raise
        if output:
            on_message(output)
        if returncode != 0:
            detail = _error_excerpt(output)
            raise ScriptRunError(
                f"script exited with status {returncode}: {detail}"
                if detail
                else f"script exited with status {returncode}"
            )
        if not output:
            on_message("The script finished with no output.")
        # No resumable session: the empty id tells the orchestrator there is
        # nothing to persist for the next run.
        return "", output

    def _run_script(self, script_path: str) -> tuple[str, int]:
        argv = list(self._command)
        if self._thread_id is not None:
            argv.extend(["--thread-scope", self._thread_id])
        argv.append(script_path)
        with self._lock:
            if self._closed:
                raise ScriptRunError("script run was closed")
            self._proc = subprocess.Popen(
                argv,
                cwd=_subprocess_cwd(self._command),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                # One ordered stream: a script's diagnostics are only readable
                # interleaved with the output they explain. The pipe stays
                # binary — a script is arbitrary and may emit bytes that are
                # not text at all, and text mode would decode strictly against
                # the admin service's locale and abandon the whole stream on
                # the first bad byte. _collect_output decodes with replacement.
                stderr=subprocess.STDOUT,
            )
            proc = self._proc
        tail = _BoundedTail()
        try:
            # The process exists, so the turn has started; a rejected start
            # (the turn was stopped meanwhile) tears it down rather than
            # letting it run to completion with nowhere to record the result.
            if self._on_ready is not None and not self._on_ready():
                # No reader has been started yet, so this side owns the pipe.
                _close_stream(proc)
                self.close()
                raise ScriptRunError("script run stopped during startup")
            _collect_output(proc, tail)
        except subprocess.TimeoutExpired as exc:
            self.close()
            raise ScriptRunError(
                f"script timed out after {TURN_TIMEOUT_SECONDS // 60} minutes",
                tail.text(),
            ) from exc
        finally:
            with self._lock:
                self._proc = None
        if self._closed:
            raise ScriptRunError("script run was closed", tail.text())
        return tail.text(), proc.returncode


def run_turn(
    server: ScriptSession,
    input_message: str,
    session_id: str | None,
    model: str,
    effort: str,
    on_message: Callable[[str | dict[str, Any]], None],
) -> tuple[str, str]:
    return server.run(input_message, session_id, model, effort, on_message)


class _BoundedTail:
    """The end of a stream, trimmed as it arrives rather than afterwards.

    A scheduled script is unattended and may be arbitrarily noisy, so the
    bound has to apply to the reading and not only to the recording: keeping a
    whole run to truncate it at the end would let one runaway loop grow the
    admin API's memory for the length of the turn.

    Written by the reader thread and read by the driving thread, deliberately
    without a lock: attribute assignment is atomic, so a read taken while the
    reader is still draining yields a slightly older tail, never a torn one.
    """

    def __init__(self) -> None:
        self._text = ""
        self._truncated = False

    def add(self, chunk: str) -> None:
        text = self._text + chunk
        if len(text) > MAX_OUTPUT_CHARS:
            self._truncated = True
            text = text[-MAX_OUTPUT_CHARS:]
        self._text = text

    def text(self) -> str:
        text = self._text.strip()
        if not self._truncated:
            return text
        return TRUNCATION_NOTICE + text[-(MAX_OUTPUT_CHARS - len(TRUNCATION_NOTICE)):]


def _collect_output(proc: subprocess.Popen[bytes], tail: _BoundedTail) -> None:
    """Drain the process's combined output into ``tail`` until it exits.

    The tail is the caller's, not a return value, so an abandoned run can
    still record what the script printed before it stopped responding.

    The read runs on its own thread so a full pipe cannot block the wait, and
    the drain after exit is bounded: a wedged descendant still holding the
    pipe open is freed by the scope teardown, not by waiting here.

    The reader owns the stream and is the only thing that closes it. Closing a
    buffered reader takes the same lock a blocked read holds, so a close from
    this thread would wait out the very run the caller is trying to abandon.
    On the timeout path the caller's kill ends the process, the read sees EOF,
    and the reader closes on its way out.
    """

    def pump() -> None:
        if proc.stdout is None:
            return
        # A binary PIPE is a BufferedReader; the Popen annotation widens it to
        # IO[bytes], which does not carry read1.
        stream = cast(io.BufferedReader, proc.stdout)
        # read1 returns what has arrived instead of waiting for a full chunk,
        # so a script that prints and then hangs has already handed over its
        # diagnostics by the time the turn is abandoned. Decoding is
        # incremental so a multi-byte character split across two reads still
        # decodes once, and lenient so one stray byte from a binary-emitting
        # tool cannot discard the run's output.
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        try:
            while chunk := stream.read1(READ_CHUNK_BYTES):
                tail.add(decoder.decode(chunk))
            tail.add(decoder.decode(b"", final=True))
        except (OSError, ValueError):
            pass
        finally:
            _close_stream(proc)

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    proc.wait(timeout=TURN_TIMEOUT_SECONDS)
    reader.join(timeout=OUTPUT_DRAIN_SECONDS)


def _close_stream(proc: subprocess.Popen[bytes]) -> None:
    if proc.stdout is None:
        return
    try:
        proc.stdout.close()
    except (OSError, ValueError):
        pass


def _error_excerpt(output: str) -> str:
    return output.strip()[-MAX_ERROR_CHARS:]


def _subprocess_cwd(command: list[str]) -> str | None:
    # In production the admin API cannot traverse the agent user's private
    # 0700 home; the sudo launcher starts as root, cds there, and demotes to
    # kern-agent. A custom test command still runs from AGENT_CWD.
    return subprocess_cwd(command, DEFAULT_COMMAND, AGENT_CWD)
