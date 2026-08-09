"""The MCP stdio shim spawned by agent harnesses as kern-agent.

Client-side only: it dials the tools, Workspace, and agent-network sockets
and serves no socket of its own. Servers authenticate it by peer credential.
"""
