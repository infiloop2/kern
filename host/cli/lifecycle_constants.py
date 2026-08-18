"""Constants shared by Kern host lifecycle commands."""

from __future__ import annotations

INSTANCE_TAG_KEY = "kern-host-agent-name"
OWNER_TAG_KEY = "kern-host"
VOLUME_ROLE_TAG_KEY = "kern-host-volume-role"
VERSION_TAG_KEY = "kern-host-version"
SSH_USER = "kern-operator"
INSTANCE_TYPE = "t3.small"
ROOT_VOLUME_SIZE_GB = 16
# Default durable admin storage. Audit logs have count and field-size bounds;
# health reports actual usage so operators can expand storage before it fills.
ADMIN_VOLUME_SIZE_GB = 16
AGENT_VOLUME_SIZE_GB = 16
ADMIN_VOLUME_DEVICE = "/dev/sdf"
AGENT_VOLUME_DEVICE = "/dev/sdg"
SSH_WAIT_ATTEMPTS = 60
SSH_WAIT_SECONDS = 10
SSH_INGRESS = {"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}
