"""Shared admin request capacity and bounded, payload-free stall diagnostics."""
from __future__ import annotations

from http.server import ThreadingHTTPServer
from pathlib import Path
import stat
import sys
import threading
import time
from typing import Any

from host.constants import WORKSPACE_BROWSER_SOCKET_PATH
from host.runtime.core import host_errors

MAX_CONCURRENT_REQUESTS = 512
SLOW_REQUEST_SECONDS = 10
REPORT_INTERVAL_SECONDS = 60
_BUSY_BODY = b'{"error":{"message":"Kern is busy. Please try again shortly.","code":"host_busy"}}'
_BUSY_RESPONSE = (
    b"HTTP/1.1 503 Service Unavailable\r\nContent-Type: application/json\r\n"
    b"Connection: close\r\nCache-Control: no-store\r\nRetry-After: 5\r\nContent-Length: "
    + str(len(_BUSY_BODY)).encode() + b"\r\n\r\n" + _BUSY_BODY
)


def request_group(method: str, path: str) -> str:
    # Never record URL parameters, resource ids, file paths, or capability tokens.
    for prefix in ("/v1/workspace/chat", "/v1/workspace/web-apps", "/v1/workspace/memory",
                   "/v1/workspace/schedules", "/v1/agent-runtime", "/v1/agent-files",
                   "/v1/approvals", "/v1/health", "/v1/login", "/v1/tools"):
        if path == prefix or path.startswith(prefix + "/"):
            return f"{method} {prefix}"
    return "other admin request"


def stack_location(frame: Any) -> str:
    parts: list[str] = []
    while frame is not None and len(parts) < 8:
        parts.append(f"{Path(frame.f_code.co_filename).name}:{frame.f_code.co_name}:{frame.f_lineno}")
        frame = frame.f_back
    return " <- ".join(parts)[:512]


def workspace_socket_snapshot() -> dict[str, int]:
    """Check the proxy socket without opening a competing connection."""
    try:
        info = Path(WORKSPACE_BROWSER_SOCKET_PATH).stat()
    except OSError:
        return {"workspace_socket_present": 0}
    return {
        "workspace_socket_present": int(stat.S_ISSOCK(info.st_mode)),
        "workspace_socket_mode": stat.S_IMODE(info.st_mode),
        "workspace_socket_uid": info.st_uid,
    }


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = MAX_CONCURRENT_REQUESTS

    def __init__(self, *args: Any, max_workers: int = MAX_CONCURRENT_REQUESTS, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._request_slots = threading.BoundedSemaphore(max_workers)
        self._capacity = max_workers
        self._diagnostic_lock = threading.Lock()
        self._active: dict[int, tuple[float, str]] = {}
        self._rejected = 0
        self._last_report = float("-inf")
        self._reporting = False

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._request_slots.acquire(blocking=False):
            with self._diagnostic_lock:
                self._rejected += 1
            # Never wait for a worker on the accept loop. Even a client which
            # does not read this tiny response may hold us for at most 100ms.
            try:
                request.settimeout(0.1)
                request.sendall(_BUSY_RESPONSE)
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._request_slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        ident = threading.get_ident()
        with self._diagnostic_lock:
            self._active[ident] = (time.monotonic(), "reading request")
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self._diagnostic_lock:
                self._active.pop(ident, None)
            self._request_slots.release()

    def describe_request(self, method: str, path: str) -> None:
        ident = threading.get_ident()
        with self._diagnostic_lock:
            if ident in self._active:
                started, _ = self._active[ident]
                self._active[ident] = (started, request_group(method, path))

    def service_actions(self) -> None:
        now = time.monotonic()
        with self._diagnostic_lock:
            slow = sum(now - started >= SLOW_REQUEST_SECONDS for started, _ in self._active.values())
            if (not self._rejected and not slow) or self._reporting or now - self._last_report < REPORT_INTERVAL_SECONDS:
                return
            active = sorted(self._active.items(), key=lambda item: item[1][0])
            rejected = self._rejected
            self._rejected = 0
            previous_report = self._last_report
            self._last_report = now
            self._reporting = True
        # logger and /proc reads must not delay acceptance or request workers.
        try:
            threading.Thread(target=self._report, args=(now, active, rejected, slow), daemon=True).start()
        except Exception:
            with self._diagnostic_lock:
                self._reporting = False
                self._rejected += rejected
                self._last_report = previous_report

    def _report(self, now: float, active: list, rejected: int, slow: int) -> None:
        try:
            context: dict[str, Any] = {
                "active_handlers": len(active), "capacity": self._capacity,
                "slow_handlers": slow, "rejected_connections": rejected,
            }
            frames = sys._current_frames()
            for index, (ident, (started, group)) in enumerate(active[:3], 1):
                context[f"request_{index}"] = f"{group}; elapsed={max(0, now - started):.1f}s"
                context[f"stack_{index}"] = stack_location(frames.get(ident))
            del frames
            for resource in ("cpu", "memory", "io"):
                try:
                    context[f"pressure_{resource}"] = Path(f"/proc/pressure/{resource}").read_text()[:256].strip()
                except OSError:
                    pass
            context.update(workspace_socket_snapshot())
            host_errors.report_warning(
                "admin_api.request_capacity",
                "Admin request capacity exhausted" if rejected else "Admin requests are taking unusually long",
                context=context,
            )
        finally:
            with self._diagnostic_lock:
                self._reporting = False
