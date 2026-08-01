"""Constants shared across deploy, the runtime services, and the smoke tests.

Defined once here so a port or the loopback address cannot drift between the
proxy, the admin API, the bootstrap templates, and the smoke harness.
"""

from __future__ import annotations

LOOPBACK = "127.0.0.1"
# The public Kern repository: the source the GitHub provisioning delivery
# pins commits from.
PUBLIC_GITHUB_REPOSITORY = "infiloop2/kern"
# The one fixed environment variable deploy and reconfigure read the
# Cloudflare Tunnel token from when a Cloudflare operator endpoint is
# configured. Secrets never ride in CLI arguments.
OPERATOR_TUNNEL_TOKEN_ENV_NAME = "KERN_CLOUDFLARE_TUNNEL_TOKEN"
ADMIN_API_PORT = 7443
# Request/response body cap shared by the admin API surfaces and the app
# backend proxy hop.
MAX_REQUEST_BODY_BYTES = 1024 * 1024
PROXY_PORT = 7445
APP_PORT_BASE = 7450
# Agent preview ports: a fixed loopback TCP range the agent may bind its own
# HTTP servers on (dev servers, test harnesses, UIs it is building) and — the
# only carve-out from its loopback egress drop — connect to, so it can test
# what it serves. An operator views a preview from their own browser via an SSH
# local forward (ssh -L); nothing is exposed on a public interface and the
# admin console never renders this content. Kept at 8000 — the classic dev
# server default — well clear of the 7xxx platform block (admin 7443, proxy
# 7445, installed apps APP_PORT_BASE + 0..MAX_INSTALLED_APPS-1 = 7450-7549) so
# the ranges can never collide even if the app block grows; test_deploy asserts
# the disjointness. See docs/architecture/agent-preview-ports.md.
AGENT_PREVIEW_PORT_BASE = 8000
AGENT_PREVIEW_PORT_COUNT = 16

# Unix socket endpoints. Each socket is served by exactly one runtime service
# package (see host/runtime/__init__.py); the default paths live here so the
# server, its clients, and the deploy verifier cannot drift apart. Servers and
# clients honor the matching KERN_*_SOCKET environment override in tests.
TOOLS_SOCKET_PATH = "/run/kern-tools/tools.sock"
AGENT_APP_SOCKET_PATH = "/run/kern-agent-app/agent-app.sock"
AGENT_NETWORK_SOCKET_PATH = "/run/kern-agent-network/agent-network.sock"
APP_BACKEND_ADMIN_SOCKET_PATH = "/run/kern-admin-api/app-backend.sock"
APP_BACKEND_GROUP = "kern-app-backends"
APP_BACKEND_GROUP_GID = 47749
# App backends synchronously proxy sends/stops through this socket. Keep their
# read deadline above the host's provider steer acknowledgement deadline so a
# caller cannot time out while the host later commits the same message.
APP_BACKEND_ADMIN_API_TIMEOUT_SECONDS = 40

# Core service accounts with pinned uids (gid always equals uid) so durable
# EBS file owners stay meaningful across root-volume replacement. Bootstrap
# renders these into the provisioning script (deploy fails if a base image
# already allocated one of the ids) and host.bootstrap.verify_deploy asserts
# the live /etc/passwd matches after provisioning. App accounts derive
# separately from each app's host_slot (see host.runtime.core.app_platform).
SERVICE_ACCOUNTS = {
    "kern-admin": 47741,
    "kern-proxy": 47742,
    "kern-agent": 47743,
    "cloudflared": 47744,
    "postgres": 47745,
    "kern-tools": 47746,
    "kern-agent-app": 47747,
    "kern-agent-network": 47748,
}
