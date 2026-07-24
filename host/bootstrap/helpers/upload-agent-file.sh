#!/usr/bin/env bash
set -euo pipefail
exec /usr/sbin/runuser -u kern-agent -- \
  env HOME=/mnt/kern-agent/agent-home \
  /usr/bin/python3 /opt/kern-host/host/runtime/root_helpers/upload_agent_file.py "$@"
