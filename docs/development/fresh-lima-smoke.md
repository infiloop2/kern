# Fresh Lima Smoke

`tests/smoke/smoke_lima.py` boots a real local Kern host and exercises the
Lima provider end to end. It uses a unique agent name, a temporary `LIMA_HOME`,
an ephemeral operator key, and exact-name teardown. It needs no secret, cloud
account, or model-provider login. Local runs need no GitHub authorization;
manual workflow runs require a repository-admin request. PR comments also
allow non-admin requesters with a current-head Codex approval stamp.

The smoke verifies:

- fresh deploy into a real Ubuntu 22.04 VM;
- the generated 2-CPU / 2-GiB plain-mode definition, with no host mounts,
  dynamic forwards, lifecycle secrets, or checkout path;
- both independently named data disks, their filesystem labels and mounts,
  their backing-file identities across every compute replacement, and durable
  agent, database, Memory, Web App, and network-policy state;
- loopback SSH, the SSH-tunneled admin UI, admin login and session rejection,
  core systemd services, nftables, and proxy allow/deny audit events;
- the same credential-free live-host contract as the fresh AWS smoke: exact
  database config schema, stable service identities, storage and database role
  isolation, installed agent guidance, static UI assets and security headers,
  malformed-request rejection, and runtime status/account records;
- provider-free Chat, Web App, Memory, and Schedule operations; pre-login
  message admission, rollback, pagination, and concurrent state transactions;
- policy validation and concurrent replacement, package-client and GitHub read
  paths, proxy protocol and concurrency edge cases, and managed-provider
  fail-closed behavior before credentials exist;
- the installed script and Hermes launchers, bundled-tool discovery/actions,
  tool socket/database/egress boundaries, media staging, audit records, and the
  network-event prune race;
- the tampered-definition power contract: stopping remains available as the
  safe response, starting the untrusted definition is refused, and restoring
  the exact generated definition permits start again;
- idempotent stop/start without replacing compute;
- upgrade with the disposable root replaced and both data disks preserved;
- upgrade from older version metadata with both durable disks preserved;
- reconfigure rotating both the admin password and operator SSH key while the
  old credentials stop working; and
- direct VM deletion followed by recover from the two preserved disks.

The GitHub workflow `.github/workflows/test-lima-host.yml` runs this smoke
automatically on every push to `main`. Comment exactly `/lima-smoke` or
`lima-smoke` on a same-repository PR. Repository admins can run it directly;
other requesters require an open, ready PR to `main` with a clean Codex approval
stamp for its current head. `authorize-admin-or-codex-stamp` checks the active
stamp for the resolved SHA in both jobs on every attempt, including reruns.
Without a qualifying stamp, it calls `authorize-repo-admin`. Manual
`workflow_dispatch` remains admin-only. Requested runs test the PR's exact head SHA
from the trusted default-branch workflow, and publish a `lima-smoke` commit
status on that SHA. Duplicate runs of one SHA share a concurrency group and
cancel the older run, while different SHAs may run in parallel. The smoke uses
a standard `ubuntu-24.04` GitHub-hosted runner with KVM, QEMU, and an attested,
pinned Lima release. A missing or inaccessible `/dev/kvm` fails the job; the
smoke never skips.

To run it locally on Linux:

```bash
python3 tests/smoke/smoke_lima.py
```

Install the prerequisites from [the README local setup](../../README.md#quick-start-run-kern-on-your-computer)
first. The harness additionally requires `qemu-system-x86_64` and readable,
writable `/dev/kvm`. It prints the temporary Lima home at startup and dumps
Lima inventory and log tails before teardown on failure.

The smoke deliberately does not duplicate AWS resource provisioning,
credentialed runtime inference, or Cloudflare Tunnel checks. Those require
external accounts or are provider-specific and remain in the fresh AWS smoke
or [persistent AWS stage](persistent-aws-stage.md). The shared credential-free
host checks run from one implementation in `smoke_aws.py`, so extending that
contract automatically extends the Lima smoke as well.
