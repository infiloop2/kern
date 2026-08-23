"""Git receive-pack controls for the GitHub integration.

One vertical feature across three privilege domains:

- ``engine`` (proxy): parses a buffered ``git-receive-pack`` command list,
  synthesizes blocked report-status answers, and, for the independent
  ``.github`` approval control, inspects packs against a quarantine mirror.
  Invoked by the GitHub guard's ``gate_response`` hook.
- ``pending`` (admin service): operator approve/reject of held pushes.
- ``approve`` (root helper): replays approved objects to GitHub — root has
  egress and reads the proxy-owned mirror; installed as the
  ``approve-github-push`` sudo helper.

This is the one deliberate exception to "integrations own no storage": the
engine writes ``pending_pushes`` rows and the on-disk quarantine mirror,
under the proxy's existing role and uid grants.
"""

from host.network_integrations.github.push_gate.engine import (
    PENDING_PUSH_LIMIT,
    GateError,
    GateResult,
    build_http_response,
    build_report_status,
    inspect,
    new_push_id,
    parse_receive_pack,
    quarantine_lock,
    side_band_requested,
)

__all__ = [
    "PENDING_PUSH_LIMIT",
    "GateError",
    "GateResult",
    "build_http_response",
    "build_report_status",
    "inspect",
    "new_push_id",
    "parse_receive_pack",
    "quarantine_lock",
    "side_band_requested",
]
