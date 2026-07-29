#!/usr/bin/env bash
set -euo pipefail

# Tear down a host thread's transient agent scope. A killed or finished task
# leaves its runtime process, and anything that process spawned (a shell still
# inside a long-running command), in the cgroup of the
# kern-agent-thread-<thread_id>.scope the run-* launcher created.
# Signalling the launcher only reparents those descendants; while any remain the
# scope stays active and its name cannot be reused, so the next task on this
# thread fails to recreate the identically named scope. SIGKILL the whole cgroup
# first: this is a kill, so graceful termination buys nothing, and it avoids
# systemctl stop blocking for the scope's TimeoutStopSec on a child that ignores
# SIGTERM. stop then reaps the emptied unit promptly and returns once it is gone;
# reset-failed frees the name even if the stopped scope lingers as failed. All
# three are no-ops when the scope is already gone (the normal-completion path),
# so the kill path can call this after every turn. Admin invokes this exact path
# through the kern-host sudoers policy.

signal_only=false
if [ "${1:-}" = "--signal-only" ]; then
  signal_only=true
  shift
fi

thread_id="${1:-}"
# The id becomes a unit name, so validate it exactly as the run-* launchers do.
if ! [[ "${thread_id}" =~ ^[A-Za-z0-9_-]{1,64}$ ]]; then
  echo "stop-agent-thread: invalid thread id: ${thread_id:-<missing>}" >&2
  exit 64
fi

scope="kern-agent-thread-${thread_id}.scope"
systemctl kill --signal=KILL "${scope}" 2>/dev/null || true
if $signal_only; then
  exit 0
fi

# Do not inherit systemd's much larger default stop deadline. SIGKILL has
# already emptied every healthy cgroup; request unit reaping asynchronously,
# then verify it for five seconds. A scope still active after that is a host
# failure (unresponsive PID 1 or an unkillable D-state process), and the caller
# deliberately keeps the thread fenced.
systemctl stop --no-block "${scope}" 2>/dev/null || true
for _attempt in $(seq 1 50); do
  if ! systemctl is-active --quiet "${scope}" 2>/dev/null; then
    systemctl reset-failed "${scope}" 2>/dev/null || true
    exit 0
  fi
  sleep 0.1
done
exit 1
