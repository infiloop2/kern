# Kern

**A permanent home for your AI team.**

Bring your own agents: Codex, Claude Code, Grok and Hermes (Muse and Antigravity
coming soon). Switch between them with ease, or let them work together as a team.
Your conversations, files, shared memory and connections stay in Kern. Kern
carries the work forward through host-managed context and memories.

Kern is the VM your agents live in, not one they spawn. Connect your internet
accounts in one place and give your agents access under network and tool rules
you control. Manage it all from a browser workspace.

## Why Kern?

- **One persistent home across agents.** Configure connections once and keep
  conversations, files and shared memory together. Switch agents or let them
  work together without rebuilding your setup.
- **An internet firewall you control.** Kern enforces where agents can connect.
  Separate tool permissions and approval rules control what they can do with
  connected accounts.
- **Orchestrate your team from anywhere.** Run agents in parallel, let them
  delegate and communicate, and schedule ongoing work. Manage the team from one
  browser command center on your laptop or phone with remote HTTPS access.

Kern does not resell model tokens or add a model-token quota. Your AI
subscription usage limits, configured controls, and any hosting or
connected-service costs still apply.

Learn more at [kernai.cloud](https://kernai.cloud), read the
[documentation](https://docs.kernai.cloud), or explore the
[design philosophy](PHILOSOPHY.md). Contributor references live in the
[repository documentation index](docs/README.md).

## One Kern. Start locally, then choose your host.

1. [**Try it on your computer.**](#quick-start-run-kern-on-your-computer)
   Run the full Kern workspace on Mac or Linux, with a local browser connection
   over SSH. No cloud account is needed for this step.
2. **Keep running it yourself.** Leave that computer powered on and awake,
   or [deploy to your AWS account](#always-on-deploy-to-aws). Add
   [Cloudflare Tunnel](#access-from-anywhere-add-a-cloudflare-tunnel) for a
   stable HTTPS address you can open on your phone. You manage the machine
   and its upgrades, and are responsible for its running costs.
3. **Or choose [Kern Cloud](https://app.kernai.cloud).** Managed hosting
   provides an always-on home without maintaining your own machine. You can
   deploy in one click, without manual setup or CLI commands.

## Quick Start: Run Kern on Your Computer

The fastest way to run Kern: a fully local host in a
[Lima](https://lima-vm.io) virtual machine on macOS or Linux, reached over
loopback SSH. Nothing is exposed to your LAN or the internet, and no cloud
account or credentials are needed.

**You need:** macOS or Linux with hardware virtualization, Python 3.11+,
[Lima >=2.2 and <3](https://lima-vm.io/docs/installation/), Git, curl and
OpenSSH (`ssh`, `scp`, `ssh-keygen`). Linux also needs QEMU (`qemu-system-*`
for your architecture and `qemu-img`).

**Run on macOS or Linux:**

```bash
curl -fsSL https://kernai.cloud/quickstart | bash
```

Quick-start guides you through dependencies and prompts to install missing ones.
The script clones the public source into `~/kern` and runs
`python3 quickstart.py` with your terminal attached.
Set `KERN_SOURCE_DIR` for the Bash process to choose a different folder.

**Or start from your own checkout:**

```bash
git clone https://github.com/infiloop2/kern.git
cd kern
python3 quickstart.py
```

Quick-start creates the `my-kern` Lima host with 2 CPUs, 2 GiB RAM and three
16 GiB disks, generates a private SSH key and admin password, then opens
the browser through loopback
SSH. Save the password in your password manager, then delete
`initial-password.txt` at the displayed path.

Quick-start creates:

| Artifact | Location or name |
| --- | --- |
| Lima VM and system disk | `kern-my-kern-<hash>` in `limactl list` |
| Two persistent data disks | `<instance>-admin` and `<instance>-agent` |
| Source checkout | `~/kern` by default |
| SSH key, initial password and connection state | `~/.local/share/kern/hosts/my-kern-<hash>/` |
| Kern's saved VM settings | `~/.lima/.kern-config/my-kern-<hash>.json` (under `LIMA_HOME` if set) |

Connect your AI account in the UI. The first start needs downloads and may take
several minutes; progress appears in the terminal.

Run `python3 quickstart.py` again from the same checkout to resume or reopen
that host without replacing credentials. Use the existing Python commands for
ongoing management:

```bash
python3 -m host.cli.stop --provider lima --agent-name my-kern
python3 -m host.cli.start --provider lima --agent-name my-kern
python3 -m host.cli.reconfigure --help
```

To update a checkout without local edits and upgrade the host:

```bash
cd ~/kern
git pull --ff-only
python3 -m host.cli.upgrade --provider lima --agent-name my-kern
python3 quickstart.py
```

**Delete the local host and its data:** find its exact name with `limactl list`
and replace the value below. This permanently removes its VM and saved work.
If resuming cleanup, skip resources that are already absent.

```bash
KERN_INSTANCE='kern-my-kern-<hash>'
limactl stop "$KERN_INSTANCE"
limactl delete --force "$KERN_INSTANCE"
limactl disk list
```

Wait until both data disks show no attached instance, then delete them:

```bash
limactl disk delete "${KERN_INSTANCE}-admin"
limactl disk delete "${KERN_INSTANCE}-agent"
```

Once both disks are gone, remove the saved credentials and configuration:

```bash
rm -rf "$HOME/.local/share/kern/hosts/${KERN_INSTANCE#kern-}"
rm -f "${LIMA_HOME:-$HOME/.lima}/.kern-config/${KERN_INSTANCE#kern-}.json"
```

Delete the `~/kern` source checkout separately if you no longer need it.

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

For a quick-start host, run these blocks in Bash on the physical computer, one
at a time so each prompt reads your typing rather than the next pasted line.
Replace `kern.example.com` with your configured hostname. The generated SSH
key is retained so local management stays independent of Cloudflare:

```bash
cd ~/kern
KERN_LOCAL_STATE=$(python3 -c 'import quickstart; print(quickstart.state_dir("my-kern"))')
read -r -s -p 'Cloudflare tunnel token: ' KERN_CLOUDFLARE_TUNNEL_TOKEN; printf '\n'
```

```bash
KERN_ADMIN_HASH=$(python3 -c 'import getpass, hashlib; print(hashlib.sha256(getpass.getpass("Admin password to set: ").encode()).hexdigest())')
```

```bash
export KERN_CLOUDFLARE_TUNNEL_TOKEN
python3 -m host.cli.reconfigure \
  --provider lima --agent-name my-kern \
  --admin-password-sha256 "$KERN_ADMIN_HASH" \
  --operator-ssh-public-key "$(cat "$KERN_LOCAL_STATE/operator_ed25519.pub")" \
  --operator-cloudflare-hostname kern.example.com
unset KERN_CLOUDFLARE_TUNNEL_TOKEN KERN_ADMIN_HASH
python3 quickstart.py
```

Enter your current admin password to retain it, or a new one to change it.
Reconfiguration replaces compute and may interrupt active work; durable data
is preserved. Verify local sign-in, then open the HTTPS hostname on your phone.
The computer must remain powered on and awake.

For a custom host, use its name and SSH public key. `deploy` accepts the same
endpoint flags for a new host; `reconfigure` replaces the full endpoint list,
so include every endpoint you want to retain.

## Always On: Deploy to AWS

A local host is only available while your machine is on and awake. For
agents that should keep working around the clock, deploy the identical host
to AWS. Everything above still applies: SSH and/or Cloudflare access, the
same commands, the same admin UI.

**Cost:** one `t3.small` EC2 instance, one public IPv4 address, and 48 GiB
of gp3 disk. A newly created
[AWS Free Tier](https://aws.amazon.com/free/) account usually costs `$0`
while its included credits remain; outside those credits, expect about
`$23/month` in `us-east-1`. AI usage is billed separately through your AI
subscriptions or API accounts.

New AWS instances use **Unlimited CPU credits**, allowing bootstrap and agent
startup to burst above the `t3.small` baseline even when earned credits run out.
Sustained CPU usage above the baseline can incur surplus-credit charges in
addition to the instance cost.

**You need:**

- An AWS account. A newly created one works; no prior configuration is needed.
- [AWS CLI v2](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html),
  Python 3.11+, OpenSSH and a Kern checkout
  (`git clone https://github.com/infiloop2/kern.git`). Run the commands below
  from that checkout.
- A Cloudflare Tunnel from the section above (recommended), or SSH-only
  access with `--operator-ssh-public-key`.

### 1. Connect Your AWS Account

Choose either option below. Run the commands in Bash (`bash` first if your
shell is Zsh). The region is part of the host identity; use the same region
for later lifecycle commands.

**Use your existing AWS administrator sign-in.** With AWS CLI 2.32.0 or newer,
run [`aws login`](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-sign-in.html)
and select your administrator IAM user or role in the browser:

```bash
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
export AWS_REGION=us-east-1
aws login --profile kern-deploy
```

After signing in, export the temporary credentials for Kern and check the account:

```bash
eval "$(aws configure export-credentials --profile kern-deploy --format env)"
aws sts get-caller-identity
```

Kern uses the exported credentials, with that identity's permissions. Repeat
the login and export steps when they expire. Continue to **2. Deploy**.

**Or create scoped deployment credentials.**

In the AWS IAM console, create a dedicated user named `kern-host-deploy`.
Create a policy from [`iam_policy.json`](iam_policy.json), attach it to that
user, and generate an access key for CLI use. Keep the key in your password
manager. The policy scopes resource changes to Kern-tagged resources; see the
[IAM policy reference](docs/architecture/iam-policy.md) for its permissions.

Run one block at a time so each prompt reads your typing rather than the next
pasted line. This keeps the secret out of shell history:

```bash
read -r -p "AWS access key ID: " AWS_ACCESS_KEY_ID
```

```bash
read -r -s -p "AWS secret access key: " AWS_SECRET_ACCESS_KEY; printf '\n'
```

```bash
export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY
unset AWS_SESSION_TOKEN
export AWS_REGION=us-east-1
aws sts get-caller-identity
```

### 2. Deploy

Deploy uses the default VPC and needs a default subnet with public IPv4 routing.

Generate an admin password, save it in your password manager, and pass its
`sha256` value to deploy:

```bash
python3 -m host.cli.generate_password
```

```bash
export KERN_CLOUDFLARE_TUNNEL_TOKEN='eyJ...'   # from the tunnel section

python3 -m host.cli.deploy \
  --agent-name my-kern \
  --operator-cloudflare-hostname kern.example.com \
  --admin-password-sha256 <sha256>
```

For SSH access, add `--operator-ssh-public-key "$(cat ~/.ssh/id_ed25519.pub)"`
in place of (or alongside) the Cloudflare hostname; create the key first with
`ssh-keygen -t ed25519` if you have none. The command creates the host and
installs Kern, streaming progress as it runs, and finishes by printing a
result JSON with the host's address.

The AWS credentials are no longer needed in this terminal after deploy:

```bash
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
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

## Manage Your Host

Run lifecycle commands from the repository root. Each command streams
progress on stderr and prints one result JSON on stdout, so `> result.json`
captures the result cleanly. Every lifecycle and power command accepts
`--provider lima` to operate a local host; AWS commands also need `AWS_REGION`
and credentials in the environment.

| Command | Behavior | Credential behavior |
| --- | --- | --- |
| `python3 -m host.cli.deploy --agent-name <name>` | Creates a new host. Fails if a Kern instance or data volume already exists for `agent_name`. | Installs the `--admin-password-sha256` digest as the admin password hash. Installs the configured operator endpoints. |
| `python3 -m host.cli.upgrade --agent-name <name>` | On AWS, retains a running or stopped instance and replaces only its root volume; Lima recreates its VM. Preserves admin and agent data volumes. Bootstrap requires the admin state version to be lower than the repo `VERSION`. | Preserves the existing admin password and operator endpoints from admin state. |
| `python3 -m host.cli.recover --agent-name <name>` | Creates a replacement host from preserved admin and agent data volumes, deleting the existing instance if present. Bootstrap requires the admin state version to equal the repo `VERSION`, unless `--allow-upgrade` is supplied. | Preserves the existing admin password and operator endpoints from admin state. |
| `python3 -m host.cli.reconfigure --agent-name <name>` | Uses the same fresh-root path as upgrade, reuses preserved admin and agent data volumes, and replaces the full operator endpoint list. Bootstrap requires the admin state version to equal the repo `VERSION`. | Installs the `--admin-password-sha256` digest as the new admin password hash. |
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
| Admin | Preserved on redeploy and marked not to delete on instance termination | Postgres state for the admin API, apps, tools, tasks, audit logs, network policy, credentials, and approved AI account identities; proxy CA/certificate, queued-push state, and a bounded temporary tool-media spool. |
| Agent | Preserved on redeploy and marked not to delete on instance termination | Agent home directory, AI account credentials and session files, CLI caches, and workspace data. |

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
passkey, recover through the operator plane. Once enrolled, the passkey is
required alongside the password on public HTTPS. SSH-forwarded access still
uses the SSH key and admin password. To reset public passkey access, run
`reconfigure` with `--reset-admin-passkeys`:

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

On a local host, use the loopback SSH endpoint and the key installed for
`kern-operator` instead:
`ssh -i <private-key> -p <port> -L 8000:127.0.0.1:8000 kern-operator@127.0.0.1`.
A quick-start host's key is `operator_ed25519` in the directory printed by
`python3 -c 'import quickstart; print(quickstart.state_dir("my-kern"))'`.

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

## Programmatic Admin API Access

Use the Admin API to manage Kern from your own scripts and tools. With an
SSH tunnel to the admin UI active, log in once to get a session cookie,
then reuse it with the CSRF header for API calls:

```bash
curl -c cookies.txt -H "Content-Type: application/json" \
  -d '{"password": "<admin-password>"}' \
  http://127.0.0.1:7443/v1/login

curl -b cookies.txt -H "X-Kern-Csrf: 1" \
  http://127.0.0.1:7443/v1/health
```

Full admin API documentation is in
[`docs/api/AdminAPI.md`](docs/api/AdminAPI.md).

## Internals

The host runs on an AWS EC2 instance or a local Lima VM. The admin API,
network proxy, tools service, Workspace service, local embedding service,
optional Cloudflare Tunnel connector, database, and agent runtime run as
separate Linux users. Filesystem ownership, peer-authenticated local sockets,
scoped database roles, and uid-based firewall rules keep the agent from
getting direct network access or broad access to host state.

For deeper details, see the
[architecture documentation](docs/architecture/index.md).

## License

Kern is licensed under the [MIT License](LICENSE). Third-party software
keeps its own license; see [NOTICE](NOTICE).
