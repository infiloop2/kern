# Architecture Diagram

Arrows are allowed capabilities. Missing arrows are denied by nftables uid
rules, Unix peer credentials, Postgres grants, filesystem ownership, fixed sudo
rules, or route allowlists. The operator plane groups the human-facing access
paths; `cloudflared` is the host user that connects one of those paths to the
admin API. The storage boxes summarize persistence and ownership boundaries.
Storage arrows show durable file ownership/use relationships, not local IPC.

```mermaid
flowchart LR
    subgraph operatorplane["operator plane"]
        direction TB
        operator["Human operator"]
        ssh["kern-operator<br/>host SSH user<br/>passwordless sudo"]
        cfedge["Cloudflare Tunnel"]
    end

    subgraph outside["outside internet"]
        direction TB
        outside_services["Internet services<br/>GitHub, OpenAI, Anthropic, AWS,<br/>package registries, tool APIs"]
    end

    subgraph host["Kern guest: AWS EC2 or Lima VM"]
        direction TB

        subgraph services["users"]
            direction TB
            root["root<br/>owns root filesystem + host code<br/>fixed privileged helper allowlist"]
            admin["kern-admin<br/>Admin API/UI + orchestrator<br/>127.0.0.1:7443<br/>no internet egress"]
            proxy["kern-proxy<br/>Network proxy<br/>127.0.0.1:7445<br/>DNS + TCP 80/443 only"]
            tools["kern-tools<br/>Tool packages + tools.sock<br/>DNS + TCP 443 only"]
            agentnetwork["kern-agent-network<br/>Network introspection socket<br/>no egress"]
            workspace["kern-workspace<br/>Chat + Web Apps + Memory + Schedules + agent.sock<br/>fixed uid, Unix sockets, explicit table grants<br/>no egress"]
            embedding["kern-embedding<br/>socket-activated ONNX inference<br/>no DB or network access"]
            inference["kern-host-inference<br/>fixed host AI provider calls<br/>DNS + TCP 443 only"]
            speech["kern-transcription<br/>resident local dictation<br/>no DB or network access"]
            agent["kern-agent<br/>Codex + Claude Code + Grok + Hermes<br/>no sudo, DB role, or direct egress"]
            db["postgres<br/>kern_admin<br/>Unix socket only, peer auth"]
            tunnel["cloudflared<br/>Tunnel connector<br/>DNS, TCP 443/7844, UDP 7844"]
        end

        subgraph storage["storage"]
            direction TB
            rootvol["Root disk, 16 GiB, replaceable<br/>OS, trusted code, systemd, nftables, helpers<br/>root-owned trust boundary"]
            adminvol["Admin disk, 16 GiB, durable<br/>Postgres data, admin-home, proxy CA/certs, Git quarantine, temporary tool media<br/>service-owned private subtrees"]
            agentvol["Agent disk, 16 GiB, durable<br/>agent-home auth, sessions, caches, workspaces<br/>root-owned managed config"]
        end
    end

    operator -->|"SSH, when configured"| ssh
    ssh -->|"sudo is root-equivalent"| root
    ssh -->|"port forward + admin login"| admin
    operator -->|"admin login"| cfedge
    tunnel -->|"outbound connector"| cfedge
    cfedge -->|"operator request + admin login"| tunnel
    tunnel -->|"forwards to 127.0.0.1:7443"| admin

    admin -->|"exact sudo helpers"| root
    root -->|"demote into transient runtime scopes"| agent
    root -->|"bootstrap, updates, provider/GitHub helpers"| outside_services
    root -->|"OS, host code, systemd, nftables, helpers"| rootvol
    root -->|"managed immutable agent config"| agentvol

    agent -->|"HTTP(S)/WS(S) only via 127.0.0.1:7445"| proxy
    proxy -->|"guarded agent egress + GitHub token injection"| outside_services

    agent -->|"MCP list/call, peer uid route"| tools
    agent -->|"network status + denials, peer uid"| agentnetwork
    admin -->|"operator tool routes, peer uid route"| tools
    tools -->|"third-party tool APIs"| outside_services

    admin -->|"browser.sock reverse proxy, peer uid"| workspace
    workspace -->|"workspace.sock bounded host routes, peer uid"| admin
    admin -->|"bounded query/passage text over Unix socket"| embedding
    workspace -->|"bounded memory query/passage text"| embedding

    agent -->|"Workspace, history, peer messaging via agent.sock"| workspace
    admin -->|"bounded PCM via transcription socket"| speech
    admin -->|"fixed host AI routes, peer uid"| inference
    workspace -->|"fixed host AI routes, peer uid"| inference
    tools -->|"fixed host AI routes, peer uid"| inference
    inference -->|"fixed provider HTTPS endpoints"| outside_services
    inference -->|"provider credentials/key reads, usage writes"| db

    admin -->|"owner role, all host tables"| db
    db -->|"pgvector conversation index"| admin
    db -->|"pgvector memory index"| workspace
    admin -->|"admin-home + disk version"| adminvol
    proxy -->|"enforcement reads, event/push writes, working token"| db
    proxy -->|"CA keypair, leaf certs, Git quarantine"| adminvol
    tools -->|"tool tables + secret key only"| db
    agentnetwork -->|"SELECT-only policy + events"| db
    tools -->|"bounded temporary media"| adminvol
    workspace -->|"explicit workspace-table DML grants"| db
    db -->|"PGDATA"| adminvol
    agent -->|"agent-home, sessions, caches, workspaces"| agentvol

```
