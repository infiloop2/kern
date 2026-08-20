"""Constants shared by Kern host lifecycle commands."""

from __future__ import annotations

SSH_USER = "kern-operator"
ROOT_VOLUME_SIZE_GB = 16
# Default durable admin storage. Audit logs have count and field-size bounds;
# health reports actual usage so operators can expand storage before it fills.
ADMIN_VOLUME_SIZE_GB = 16
AGENT_VOLUME_SIZE_GB = 16
SSH_WAIT_ATTEMPTS = 60
SSH_WAIT_SECONDS = 10
# Hard deadline for the remote bootstrap run. Bootstrap normally takes
# several minutes; a hang past this bound is terminated so the calling
# operation can release its lock and run its failure cleanup.
BOOTSTRAP_TIMEOUT_SECONDS = 3600
# Per-attempt bound on one SSH readiness probe: connection setup is bounded
# by ConnectTimeout, this bounds a daemon that accepts but never answers.
SSH_PROBE_TIMEOUT_SECONDS = 60
# Bound on copying the runtime code archive to the host.
SCP_TIMEOUT_SECONDS = 600
