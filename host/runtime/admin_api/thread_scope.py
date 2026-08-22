"""Host-side teardown of per-thread transient agent scopes.

Every turn runs inside a systemd scope named after its host thread
(``kern-agent-thread-<thread_id>.scope``, created by the run-*
launchers). Freeing that scope after a turn — killed or completed — is a host
invariant shared by all agent runtimes, so it lives here rather than in each
adapter: the privileged stop-agent-thread helper SIGKILLs the scope's whole
cgroup and returns once the unit is gone, so a same-thread follow-up can
recreate the name. See ``host/bootstrap/helpers/stop-agent-thread.sh``.
"""

from __future__ import annotations

import subprocess

STOP_COMMAND = ["/usr/bin/sudo", "-n", "/usr/local/lib/kern-host/stop-agent-thread"]
INTERRUPT_TIMEOUT_SECONDS = 3
CLOSE_TIMEOUT_SECONDS = 7


class ThreadScopeError(RuntimeError):
    pass


def _is_production_turn(command: list[str], launcher_command: list[str]) -> bool:
    return command[: len(launcher_command)] == launcher_command


def interrupt_thread_scope(
    thread_id: str | None, command: list[str], launcher_command: list[str]
) -> None:
    """Request SIGKILL for a production turn scope without waiting to reap it."""
    if thread_id is None or not _is_production_turn(command, launcher_command):
        return
    try:
        subprocess.run(
            [*STOP_COMMAND, "--signal-only", thread_id],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=INTERRUPT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        # The owning execution thread performs the authoritative bounded close.
        pass


def stop_thread_scope(
    thread_id: str | None, command: list[str], launcher_command: list[str]
) -> None:
    """Free the thread's scope; a no-op unless this is a production launcher turn.

    Only the production sudo launcher creates a real systemd scope; a custom
    test command runs in-process with no scope to stop. Codex folds
    ``--thread-scope`` into its command, so the launcher is matched by prefix.
    """
    if thread_id is None or not _is_production_turn(command, launcher_command):
        return
    try:
        result = subprocess.run(
            [*STOP_COMMAND, thread_id],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=CLOSE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ThreadScopeError("timed out reaping the agent process scope") from exc
    if result.returncode != 0:
        raise ThreadScopeError("the agent process scope did not close")
