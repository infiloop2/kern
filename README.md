# Kern

**A permanent home for your agent swarm. Persistent. Governed. Improving over
time.**

Kern gives your AI agents a permanent home where they can work continuously,
remember what they learn, and build on past experience, all within one
fail-closed, auditable network boundary you control. Codex, Claude Code, and
Hermes are supported. Kern is the host, not the harness: it does not replace
your agents or decide how they reason; bring the agents and the subscriptions
you already pay for. Learn more at [kernai.cloud](https://kernai.cloud), and
read the thinking behind the design on
[the Kern blog](https://kernai.cloud/blog).

**Why agents need a home:**

- **Persistent and always on:** agents keep working after you close the laptop
  and come back to work already in progress, whether the host runs in the
  cloud or on your own machine.
- **Improving through memory:** the whole swarm shares one memory that each
  agent searches before it starts and writes back to when it learns something
  worth keeping; files, projects, and past threads stay on durable volumes,
  so the agent working for you next month knows more than the one that
  started today.
- **Governed by a hard boundary:** agents run with no permission prompts as
  unprivileged Linux users behind one fail-closed network policy: anything
  not listed is denied by default, every request is audited with the rule
  that decided it, and the host injects provider credentials in transit so
  reusable secrets never live with an agent.
- **Controlled tools:** bundled packages (Gmail, Google Calendar, Brave
  Search, X/Twitter, LinkedIn, Instagram, Polymarket, Interactive Brokers,
  Runway and Seedance media generation, and more) connect agents to
  third-party services through deterministic data paths, with consequential
  actions such as sending email or publishing a post held for your approval
  before they run.
- **A command center, not terminals:** direct every agent from your laptop or
  phone: **Chat** for threaded agent sessions you can step into when
  judgment is needed, **Apps** for durable agent-built interfaces so you read
  a queue or a board instead of scrolling a transcript, plus host-global
  **Memory** and **Schedules**.

These choices follow from a broader set of beliefs about running AI agents;
see [PHILOSOPHY.md](PHILOSOPHY.md).

The Kern host (bootstrap, database, services, firewall, and verification) is
identical everywhere it runs. This README builds it up in three steps, each
optional after the first:

1. [**Quick start:**](#quick-start-run-kern-on-your-computer) a fully local
   host on your Mac or Linux machine. Free, private, no cloud account.
2. [**Access from anywhere:**](#access-from-anywhere-add-a-cloudflare-tunnel)
   add a Cloudflare Tunnel so you can open Kern from any browser, including
   mobile.
3. [**Always on:**](#always-on-deploy-to-aws) deploy the identical host to
   AWS (~$23/month) if your machine isn't online all the time.

## Quick Start: Run Kern on Your Computer

The fastest way to run Kern: a fully local host in a
[Lima](https://lima-vm.io) virtual machine on macOS or Linux, reached over
loopback SSH. Nothing is exposed to your LAN or the internet, and no cloud
account or credentials are needed.

**You need:**

- macOS or Linux with hardware virtualization enabled. Kern uses 2 CPUs,
  2 GiB of RAM, and 48 GiB of disk.
- [Lima](https://lima-vm.io/docs/installation/). On macOS:
  `brew install lima`.
- Python 3.11+ and OpenSSH (present by default on macOS and mainstream
  Linux). Every Kern command is plain standard-library Python with **zero
  dependencies**; there is nothing to `pip install`. Prefer
  [uv](https://docs.astral.sh/uv/)? Run any `python3 -m ...` command in this
  README as `uv run --python 3.12 python -m ...` instead.

**Deploy:**

```bash
# 1. Get Kern
git clone https://github.com/infiloop2/kern.git
cd kern

# 2. Generate an admin password and its SHA-256 hash; save the password
python3 -m host.cli.generate_password

# 3. No SSH key yet? Create one first with:
ssh-keygen -t ed25519

# 4. Deploy (the first run downloads an Ubuntu image; allow several minutes)
python3 -m host.cli.deploy \
  --provider lima \
  --agent-name my-kern \
  --admin-password-sha256 <sha256-from-step-2> \
  --operator-ssh-public-key "$(cat ~/.ssh/id_ed25519.pub)"

# 5. Tunnel the admin UI through the SSH port printed in the result JSON.
# For example, if the result contains `"port": 61180`:
ssh -p 61180 -N -L 7443:127.0.0.1:7443 kern-operator@127.0.0.1

# 6. Open http://127.0.0.1:7443 and sign in with the admin password
```

That's it. The admin UI guides you through connecting an AI provider,
enabling network access, and adding optional tools.

An SSH port such as `61180` is normal, but it is dynamically allocated rather
than a fixed default and may change after a stop/start. Always use the current
`ssh.port` value from the command's result JSON.

What deploy created: one disposable VM plus two durable 16 GiB data disks
that hold all Kern state and survive every upgrade. Management SSH is
forwarded only to `127.0.0.1` on your machine.

Lima always keeps that dynamically allocated loopback management endpoint for
provisioning and `limactl shell`; `--operator-ssh-public-key` additionally
installs persistent `kern-operator` access.

You may use `limactl shell <instance>` for local administrative access to the
VM. Do not use `limactl` or filesystem tools to modify Kern-managed instances
or disks: doing so is unsupported and may cause data loss.

To permanently remove a local host, delete the disposable VM and then its two
durable disks. This is the only supported direct resource mutation and is
irreversible:

```bash
limactl delete --force <instance>
limactl disk delete <instance>-admin   # deletes all admin state
limactl disk delete <instance>-agent   # deletes the agent home
```

## Access From Anywhere: Add a Cloudflare Tunnel

With SSH-only access, the admin UI is available only from a computer that
holds your SSH key. A Cloudflare Tunnel gives Kern a stable HTTPS address you
can open from any browser, including mobile. The connector dials out from
inside the VM, so no inbound port is opened anywhere.
[Cloudflare's free plan](https://www.cloudflare.com/plans/zero-trust-services/)
covers this for personal use.

The tunnel is transport and Cloudflare edge (DDoS) protection only; your
admin password login is what protects the admin UI, so no Cloudflare Access
application is needed. You can also add a passkey in the admin UI for stronger
sign-in protection.

You will create an active domain, a Zero Trust organization, a tunnel, and a
published hostname. Cloudflare moves dashboard menus occasionally; if a label
differs, look for the same concept in its current
[tunnel setup](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/get-started/create-remote-tunnel/)
instructions.

### 1. Add an Active Domain

- Sign up at [dash.cloudflare.com](https://dash.cloudflare.com) on the free
  plan.
- Either buy a domain through **Domain Registration > Register Domains**, or
  select **Account Home > Add a domain** and change the nameservers at your
  current registrar to the two Cloudflare assigns you.
- Wait for an added domain to show **Active** before continuing. Nameserver
  changes can take from minutes to a day or two. A domain bought through
  Cloudflare activates immediately.

### 2. Complete Zero Trust Onboarding

- Open **Zero Trust** in the Cloudflare sidebar.
- Pick a unique team name and select the **Free** plan. Cloudflare requires a
  payment method even for this `$0` plan, but does not charge for the plan.

### 3. Create a Tunnel and Copy Its Token

Choose the final hostname now, for example `kern.example.com`.

- Go to **Networking > Tunnels > Create a tunnel** and name it, for example
  `kern`.
- The next screen shows connector installation commands. Do not run them;
  Kern installs the connector on the host. Copy only the long token
  starting with `eyJ` from the end of any installation command.
- The tunnel remains **Inactive** or **Down** until deploy connects it. This
  is expected. If you lose the token, open the tunnel's **Overview** tab and
  copy it from the installation command again, or select **Refresh token**.

### 4. Publish the Hostname

- Open the tunnel, then select **Routes > Add route > Published application**.
- Enter the final hostname you chose, exactly and with no path.
- Set **Service URL** to `http://localhost:7443`. This hop stays on the
  host's loopback interface; browsers still reach the hostname over HTTPS.
- Save the route. Cloudflare creates its DNS record automatically, so do not
  create another one.

### 5. Deploy or Reconfigure With the Hostname

Export the token, then pass the hostname to deploy, either instead of or
alongside the SSH key:

```bash
export KERN_CLOUDFLARE_TUNNEL_TOKEN='eyJ...'

python3 -m host.cli.deploy \
  --provider lima \
  --agent-name my-kern \
  --admin-password-sha256 <sha256> \
  --operator-cloudflare-hostname kern.example.com
```

Already deployed in the quick start? Add the tunnel to the existing host with
`reconfigure`. This replaces the existing SSH endpoint with Cloudflare-only
access:

```bash
export KERN_CLOUDFLARE_TUNNEL_TOKEN='eyJ...'

python3 -m host.cli.reconfigure \
  --provider lima \
  --agent-name my-kern \
  --admin-password-sha256 <sha256> \
  --operator-cloudflare-hostname kern.example.com
```

Once the command finishes, the tunnel shows **Healthy** in Cloudflare. Open your
hostname and sign in with the admin password.

## Always On: Deploy to AWS

A local host is only available while your machine is on and awake. For
agents that should keep working around the clock, deploy the identical host
to AWS. Everything above still applies: SSH and/or Cloudflare access, the
same commands, the same admin UI.

**Cost:** one `t3.small` EC2 instance, one public IPv4 address, and 48 GiB
of gp3 disk. A newly created
[AWS Free Tier](https://aws.amazon.com/free/) account usually costs `$0`
while its included credits remain; outside those credits, expect about
`$23/month` in `us-east-1`. AI provider usage is billed separately through
your Codex or Claude Code subscription.

**You need:**

- An AWS account. A newly created one works; no prior configuration is needed.
- [AWS CLI v2.32.0 or newer](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html).
- A Cloudflare Tunnel from the section above (recommended), or SSH-only
  access with `--operator-ssh-public-key`.

### 1. Create Temporary AWS Administrator Credentials

For a brand-new AWS account, the easiest path is to use the account owner
for this first deployment:

1. Open the [AWS console](https://console.aws.amazon.com/) and choose
   **Sign in using root user email**.
2. Sign in with the email address used to create the AWS account. Enable MFA
   on the root user if you have not already.
3. In your terminal, run the commands below. `aws login` opens the browser
   and lets you select the signed-in account owner session.

```bash
aws login
eval "$(aws configure export-credentials --format env)"
aws sts get-caller-identity
```

`aws login` creates a temporary session. The `eval` line exports that session's
credentials into the current terminal so Kern can use them; it does not create
or store a root access key. The last command prints the account and identity
that will create the Kern resources.

Set the region for the host as well; it is part of the agent's identity, so
later lifecycle commands must use the same region:

```bash
export AWS_REGION=us-east-1
```

If you use an IAM user or federated identity instead, its administrator
needs to grant the permissions in [`iam_policy.json`](iam_policy.json). Then
run the same commands while signed in as that identity. If `aws login` is
unavailable, update AWS CLI v2. Existing access keys also work: export
`AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`.

### 2. Deploy

```bash
export KERN_CLOUDFLARE_TUNNEL_TOKEN='eyJ...'   # from the tunnel section

python3 -m host.cli.deploy \
  --agent-name my-kern \
  --operator-cloudflare-hostname kern.example.com \
  --admin-password-sha256 <sha256>
```

For SSH access, pass `--operator-ssh-public-key` in place of (or alongside)
the Cloudflare hostname, exactly as in the quick start. The command creates
the host and installs Kern, streaming progress as it runs, and finishes by
printing a result JSON with the host's address.

The AWS credentials are no longer needed after deploy. Remove them from this
terminal and end the browser login session:

```bash
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
aws logout
```

### 3. Open Kern

If you used Cloudflare, the tunnel now shows **Healthy**; open your hostname
and sign in with the admin password.

If you deployed with SSH only, use the `public_dns` value from the deploy
result to start the SSH tunnel, and leave the terminal open:

```bash
ssh -i ~/.ssh/id_ed25519 -L 7443:127.0.0.1:7443 kern-operator@<public-dns>
```

Then open [http://127.0.0.1:7443](http://127.0.0.1:7443) and sign in with
the admin password. Type `exit` in the SSH terminal to close the tunnel; the
host keeps running.

When an SSH endpoint is configured, Kern keeps EC2 security-group ingress
for TCP 22 and installs the key for `kern-operator`. If SSH is omitted, the
final host closes EC2 SSH ingress after bootstrap.

### AWS Account Setup for Regular Use

For a first evaluation, the short-lived `aws login` session above avoids
creating an access key. Deploy uses the default VPC and needs a default
subnet with public IPv4 routing.

For regular use or automation, attach `iam_policy.json` to a federated IAM
role. The policy requires Kern tags on created resources, allows EC2 updates
and cleanup only on Kern-tagged resources, and leaves region selection to
`AWS_REGION`. See
[`docs/architecture/iam-policy.md`](docs/architecture/iam-policy.md) for why
each policy statement is needed and how its resource scope is constrained.

If federation is unavailable, the commands below create a dedicated IAM user
with the same policy. AWS recommends federation instead of long-lived IAM
user credentials for real data.

```bash
AWS_ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"

aws iam create-policy \
  --policy-name kern-host-deploy \
  --policy-document file://iam_policy.json

aws iam create-user --user-name kern-host-deploy
aws iam attach-user-policy \
  --user-name kern-host-deploy \
  --policy-arn "arn:aws:iam::$AWS_ACCOUNT_ID:policy/kern-host-deploy"

aws iam create-access-key --user-name kern-host-deploy

export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
```

For temporary credentials, also export `AWS_SESSION_TOKEN`, and unset it
when using long-lived access keys, or every AWS call fails with an
authentication error.

## Manage Your Host

Run lifecycle commands from the repository root. Each command streams
progress on stderr and prints one result JSON on stdout, so `> result.json`
captures the result cleanly. Every command accepts `--provider lima` to
operate a local host; AWS commands also need `AWS_REGION` and credentials in
the environment.

| Command | Behavior | Credential behavior |
| --- | --- | --- |
| `python3 -m host.cli.deploy --agent-name <name>` | Creates a new host. Fails if a Kern instance or data volume already exists for `agent_name`. | Installs the `--admin-password-sha256` digest as the admin password hash. Installs the configured operator endpoints. |
| `python3 -m host.cli.upgrade --agent-name <name>` | Replaces the instance/root volume and reuses the preserved admin and agent data volumes. Requires an existing instance and existing data volumes. Bootstrap requires the admin state version to be lower than the repo `VERSION`. | Preserves the existing admin password and operator endpoints from admin state. |
| `python3 -m host.cli.recover --agent-name <name>` | Creates a replacement host from preserved admin and agent data volumes when no Kern instance exists. Bootstrap requires the admin state version to equal the repo `VERSION`, unless `--allow-upgrade` is supplied. | Preserves the existing admin password and operator endpoints from admin state. |
| `python3 -m host.cli.reconfigure --agent-name <name>` | Replaces an existing instance/root volume, reuses preserved admin and agent data volumes, and replaces the full operator endpoint list. Requires an existing instance and existing data volumes. Bootstrap requires the admin state version to equal the repo `VERSION`. | Installs the `--admin-password-sha256` digest as the new admin password hash. |
| `python3 -m host.cli.start --agent-name <name>` | Starts the existing instance for the agent and waits until it is running. | Does not change credentials, root disk, data volumes, version, or operator endpoints. |
| `python3 -m host.cli.stop --agent-name <name>` | Stops the existing instance for the agent and waits until it is stopped. | Does not change credentials, root disk, data volumes, version, or operator endpoints. |
| `python3 -m host.cli.generate_password` | Prints a generated admin password and its SHA-256 digest, then exits. Touches no config, cloud resources, or files. | Store the password in your password manager; pass the digest to `--admin-password-sha256`. |

For Lima, `start` verifies that the stopped VM's definition exactly matches
the current checkout. If you update Kern while a local host is stopped and
its generated definition changed, run `upgrade` (or `reconfigure` when
rotating access) instead of `start`; the replacement keeps both durable data
disks.

The full argument and environment-variable reference, including
`--bootstrap-from-github` and `--allow-upgrade`, is in
[`docs/api/CLI.md`](docs/api/CLI.md).

Lifecycle commands fail before replacing an existing instance when the
resource shape or version tag is incompatible with the command. Bootstrap
then checks the preserved admin disk version as the authoritative source
before writing any upgraded state.

### Durable Data

The host uses three volumes (a local host uses the same layout as a
disposable VM root disk plus two durable Lima data disks):

| Volume | Lifecycle | Contents |
| --- | --- | --- |
| Root | Recreated on redeploy and deleted on instance termination | Ubuntu 22.04, system packages, Node.js, Python, Codex CLI, Claude Code CLI, nftables, OpenSSL, curl, jq, CA certificates, and swap. |
| Admin | Preserved on redeploy and marked not to delete on instance termination | Postgres state for the admin API, apps, tools, tasks, audit logs, network policy, credentials, and provider pins; proxy CA/certificate, queued-push state, and a bounded temporary tool-media spool. |
| Agent | Preserved on redeploy and marked not to delete on instance termination | Agent home directory, provider auth/session files, CLI caches, and workspace data. |

Every AWS resource deploy creates is tagged so it can be found and cleaned
up:

| Tag | Value | On |
| --- | --- | --- |
| `kern-host-agent-name` | `<agent_name>` | instance, volume, security group |
| `kern-host` | `true` | instance, volume, security group |
| `Name` | `kern-host-<agent_name>` | instance, volume |
| `kern-host-volume-role` | `admin` or `agent` | data volumes |
| `kern-host-version` | repo `VERSION` | instance |

See [`docs/api/DeployResult.md`](docs/api/DeployResult.md) for the lifecycle
result file schema.

### Recovering From Lost Passkeys

If you enable passkey login for the admin UI and later lose every enrolled
passkey, you can still recover from the operator plane; passkeys are
optional convenience credentials, and the admin password remains a valid way
in. Run `reconfigure` with `--reset-admin-passkeys`:

```bash
python3 -m host.cli.reconfigure \
  --agent-name my-kern \
  --operator-cloudflare-hostname <hostname> \
  --admin-password-sha256 <sha256> \
  --reset-admin-passkeys
```

This deletes every enrolled admin passkey so you can sign in with the admin
password again and enroll new passkeys. It preserves your admin and agent
data volumes (Postgres state, tasks, audit logs, credentials, apps, network
policy) unchanged. Because it is a normal `reconfigure`, it installs the
`--admin-password-sha256` digest as the admin password and replaces the full
operator endpoint list. Pass your current password digest and endpoints
unless you also intend to change them. The same flow doubles as the way to
move to a new public admin hostname.

### Viewing an Agent Preview Server

The agent can run web servers, such as a dev server, a UI it is building, or a
test harness, on a reserved loopback port range, `8000-8015`. Nothing on this
range is exposed publicly, and it is not shown in the admin UI. To view one,
forward its port to your own machine over SSH (this needs SSH operator
access) and open it in your browser:

```bash
ssh -i ~/.ssh/id_ed25519 -L 8000:127.0.0.1:8000 kern-operator@<public-dns>
```

On a local host, use the loopback SSH endpoint instead:
`ssh -p <port> -L 8000:127.0.0.1:8000 kern-operator@127.0.0.1`.

Then open **`http://preview.localhost:8000`** (matching the port you
forwarded).

Use the `preview.localhost` hostname, not `localhost` or `127.0.0.1`. A
preview server serves agent-authored content, and browser cookies are scoped
by hostname regardless of port, so if you browse a preview on the same
hostname you use for the admin UI (`http://127.0.0.1:7443` in the tunnel
above), your admin session cookie would be sent to it. `preview.localhost`
resolves to loopback in modern browsers and is a distinct hostname, so no
admin cookie is ever in scope. As with any untrusted web page, do not enter
credentials into a preview.

If your browser does not resolve `*.localhost` (some Safari builds), bind a
dedicated loopback address instead and open that:

```bash
ssh -i ~/.ssh/id_ed25519 -L 127.0.0.9:8000:127.0.0.1:8000 kern-operator@<public-dns>
# then open http://127.0.0.9:8000  (on macOS, first: sudo ifconfig lo0 alias 127.0.0.9 up)
```

## Admin API and File Uploads

With an SSH tunnel to the admin UI active, log in once to get a session
cookie, then reuse it (with the CSRF header) for API calls:

```bash
curl -c cookies.txt -H "Content-Type: application/json" \
  -d '{"password": "<admin-password>"}' \
  http://127.0.0.1:7443/v1/login

curl -b cookies.txt -H "X-Kern-Csrf: 1" \
  http://127.0.0.1:7443/v1/health
```

Full admin API documentation is in
[`docs/api/AdminAPI.md`](docs/api/AdminAPI.md).

Upload a file through the authenticated admin API. The host stores it in the
durable agent workspace with a sortable UTC timestamp prefix and returns the
relative path to reference in a task:

```bash
curl -b cookies.txt -H "X-Kern-Csrf: 1" \
  --data-binary @./reference.png \
  'http://127.0.0.1:7443/v1/agent-files/upload?filename=reference.png'
```

## Internals

The host runs on an AWS EC2 instance or a local Lima VM. The admin API,
network proxy, tools service, Workspace service, optional Cloudflare Tunnel
connector, database, and agent runtime run as separate Linux users.
Filesystem ownership, peer-authenticated local sockets, scoped database
roles, and uid-based firewall rules keep the agent from getting direct
network access or broad access to host state.

For deeper details, see the
[architecture documentation](docs/architecture/index.md).

## License

Kern is source-available under the Business Source License 1.1.
You may self-deploy and run Kern for non-commercial purposes at no charge.
Commercial production use requires a commercial license, available on request
from the copyright holder. Evaluation, development, and testing are free for
everyone under the license's standard non-production grant.

The Change Date is 2030-07-09, after which the Change License is the GNU
Affero General Public License v3.0 or any later version. See [LICENSE](LICENSE)
and [NOTICE](NOTICE).
