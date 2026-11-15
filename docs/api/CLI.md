# Lifecycle CLI

Lifecycle commands take arguments and standard environment variables; there
are no configuration files. Progress streams on stderr; stdout carries one
result JSON ([Lifecycle result](DeployResult.md)). This page lists flags and
environment variables; what each command does to a host is in
[Manage Your Host](../../README.md#manage-your-host).

## Arguments

| Argument | Commands | Required | Behavior |
| --- | --- | --- | --- |
| `--agent-name <name>` | all except `generate_password` | Yes | Stable host name. Must be 1-50 characters and contain only letters, numbers, hyphen (`-`), and underscore (`_`). Lifecycle commands use it to find the host and its preserved data volumes. |
| `--provider <aws\|lima>` | all except `generate_password` | No (default `aws`) | Infrastructure provider. `aws` provisions EC2/EBS; `lima` provisions a dedicated local VM with durable Lima data disks on this machine ([local setup](../../README.md#quick-start-run-kern-on-your-computer)). The Kern guest, bootstrap, and runtime are identical on both. |
| `--operator-ssh-public-key <key>` | `deploy`, `reconfigure` | At least one endpoint | OpenSSH `ssh-ed25519` or `ssh-rsa` public key content installed for persistent operator access. |
| `--operator-cloudflare-hostname <host>` | `deploy`, `reconfigure` | At least one endpoint | Exact Cloudflare-protected hostname that routes to the admin UI/API, for example `kern.example.com`. Wildcards are rejected. The tunnel token is read from `KERN_CLOUDFLARE_TUNNEL_TOKEN` and encrypted in admin state so upgrade, recover, and reconfigure can recreate the tunnel service. |
| `--admin-password-sha256 <hex>` | `deploy`, `reconfigure` | Yes | SHA-256 hex digest of the admin password. The host stores only this hash; `python3 -m host.cli.generate_password` prints a generated password with its digest. |
| `--bootstrap-from-github [commit-sha]` | `deploy`, `upgrade`, `recover`, `reconfigure` | No | AWS only (rejected with `--provider lima`). Provisions the instance from a pinned `infiloop2/kern` commit via EC2 user data instead of pushing the local checkout over SSH; without a value, the latest `main` commit is pinned. The pinned commit's `VERSION` is the operation's target and must exactly equal the local checkout's `VERSION`; any difference aborts before anything in AWS is touched, so to deploy other code check out that commit and run its CLI. The CLI asks for confirmation. |
| `--allow-upgrade` | `recover` | No | Allows recovery to advance preserved admin state from an older version to the target `VERSION`, with or without an existing instance; existing compute is replaced. |
| `--instance-type <type>` | `deploy`, `recover` | No | AWS only (rejected with `--provider lima`). EC2 instance type such as `m7i.xlarge`; it must support the x86_64 Ubuntu image. Omitted, the code default (`t3.small`) is used. `upgrade` and `reconfigure` keep the existing instance's type. |
| `--lima-cpus <n>`, `--lima-memory-gib <n>`, `--lima-root-disk-gib <n>` | `deploy`, `upgrade`, `recover`, `reconfigure` | No | Lima only. Compute shape for the VM created by this command: 1-1024 CPUs, 1-65536 GiB memory, 16-65536 GiB root disk. Omitted values keep the saved shape (defaults: 2 CPUs, 2 GiB, 16 GiB). |
| `--lima-admin-disk-gib <n>`, `--lima-agent-disk-gib <n>` | `deploy` | No | Lima only. Size of each new durable data disk, 16-65536 GiB (default 16). Existing disks are never resized. |
| `--reset-admin-passkeys` | `reconfigure` | No | Recovery switch that deletes all enrolled admin passkeys. Use it after losing passkey access or when replacing the public admin hostname. It does not alter the password beyond reconfigure's existing password input. |

## Environment variables

| Variable | Required | Behavior |
| --- | --- | --- |
| `AWS_REGION` | `--provider aws` | AWS region of the host; `AWS_DEFAULT_REGION` also works. The region is part of the agent's identity: its data volumes live there. Not read by `--provider lima`. |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | `--provider aws` | AWS credentials. Not read by `--provider lima`. |
| `AWS_SESSION_TOKEN` | For temporary credentials | Used exactly when set. A stale token next to fresh static keys fails closed at AWS with an authentication error; unset it for static-key runs. |
| `KERN_CLOUDFLARE_TUNNEL_TOKEN` | With `--operator-cloudflare-hostname` | The Cloudflare Tunnel token. Secrets never ride in CLI arguments. |
