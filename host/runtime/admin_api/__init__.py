"""The kern-admin service process.

Owns two message surfaces and nothing else touches them:
- the operator TCP API on 127.0.0.1:ADMIN_API_PORT (service.py), and
- the workspace Unix socket WORKSPACE_ADMIN_SOCKET_PATH
  (workspace_api.py).
The modules here implement transport auth, routing, operator-facing domains,
GitHub credential/audit flows, and upgrade polling. Agent turn orchestration,
provider trust, and harness adapters live in ``host.runtime.agent_runtime``
and run in this process behind these surfaces. The tools socket is reached
only as a client through tools_client.py.
"""
