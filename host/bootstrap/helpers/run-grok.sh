#!/usr/bin/env bash
set -euo pipefail
cd /mnt/kern-agent/agent-home

# Grok's server-side web search is not offered on this host, so every
# invocation passes --disable-web-search, which turns off that search and
# Grok's web fetch together. There is no operator toggle and therefore no
# decision for a caller to state.
#
# The flag is a top-level grok option and must precede the `agent` subcommand:
# `grok agent ... stdio --disable-web-search` is rejected outright as an
# unexpected argument, so the ordering below is load-bearing, not cosmetic.
#
# This is defence in depth, not the enforcement. The agent has a shell and can
# run `grok` without this launcher; what actually holds is the network proxy,
# which denies a web-search declaration by inspecting the request body no
# matter which process sent it.

# The transient scope puts the runtime and everything it spawns into the
# resource-limited kern_agent.slice instead of the admin API's service
# cgroup. systemd-run --scope runs the command as its own child, so the stdio
# pipes and the stdin-EOF shutdown path are unchanged. BindsTo restores the
# lifecycle coupling the cgroup move removed: when the admin API service
# stops, restarts, or crashes, systemd stops the scope too, so no orphaned
# runtime keeps mutating agent-home after its task was recovered as failed.
#
# A leading "--thread-scope <thread_id>" pair (consumed here, never passed to
# the CLI) names the scope kern-agent-thread-<thread_id>.scope. The name comes
# from this root helper and is validated as a host thread id. Web App API
# targeting is explicit and independent of this process scope.
unit_args=()
if [ "${1:-}" = "--thread-scope" ]; then
  if ! [[ "${2:-}" =~ ^[A-Za-z0-9_-]{1,64}$ ]]; then
    echo "invalid --thread-scope thread id: ${2:-<missing>}" >&2
    exit 64
  fi
  unit_args=(--unit "kern-agent-thread-$2")
  shift 2
fi

# GROK_EXTRA_CA_BUNDLE is not an optimization: Grok's HTTP client is built
# against rustls with bundled webpki roots and does not consult the system CA
# store at all, so NODE_EXTRA_CA_CERTS and SSL_CERT_FILE (set here for any
# helper subprocess) are ignored by the agent itself. Without this variable
# every request through the MITM proxy fails TLS. That is the correct failure
# direction — closed, not bypassed — but it is also total, so this is the one
# environment entry the Grok runtime cannot lose.
#
# --no-leader keeps one turn to one process: leader mode would spawn a shared
# background agent behind its own socket, outliving the scope this launcher
# creates and breaking the host's authoritative reap.
# GROK_LOGIN_DEVICE_FLOW is equally load-bearing: without it Grok 1.0.5's ACP
# authenticate method chooses a loopback OAuth callback, which cannot complete
# from the operator's browser on a remote host.
exec systemd-run --quiet --collect --scope --slice=kern_agent.slice \
  "${unit_args[@]}" \
  --property=BindsTo=kern-admin-api.service \
  --property=MemoryHigh=35% \
  --property=MemoryMax=50% \
  --property=MemorySwapMax=3G \
  --property=TasksMax=1024 \
  /usr/sbin/runuser -u kern-agent -- env \
  HOME=/mnt/kern-agent/agent-home \
  TMPDIR=/mnt/kern-agent/agent-home/.tmp \
  GROK_HOME=/mnt/kern-agent/agent-home/.grok \
  HTTP_PROXY=http://127.0.0.1:@PROXY_PORT@ \
  HTTPS_PROXY=http://127.0.0.1:@PROXY_PORT@ \
  ALL_PROXY=http://127.0.0.1:@PROXY_PORT@ \
  NO_PROXY=127.0.0.1,localhost \
  GROK_EXTRA_CA_BUNDLE=/usr/local/share/ca-certificates/kern-network-proxy.crt \
  NODE_EXTRA_CA_CERTS=/usr/local/share/ca-certificates/kern-network-proxy.crt \
  SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
  REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
  GROK_DISABLE_AUTOUPDATER=1 \
  GROK_LOGIN_DEVICE_FLOW=1 \
  /usr/local/bin/grok --disable-web-search agent --no-leader stdio
