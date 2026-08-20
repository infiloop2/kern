#!/usr/bin/env bash
set -euo pipefail
cd /mnt/kern-agent/agent-home

# Bootstrap installs Hermes's static harness config. This launcher supplies
# the task-specific region and builds the process boundary:
#
#   region=<aws-region> (REQUIRED first argument) -> exported as AWS_REGION so
#       Hermes signs for and calls exactly the operator-configured shared
#       Bedrock region. The network proxy separately enforces the same region.
#
#   Fixed flags -> the provider is always bedrock, only the terminal and file
#       toolsets plus the bundled-tools MCP shim load (no web, browser,
#       skills, messaging, cron, or computer-use surfaces), approvals are
#       disabled (--yolo: the OS/proxy boundary is the enforcement, as with
#       the other harnesses), and quiet mode keeps stdout to the answer text.
case "${1:-}" in
  region=*)
    region="${1#region=}"
    if ! [[ "${region}" =~ ^[a-z]{2}-[a-z]+-[0-9]$ ]]; then
      echo "run-hermes: invalid region: ${region}" >&2
      exit 64
    fi
    ;;
  *)
    echo "run-hermes: first argument must be region=<aws-region>" >&2
    exit 64
    ;;
esac
shift

unit_args=()
if [ "${1:-}" = "--thread-scope" ]; then
  if ! [[ "${2:-}" =~ ^[A-Za-z0-9_-]{1,64}$ ]]; then
    echo "invalid --thread-scope thread id: ${2:-<missing>}" >&2
    exit 64
  fi
  unit_args=(--unit "kern-agent-thread-$2")
  shift 2
fi

# The agent process NEVER holds the operator's AWS credential. Hermes signs
# with fixed dummy values; the network proxy checks the allowed request scope
# and re-signs it with the operator's real key, which lives encrypted in the
# database and is readable only by the admin and proxy roles. The access key
# id is Hermes's own routing identity (host/network_integrations/bedrock/
# manifest.py), so the proxy attributes each request's reported token usage
# to the Hermes runtime.

# SSL_CERT_FILE covers Hermes'\''s httpx clients and AWS_CA_BUNDLE its boto3
# chain; both point at the system bundle that includes the Kern proxy
# CA. IMDS is disabled so the AWS SDKs never wait on the unreachable
# instance-metadata endpoint before reading the injected signing identity.
export AWS_ACCESS_KEY_ID="AKIAKERNHERMES"
export AWS_SECRET_ACCESS_KEY="kern-bedrock-dummy-secret"
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
  AWS_REGION="${region}" \
  HTTP_PROXY=http://127.0.0.1:@PROXY_PORT@ \
  HTTPS_PROXY=http://127.0.0.1:@PROXY_PORT@ \
  ALL_PROXY=http://127.0.0.1:@PROXY_PORT@ \
  NO_PROXY=127.0.0.1,localhost \
  SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
  REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
  AWS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
  AWS_EC2_METADATA_DISABLED=true \
  /usr/local/lib/hermes-venv/bin/python \
  /usr/local/lib/kern-host/hermes-stdin.py \
  "$@"
