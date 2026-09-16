"""Thread identity derived from a peer PID and a host-created process scope."""

from pathlib import Path
import re
import socket
import subprocess

THREAD_SCOPE_RE = re.compile(r"(?:^|/)kern-agent-thread-([A-Za-z0-9_-]{1,64})\.scope$")


def peer_thread_id(pid: int, proc_root: Path = Path("/proc")) -> str | None:
    """Derive the MCP shim's host thread from its kernel-assigned cgroup.

    This is an informational identity, not app authorization. The peer PID is
    obtained with SO_PEERCRED and the thread id is accepted only from the
    root-created per-turn scope name.
    """
    try:
        lines = (proc_root / str(pid) / "cgroup").read_text().splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    for line in lines:
        thread_id = thread_id_from_cgroup(line.split(":", 2)[-1])
        if thread_id is not None:
            return thread_id
    return None


def thread_id_from_cgroup(path: str) -> str | None:
    # systemd may hex-escape unit-name bytes in a cgroup component.
    path = re.sub(r"\\x([0-9a-fA-F]{2})", lambda match: chr(int(match.group(1), 16)), path)
    match = THREAD_SCOPE_RE.search(path)
    return match.group(1) if match is not None else None


def tcp_peer_thread_id(connection: socket.socket) -> str | None:
    """Read the local client's socket cgroup from kernel socket diagnostics.

    Git uses the TCP proxy, so SO_PEERCRED is unavailable. Match the reversed
    connection tuple to the client's established socket, never request headers
    or branch names. Missing diagnostics leave the origin unknown.
    """
    try:
        client_host, client_port = connection.getpeername()[:2]
        server_host, server_port = connection.getsockname()[:2]
        result = subprocess.run(
            ["/usr/bin/ss", "-Hnt", "--cgroup", "state", "established",
             f"src {client_host} and sport = :{client_port} and dst {server_host} and dport = :{server_port}"],
            capture_output=True, text=True, check=True, timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    rows = result.stdout.splitlines()
    if len(rows) != 1:
        return None
    for field in rows[0].split():
        if field.startswith("cgroup:"):
            return thread_id_from_cgroup(field.removeprefix("cgroup:"))
    return None
