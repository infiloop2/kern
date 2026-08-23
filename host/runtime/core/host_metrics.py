"""Host resource metrics and bounded agent-slice process snapshots."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import time
from typing import Any

AGENT_CGROUP_ROOT = Path("/sys/fs/cgroup/kern_agent.slice")
PROC_ROOT = Path("/proc")
AGENT_PROCESS_LIMIT = 1000

def agent_processes() -> dict[str, Any]:
    """Return a bounded process snapshot for the agent runtime slice — exactly
    the fields the admin UI renders."""
    pids = sorted(_agent_slice_pids())
    uptime = _proc_uptime()
    clk_tck = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
    processes: list[dict[str, Any]] = []
    for pid in pids[:AGENT_PROCESS_LIMIT]:
        process = _agent_process_info(pid, uptime, clk_tck)
        if process is not None:
            processes.append(process)
    return {"processes": processes, "truncated": len(pids) > AGENT_PROCESS_LIMIT}


def _agent_slice_pids() -> set[int]:
    if not AGENT_CGROUP_ROOT.is_dir():
        return set()
    pids: set[int] = set()
    try:
        for proc_file in AGENT_CGROUP_ROOT.rglob("cgroup.procs"):
            try:
                lines = proc_file.read_text().splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            for line in lines:
                try:
                    pid = int(line)
                except ValueError:
                    continue
                if pid > 0:
                    pids.add(pid)
    except OSError:
        return set()
    return pids


def _agent_process_info(pid: int, uptime: float, clk_tck: int) -> dict[str, Any] | None:
    proc_dir = PROC_ROOT / str(pid)
    try:
        stat = _proc_stat(proc_dir / "stat")
        status = _proc_status(proc_dir / "status")
    except (OSError, ValueError, IndexError):
        return None
    name = status.get("Name") or stat["name"]
    cmdline = _proc_cmdline(proc_dir / "cmdline") or f"[{name}]"
    result: dict[str, Any] = {
        "pid": pid,
        "state": stat["state"],
        "name": name,
        "cmdline": cmdline,
    }
    rss_bytes = _rss_bytes(status.get("VmRSS"))
    if rss_bytes is not None:
        result["rss_bytes"] = rss_bytes
    if uptime > 0 and clk_tck > 0:
        result["elapsed_seconds"] = int(max(0.0, uptime - (stat["start_ticks"] / clk_tck)))
    return result


def _proc_stat(path: Path) -> dict[str, Any]:
    raw = path.read_text()
    left = raw.find("(")
    right = raw.rfind(")")
    if left < 0 or right <= left:
        raise ValueError("malformed proc stat")
    fields = raw[right + 2 :].split()
    return {
        "name": raw[left + 1 : right],
        "state": fields[0],
        "start_ticks": int(fields[19]),
    }


def _proc_status(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        values[key] = value.strip()
    return values


def _proc_cmdline(path: Path) -> str:
    try:
        raw = path.read_bytes()
    except OSError:
        return ""
    return raw.rstrip(b"\0").replace(b"\0", b" ").decode("utf-8", "replace")


def _proc_uptime() -> float:
    try:
        return float((PROC_ROOT / "uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0


def _rss_bytes(rss_line: str | None) -> int | None:
    if not rss_line:
        return None
    parts = rss_line.split()
    if not parts:
        return None
    try:
        value = int(parts[0])
    except ValueError:
        return None
    unit = parts[1].lower() if len(parts) > 1 else "kb"
    return value * 1024 if unit == "kb" else value

def host_metrics() -> dict[str, Any]:
    return {
        "cpu": {"usage_percent": cpu_usage_percent()},
        "memory": memory_metrics(),
        "filesystem": filesystem_metrics(),
        "swap": swap_metrics(),
    }


def cpu_usage_percent() -> float:
    # Deliberately samples /proc/stat 50ms apart on the calling thread: health
    # requests each run on their own handler thread, so the brief block delays
    # only that response, and it keeps the metric stateless.
    first = _cpu_times()
    time.sleep(0.05)
    second = _cpu_times()
    idle_delta = second["idle"] - first["idle"]
    total_delta = second["total"] - first["total"]
    if total_delta <= 0:
        return 0.0
    return round(100.0 * (1.0 - idle_delta / total_delta), 1)


def _cpu_times() -> dict[str, int]:
    values = [int(part) for part in Path("/proc/stat").read_text().splitlines()[0].split()[1:]]
    idle = values[3] + values[4]
    return {"idle": idle, "total": sum(values)}


def memory_metrics() -> dict[str, int]:
    mem = _proc_meminfo()
    total = mem["MemTotal"] * 1024
    available = mem.get("MemAvailable", 0) * 1024
    return {"used_bytes": total - available, "total_bytes": total}


def _filesystem_usage(path: str) -> dict[str, int] | None:
    try:
        usage = shutil.disk_usage(path)
    except FileNotFoundError:
        return None
    return {"used_bytes": usage.used, "total_bytes": usage.total}


def filesystem_metrics() -> dict[str, Any]:
    root = _filesystem_usage("/") or {"used_bytes": 0, "total_bytes": 0}
    mounts = {"root": root}
    for name, path in (
        ("admin", "/mnt/kern-admin"),
        ("agent", "/mnt/kern-agent"),
    ):
        usage = _filesystem_usage(path)
        if usage is not None:
            mounts[name] = usage
    return {"mounts": mounts}


def swap_metrics() -> dict[str, int]:
    mem = _proc_meminfo()
    total = mem.get("SwapTotal", 0) * 1024
    free = mem.get("SwapFree", 0) * 1024
    return {"allocated_bytes": total, "used_bytes": total - free}


def _proc_meminfo() -> dict[str, int]:
    values: dict[str, int] = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, value = line.split(":", 1)
        values[key] = int(value.strip().split()[0])
    return values
