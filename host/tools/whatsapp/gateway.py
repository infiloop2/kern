"""Private supervisor/client for Kern's WhatsApp linked-device gateway.

The Node child owns the long-lived WhatsApp Web socket and its frequently
changing Signal key state.  It is deliberately not a network service: the
tools process talks to it over one inherited stdin/stdout pipe, and only the
admin-only linked-device routes or the WhatsApp tool package can reach this
module.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import select
import shutil
import subprocess
import threading
import time
from typing import Any, Callable, Iterator

from host.runtime.core import host_errors, state
from host.tools import ToolServiceError


DEFAULT_SCRIPT = Path(__file__).with_name("gateway.mjs")
DEFAULT_STATE_DIR = Path("/mnt/kern-admin/tools-state/whatsapp")
DEFAULT_MODULE_DIR = Path("/usr/local/lib/kern-node/node_modules")
REQUEST_TIMEOUT_SECONDS = 40
STARTUP_TIMEOUT_SECONDS = 10
RESTART_DELAY_SECONDS = 3
MAX_CREDS_FILE_BYTES = 1024 * 1024


class WhatsAppGatewayError(ToolServiceError):
    """A redacted gateway failure safe to show to agents or operators."""


class WhatsAppGateway:
    def __init__(
        self,
        *,
        script: Path | None = None,
        state_dir: Path | None = None,
        module_dir: Path | None = None,
    ) -> None:
        self.script = script or Path(os.environ.get("KERN_WHATSAPP_GATEWAY_SCRIPT", DEFAULT_SCRIPT))
        # The constructor override exists for isolated gateway tests;
        # production always uses the fixed private directory above.
        self.state_dir = state_dir or DEFAULT_STATE_DIR
        self.module_dir = module_dir or Path(os.environ.get("KERN_HOST_NODE_MODULES", DEFAULT_MODULE_DIR))
        self._process: subprocess.Popen[str] | None = None
        self._lock = threading.RLock()
        self._next_id = 1

    @contextmanager
    def lifecycle(self) -> Iterator[None]:
        """Serialize enablement changes with requests and process lifecycle."""
        with self._lock:
            yield

    @staticmethod
    def _report_unexpected_exit(exit_code: int | None) -> None:
        host_errors.report_warning(
            "tools.whatsapp.gateway",
            "WhatsApp gateway child exited unexpectedly.",
            context={"exit_code": exit_code},
        )

    def _recover_child_after_exit(
        self,
        process: subprocess.Popen[str],
        claimed: bool = False,
    ) -> None:
        """Restart only WhatsApp after an unexpected child exit."""
        exit_code = process.wait()
        with self._lock:
            unexpected = claimed or self._process is process
            if self._process is process:
                self._process = None
        if not unexpected:
            return
        self._report_unexpected_exit(exit_code)
        time.sleep(RESTART_DELAY_SECONDS)
        restart_error: BaseException | None = None
        with self._lock:
            if self._process is not None:
                return
            try:
                if "whatsapp" not in state.enabled_tool_ids():
                    return
                self._start_locked()
            except Exception as exc:
                restart_error = exc
        if restart_error is not None:
            host_errors.report_warning(
                "tools.whatsapp.gateway",
                restart_error,
                context={"operation": "restart"},
            )

    def _signal_unexpected_exit_locked(self, process: subprocess.Popen[str]) -> None:
        """Stop a broken child and hand its recovery to a background thread."""
        claimed = self._process is process
        if claimed:
            self._process = None
        try:
            process.terminate()
        except OSError:
            pass
        if claimed:
            threading.Thread(
                target=self._recover_child_after_exit,
                args=(process, True),
                daemon=True,
            ).start()

    def _start_locked(self) -> subprocess.Popen[str]:
        process = self._process
        if process is not None and process.poll() is None:
            return process
        if not self.script.is_file():
            raise WhatsAppGatewayError("WhatsApp gateway is not installed on this host.")
        if not all((self.module_dir / package).is_dir() for package in ("baileys", "qrcode")):
            raise WhatsAppGatewayError("WhatsApp gateway dependencies are not installed on this host.")
        try:
            if self.state_dir.is_symlink():
                raise WhatsAppGatewayError("WhatsApp gateway storage cannot be a symlink.")
            self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            if self.state_dir.is_symlink() or not self.state_dir.is_dir():
                raise WhatsAppGatewayError("WhatsApp gateway storage is unavailable.")
            self.state_dir.chmod(0o700)
        except OSError as exc:
            raise WhatsAppGatewayError("WhatsApp gateway storage is unavailable.") from exc
        try:
            process = subprocess.Popen(
                ["/usr/local/bin/node", str(self.script)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                text=True,
                bufsize=1,
                env={
                    **os.environ,
                    "KERN_WHATSAPP_STATE_DIR": str(self.state_dir),
                    "KERN_HOST_NODE_MODULES": str(self.module_dir),
                },
            )
        except OSError as exc:
            raise WhatsAppGatewayError("WhatsApp gateway could not start.") from exc
        # Register immediately so service shutdown can see and terminate a
        # child even if SIGTERM interrupts the readiness wait below.
        self._process = process
        if process.stdout is None:
            process.terminate()
            self._process = None
            raise WhatsAppGatewayError("WhatsApp gateway could not start.")
        deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
        ready = False
        while time.monotonic() < deadline and process.poll() is None:
            readable, _, _ = select.select(
                [process.stdout], [], [], max(0.0, deadline - time.monotonic())
            )
            if not readable:
                break
            line = process.stdout.readline()
            if not line:
                break
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(message, dict) and message.get("ready") is True:
                ready = True
                break
        if not ready:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
            self._process = None
            raise WhatsAppGatewayError("WhatsApp gateway failed its startup readiness check.")
        threading.Thread(target=self._recover_child_after_exit, args=(process,), daemon=True).start()
        return process

    def start(self) -> None:
        with self._lock:
            self._start_locked()

    def _diagnostic_context(
        self,
        operation: str,
        result: dict[str, Any] | None = None,
    ) -> dict[str, str | int | bool | None]:
        """Return only non-secret gateway state for Host diagnostics."""
        process = self._process
        exit_code = process.poll() if process is not None else None
        context: dict[str, str | int | bool | None] = {
            "operation": operation,
            "child_running": process is not None and exit_code is None,
            "child_exit_code": exit_code if isinstance(exit_code, int) else None,
        }
        if not isinstance(result, dict):
            return context
        status = result.get("status")
        context.update({
            "status": status if isinstance(status, str) else "invalid",
            "connected": result.get("connected") is True,
            "retained_data": result.get("retained_data") is True,
            "qr_available": (
                isinstance(result.get("qr_data_url"), str)
                and bool(result["qr_data_url"])
            ),
            "account_present": isinstance(result.get("account"), dict),
        })
        diagnostic = result.get("diagnostic")
        if isinstance(diagnostic, dict):
            for key in (
                "connection_updates",
                "last_connection_event",
                "last_disconnect_code",
                "phase",
                "error_name",
                "error_code",
                "status_code",
                "transport_phase",
                "link_timed_out",
            ):
                value = diagnostic.get(key)
                if value is None or isinstance(value, (str, int, bool)):
                    context[key] = value
        return context

    def _report_operator_failure(
        self,
        operation: str,
        error: WhatsAppGatewayError,
        result: dict[str, Any] | None = None,
    ) -> None:
        host_errors.report_warning(
            "tools.whatsapp.gateway",
            error,
            context=self._diagnostic_context(operation, result),
            kind=f"whatsapp_{operation}_failed",
        )

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        with self._lock:
            process = self._start_locked()
            if process.stdin is None or process.stdout is None:
                raise WhatsAppGatewayError("WhatsApp gateway is unavailable.")
            request_id = self._next_id
            self._next_id += 1
            interrupted_message = (
                "WhatsApp send outcome is unknown. Do not retry automatically; check the recipient chat first."
                if method == "send_message"
                else "WhatsApp gateway stopped unexpectedly. Retry the request."
            )
            try:
                process.stdin.write(json.dumps({"id": request_id, "method": method, "params": params or {}}) + "\n")
                process.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                self._signal_unexpected_exit_locked(process)
                raise WhatsAppGatewayError(interrupted_message) from exc
            deadline = time.monotonic() + timeout_seconds
            while time.monotonic() < deadline:
                ready, _, _ = select.select([process.stdout], [], [], max(0.0, deadline - time.monotonic()))
                if not ready:
                    break
                line = process.stdout.readline()
                if not line:
                    self._signal_unexpected_exit_locked(process)
                    raise WhatsAppGatewayError(interrupted_message)
                try:
                    response = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(response, dict) or response.get("id") != request_id:
                    continue
                if response.get("ok") is not True:
                    message = response.get("error")
                    raise WhatsAppGatewayError(
                        message if isinstance(message, str) and message else "WhatsApp gateway request failed."
                    )
                result = response.get("result")
                if not isinstance(result, dict):
                    raise WhatsAppGatewayError("WhatsApp gateway returned an invalid response.")
                return result
            self._stop_locked()
            raise WhatsAppGatewayError(interrupted_message)

    def request_if_enabled(
        self,
        method: str,
        params: dict[str, Any] | None,
        enabled: Callable[[], bool],
    ) -> dict[str, Any]:
        """Recheck enablement while serializing against disable/stop.

        A tool call may have passed the generic enablement gate just before the
        operator disables it. Keeping this check and the complete child request
        under the gateway lock ensures Disable either waits and stops last, or
        wins first and prevents the stale call from restarting the child.
        """
        with self._lock:
            if not enabled():
                raise WhatsAppGatewayError(
                    "WhatsApp is disabled. Enable it under Home > Integrations."
                )
            return self.request(method, params)

    def _stop_locked(self) -> None:
        process, self._process = self._process, None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()

    def _graceful_stop_locked(self) -> None:
        process = self._process
        if process is not None and process.poll() is None:
            try:
                # The child synchronously flushes a pending debounced cache
                # write and closes the socket without logging out. A broken or
                # wedged child still falls through to bounded termination.
                self.request("shutdown", timeout_seconds=2)
            except WhatsAppGatewayError:
                pass
        self._stop_locked()

    def stop(self) -> None:
        with self._lock:
            self._graceful_stop_locked()

    @staticmethod
    def _account_from_credentials(credentials: object) -> dict[str, str] | None:
        if not isinstance(credentials, dict) or credentials.get("registered") is not True:
            return None
        raw_account = credentials.get("me")
        if not isinstance(raw_account, dict):
            return None
        raw_id = raw_account.get("id")
        raw_label = raw_account.get("name")
        account_id = raw_id[:200] if isinstance(raw_id, str) else ""
        account_label = raw_label[:200] if isinstance(raw_label, str) else account_id
        if not account_id and not account_label:
            return None
        return {"id": account_id, "label": account_label or "WhatsApp account"}

    def _retained_state_locked(self) -> tuple[bool, dict[str, str] | None]:
        auth_path = self.state_dir / "auth"
        credentials_path = auth_path / "creds.json"
        messages_path = self.state_dir / "messages.json"
        retained_data = (
            auth_path.exists()
            or auth_path.is_symlink()
            or messages_path.exists()
            or messages_path.with_suffix(".json.tmp").exists()
        )
        try:
            if credentials_path.stat().st_size > MAX_CREDS_FILE_BYTES:
                raise ValueError("credentials file is unexpectedly large")
            credentials = json.loads(credentials_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            credentials = None
        return retained_data, self._account_from_credentials(credentials)

    def _clear_local_data_locked(self) -> None:
        for path in (
            self.state_dir / "auth",
            self.state_dir / "messages.json",
            self.state_dir / "messages.json.tmp",
        ):
            try:
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink(missing_ok=True)
            except OSError as exc:
                raise WhatsAppGatewayError(
                    "WhatsApp session data could not be deleted from this host."
                ) from exc

    def disconnect(self) -> dict[str, Any]:
        """Best-effort provider logout plus authoritative host-side deletion.

        The Python host remains responsible for deleting retained local state
        even when the child cannot complete provider logout.
        """
        with self._lock:
            retained_data, account = self._retained_state_locked()
            process = self._process
            running = process is not None and process.poll() is None
            if not retained_data and not running:
                return {
                    "status": "disconnected",
                    "connected": False,
                    "retained_data": False,
                    "account": None,
                    "qr_data_url": "",
                    "error": "",
                }
            try:
                result = self.request("disconnect")
                result_account = result.get("account")
                if isinstance(result_account, dict):
                    result_id = result_account.get("id")
                    result_label = result_account.get("label")
                    if isinstance(result_id, str) and isinstance(result_label, str):
                        account = {"id": result_id[:200], "label": result_label[:200]}
            except WhatsAppGatewayError:
                # A missing/broken adapter cannot block deletion of retained
                # credentials and cached messages from the host-owned path.
                pass
            self._stop_locked()
            self._clear_local_data_locked()
            return {
                "status": "disconnected",
                "connected": False,
                "retained_data": False,
                "account": account,
                "qr_data_url": "",
                "error": "",
            }

    def suspended_status(self) -> dict[str, Any]:
        """Stop the child and describe any retained registered session.

        This deliberately reads only the non-secret account identity and the
        registration marker from Baileys' private credentials file. It lets the
        admin UI offer Disconnect while the tool is disabled without starting
        a WhatsApp socket merely to answer its status poll.
        """
        with self._lock:
            self._graceful_stop_locked()
            retained_data, account = self._retained_state_locked()
            if account is None:
                return {
                    "status": "disconnected",
                    "connected": False,
                    "retained_data": retained_data,
                    "account": None,
                    "qr_data_url": "",
                    "error": "",
                }
            return {
                "status": "suspended",
                "connected": False,
                "retained_data": retained_data,
                "account": account,
                "qr_data_url": "",
                "error": "",
            }

    def suspended_status_if_disabled(self, enabled: Callable[[], bool]) -> dict[str, Any]:
        """Serialize a disabled status read with enable/start.

        The caller may have observed disabled state before waiting for this
        lock. If Enable committed in the meantime, return live status instead
        of letting that stale poll stop the newly started listener.
        """
        with self._lock:
            if enabled():
                return self.request_if_enabled("status", None, enabled)
            return self.suspended_status()

    def operator(
        self,
        operation: str,
        enabled: Callable[[], bool],
    ) -> dict[str, Any]:
        """Handle the WhatsApp-specific operator flow behind ToolService."""
        with self.lifecycle():
            is_enabled = enabled()
            if operation == "enable":
                if not is_enabled:
                    raise WhatsAppGatewayError("WhatsApp is not enabled.")
                try:
                    self.start()
                except WhatsAppGatewayError as exc:
                    self._report_operator_failure(operation, exc)
                    raise
                return {"tool_id": "whatsapp", "enabled": True}
            if operation == "disable":
                if is_enabled:
                    return {"tool_id": "whatsapp", "enabled": True}
                self.stop()
                return {"tool_id": "whatsapp", "enabled": False}
            if operation == "disconnect":
                return self.disconnect()
            if operation == "status" and not is_enabled:
                return self.suspended_status_if_disabled(enabled)
            if operation == "connect" and not is_enabled:
                raise WhatsAppGatewayError("WhatsApp is not enabled.")
            if operation in {"status", "connect"}:
                try:
                    result = self.request(operation)
                except WhatsAppGatewayError as exc:
                    if operation == "connect":
                        diagnostic_status = None
                        process = self._process
                        if process is not None and process.poll() is None:
                            try:
                                diagnostic_status = self.request(
                                    "status", timeout_seconds=2
                                )
                            except WhatsAppGatewayError:
                                pass
                        self._report_operator_failure(
                            operation, exc, diagnostic_status
                        )
                    raise
                if operation == "connect" and result.get("status") not in {"qr", "connected"}:
                    diagnostic = result.get("diagnostic")
                    qr_timed_out = isinstance(diagnostic, dict) and (
                        diagnostic.get("link_timed_out") is True
                        or diagnostic.get("phase") == "qr_timeout"
                    )
                    issue = (
                        "WhatsApp did not provide a QR code during the 30-second link wait."
                        if result.get("status") == "connecting" or qr_timed_out
                        else "WhatsApp linking ended before a QR code was available."
                    )
                    host_errors.report_warning(
                        "tools.whatsapp.gateway",
                        issue,
                        context=self._diagnostic_context(operation, result),
                        kind="whatsapp_qr_unavailable",
                    )
                return result
            raise WhatsAppGatewayError("Unknown WhatsApp service operation.")


GATEWAY = WhatsAppGateway()


def gateway_request(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    return GATEWAY.request_if_enabled(
        method,
        params,
        lambda: "whatsapp" in state.enabled_tool_ids(),
    )
