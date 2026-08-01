# Lifecycle Result

Each lifecycle command prints exactly one result JSON on stdout; all progress
streams on stderr, so `> result.json` captures the result cleanly. The result
contains no secrets.

## Provisioning result

`deploy`, `upgrade`, `recover`, and `reconfigure` replace or create a host and
return this shape:

```json
{
  "agent_name": "kern-dev-agent",
  "instance_id": "i-0123456789abcdef0",
  "region": "us-east-1",
  "public_dns": "ec2-203-0-113-10.compute-1.amazonaws.com",
  "ssh_user": "kern-operator",
  "admin_ui_local_url": "http://127.0.0.1:7443",
  "admin_volume_id": "vol-0123456789abcdef0",
  "agent_volume_id": "vol-0fedcba9876543210",
  "version": "x.y.z",
  "operator_connections": [
    {"mode": "ssh"},
    {"mode": "cloudflare_tunnel", "hostname": "kern.example.com"}
  ]
}
```

| Field | Presence | Behavior |
| --- | --- | --- |
| `agent_name`, `instance_id`, `region` | Always | Input host name and the created/replacement EC2 instance identity. |
| `public_dns` | Always | EC2 public DNS name used for SSH access when an SSH endpoint is configured. |
| `ssh_user` | Always | `kern-operator`. |
| `admin_ui_local_url` | Always | Local URL after forwarding the admin port: `http://127.0.0.1:7443`. |
| `admin_volume_id`, `agent_volume_id` | Always | Durable EBS volume ids attached to the host. |
| `version` | Always | Target `VERSION` installed by this provisioning command. |
| `operator_connections` | `deploy`, `reconfigure` | Public summary of the replacement endpoint list. Tunnel tokens and SSH key material are omitted. Upgrade/recover preserve the stored list and omit this field. |

No result carries the admin password. Deploy and reconfigure accept only
`--admin-password-sha256` (the SHA-256 hex digest of the operator's chosen
password); the host stores only that hash, so neither the CLI, its output, nor
the instance ever holds the cleartext.

## Power result

`start` and `stop` do not run bootstrap or install a version. They report the
existing instance's power transition:

```json
{
  "agent_name": "kern-dev-agent",
  "instance_id": "i-0123456789abcdef0",
  "region": "us-east-1",
  "operation": "start",
  "initial_state": "stopped",
  "state": "running",
  "public_dns": "ec2-203-0-113-10.compute-1.amazonaws.com",
  "public_ip": "203.0.113.10",
  "ssh_user": "kern-operator",
  "admin_ui_local_url": "http://127.0.0.1:7443",
  "admin_volume_id": "vol-0123456789abcdef0",
  "agent_volume_id": "vol-0fedcba9876543210"
}
```

| Field | Presence | Behavior |
| --- | --- | --- |
| `agent_name`, `instance_id`, `region` | Always | Input host name and existing EC2 instance identity. |
| `operation` | Always | `start` or `stop`. |
| `initial_state`, `state` | Always | EC2 state before the command and after the requested transition. |
| `public_dns`, `public_ip` | When AWS reports a non-empty value | Current public address metadata. A stopped instance may omit either field. |
| `ssh_user`, `admin_ui_local_url` | Always | Operator SSH identity and the local forwarded admin URL. |
| `admin_volume_id`, `agent_volume_id` | When the tagged volume is found | Durable volume ids associated with the host. Valid power operations require both volumes, so normal results contain both. |

Power results never contain `version` or `operator_connections` because the
commands do not change those values.

## Secret handling

Lifecycle results carry no secrets. The admin password is never handled by the
CLI — deploy and reconfigure take only its SHA-256 digest through
`--admin-password-sha256`, and the host stores only that hash — so no result
ever contains the password. Tunnel tokens and SSH key material are likewise
omitted from `operator_connections`. Each command prints its result to stdout
rather than writing a file, so redirect it yourself (`> result.json`) if you
want to keep it. Reaching the admin UI still requires credentials the result
does not contain: with SSH access, the matching private SSH key; with a
Cloudflare Tunnel, the Kern admin password.
