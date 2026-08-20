# Host Provider Boundary and Local Lima Host

Kern runs the same host on two infrastructure providers: `aws` (the default)
provisions EC2, EBS, and a security group; `lima` provisions a dedicated local
Ubuntu VM and two independent Lima data disks on the operator's machine. This
document is the authoritative design of the provider boundary and the local
Lima host; `tests/test_deploy_lima.py` enforces its contracts. The operator
setup is in the [README](../../README.md#quick-start-run-kern-on-your-computer).

## Decision

Kern supports more than one infrastructure provider without creating more than
one Kern runtime. The provider boundary is an operator-side lifecycle
boundary. Everything inside the guest after its data devices have been
identified is shared:

- the Ubuntu bootstrap;
- fixed users and groups;
- the filesystem and ownership model;
- PostgreSQL and all migrations;
- systemd services;
- nftables and the network proxy;
- apps and tools;
- Codex, Claude Code, and Hermes integrations;
- deployment verification;
- SSH operator access and previews; and
- the optional Cloudflare Tunnel connector.

There is no Docker Compose edition, provider-specific app path, local database
implementation, or reduced local runtime. A feature added above the provider
boundary must work on every provider without provider-aware code.

The two providers are implemented as parallel operator-side modules sharing
the payload, provisioning transport, and guest bootstrap. Extracting a shared
`HostProvider` engine is deliberately deferred until a third provider makes
its churn worthwhile; the contracts such an engine would encode — the
operation matrix, failure postconditions, fail-closed discovery, and disk
preservation — are enforced on both modules by tests today.

The Lima provider targets macOS and Linux. Windows can be added by a later
provider rather than by putting Windows conditionals into the Lima module or
the shared lifecycle.

## Goals

1. Run the same Kern guest on AWS and on a private local machine.
2. Preserve the replaceable-root, durable-admin, durable-agent storage
   lifecycle.
3. Keep provider-specific code small, explicit, and mechanically testable.
4. Give `deploy`, `upgrade`, `recover`, `reconfigure`, `start`, and `stop` the
   same operation matrix and version rules on every provider.
5. Keep SSH tunnels and Cloudflare Tunnel behavior the same from the guest's
   point of view.
6. Make disk discovery recoverable without a Kern metadata service or an
   indispensable local registry file.
7. Fail closed on ambiguous compute, partial disk pairs, unexpected
   attachment, or provider output that Kern cannot validate.

## Non-goals

- Running Kern directly on the operator's everyday Linux installation.
  Bootstrap manages system users, sudo, systemd, PostgreSQL, nftables, global
  packages, and trusted filesystem paths and therefore requires a dedicated
  guest.
- Splitting services into containers. Linux users, peer credentials, Unix
  sockets, cgroups, systemd, and uid-aware nftables are part of Kern's
  security model rather than incidental packaging.
- Making a local host available while its physical machine is powered off or
  asleep.
- Making Kern offline. Agent providers, GitHub, package installation,
  Cloudflare Tunnel, and enabled tools still need their configured network
  services.
- Hiding meaningful provider security differences. The local machine owner is
  the infrastructure administrator, and the Lima provider does not claim an
  off-guest firewall boundary equivalent to an AWS security group.
- Generalizing over arbitrary clouds or hypervisors before a third provider
  exists.

## Invariants

Every provider preserves these invariants:

1. There is at most one active compute host for an `agent_name`.
2. Durable storage is exactly one `admin` device and one `agent` device.
3. Compute is disposable; a failed or replaced compute host never owns the
   durable disks' deletion lifecycle.
4. `deploy` requires no compute and no durable storage.
5. `upgrade` and `reconfigure` require compute plus both durable devices.
6. `recover` requires both durable devices and no compute.
7. Start and stop require exactly one compute host and both durable devices.
8. Provider metadata is a discovery hint. The mounted admin state and its
   version gate are authoritative before preserved state is modified.
9. A bootstrap failure destroys or powers off the new compute host, removes
   temporary provisioning access, and preserves durable storage.
10. Only the operator CLI selection point, fixed guest storage-adapter
    dispatcher, and provider modules may branch on `aws` versus `lima`.
11. Guest services never import operator-side provider code.
12. No local host directory is mounted into the VM. In particular, Lima's
    default host-home mount is disabled.

## Architecture

```text
CLI parsing (--provider aws|lima)
          |
   provider selection point
     |                |
     v                v
 AWS adapter      Lima adapter
 lifecycle_aws.py lifecycle_lima.py
 EC2/EBS/SG       VM / disks / NAT /
                  loopback SSH
     |                |
     +-------+--------+
             v
  shared provisioning transport
  payload, code archive, deploy key,
  SSH delivery (lifecycle_bootstrap.py)
             |
             v
   guest storage adapter dispatcher
   storage_aws.py | storage_lima.py
   -> two verified block devices
             |
             v
   provider-neutral bootstrap.sh
   mount, users, PostgreSQL,
   services, firewall, verification
```

## Source layout

```text
host/
  cli/
    lifecycle.py            # lifecycle CLI parsing and provider selection
    lifecycle_aws.py        # AWS lifecycle, EC2/EBS/security-group operations
    aws_resources.py        # AWS CLI resource primitives
    aws_checks.py           # AWS resource/version preflight
    power_aws.py            # AWS start/stop lifecycle
    lifecycle_lima.py       # Lima compute, disks, power, discovery, and the
                            # loopback SSH endpoint
    power.py                # start/stop CLI and provider selection
    lifecycle_bootstrap.py  # shared SSH delivery: single-use deploy key,
                            # code archive copy, bounded bootstrap run
    operation_lock.py       # shared local advisory serialization primitive
    lifecycle_constants.py  # provider-neutral storage/transport constants
    lifecycle_types.py      # LifecycleCommand
  bootstrap/
    render.py               # payload, startup script, code archive rendering
    storage_resolver.py     # fixed adapter selection and two-device contract
    storage_aws.py          # EBS id to attached block-device resolution
    storage_lima.py         # named Lima disk to block-device resolution
    bootstrap.sh            # provider-neutral mount and later provisioning
```

Provider modules run only on the operator machine. No provider adds files
under `host/runtime`, `host/apps`, or `host/tools`, and guest services never
import them. Provider selection is a fixed two-way branch on `--provider`,
not plugin discovery: loading arbitrary provider code would make the
lifecycle and its destructive operations harder to audit. `lifecycle.py`
imports the Lima module lazily, so AWS operation never depends on it.

## Inputs and configuration

The lifecycle commands take:

```text
--provider aws|lima
```

`aws` is the default. AWS requires `AWS_REGION` and its existing credentials
(`InputConfig` carries `agent_name` plus `aws_region`). Lima requires no
cloud configuration: `LimaConfig` carries only the validated `agent_name`,
and `--bootstrap-from-github` and the AWS environment variables are rejected
or ignored respectively.

Compute shape is pinned on both providers — `INSTANCE_TYPE` (`t3.small`) for
EC2 and `LIMA_VM_CPUS` / `LIMA_VM_MEMORY_GIB` (2 CPUs / 2 GiB) for Lima — so
every host runs identical compute, every replacement recreates an identical
VM, and the rendered Lima definition is fully deterministic.

Kern runs `limactl` with the parent environment unchanged, so an ambient
`LIMA_HOME` selects a non-default Lima home exactly as it would for direct
`limactl` use; Kern never removes paths inside that directory itself.

Lima provisions from the local checkout over SSH. `--bootstrap-from-github`
remains AWS-only until detached provisioning has provider-neutral failure
reporting; this is a provisioning transport difference, not a different Kern
runtime.

## Operation matrix

| Operation | Compute before | Storage before | Action |
| --- | --- | --- | --- |
| `deploy` | none | none | Create both disks and one compute host; initialize state. |
| `upgrade` | exactly one | both | Destroy compute, preserve disks, create current root, require older durable version. |
| `recover` | none | both | Create current root, require equal durable version unless `--allow-upgrade`. |
| `reconfigure` | exactly one | both | Destroy compute, preserve disks, create current root, replace operator access/password. |
| `start` | exactly one stopped | both | Provider start only; no bootstrap. |
| `stop` | exactly one running | both | Provider stop only; no bootstrap. |

Both providers share the pieces around this matrix: the bootstrap payload and
startup-script rendering, the single-use deploy key, the runtime code
archive, SSH provisioning with hard deadlines, the version-hint checks, and
the result serialization. Each provider owns mapping `agent_name` to its
resource namespace, provider CLI/environment validation, discovery, placement,
durable disk creation and attachment, disposable root creation and deletion,
start/stop, outer network exposure, and obtaining an SSH endpoint.

The CLI holds one provider-neutral, per-provider/per-agent advisory lock for
every AWS and Lima lifecycle or power operation. It lives under
`$XDG_RUNTIME_DIR/kern/locks/`, with a private temporary-runtime fallback, so
it is ephemeral rather than lifecycle state. It prevents two Kern commands
started by the same local user from interleaving. It does not serialize direct
provider commands. Kern-managed resources must therefore be changed only
through the Kern CLI; concurrent or out-of-band mutation is unsupported.

## Failure and cleanup contract

Provider operations can fail between any two infrastructure calls, so the
contract is expressed in resource postconditions:

- Storage creation either returns one discoverable detached disk or leaves a
  visible resource the next run identifies by exact id. It never silently
  deletes a durable disk.
- A launch failure deletes the disposable compute record selected for that
  launch and preserves both durable disks.
- Compute deletion is idempotent for an already absent resource and waits
  until both durable devices are detached, within a bounded window.
- Shared provisioning failure triggers the same compute cleanup.
- Cleanup never invokes a provider storage-delete operation. Durable storage
  deletion is an explicit, separately confirmed operator action.
- A failed first deploy may leave one or two newly created blank durable
  disks. The next deploy refuses them and identifies their exact resource
  ids.
- Ambiguous discovery, malformed provider JSON, an unknown state, or an
  attachment to an unexpected compute host is a configuration error before
  any mutation.

Every subprocess uses argument arrays with bounded timeouts; no phase waits
unboundedly. The SSH transport enforces one overall readiness deadline with
per-probe bounds, a transfer bound on the code archive copy, and a hard
bootstrap deadline that terminates the session so cleanup can run. Lifecycle
preflight verifies `ssh`, `scp`, and `ssh-keygen` on `PATH` before any
destructive step, and entry points convert residual subprocess and OS errors
into the diagnostic and return-code contract rather than tracebacks.

## Guest storage handoff

Storage discovery occurs in two distinct places and is not conflated:

1. The operator-side provider finds the persistent resources that belong to
   an agent.
2. The guest-side resolver named in the payload maps those attached resources
   to Linux block devices.

The bootstrap payload carries:

```json
{
  "storage": {
    "resolver": "lima",
    "resolver_input": {
      "admin": {"disk_name": "kern-alice-2bd806c97f0e00af1a1fc332-admin"},
      "agent": {"disk_name": "kern-alice-2bd806c97f0e00af1a1fc332-agent"}
    }
  }
}
```

The AWS shape is:

```json
{
  "storage": {
    "resolver": "aws",
    "resolver_input": {
      "admin": {"volume_id": "vol-0123456789abcdef0"},
      "agent": {"volume_id": "vol-0fedcba9876543210"}
    }
  }
}
```

`storage_resolver.py` selects an adapter from a fixed registry. The AWS
adapter performs the EBS-id-to-NVMe/by-id lookup, and the Lima adapter reads
the root-only `/run/kern-provider/lima-disks.json` metadata written at first
boot. Each adapter returns one verified block device per role. The
provider-neutral `bootstrap.sh` then:

1. verifies the two resolved devices are different block devices;
2. verifies an existing filesystem label is either absent or matches its role;
3. on deploy only, formats an unformatted device once as ext4 with
   `KERN_ADMIN` or `KERN_AGENT` — replacement modes refuse to format a
   preserved device that has no filesystem;
4. obtains its filesystem UUID;
5. writes the UUID mount to `/etc/fstab`; and
6. mounts at `/mnt/kern-admin` and `/mnt/kern-agent`.

No code after this point knows the provider. Mounting by filesystem UUID
keeps later guest boots independent of `/dev/vd*` ordering.

The Lima metadata handoff is the only provider-specific code inside the
guest: a minimal, non-secret system-provision script copies
`LIMA_CIDATA_DISK_<n>_NAME` and `LIMA_CIDATA_DISK_<n>_DEVICE` into
`/run/kern-provider/lima-disks.json` before durable disks are mounted,
sourcing Lima's cidata environment file with auto-export as a fallback for
releases that do not export the variables. Lima supplies a bare virtio device
basename such as `vdb`; the handoff accepts only that grammar and records the
guest path as `/dev/vdb`. The resolver rejects a missing or duplicate name and
never relies on attachment order such as "`admin` is always `/dev/vdb`". Lima
plain mode still runs system provisioning scripts,
so this handoff does not require the Lima guest agent.

### Durable resource scope

Each lifecycle command targets exactly one managed host selected by
`--agent-name`; it does not limit an operator to one host. Multiple agent names
may coexist in one AWS account or one Lima namespace. AWS tags separate their
resources, while Lima derives exact instance and disk names from a normalized
agent-name prefix plus a 24-hex-character SHA-256 suffix, so names that
normalize to the same spelling still remain distinct. Filesystem labels bind
each attached durable device only to its admin or agent role, not to an owning
agent name. `version.json` remains the authoritative version gate and likewise
does not duplicate the agent name. Consequently, ordinary multi-host operation
keeps disks separate by provider resource identity. Copying, renaming,
replacing, or otherwise mutating Kern-managed resources outside the Kern CLI
is unsupported; Kern does not attempt to infer ownership after such changes.

## AWS provider

The AWS path is the original Kern lifecycle:

| Step | AWS implementation |
| --- | --- |
| Discovery | `DescribeInstances` and `DescribeVolumes` filtered by `kern-host=true`, agent name, and storage role tags. |
| Placement | Reuse the data volumes' availability zone; otherwise choose a public default-VPC subnet. |
| Storage creation | Create encrypted gp3 EBS volumes with agent and role tags. |
| Launch | Resolve AMI/network/security group, run the EC2 instance, wait for `running`, attach both EBS volumes, and set `DeleteOnTermination=false` for them. |
| Compute deletion | Set preserved-volume deletion flags defensively, terminate the instance, wait for termination and detachment. |
| Access finalization | Revoke temporary security-group SSH ingress unless SSH is an operator endpoint. |
| Start / stop | EC2 state transitions and waiters. |
| Guest storage spec | EBS volume ids. |

The security group is AWS's off-guest perimeter. Its tags are an access-state
hint for upgrade/recover before the admin disk is mounted; the admin disk is
authoritative for the actual operator connections when bootstrap runs.

## Lima provider

### Resource names

Lima has named instances and named independent disks rather than arbitrary
resource tags. Kern derives a collision-resistant local key:

```python
normalized = re.sub(r"[-_]+", "-", agent_name.lower()).strip("-") or "agent"
digest = sha256(agent_name.encode("utf-8")).hexdigest()[:24]
prefix = normalized[:16].rstrip("-") or "agent"
local_key = f"{prefix}-{digest}"
```

The resources are:

```text
instance: kern-<local_key>
admin:    kern-<local_key>-admin
agent:    kern-<local_key>-agent
```

Collapsing and trimming separators makes every accepted Kern agent name a
valid Lima identifier; the `agent` fallback covers names made only of
separators. The hash preserves the distinction between names that normalize
to the same Lima-safe spelling. Exact names are generated by one function and
validated again before every mutating call.

### Preflight

The Lima provider performs these read-only checks before any mutation:

1. `limactl` exists and its version is within the tested range pinned in the
   module (`>= 1.0.0`, `< 3.0.0`); outside it, preflight fails with an
   upgrade instruction rather than parsing an unknown JSON schema.
2. `limactl list --format=json` and `limactl disk list --json` return
   accepted schemas.
3. `ssh`, `scp`, and `ssh-keygen` exist on `PATH`, so a missing transport
   tool cannot surface only after a working VM has been deleted.
4. The resolved instance and disk names contain only the expected prefix and
   safe characters.
5. Replacement operations read the single `# kern-version: x.y.z` hint from
   the stored VM definition and reject a known-incompatible upgrade or
   reconfigure before deleting the working VM. The mounted admin disk remains
   authoritative when bootstrap runs.

Lifecycle operations additionally validate the complete candidate VM
definition with `limactl validate --fill` — which resolves current-host
defaults, including the platform driver, without mutating state — and build
the runtime code archive before any existing instance is touched.

### Discovery

Inventory runs:

```text
limactl list --format=json
limactl disk list --json
```

It selects only exact deterministic names, never a prefix or substring, and
tolerates the field spellings Lima has used across releases. Discovery fails
closed on malformed JSON, duplicate records, unknown instance states, a disk
attached to a foreign instance, and disks that report attachment to the
deterministic instance name while no such instance record exists (a stale
transition that would otherwise surface as an opaque create failure).

Lima's outer configuration does not vary by operator endpoint — management
SSH stays on host loopback and NAT stays outbound-capable — so there is no
Lima access-state hint. The preserved admin disk, not a duplicate Lima
metadata record, restores the actual operator connections during bootstrap.

### Resource ownership

Kern owns the exact deterministic instance and disk names for an agent while a
lifecycle command holds its operation lock. `limactl shell` may be used for
local guest administration, but direct mutating `limactl` commands or
filesystem changes to managed resources are unsupported. An operator who
deletes, renames, recreates, edits, or concurrently mutates them assumes
responsibility for the result, including possible data loss. The provider does
not add launch stamps, inode tracking, or repeated race checks for this
unsupported case.

Power `start` still verifies that the stored definition is byte-identical to
the definition this Kern version generates. This is a configuration-integrity
gate, not an ownership or concurrency protocol: it prevents Kern from booting a
known edited definition with host mounts, forwarded ports, a changed shape, or
missing durable disks. Stopping a definition-less or edited instance remains
possible so the operator can leave it powered down.

### Disk creation and persistence

The provider creates standalone disks:

```text
limactl disk create kern-<local-key>-admin --size 16GiB
limactl disk create kern-<local-key>-agent --size 16GiB
```

Kern, not Lima, formats them so AWS and Lima use the same filesystem labels,
UUID mounts, ownership initialization, and empty-state checks.

Standalone Lima disks survive instance deletion. The generated instance
definition attaches them as `additionalDisks` with Lima-side formatting
disabled. The root disk is part of the Lima instance and is disposable.

Disks are created only during `deploy`; replacement modes fail closed if a
preserved disk disappears after preflight rather than creating empty
replacement storage. The provider deletes instances only through exact
validated `limactl` names, never removes anything in the Lima home directly,
and no lifecycle command invokes `limactl disk delete`.

### Command sequences

All subprocesses use argument arrays, capture stderr, and set a bounded
timeout. A fresh deploy uses:

```text
limactl list --format=json
limactl disk list --json
limactl validate --fill <generated-yaml>
limactl disk create <admin-name> --size=16GiB
limactl disk create <agent-name> --size=16GiB
limactl create --name=<instance-name> --tty=false <generated-yaml>
limactl start --tty=false <instance-name>
limactl list --format=json
ssh -F <lima-ssh-config> lima-<instance-name> sudo bash -s
```

The final list must show the instance running with a nonzero `sshLocalPort`.
The last command streams the shared stage-one startup script on stdin through
Lima's generated management SSH configuration — never as an argument. That
script creates `kern-operator`, installs the single-use deploy key, and writes
the root-only guest payload; shared `kern-operator` SSH provisioning then runs
against the loopback endpoint. Stage-one delivery retries transient SSH failures
within a bounded budget.

Upgrade and reconfigure validate the candidate definition and build all local
artifacts first, then:

```text
limactl stop <instance-name>
limactl delete --force --tty=false <instance-name>
limactl disk list --json   # repeated until both disks detach, bounded
```

then repeat `create`, `start`, and shared SSH provisioning with the preserved
disk names. `recover` begins at that same detached-disk point without
deleting compute. Power operations are only `limactl start --tty=false` /
`limactl stop` plus exact-name JSON inspections. No operation invokes
`limactl disk delete`, `limactl factory-reset`, or direct filesystem removal.

### VM definition

The provider generates the entire Lima definition from trusted, validated
fields:

```yaml
# Generated by Kern; do not edit. Lifecycle commands replace this instance.
minimumLimaVersion: "1.0.0"
plain: true
images:
- location: <Ubuntu 22.04 stable-channel image>
  arch: "x86_64"
- location: <Ubuntu 22.04 stable-channel image>
  arch: "aarch64"
cpus: 2
memory: "2GiB"
disk: "16GiB"
mounts: []
containerd:
  system: false
  user: false
additionalDisks:
- name: kern-<local-key>-admin
  format: false
- name: kern-<local-key>-agent
  format: false
provision:
- mode: system
  script: <non-secret disk-name/device metadata handoff only>
```

The image tracks Canonical's Ubuntu 22.04 stable release channel, the same
policy as the AWS provider's SSM "current" AMI parameter, rather than a
byte-pinned build. Plain mode disables Lima filesystem mounts, dynamic port
forwarding, built-in containerd, and the guest agent while retaining base SSH
setup and system provisioning; `mounts: []` and the containerd stanza state
the same guarantees explicitly. Consequently the VM does not inherit Lima's
default host-home mount and does not automatically forward the admin API or
an agent-opened preview port. The rendered definition is covered by an exact
contract test.

The definition never contains the bootstrap payload, Cloudflare tunnel token,
deploy key, admin password hash, or any other lifecycle secret: secrets
travel only over SSH stdin after the VM is running, and then exist only in
the CLI process and the guest's root-only staging paths.

After start, the provider reads the allocated loopback `sshLocalPort` from
`limactl list --format=json` and hands `127.0.0.1:<port>` with user
`kern-operator` to the shared SSH provisioning code, which accepts a
non-default port.

The local machine owner retains hypervisor-level access through Lima. This is
equivalent to an AWS account administrator's infrastructure authority and is
outside the guest operator-endpoint boundary.

### SSH and previews

When an SSH operator connection is configured, bootstrap installs the same
operator public key as on AWS. The lifecycle result carries the loopback SSH
endpoint for the tunnel commands documented in the operator guide.

Agent preview forwarding remains:

```text
browser -> local loopback -> SSH -> guest loopback preview port
```

Only SSH is exposed to the host loopback. Admin port `7443`, app ports,
service sockets, and preview ports are never direct Lima forwards.

A remote user cannot reach a loopback-only local SSH endpoint unless the
machine owner separately provides a path to it. Remote local-host operation
uses Cloudflare Tunnel; exposing SSH on a LAN or public interface would be an
explicit future feature with its own threat review.

### Cloudflare Tunnel

Cloudflare Tunnel needs only outbound DNS, HTTPS, and tunnel transport from
inside the guest. The same bootstrap installs the same pinned `cloudflared`
binary, token file, systemd unit, uid-specific nftables rules, hostname
check, HTTPS check, and admin login gate. Lima NAT requires no public VM
address and no inbound port. Laptop sleep or shutdown makes the tunnel
unavailable; `kern-cloudflared.service` reconnects after the VM resumes.

There is no Cloudflare Access identity gate; Kern's admin authentication is
the login boundary.

### Outer network boundary

AWS has a security group outside the guest. Default Lima NAT does not provide
the same portable per-VM, off-guest egress rule layer across macOS and Linux.
The local provider therefore guarantees:

- no inbound service forwarded except SSH on host loopback;
- no host filesystem mounts;
- the same guest nftables policy;
- the same uid-isolated network proxy;
- the same Cloudflare-specific guest egress rules; and
- no cloud instance metadata or attached cloud IAM role.

It does **not** claim that a process which has compromised guest root is
still bounded by an AWS-like off-guest port allowlist. Adding macOS `pf`,
Linux host nftables, or hypervisor-specific filtering would create
substantial cross-platform privileged maintenance and is not part of this
provider. This difference is documented in the operator guide, which also
recommends FileVault or equivalent full-disk encryption: Lima data disk files
are protected by the local machine's storage, not by EBS encryption.

## Security-group continuity

AWS must attach a security group before a replacement VM can mount the admin
disk. Deploy and reconfigure generate its rules from the requested operator
endpoints. Upgrade and recover preserve the previous managed group's two
relevant choices — inbound SSH and Cloudflare Tunnel egress — while rebuilding
Kern's exact rule set:

- Local-checkout provisioning temporarily opens SSH for its single-use deploy
  key, then closes it again unless the previous group allowed operator SSH.
- GitHub provisioning needs no temporary SSH connection. It rebuilds the final
  rules directly from the previous group.
- If no previous group exists, Kern keeps SSH closed and allows Cloudflare
  Tunnel egress so the restored connector can recover.

Lima has no security group. Its loopback-only management SSH forward and
outbound NAT are constant across replacements, so there is no outer network
state to preserve. On both providers, bootstrap separately restores the
authoritative operator connections from the admin disk.

## Result contract

Lima results are provider-neutral:

```json
{
  "agent_name": "alice",
  "provider": "lima",
  "host": {"id": "kern-alice-2bd806c97f0e00af1a1fc332", "state": "running"},
  "storage": {
    "admin": {"id": "kern-alice-2bd806c97f0e00af1a1fc332-admin"},
    "agent": {"id": "kern-alice-2bd806c97f0e00af1a1fc332-agent"}
  },
  "ssh": {"host": "127.0.0.1", "port": 61234, "user": "kern-operator"},
  "admin_ui_local_url": "http://127.0.0.1:7443",
  "version": "x.y.z"
}
```

AWS results keep their existing fields (`instance_id`, `region`,
`public_dns`, `admin_volume_id`, `agent_volume_id`, …) and additionally state
`"provider": "aws"`. Power results share the provisioning shape plus
`operation` and `initial_state`, and omit `version` because power operations
install nothing. No result carries secrets, the deploy private key, tunnel
token, Lima internal paths, or the raw VM definition. The full schema is in
[docs/api/DeployResult.md](../api/DeployResult.md).

## Testing

- `tests/test_deploy_lima.py` runs without limactl: every `limactl`
  invocation goes through the module's single subprocess wrapper, which the
  tests replace with a stateful fake that records exact argument arrays. It
  enforces this document's contracts: exact-name fail-closed discovery,
  deterministic name collision resistance, the isolated secret-free plain-mode
  definition, the operation matrix, exact-name cleanup, the start-time
  definition-integrity gate, and the absence of any disk-deletion path.
- `tests/test_deploy.py` covers the AWS flow, the shared parsing and provider
  selection, the SSH transport deadlines, and the guest bootstrap storage
  path: one device per role, distinct devices, label checks, format-once on
  deploy only, and UUID mounts.
- The runtime, app, and tool test suites are provider-neutral and run once; a
  new feature above the provider boundary is not tested per provider.
- `tests/smoke/smoke_lima.py` runs automatically on a GitHub-hosted Linux
  runner. It boots real Lima compute and disks, provisions the complete guest,
  exercises SSH/admin access and network enforcement, runs every Lima lifecycle
  operation, rotates credentials, proves durable state survives replacement,
  and recovers after direct VM deletion. It needs no external credential and
  never skips when KVM is unavailable.
- Credentialed live smoke and stage coverage for AWS also lives in
  `tests/smoke/` and `tests/stage/`.

## Lima references

- [Lima additional disks](https://lima-vm.io/docs/config/disk/) documents
  independently named disks, JSON listing, attachment, and survival across
  instance deletion.
- [Lima internal data structure](https://lima-vm.io/docs/dev/internals/)
  documents the additional-disk name and guest-device metadata.
- [Lima port forwarding](https://lima-vm.io/docs/config/port/) documents
  loopback host-to-guest forwarding.
- [Lima plain mode](https://lima-vm.io/docs/config/plain/) documents disabling
  mounts, dynamic forwarding, the guest agent, and containerd while retaining
  SSH and provisioning scripts.
- [Lima SSH](https://lima-vm.io/docs/usage/ssh/) documents obtaining and using
  an instance's SSH configuration.
