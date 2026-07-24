#!/usr/bin/env bash
set -euo pipefail
exec env PYTHONPATH=/opt/kern-host /usr/bin/python3 -m host.network_integrations.github.push_gate.approve
