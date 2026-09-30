# Kern Cloud consumer contract

Kern Cloud in Infiverse downloads a pinned public Kern source archive and runs
its lifecycle CLI inside a Python 3.11 Lambda. A Kern release can break Cloud
without breaking Kern's own callers. The hardcoded contract test in
[`tests/test_kern_cloud_contract.py`](../../tests/test_kern_cloud_contract.py)
protects the requirements below without accessing Infiverse or running Kern.

**Do not update this test under normal circumstances. Do not relax an expectation
just to make a Kern PR pass. An intentional contract change requires the
corresponding Infiverse support update, coordinated before the Kern release.**
A structural refactor may require adjusting how the checker finds code, but it
must preserve the consumer expectations; explain that distinction in review.

## Dependency analysis

Traced against `infiversehq/infiverse` commit
`dbfd3ea7c566f87e4a1a74224c036ceafbfe5696` on September 30, 2026.
Paths below are in `apps/kernhosting/backend_lambda_functions/kern-hosting/code/`
unless stated otherwise. These are manually reviewed requirements, not a
runtime dependency on the Infiverse repository.

| Cloud consumer | Kern requirement | Why it matters |
| --- | --- | --- |
| `kern_cli.py` | `python -m host.cli.{deploy,upgrade,reconfigure,start,stop}` and `--agent-name`; AWS remains the default provider | Cloud constructs these commands directly and supplies no provider flag. |
| `kern_cli.py` | `--bootstrap-from-github <40-hex SHA>` for deploy, upgrade and reconfigure; one `y\n` on stdin | Cloud has no interactive terminal. It needs detached GitHub delivery; a synchronous SSH bootstrap could exceed the Lambda budget. |
| `kern_cli.py` | `--operator-cloudflare-hostname`, `--admin-password-sha256`, optional `--operator-ssh-public-key` for deploy/reconfigure; optional `--reset-admin-passkeys` for reconfigure | Endpoint and password recovery use these exact names and command modes. |
| `kern_cli.py` | AWS access key, secret key, session token and region environment; `KERN_CLOUDFLARE_TUNNEL_TOKEN` | The child receives an explicit environment. New required variables/dependencies need Cloud packaging changes. |
| `operation_runner.py` | One stdout JSON object; identity (`agent_name`, `region`), instance ID, both volume IDs, public DNS (optional for stop) | Cloud validates identity, persists these fields and uses them to display/manage the deployment. |
| `operation_runner.py` | Provisioning results contain target `version` and exact `github_source` = `infiloop2/kern@<SHA>`; no `admin_password` | Cloud rejects a result for the wrong release/source or containing a cleartext admin password. Power commands do not install a version. |
| `mirror_client.py`, `hosting_common.py`, `kern_cli.py` | Public repository `infiloop2/kern`, root `VERSION` in `x.y.z` format, minimum `1.0.0`, root `iam_policy.json`; archive prefix `kern-<SHA>/` | Cloud discovers public `main`, then reads/downloads by immutable commit. The archive prefix is GitHub's packaging convention. |
| `hosting_common.py`, `admin_handlers.py`, `approved_iam_policy.json` | Exact canonical JSON IAM policy digest | Cloud refuses release forwarding when the release policy differs from its separately reviewed policy. The digest in the test is hardcoded from that reviewed copy. |
| `aws_ops.py` | Instance, volume and security-group tags `kern-host=true` and `kern-host-agent-name=<namespaced name>` | Cloud probes and destroys resources directly through AWS, rather than a Kern destroy command. Tag drift could leave resources undiscovered. |
| `cloudflare_client.py` | HTTP admin service on port 7443 | Cloud configures tunnel ingress to `http://localhost:7443`. |
| `kern_status_client.py` | Public `GET /v1/login/status`, HTTP 200, JSON exactly `{"passkey_configured": boolean}` | Cloud reads the enrollment bit without a session and rejects extra keys, redirects, non-JSON content and oversized responses. |

The currently reviewed Kern and Cloud IAM policies match. Existing per-repo
tests mostly mock the other side of the lifecycle call, which is why an explicit
consumer contract is useful.

## CI behavior and limits

The existing `test-all-host.yml` workflow discovers this test with the rest of
`tests/` on every PR and push to main, without path filtering. It runs inside the
usual no-network test sandbox and participates in **Run all host tests**. No
separate workflow, checker framework, credentials or Infiverse checkout is needed.

Run the same check locally:

```bash
python3.11 -m unittest discover -s tests -p test_kern_cloud_contract.py -v
```

Six ordinary unittest cases parse source as data and assert hardcoded
declarations and selected expressions. A small helper normalizes formatting and
selects a function; there is no custom AST traversal or contract-checking engine. It does not import Kern, invoke a lifecycle command,
provision infrastructure, contact GitHub/AWS/Cloudflare, or test Infiverse.
An equivalent source refactor may fail conservatively and require review.
This is a static compatibility guard, not proof of runtime behavior. In
particular, it cannot prove elapsed time, secret-free output on every branch,
HTTP content type/size, third-party API availability, IAM authorization in AWS,
or deployment/bootstrap success. Cloud's 780-second subprocess limit,
64-KiB output bounds, 900-second Lambda timeout, Python 3.11 plus bundled AWS
CLI, archive size bounds, and HTTP response bounds remain operational
requirements for changes in those areas. Existing live smoke coverage remains
separate.
