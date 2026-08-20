#!/usr/bin/env bash
set -euo pipefail
cd /mnt/kern-agent/agent-home

# This launcher is authoritative for translating the operator's web-search
# decision into Claude CLI flags. The caller states policy only; this script
# builds the enforcement. The decision is this script's REQUIRED first argument:
#
#   web-search=off -> append a --settings override that denies the WebSearch
#                     tool. This is the highest-precedence CLI settings layer,
#                     nothing is written to disk, and the agent cannot influence
#                     the launched command.
#   web-search=on  -> add nothing; the tool stays available to Claude.
#
# The orchestrator derives on/off from the operator toggle for agent turns (see
# host/runtime/admin_api/claude_code.py). Non-agent maintenance calls (auth, usage) run no
# model turn that could use the tool, so they pass web-search=off to keep the
# deny-by-default posture. The network proxy enforces the same operator toggle
# independently, so a mistake here cannot let web search past the proxy.
case "${1:-}" in
  web-search=on) web_search_settings=() ;;
  web-search=off) web_search_settings=(--settings '{"permissions":{"deny":["WebSearch"]}}') ;;
  *)
    echo "run-claude-code: first argument must be web-search=on or web-search=off" >&2
    exit 64
    ;;
esac
shift

# Keep agent and auth invocations from emitting nonessential background
# traffic. Current Claude Code builds exit successfully but omit the Fable row
# from `/usage` when DISABLE_TELEMETRY is set, including through the umbrella
# flag. Preserve the opt-outs that do not alter usage for this one
# host-owned maintenance probe, while leaving agent/auth privacy unchanged.
claude_environment=(
  CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
  DISABLE_TELEMETRY=1
  DISABLE_ERROR_REPORTING=1
)
if [ "${1:-}" = "-p" ] && [ "${2:-}" = "/usage" ]; then
  claude_environment=(
    DISABLE_AUTOUPDATER=1
    DISABLE_FEEDBACK_COMMAND=1
    DISABLE_ERROR_REPORTING=1
  )
fi

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
exec systemd-run --quiet --collect --scope --slice=kern_agent.slice \
  "${unit_args[@]}" \
  --property=BindsTo=kern-admin-api.service \
  /usr/sbin/runuser -u kern-agent -- env \
  HOME=/mnt/kern-agent/agent-home \
  TMPDIR=/mnt/kern-agent/agent-home/.tmp \
  CLAUDE_CONFIG_DIR=/mnt/kern-agent/agent-home/.claude \
  HTTP_PROXY=http://127.0.0.1:@PROXY_PORT@ \
  HTTPS_PROXY=http://127.0.0.1:@PROXY_PORT@ \
  ALL_PROXY=http://127.0.0.1:@PROXY_PORT@ \
  NO_PROXY=127.0.0.1,localhost \
  NODE_EXTRA_CA_CERTS=/usr/local/share/ca-certificates/kern-network-proxy.crt \
  SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
  REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
  CLAUDE_CODE_CERT_STORE=system \
  "${claude_environment[@]}" \
  /usr/local/bin/claude "${web_search_settings[@]}" "$@"
