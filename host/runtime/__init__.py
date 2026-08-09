"""Kern runtime installed on EC2, split one package per service process.

The boundary rule: every message surface (TCP port or Unix socket) is served
by exactly one package, and that package is the only code that parses
messages arriving on it. Shared libraries live in ``core`` and serve no
socket.

- ``admin_api``     kern-admin: operator TCP API + workspace socket
- ``network_proxy`` kern-proxy: agent egress proxy (PROXY_PORT)
- ``tools``         kern-tools: agent-facing tools socket
- ``agent_network`` kern-agent-network: read-only introspection socket
- ``workspace``     kern-workspace: Chat, Web Apps, and the agent Workspace API
- ``agent_shim``    kern-agent: MCP stdio shim, client-side only
- ``core``          shared storage/state/policy libraries, no socket
- ``deploy``        bootstrap-run CLIs (migrations, effective config)
- ``root_helpers``  standalone CLIs invoked as root via sudo helpers

Socket paths and ports live in ``host.constants`` so servers, clients, and
the deploy verifier share one definition.
"""
