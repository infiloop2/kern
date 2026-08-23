#!/usr/bin/env bash
set -euo pipefail
cd /mnt/kern-agent/agent-home

# Runs one static bash script from the agent home as the agent user, for the
# script agent runtime (host/runtime/agent_runtime/script_runner.py). This is the
# same process boundary the model runtimes get — the kern_agent.slice scope,
# the proxy environment, and the per-thread scope name — so a scheduled script
# is as confined as an agent turn, and no more privileged.
#
# Root's only job here is to build that boundary. It never opens the script:
# the path is agent-controlled, so every filesystem decision about it is made
# after the demotion to kern-agent, where an agent-planted symlink can reach
# nothing the agent could not already reach. What root does check is the
# spelling, which keeps a path that could never be a script from becoming a
# process at all. host/agent_scripts.py holds the same contract for the
# workspace, which rejects a malformed path when a schedule is saved.
#
#   [--thread-scope <thread_id>]  optional, consumed here: names the scope
#       kern-agent-thread-<thread_id>.scope, exactly as the model launchers do.
#   <script_path>  REQUIRED: an absolute .sh path under the agent home.

unit_args=()
if [ "${1:-}" = "--thread-scope" ]; then
  if ! [[ "${2:-}" =~ ^[A-Za-z0-9_-]{1,64}$ ]]; then
    echo "invalid --thread-scope thread id: ${2:-<missing>}" >&2
    exit 64
  fi
  unit_args=(--unit "kern-agent-thread-$2")
  shift 2
fi

if [ "$#" -ne 1 ]; then
  echo "run-agent-script: usage: run-agent-script [--thread-scope <id>] <script-path>" >&2
  exit 64
fi
script_path="$1"
if ! [[ "${script_path}" =~ ^/mnt/kern-agent/agent-home/([A-Za-z0-9._-]+/)*[A-Za-z0-9._-]+\.sh$ ]]; then
  echo "run-agent-script: script path must be an absolute .sh path under the agent home" >&2
  exit 64
fi
# The character class above admits "." and ".." as whole segments; a relative
# segment must not survive into a path root is about to hand to bash.
case "/${script_path}/" in
  *"/../"*|*"/./"*)
    echo "run-agent-script: script path must not contain '.' or '..' segments" >&2
    exit 64
    ;;
esac

# RuntimeMaxSec is the backstop for the admin API's own turn timeout: a script
# is automation with a fixed budget, not an interactive session, so the scope
# stops on its own even if nothing is watching it. BindsTo covers the other
# direction — an admin API restart takes the scope with it, so no scheduled
# script keeps mutating agent-home after its run was recovered as failed.
exec systemd-run --quiet --collect --scope --slice=kern_agent.slice \
  "${unit_args[@]}" \
  --property=BindsTo=kern-admin-api.service \
  --property=RuntimeMaxSec=930 \
  --property=MemoryHigh=35% \
  --property=MemoryMax=50% \
  --property=MemorySwapMax=3G \
  --property=TasksMax=1024 \
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
  AWS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
  /bin/bash -c '
set -euo pipefail
script="$1"
# Checked as kern-agent, so a planted symlink can redirect this to nothing
# the agent could not already open for itself.
if [ -L "${script}" ]; then
  echo "run-agent-script: script path is a symlink" >&2
  exit 64
fi
if [ ! -f "${script}" ]; then
  echo "run-agent-script: script not found: ${script}" >&2
  exit 66
fi
cd /mnt/kern-agent/agent-home
# Invoked through bash by path: the script needs no executable bit and no
# shebang, and the runtime is bash whatever the file claims.
exec /bin/bash "${script}"
' run-agent-script "${script_path}"
