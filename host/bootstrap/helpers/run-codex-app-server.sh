#!/usr/bin/env bash
set -euo pipefail
cd /mnt/kern-agent/agent-home
# The transient scope puts the runtime and everything it spawns into the
# resource-limited kern_agent.slice instead of the admin API's service
# cgroup. systemd-run --scope runs the command as its own child, so the stdio
# pipes and the stdin-EOF shutdown path are unchanged. BindsTo restores the
# lifecycle coupling the cgroup move removed: when the admin API service
# stops, restarts, or crashes, systemd stops the scope too, so no orphaned
# runtime keeps mutating agent-home after its task was recovered as failed.
#
# A leading "--thread-scope <thread_id>" pair names the scope
# kern-agent-thread-<thread_id>.scope. The name comes from this root helper and
# is validated as a host thread id. Web App API targeting is explicit and
# independent of this process scope.
unit_args=()
if [ "${1:-}" = "--thread-scope" ]; then
  if ! [[ "${2:-}" =~ ^[A-Za-z0-9_-]{1,64}$ ]]; then
    echo "invalid --thread-scope thread id: ${2:-<missing>}" >&2
    exit 64
  fi
  unit_args=(--unit "kern-agent-thread-$2")
  shift 2
fi
exec systemd-run --quiet --collect --scope --slice=kern_agent.slice \
  "${unit_args[@]}" \
  --property=BindsTo=kern-admin-api.service \
  /usr/sbin/runuser -u kern-agent -- env \
  HOME=/mnt/kern-agent/agent-home \
  TMPDIR=/mnt/kern-agent/agent-home/.tmp \
  HTTP_PROXY=http://127.0.0.1:@PROXY_PORT@ \
  HTTPS_PROXY=http://127.0.0.1:@PROXY_PORT@ \
  ALL_PROXY=http://127.0.0.1:@PROXY_PORT@ \
  NO_PROXY=127.0.0.1,localhost \
  NODE_EXTRA_CA_CERTS=/usr/local/share/ca-certificates/kern-network-proxy.crt \
  SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
  REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
  /usr/local/bin/codex app-server --listen stdio://
