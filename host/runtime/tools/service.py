"""Dedicated tools service.

Tool packages make outbound HTTPS calls to third parties (Google, Brave) and
parse their responses, so they need internet egress and are the host code most
exposed to attacker-influenced data. This service runs them out of the admin
service: it runs as the dedicated ``kern-tools`` user, which is the only
non-root uid that executes tool packages with direct DNS and HTTPS, and
connects to Postgres as the ``kern-tools`` role. That role is limited to
the tool tables plus read access to the encryption key used for tool secrets.
The admin service therefore holds no internet egress and executes no
third-party tool action.

It serves the agent-facing tools socket (``tools/list``, ``tools/call``, and
the shim's bounded raw-media streams) and the operator delegation routes the admin service forwards for
the operations that need this service's egress (OAuth code exchange, token
revoke) or run tool code over third-party data (approved-action execution). All
tool credentials, config, approvals, and audit events live in the tool tables,
reached with the scoped role over the same peer-authenticated Postgres socket.
Staged media are ephemeral mode-0600 files in this service's private admin-volume
directory, indexed only in memory and addressed by tool-scoped opaque ids.
"""

from __future__ import annotations

import signal
from types import FrameType

from host.runtime.core import host_errors, state
from host.runtime.tools import api as tools_api, tools_host
from host.tools import ToolServiceError


def _terminate_on_signal(_signum: int, _frame: FrameType | None) -> None:
    # Python's default SIGTERM action exits immediately without unwinding the
    # try/finally below. Raising SystemExit lets the parent flush and stop its
    # child before systemd's mixed-mode stop escalates to the whole cgroup.
    raise SystemExit(0)


def main() -> int:
    signal.signal(signal.SIGTERM, _terminate_on_signal)
    # This service executes approved actions, so it owns their crash recovery:
    # an approval stuck in 'approved' when this service last stopped had its
    # single execute_approved call interrupted, so mark it failed before serving
    # (an unknown outcome spends a single-use approval). Owning it here, rather
    # than in admin startup, avoids racing a live execution when only the admin
    # service restarts.
    tools_host.recover_interrupted_approvals()
    services = [
        (tool.manifest.tool_id, tools_host.tool_service(tool))
        for tool in tools_host.BUNDLED_TOOLS.values()
        if tool.manifest.service
    ]
    try:
        enabled = state.enabled_tool_ids()
        for tool_id, service in services:
            if tool_id not in enabled or service is None:
                continue
            try:
                service.start()
            except ToolServiceError as exc:
                # Other tools remain available; the service's operator status
                # surfaces its bounded error and a later request retries.
                host_errors.report_warning(
                    "tools.service.start",
                    exc,
                    context={"tool_id": tool_id},
                    kind="tool_service_start_failed",
                )
        tools_api.serve_forever()
    finally:
        for _tool_id, service in reversed(services):
            if service is not None:
                service.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
