"""AWS-only resource names, shapes, tags, and network policy."""

from __future__ import annotations

INSTANCE_TAG_KEY = "kern-host-agent-name"
OWNER_TAG_KEY = "kern-host"
VOLUME_ROLE_TAG_KEY = "kern-host-volume-role"
VERSION_TAG_KEY = "kern-host-version"
INSTANCE_TYPE = "t3.small"
ADMIN_VOLUME_DEVICE = "/dev/sdf"
AGENT_VOLUME_DEVICE = "/dev/sdg"
SSH_INGRESS = {
    "IpProtocol": "tcp",
    "FromPort": 22,
    "ToPort": 22,
    "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
}
