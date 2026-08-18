# Kern

Kern is a controlled AI agent host with strong network activity gating.
It runs Codex and Claude Code on infrastructure you own while keeping the agent
behind an explicit, auditable network policy. Learn more at
[kernai.cloud](https://kernai.cloud), and read the thinking behind the design
on [the Kern blog](https://kernai.cloud/blog).

## Deploy Your First Host

Kern uses a Cloudflare Tunnel to give the admin UI a stable HTTPS address,
protected by your admin password login and Cloudflare's edge (DDoS) protection.
The steps below use this setup. It takes a few extra steps if you are new to
Cloudflare, but once configured you can open Kern securely from any
browser, including mobile.

Alternatively, you can deploy without HTTPS UI access and connect using SSH
port forwarding. That setup is simpler, but the UI is available only from a
computer that holds your SSH private key, not from mobile or browsers on other
devices. To take it, skip Cloudflare (step 3) and pass an SSH operator
endpoint at [Deploy](#4-deploy). Tailscale SSH support is coming.

### Before You Start

You need:

- An AWS account. The walkthrough works with a newly created AWS account, so
  no prior AWS configuration is required.
- A macOS or Linux terminal with Git, Python 3.11, and
  [AWS CLI v2.32.0 or newer](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html).
- A Cloudflare account. The walkthrough works with a newly created Cloudflare
  account, so no prior Cloudflare configuration is required.

### Cost

Kern deploys one `t3.small` EC2 instance, one public IPv4 address, and
48 GiB of gp3 disk, plus a Cloudflare Tunnel. A newly
created [AWS Free Tier](https://aws.amazon.com/free/) account usually costs
`$0` while its included credits remain; outside those credits, expect about
`$23/month` in `us-east-1`.
[Cloudflare's free plan](https://www.cloudflare.com/plans/zero-trust-services/)
costs `$0` for limited personal use. AI provider usage is billed separately
through your Codex or Claude Code subscription.

### 1. Download Kern

```bash
git clone https://github.com/infiloop2/kern.git
cd kern
```

### 2. Create Temporary AWS Administrator Credentials

For a brand-new AWS account, the easiest path is to use the account owner for
this first deployment:

1. Open the [AWS console](https://console.aws.amazon.com/) and choose **Sign in
   using root user email**.
2. Sign in with the email address used to create the AWS account. Enable MFA on
   the root user if you have not already.
3. In your terminal, run the commands below. `aws login` opens the browser and
   lets you select the signed-in account owner session.

```bash
aws login
eval "$(aws configure export-credentials --format env)"
aws sts get-caller-identity
```

The last command prints the account and identity that will create the
Kern resources. This creates temporary administrator credentials in the
current terminal; it does not create or store a root access key.

Set the region for the host as well; it is part of the agent's identity, so
later lifecycle commands must use the same region:

```bash
export AWS_REGION=us-east-1
```

If you use an IAM user or federated identity instead, its administrator needs
to grant the permissions in [`iam_policy.json`](iam_policy.json). Then run the
same commands while signed in as that identity.

If `aws login` is unavailable, update AWS CLI v2. Existing access keys also
work: export `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`.

### 3. Set Up a Cloudflare Tunnel

A Cloudflare Tunnel is recommended because it gives Kern a persistent
HTTPS address and an admin UI you can open securely from anywhere. The tunnel is
transport and Cloudflare edge (DDoS) protection only; your admin password login
is what protects the admin UI, so no Cloudflare Access application is needed. To
deploy without Cloudflare, skip this step and pass `--operator-ssh-public-key`
at [Deploy](#4-deploy) instead of a Cloudflare hostname.

You will create an active domain, a Zero Trust organization, a tunnel, and a
published hostname. Cloudflare moves dashboard menus occasionally; if a label
differs, look for the same concept in its current
[tunnel setup](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/get-started/create-remote-tunnel/)
instructions.

#### 3.1. Add an Active Domain

- Sign up at [dash.cloudflare.com](https://dash.cloudflare.com) on the free
  plan.
- Either buy a domain through **Domain Registration > Register Domains**, or
  select **Account Home > Add a domain** and change the nameservers at your
  current registrar to the two Cloudflare assigns you.
- Wait for an added domain to show **Active** before continuing. Nameserver
  changes can take from minutes to a day or two. A domain bought through
  Cloudflare activates immediately.

#### 3.2. Complete Zero Trust Onboarding

- Open **Zero Trust** in the Cloudflare sidebar.
- Pick a unique team name and select the **Free** plan. Cloudflare requires a
  payment method even for this `$0` plan, but does not charge for the plan.

#### 3.3. Create a Tunnel and Copy Its Token

Choose the final hostname now, for example `kern.example.com`.

- Go to **Networking > Tunnels > Create a tunnel** and name it, for example
  `kern`.
- The next screen shows connector installation commands. Do not run them;
  Kern installs the connector on the host. Copy only the long token
  starting with `eyJ` from the end of any installation command.
- The tunnel remains **Inactive** or **Down** until deploy connects it. This is
  expected.

#### 3.4. Publish the Hostname

- Open the tunnel, then select **Routes > Add route > Published application**.
- Enter the final hostname you chose, exactly and with no path.
- Set **Service URL** to `http://localhost:7443`. This hop stays on the host's
  loopback interface; browsers still reach the hostname over HTTPS.
- Save the route. Cloudflare creates its DNS record automatically, so do not
  create another one.

#### 3.5. Export the Tunnel Token

```bash
export KERN_CLOUDFLARE_TUNNEL_TOKEN='eyJ...'
```

If you lose the token, open the tunnel's **Overview** tab and copy it from the
installation command again, or select **Refresh token**.

### 4. Deploy

Choose an admin password and keep it in your password manager; the deploy
command takes only its SHA-256 hash, so no process or file ever holds the
password itself. To generate a strong password and its hash in one step:

```bash
python3 -m host.cli.generate_password
```

Store the printed password, then deploy with the digest (or hash your own
password with `printf %s 'your-chosen-password' | sha256sum`):

```bash
python3 -m host.cli.deploy \
  --agent-name my-kern \
  --operator-cloudflare-hostname <hostname-from-step-3> \
  --admin-password-sha256 <sha256-from-above>
```

To reach the host over SSH instead of a Cloudflare Tunnel — a simpler setup
that skips step 3, with the admin UI available only while you run an SSH
tunnel — pass `--operator-ssh-public-key` with your OpenSSH public key in
place of (or alongside) `--operator-cloudflare-hostname`. See
[SSH Operator Access](#ssh-operator-access) to create the key.

The command creates the host and installs Kern, streaming progress as
it runs, and finishes by printing a result JSON with the host's address.

The AWS credentials are no longer needed after deploy. Remove them from this
terminal and end the browser login session:

```bash
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
aws logout
```

### 5. Open Kern

Have the admin password you chose in step 4 ready.

If you used Cloudflare, the tunnel now shows **Healthy**. Open the hostname from
step 3 and sign in with the admin password.

If you deployed without Cloudflare, use the `public_dns` value from the same
deploy result to start the SSH tunnel. Leave this terminal open:

```bash
ssh -i ~/.ssh/kern_operator \
  -L 7443:127.0.0.1:7443 \
  kern-operator@<public-dns>
```

Then open [http://127.0.0.1:7443](http://127.0.0.1:7443) and sign in with the
admin password. Type `exit` in the SSH terminal to close the tunnel; the host
keeps running.

Your host is ready. The admin UI guides you through connecting an AI provider,
enabling network access, and adding optional tools.

## Why Use Kern

- **Runs in the cloud by default:** keep long-running agents active without
  keeping your laptop open.
- **No permission prompts:** the agent runs autonomously in auto-approve mode
  as an unprivileged Linux user, while filesystem and network controls prevent
  broad host-state access, unapproved data leaks, and unexpected internet
  actions.
- **Controlled tools:** bundled tool packages (Gmail, Zoho Mail, Google Calendar,
  Brave Search, X/Twitter, LinkedIn, LinkedIn Discovery, Instagram, Instagram
  Discovery, Polymarket, Interactive Brokers, Runway media generation, Seedance
  video generation, OpenAI image generation) connect
  agents to third-party services through deterministic data paths, with
  operator approval required for outward-facing actions such as sending email
  or publishing a post
  ([tools architecture](docs/architecture/tools/README.md)).
- **Built-in workspaces:** **Chat** provides threaded agent conversations and
  **Apps** provides isolated, agent-generated Web App workspaces with durable
  UI, data, and checkpoints. Host-global **Memory** and **Schedules** are
  available to every agent; a schedule runs an agent turn or, for recurring
  work that needs no reasoning, a static bash script from the agent home. These are fixed host capabilities—not installable
  packages—and share one restricted service behind the authenticated admin UI. See
  [docs/architecture/workspaces/workspaces.md](./docs/architecture/workspaces/workspaces.md).

These choices follow from a broader set of beliefs about running AI agents.
See [PHILOSOPHY.md](./PHILOSOPHY.md).

## Configuration Reference

Lifecycle commands take arguments and standard environment variables; there
are no configuration files.

Arguments:

| Argument | What To Put |
| --- | --- |
| `--agent-name` | Stable host name. Lifecycle commands use it to find the same host and data volumes. |
| `--operator-ssh-public-key` | For SSH operator access: the public key content to install, for example the output of `cat ~/.ssh/id_ed25519.pub`. |
| `--operator-cloudflare-hostname` | For a Cloudflare Tunnel: the fixed hostname that routes to the admin UI/API. |
| `--admin-password-sha256` | SHA-256 hex digest of the chosen admin password. |

Deploy and reconfigure require at least one operator endpoint; use one or
both endpoint arguments.

Environment variables:

| Variable | What To Put |
| --- | --- |
| `AWS_REGION` | AWS region of the host. Required by every command; `AWS_DEFAULT_REGION` also works. |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | AWS credentials. |
| `AWS_SESSION_TOKEN` | Set only for temporary credentials (an assumed STS role); unset it when using long-lived access keys, or every AWS call fails with an authentication error. |
| `KERN_CLOUDFLARE_TUNNEL_TOKEN` | The Cloudflare Tunnel token. Required when `--operator-cloudflare-hostname` is passed. |

For long-lived credentials, export `AWS_ACCESS_KEY_ID` and
`AWS_SECRET_ACCESS_KEY`. For temporary credentials, also export
`AWS_SESSION_TOKEN`, and repeat the AWS sign-in and export step before a
lifecycle command whenever they expire.

### AWS Account Setup

For a first evaluation, the short-lived `aws login` session in the walkthrough
avoids creating an access key. Deploy uses the default VPC and needs a default
subnet with public IPv4 routing.

For regular use or automation, attach `iam_policy.json` to a federated IAM role.
The policy requires Kern tags on created resources, allows EC2 updates
and cleanup only on Kern-tagged resources, and leaves region selection to
`AWS_REGION`.
See [`docs/architecture/iam-policy.md`](docs/architecture/iam-policy.md) for
why each policy statement is needed and how its resource scope is constrained.

If federation is unavailable, the commands below create a dedicated IAM user
with the same policy. AWS recommends federation instead of long-lived IAM user
credentials for real data.

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

### SSH Operator Access

Create an SSH keypair if you do not already have one:

```bash
ssh-keygen -t ed25519 -C kern-operator -f ~/.ssh/kern_operator
```

Then pass the public key to deploy or reconfigure:

```bash
--operator-ssh-public-key "ssh-ed25519 AAAA... kern-operator"
```

When an SSH endpoint is configured, Kern keeps EC2 security-group ingress
for TCP 22 and installs the key for `kern-operator`. If SSH is omitted,
the final host closes EC2 SSH ingress after bootstrap.

### Viewing an Agent Preview Server

The agent can run web servers — a dev server, a UI it is building, a test
harness — on a reserved loopback port range, `8000-8015`. Nothing on this range
is exposed publicly, and it is not shown in the admin UI. To view one, forward
its port to your own machine over SSH (this needs SSH operator access enabled,
above) and open it in your browser:

```bash
ssh -i ~/.ssh/kern_operator \
  -L 8000:127.0.0.1:8000 \
  kern-operator@<public-dns>
```

Then open **`http://preview.localhost:8000`** (matching the port you forwarded).

Use the `preview.localhost` hostname, not `localhost` or `127.0.0.1`. A preview
server serves agent-authored content, and browser cookies are scoped by hostname
regardless of port — so if you browse a preview on the same hostname you use for
the admin UI (`http://127.0.0.1:7443` in the tunnel above), your admin session
cookie would be sent to it. `preview.localhost` resolves to loopback in modern
browsers and is a distinct hostname, so no admin cookie is ever in scope. As with
any untrusted web page, do not enter credentials into a preview.

If your browser does not resolve `*.localhost` (some Safari builds), bind a
dedicated loopback address instead and open that:

```bash
ssh -i ~/.ssh/kern_operator -L 127.0.0.9:8000:127.0.0.1:8000 kern-operator@<public-dns>
# then open http://127.0.0.9:8000  (on macOS, first: sudo ifconfig lo0 alias 127.0.0.9 up)
```

### Cloudflare Tunnel Operator Access

The recommended walkthrough above covers Cloudflare setup from a new account.
Pass the published hostname to deploy or reconfigure and export the tunnel
token:

```bash
export KERN_CLOUDFLARE_TUNNEL_TOKEN='eyJ...'
--operator-cloudflare-hostname kern.example.com
```

Kern installs `cloudflared` as a systemd service, enables it across
reboots, and verifies during bootstrap that the configured hostname reaches the
admin API's login gate (never an unauthenticated `200`). The tunnel is transport
and Cloudflare edge protection only; the admin password login is the
authentication boundary.

See [`docs/api/CLI.md`](docs/api/CLI.md) for the full argument and
environment reference.

## Manage Your Host

Run lifecycle commands from the repository root. Each command streams
progress on stderr and prints one result JSON on stdout, so `> result.json`
captures the result cleanly.

Host lifecycle commands:

| Command | Behavior | Credential behavior |
| --- | --- | --- |
| `python3 -m host.cli.deploy --agent-name <name>` | Creates a new host. Fails if a Kern instance or data volume already exists for `agent_name`. | Installs the `--admin-password-sha256` digest as the admin password hash. Installs the configured operator endpoints. |
| `python3 -m host.cli.upgrade --agent-name <name>` | Replaces the EC2 instance/root volume and reuses the preserved admin and agent data volumes. Requires an existing instance and existing data volumes. Bootstrap requires the admin state version to be lower than the repo `VERSION`. | Preserves the existing admin password and operator endpoints from admin state. |
| `python3 -m host.cli.recover --agent-name <name>` | Creates a replacement host from preserved admin and agent data volumes when no Kern instance exists. Bootstrap requires the admin state version to equal the repo `VERSION`, unless `--allow-upgrade` is supplied. | Preserves the existing admin password and operator endpoints from admin state. |
| `python3 -m host.cli.reconfigure --agent-name <name>` | Replaces an existing EC2 instance/root volume, reuses preserved admin and agent data volumes, and replaces the full operator endpoint list. Requires an existing instance and existing data volumes. Bootstrap requires the admin state version to equal the repo `VERSION`. | Installs the `--admin-password-sha256` digest as the new admin password hash. |
| `python3 -m host.cli.start --agent-name <name>` | Starts the existing EC2 instance for the agent and waits until it is running. | Does not change credentials, root disk, data volumes, version, or operator endpoints. |
| `python3 -m host.cli.stop --agent-name <name>` | Stops the existing EC2 instance for the agent and waits until it is stopped. | Does not change credentials, root disk, data volumes, version, or operator endpoints. |
| `python3 -m host.cli.generate_password` | Prints a generated admin password and its SHA-256 digest, then exits. Touches no config, AWS resources, or files. | Store the password in your password manager; pass the digest to `--admin-password-sha256`. |

Shared flags:

| Flag | Commands | Behavior |
| --- | --- | --- |
| `--agent-name <name>` | all | Required. Stable host name: 1-50 characters of letters, numbers, hyphen, underscore. |
| `--operator-ssh-public-key <key>` | `deploy`, `reconfigure` | Installs this OpenSSH public key as the SSH operator endpoint. At least one operator endpoint is required. |
| `--operator-cloudflare-hostname <host>` | `deploy`, `reconfigure` | Configures a Cloudflare Tunnel operator endpoint at this exact hostname; the tunnel token is read from `KERN_CLOUDFLARE_TUNNEL_TOKEN`. At least one operator endpoint is required. |
| `--admin-password-sha256 <hex>` | `deploy`, `reconfigure` | Required. SHA-256 hex digest of the chosen admin password, for example `printf %s 'your-password' | sha256sum`. The CLI and the host only ever see this hash. |
| `--bootstrap-from-github [commit-sha]` | `deploy`, `upgrade`, `recover`, `reconfigure` | Provisions the instance from a pinned `infiloop2/kern` commit via EC2 user data instead of pushing the local checkout over SSH; without a value, the latest `main` commit is pinned. The CLI first reads the commit's `VERSION` from GitHub — that version is the operation's target and must exactly equal the local checkout's `VERSION`, or the command aborts before any AWS call — and asks for confirmation. The command returns once the instance is launched with its volumes attached; bootstrap completes on the host, and a bootstrap failure terminates the instance. |
| `--allow-upgrade` | `recover` | Allows no-instance recovery to advance preserved admin state from an older version to the target `VERSION`. |

Lifecycle commands fail before replacing an existing instance when the AWS
resource shape or version tag is incompatible with the command. Bootstrap then
checks the preserved admin disk version as the authoritative source before
writing any upgraded state.

The admin toolbar quietly shows version status after checking the `VERSION` on
the public repository's `main` branch. A small upgrade icon shows the available
version and reminds you to use the operator plane; a small checkmark confirms
the host is at the latest version. The icons themselves perform no action.

### Recovering From Lost Passkeys

If you enable passkey login for the admin UI and later lose every enrolled
passkey, you can still recover from the operator plane — passkeys are optional
convenience credentials, and the admin password remains a valid way in. Run
`reconfigure` with `--reset-admin-passkeys`:

```bash
python3 -m host.cli.reconfigure \
  --agent-name my-kern \
  --operator-cloudflare-hostname <hostname> \
  --admin-password-sha256 <sha256> \
  --reset-admin-passkeys
```

This deletes every enrolled admin passkey so you can sign in with the admin
password again and enroll new passkeys. It preserves your admin and agent data
volumes (Postgres state, tasks, audit logs, credentials, apps, network policy)
unchanged. Because it is a normal `reconfigure`, it installs the
`--admin-password-sha256` digest as the admin password and replaces the full
operator endpoint list — pass your current password digest and endpoints unless
you also intend to change them. The same flow doubles as the way to move to a
new public admin hostname.

The host uses three EBS volumes:

| Volume | Lifecycle | Contents |
| --- | --- | --- |
| Root | Recreated on redeploy and deleted on instance termination | Ubuntu 22.04, system packages, Node.js, Python, Codex CLI, Claude Code CLI, nftables, OpenSSL, curl, jq, CA certificates, and swap. |
| Admin | Preserved on redeploy and marked not to delete on instance termination | Postgres state for the admin API, apps, tools, tasks, audit logs, network policy, credentials, and provider pins; proxy CA/certificate, queued-push state, and a bounded temporary tool-media spool. |
| Agent | Preserved on redeploy and marked not to delete on instance termination | Agent home directory, provider auth/session files, CLI caches, and workspace data. |

Every AWS resource deploy creates is tagged so it can be found and cleaned up:

| Tag | Value | On |
| --- | --- | --- |
| `kern-host-agent-name` | `<agent_name>` | instance, volume, security group |
| `kern-host` | `true` | instance, volume, security group |
| `Name` | `kern-host-<agent_name>` | instance, volume |
| `kern-host-volume-role` | `admin` or `agent` | data volumes |
| `kern-host-version` | repo `VERSION` | instance |

See [`docs/api/DeployResult.md`](docs/api/DeployResult.md) for the lifecycle
result file schema.

## Admin API and File Uploads

With the SSH tunnel from [step 5](#5-open-kern) active, log in once to get a
session cookie, then reuse it (with the CSRF header) for API calls:

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

Agent Chat exposes this flow through the attachment button in its task
composer. It keeps up to ten selections in browser memory until Send, then
uploads each file and adds the returned `user-files/...` paths to the task
message.

## Internals

The host runs on an AWS EC2 instance. The admin API, network proxy, tools
service, Workspace service, optional Cloudflare Tunnel connector, database,
and agent runtime run as separate Linux users. Filesystem ownership,
peer-authenticated local sockets, scoped database roles, and uid-based
firewall rules keep the agent from getting direct network access or broad
access to host state.

For deeper architecture and contribution notes, read:

- [`docs/architecture/diagram.md`](docs/architecture/diagram.md), for a
  one-page host capability map
- [`docs/architecture/index.md`](docs/architecture/index.md)
- [`docs/development/index.md`](docs/development/index.md)
- [`docs/api/index.md`](docs/api/index.md)
- [`docs/audit-reports/README.md`](docs/audit-reports/README.md)

## License

Kern is source-available under the Business Source License 1.1.
You may self-deploy and run Kern for non-commercial purposes at no charge.
Commercial production use requires a commercial license, available on request
from the copyright holder. Evaluation, development, and testing are free for
everyone under the license's standard non-production grant.

The Change Date is 2030-07-09, after which the Change License is the GNU
Affero General Public License v3.0 or any later version. See [LICENSE](LICENSE)
and [NOTICE](NOTICE).
