#!/usr/bin/env python3
"""The smoke test: deploy a real host, validate it end to end, tear it down.

This is the fresh-host smoke test for the project. It validates the deploy,
bootstrap, admin API, proxy, and state-management paths that unit tests cannot.
It deliberately does not require Codex or Claude OAuth login; persistent,
login-dependent runtime checks live in ``tests/stage/stage_aws.py``.

  - the deploy path (subnet selection, security group, IMDSv2, SSH provisioning)
  - the bootstrap on real Ubuntu 22.04 (apt, npm, user/permission setup,
    nftables, systemd, the proxy CA)
  - the admin API answering over the SSH tunnel (health, network policy)
  - the real admin UI in headless Chrome: login, app navigation, Mission
    Pursuit popovers and settings, first-message turn creation, and the clear
    pre-provider-login rejection shown back in the workspace
  - admin API contract edge cases over the tunnel: auth rejection, the
    thread-message admission contract and its 4xx responses,
    policy validation (including managed OpenAI and Claude provider schema),
    and event pagination
  - concurrency on the real host: parallel message admissions, a same-key
    concurrent policy replaces, and parallel proxy traffic
    with consistent event sequencing
  - state transaction edge cases on the real host: racing first messages on
    one thread are all rejected whole-cloth (a rejected admission leaves no
    thread row and no events), racing stops answer cleanly, and parallel
    writers never duplicate an event seq
  - network enforcement on the real host: the agent reaches an allowed domain
    only through the proxy, a denied path is blocked, direct external egress is
    dropped by nftables, and the admin listener admits only the Cloudflare,
    SSH-operator, admin-service, and root identities
  - deploy-time config schema on the real host: agent_runtime, agent_type,
    operator connection details and network_controls are absent from persisted runtime
    config; first boot creates an empty runtime network policy, with no
    network_status.json, and all three runtimes stay deactivated until the runtime
    policy enables their managed providers
  - proxy protocol edge cases: CONNECT port pinning, unknown hosts, Host
    header mismatch, percent-encoded paths against path guards, wildcard
    domain rules, malformed request lengths, and plain-HTTP proxying
  - provider guard pre-login behavior: managed OpenAI/Claude access wakes
    runtimes but does not require completing OAuth
  - real Hermes launcher startup through the admin sudo path, systemd
    scope, installed package, stdin protocol, dummy AWS identity, and proxy,
    which denies locally before an upstream call because no credential exists
  - the cross-process event log: the network event table is pushed past its
    amortized prune threshold by the proxy process (writing under its narrow
    database role) while the admin API concurrently pages it — reads stay
    consistent, seqs stay unique and ordered, and the proxy role can write
    exactly that one table

Only run this from the dedicated smoke GitHub Action or manually with the
scoped smoke AWS credentials. It creates real billable AWS resources and needs
real network. Unit-test CI runs with no network on purpose.

This script starts by resetting any stale smoke data volumes, then tears down
all resources tagged for the smoke agent (instances, volumes, and security
groups), even if deploy failed before writing its result file.

The smoke owns its own deploy config: it deploys an agent named
``kern-smoke`` into ``SMOKE_REGION`` (below), which is pinned to the
region scoped in ``tests/smoke/iam_policy_smoke.json`` so the two cannot drift. It
also generates an ephemeral SSH keypair for operator access and discards it at
teardown. So there is nothing to write by hand — no config file and no SSH key.

Environment assumptions (each is checked, with a clear failure if missing):

  1. The ``aws`` CLI and ``ssh`` are installed and on PATH.
  2. AWS credentials with the permissions in ``tests/smoke/iam_policy_smoke.json``
     are exported as ``AWS_ACCESS_KEY_ID`` / ``AWS_SECRET_ACCESS_KEY``. See
     docs/development/fresh-aws-smoke.md for how to create a scoped IAM user.

Cost: one t3.small plus a 16 GiB root gp3 volume, a 16 GiB encrypted admin
volume, and a 16 GiB encrypted agent volume for the few minutes the test runs
(about one US cent). The launcher probes cannot incur model inference cost
because no Bedrock credential exists. Teardown
removes the instance root volume and all tagged smoke data volumes.

Usage:
    export AWS_ACCESS_KEY_ID=...
    export AWS_SECRET_ACCESS_KEY=...
    python3 tests/smoke/smoke_aws.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets as py_secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import quote

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from host.constants import ADMIN_API_PORT as ADMIN_PORT, AGENT_PREVIEW_PORT_BASE, PROXY_PORT
from host.network_integrations.bedrock.manifest import ROUTING_ACCESS_KEY_ID
from host.runtime.core.state import PRUNE_EVERY
from host.runtime.tools.tools_host import BUNDLED_TOOLS

# The agent's whole MCP surface. Spelled out rather than derived from the shim
# so a deployed host is checked against what we intend the model to see: these
# declarations head the prompt, and any movement in them re-encodes every
# cached context behind them (host/agent_tool_surface.py).
STATIC_SHIM_TOOLS = [
    "list_bundled_tools",
    "describe_tool",
    "call_tool",
    "check_tool_approval",
    "list_network_integrations",
    "recent_network_denials",
    "stage_image",
    "stage_video",
    "search_conversation_history",
    "read_thread_history",
    "workspace_api",
]

# Region the smoke deploys into. Keep in sync with the region scoped in
# tests/smoke/iam_policy_smoke.json — change both together.
SMOKE_REGION = "us-east-1"
SMOKE_AGENT_NAME = "kern-smoke"
ACCESS_KEY_ENV = "AWS_ACCESS_KEY_ID"
SECRET_KEY_ENV = "AWS_SECRET_ACCESS_KEY"

HEALTH_TIMEOUT = 600  # bootstrap installs packages; allow time before the API answers
DEPLOY_TIMEOUT = 1200  # tolerate a slow Ubuntu mirror, but bound deploys to 20 minutes
MESSAGE_LIMIT = 50_000  # mirrors the admin API's message cap
# Public thread events that explicitly settle running work. Successful
# completion has no event and is observed through the thread's idle status.
TURN_TERMINAL_STATUSES = {
    "thread.error": "failed",
    "thread.stopped": "cancelled",
}
# The per-thread fence while a previous turn's process closes; senders retry it.
THREAD_BUSY_MARKER = "agent is finishing"
RUNTIME_INACTIVE_MARKER = "messages run only while it is active"
SMOKE_RUNTIMES = ("codex", "claude_code", "hermes")
OFFERED_RUNTIMES = ("codex", "claude_code", "grok", "hermes")
SMOKE_OAUTH_RUNTIMES = ("codex", "claude_code")
SMOKE_MANAGED_PROVIDERS = {"openai": True, "claude": True, "bedrock": True}
SMOKE_BEDROCK_REGION = "us-east-1"
SMOKE_RUNTIME_MODELS = {
    "codex": "gpt-5.6-terra",
    "claude_code": "claude-opus-5",
    "grok": "grok-4.6",
    "hermes": "qwen.qwen3-coder-next",
}
SMOKE_BEDROCK_MODELS = (
    "deepseek.v3.2",
    "qwen.qwen3-coder-next",
    "moonshotai.kimi-k2.5",
)
SMOKE_GITHUB_INTEGRATION = {"enabled": True, "write_repositories": [{"owner": "infiloop2", "repo": "kern"}]}
SMOKE_MANAGED_DOMAINS = (
    "api.openai.com",
    "auth.openai.com",
    "chatgpt.com",
    "api.anthropic.com",
    "platform.claude.com",
    "github.com",
    "api.github.com",
    "uploads.github.com",
    "codeload.github.com",
    "raw.githubusercontent.com",
    "objects.githubusercontent.com",
    "github-cloud.githubusercontent.com",
    "release-assets.githubusercontent.com",
    "bedrock-runtime.us-east-1.amazonaws.com",
    "bedrock-runtime.us-east-2.amazonaws.com",
    "bedrock-runtime.us-west-2.amazonaws.com",
)
SMOKE_TOOL_CALLS: dict[str, tuple[tuple[str, dict], ...]] = {
    "apify": (
        (
            "search_businesses",
            {"query": "bakery", "location": "London, United Kingdom", "limit": "1"},
        ),
        (
            "get_business_details",
            {
                "place_id": "ChIJN1t_tDeuEmsRUsoyG83frY4",
                "max_reviews": "0",
                "max_images": "0",
            },
        ),
    ),
    "brave_search": (("search_web", {"query": "Kern"}),),
    "web_fetch": (("fetch_page", {"url": "https://example.com/"}),),
    "gmail": (
        ("search_messages", {}),
        ("read_message", {"message_id": "smoke-message"}),
        ("read_thread", {"thread_id": "smoke-thread"}),
        ("list_labels", {}),
        ("list_drafts", {}),
        (
            "send_email",
            {
                "to": "stage@example.com",
                "subject": "Kern smoke",
                "blocks": [{"type": "paragraph", "text": "Smoke test; never sent."}],
            },
        ),
        ("message_action", {"action": "mark_read", "message_ids": ["smoke-message"]}),
        ("label_action", {"action": "create", "name": "kern-smoke"}),
        (
            "draft_action",
            {
                "action": "create",
                "to": "stage@example.com",
                "subject": "Kern smoke draft",
                "blocks": [{"type": "paragraph", "text": "Smoke test draft."}],
            },
        ),
    ),
    "google_calendar": (
        ("read_events", {}),
        (
            "event_change",
            {
                "operation": "create",
                "summary": "Kern smoke",
                "start_time": "2099-01-01T00:00:00+00:00",
                "end_time": "2099-01-01T01:00:00+00:00",
            },
        ),
    ),
    "google_search_console": (
        ("list_properties", {}),
        (
            "query_search_analytics",
            {
                "site_url": "https://example.com/",
                "start_date": "2099-01-01",
                "end_date": "2099-01-02",
            },
        ),
        ("list_sitemaps", {"site_url": "https://example.com/"}),
        (
            "inspect_url",
            {
                "site_url": "https://example.com/",
                "inspection_url": "https://example.com/",
            },
        ),
        (
            "submit_sitemap",
            {
                "site_url": "https://example.com/",
                "sitemap_url": "https://example.com/sitemap.xml",
            },
        ),
    ),
    "ibkr": (
        ("get_accounts", {}),
        ("get_positions", {}),
        ("get_account_summary", {}),
        ("get_trades", {"days": "1"}),
    ),
    "instagram": (
        ("get_profile", {}),
        ("get_recent_media", {"limit": "1"}),
        ("get_publishing_limit", {}),
        ("post_reel", {"video_asset_id": "$INSTAGRAM_VIDEO"}),
    ),
    "instagram_discovery": (
        ("search_reels", {"query": "Kern", "limit": "1"}),
        ("get_trending_reels", {"limit": "1"}),
        ("search_hashtag", {"hashtag": "ai", "limit": "1"}),
        ("get_reels_by_audio", {"audio_id": "1", "limit": "1"}),
        ("get_reel_details", {"url": "https://www.instagram.com/reel/ABC123/"}),
    ),
    "linkedin": (
        ("get_profile", {}),
        ("create_post", {"text": "Kern smoke; never published."}),
    ),
    "linkedin_discovery": (
        ("search_posts", {"query": "Kern", "limit": "1"}),
    ),
    # The remaining Polymarket actions need a live market/token from this
    # run's listing. check_tools_surface derives those after these three calls.
    "polymarket": (
        ("list_markets", {"limit": "10"}),
        ("list_events", {"limit": "1"}),
        ("search", {"query": "bitcoin", "limit_per_type": "1"}),
    ),
    "reddit": (
        ("get_profile", {}),
        ("get_home_feed", {"limit": "1"}),
        ("get_subreddit_posts", {"subreddit": "popular", "limit": "1"}),
        ("search_posts", {"query": "Kern", "limit": "1"}),
        ("read_post", {"post_id": "abc", "comment_limit": "1"}),
        (
            "create_post",
            {"subreddit": "popular", "title": "Kern smoke", "kind": "self", "text": "Never published."},
        ),
        ("create_comment", {"parent_id": "t3_abc", "text": "Never published."}),
    ),
    "openai_images": (
        ("generate_image", {"prompt": "Kern smoke"}),
    ),
    "runway": (
        ("generate_video", {"prompt": "Kern smoke", "image_asset_id": "$RUNWAY_IMAGE"}),
        ("edit_video", {"prompt": "Kern smoke", "video_asset_id": "$RUNWAY_VIDEO"}),
        ("generate_image", {"prompt": "Kern smoke"}),
        ("generate_speech", {"text": "Kern smoke"}),
        ("get_task", {"task_id": "kern-smoke-missing"}),
        ("save_video", {"task_id": "kern-smoke-missing"}),
    ),
    "seedance": (
        ("generate_video", {"prompt": "Kern smoke"}),
        ("get_task", {"task_id": "kern-smoke-missing"}),
        ("save_video", {"task_id": "kern-smoke-missing"}),
    ),
    "twitter": (
        ("search_tweets", {"query": "Kern", "max_results": "10"}),
        ("read_tweet", {"tweet_id": "1"}),
        ("user_tweets", {"username": "kern", "max_results": "5"}),
        ("get_trends", {"max_trends": "1"}),
        ("get_personalized_trends", {}),
        ("lookup_user", {"username": "kern"}),
        ("post_tweet", {"text": "Kern smoke. Never published."}),
    ),
    "twitterapi_io": (
        ("search_tweets", {"query": "Kern", "query_type": "Latest"}),
    ),
    "zoho_mail": (
        ("search_messages", {"search_key": "entire:Kern", "limit": "1"}),
        ("list_folders", {}),
        ("list_senders", {}),
        ("list_messages", {"folder_id": "1", "limit": "1"}),
        ("read_message", {"folder_id": "1", "message_id": "1"}),
        ("create_folder", {"name": "Kern Smoke"}),
        (
            "move_messages",
            {"message_ids": ["1"], "destination_folder_id": "2"},
        ),
        ("archive_messages", {"message_ids": ["1"]}),
        (
            "send_email",
            {
                "to": "stage@example.com",
                "subject": "Kern smoke",
                "blocks": [{"type": "paragraph", "text": "Smoke test; never sent."}],
            },
        ),
    ),
}


def managed_integrations(providers: dict[str, bool]) -> dict:
    return {provider: {"enabled": True} for provider, enabled in providers.items() if enabled}


def network_policy(providers: dict[str, bool], custom_domains: dict | None = None) -> dict:
    integrations = managed_integrations(providers)
    if custom_domains:
        integrations["custom"] = {"domains": custom_domains}
    return {"network_integrations": integrations}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.parse_args(argv)

    for tool in ("aws", "ssh", "ssh-keygen"):
        if shutil.which(tool) is None:
            print(f"error: {tool!r} is required on PATH", file=sys.stderr)
            return 2

    smoke = AwsSmoke()
    try:
        smoke.prepare()
        smoke.deploy()
        smoke.open_tunnel()
        smoke.check_credential_free_host_surface()
        print(f"\n{smoke.passed}/{smoke.total} checks passed")
        return 0 if smoke.passed == smoke.total else 1
    except Exception as exc:  # noqa: BLE001 - report, then always tear down in finally
        smoke.print_network_events("Network events before failure", since=0)
        print(f"\n[FAIL] {exc}", file=sys.stderr)
        return 1
    finally:
        smoke.teardown()


class AwsSmoke:
    # Host-side thread ids are the API thread ids verbatim; the stage harness
    # sets a per-run prefix that api_thread_id applies to every thread route.
    thread_prefix = "thread-"

    def __init__(self, workdir: Path | None = None) -> None:
        self.agent_runtime = "codex"
        self.workdir = workdir or Path(tempfile.mkdtemp(prefix="smoke-aws-"))
        self.control_socket = self.workdir / "ssh-control"
        self.ssh_key: str | None = None
        self.public_key: str | None = None
        self.region = SMOKE_REGION
        self.result: dict | None = None
        self.tunnel_open = False
        self.passed = 0
        self.total = 0
        self.parallel_threads: dict[str, tuple[str, str]] = {}  # runtime -> (thread id, token)

    @property
    def managed_domains(self) -> tuple[str, ...]:
        return SMOKE_MANAGED_DOMAINS

    @property
    def expected_agent_name(self) -> str:
        """Agent identity expected in the installed host configuration.

        Provider-neutral live-host checks are also exercised by the Lima
        smoke, whose per-run identity is intentionally unique.
        """
        return SMOKE_AGENT_NAME

    def api_thread_id(self, thread_id: str) -> str:
        """The host-side thread id for a harness thread name."""
        return self.thread_prefix + thread_id

    @staticmethod
    def thread_id_component(value: str) -> str:
        """A runtime or model name safe to embed in a product thread id."""
        return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")

    def message_body(
        self,
        message: str,
        *,
        runtime: str | None = None,
        model: str | None = None,
        effort: str | None = None,
    ) -> dict:
        """A first-message body: the full session config a new thread requires."""
        selected_runtime = runtime or self.agent_runtime
        selected_model = model or SMOKE_RUNTIME_MODELS[selected_runtime]
        return {
            "message": message,
            "agent_runtime": selected_runtime,
            "model": selected_model,
            "effort": effort or "high",
        }

    def follow_up_body(self, message: str) -> dict:
        """A config-less body: an existing thread derives its stored config."""
        return {"message": message}

    def send_message(
        self,
        thread_id: str,
        message: str,
        *,
        runtime: str | None = None,
        model: str | None = None,
        effort: str | None = None,
    ) -> dict:
        """POST a thread's first message (full session config) and return the
        response ({"status": "accepted", "thread": {...}})."""
        return self._post_message(
            thread_id, self.message_body(message, runtime=runtime, model=model, effort=effort)
        )

    def send_follow_up(self, thread_id: str, message: str) -> dict:
        """POST a config-less message: steers the running turn or starts a new
        turn on the thread's stored session config."""
        return self._post_message(thread_id, self.follow_up_body(message))

    def _post_message(self, thread_id: str, body: dict, *, timeout: float = 60) -> dict:
        """POST /messages, retrying the transient per-thread fence while a
        previous turn's process finishes closing (there is no queue, so the
        host answers 409 and callers retry)."""
        deadline = time.time() + timeout
        while True:
            try:
                return self._api(
                    "POST", f"/v1/threads/{self.api_thread_id(thread_id)}/messages", body
                )
            except AssertionError as exc:
                if THREAD_BUSY_MARKER in str(exc) and time.time() < deadline:
                    time.sleep(2)
                    continue
                raise

    def runtime_status_record(self, status_response: dict, runtime: str | None = None) -> dict:
        runtime = runtime or self.agent_runtime
        runtimes = status_response.get("runtimes")
        if not isinstance(runtimes, list):
            raise AssertionError(f"agent runtime status has wrong shape: {status_response}")
        for item in runtimes:
            if isinstance(item, dict) and item.get("type") == runtime:
                return item
        raise AssertionError(f"agent runtime {runtime} missing from status: {status_response}")

    def enforcement_policy(self) -> dict:
        """Self-contained policy pushed at runtime, independent of deploy config.

        The /zen guard backs the percent-encoded-path check; the wildcard rule
        backs the wildcard domain check. All three provider integrations stay
        on so every runtime can be active. The Bedrock connection makes Hermes
        available.
        The GitHub integration backs the repo-scope enforcement checks.
        """
        policy = network_policy(
            SMOKE_MANAGED_PROVIDERS,
            {
                "example.com": {"allow_http_methods": ["GET"], "path_guards": ["^/$", "^/zen$"]},
                "*.example.com": {"allow_http_methods": ["GET"]},
            },
        )
        policy["network_integrations"]["github"] = json.loads(json.dumps(SMOKE_GITHUB_INTEGRATION))
        # The package integrations back check_package_client_headers, which
        # drives the real pip and npm clients against the header allowlists.
        policy["network_integrations"]["python_packages"] = {"enabled": True}
        policy["network_integrations"]["npm_packages"] = {"enabled": True}
        return policy

    def check_credential_free_host_surface(self) -> None:
        """Exercise the live host without external account credentials.

        The Lima smoke reuses this provider-neutral contract after booting its
        own guest, so additions here cover both real deployment providers.
        """
        checks = (
            self.check_health,
            self.check_host_config_schema,
            self.check_agent_home_guidance,
            self.check_ui_page,
            self.check_admin_auth,
            self.check_workspace_backends_without_providers,
            self.check_embedding_index_resource_load,
            self.check_initial_disabled_provider_deploy,
            self.check_network_policy,
            self.check_policy_validation_and_concurrency,
            self.check_turn_admission_contract,
            self.check_admin_concurrency,
            self.check_state_transactions,
            self.check_event_pagination,
            self.check_enforcement,
            self.check_github_read_paths,
            self.check_package_client_headers,
            self.check_proxy_edge_cases,
            self.check_proxy_concurrency,
            self.check_pre_login_provider_guards,
            self.check_precredential_bedrock_harness_launchers,
            self.check_installed_agent_script_launcher,
            self.check_tools_surface,
            self.check_network_event_prune_race,
        )
        for check in checks:
            check()

    # --- lifecycle ---------------------------------------------------------

    def prepare(self) -> None:
        """Generate an ephemeral operator SSH key and pin the region to the
        IAM policy's, so the caller provides neither arguments nor a key."""
        self.ssh_key = str(self.workdir / "operator_key")
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-N", "", "-q", "-C", "kern-smoke", "-f", self.ssh_key],
            check=True,
        )
        self.public_key = Path(f"{self.ssh_key}.pub").read_text().strip()
        self.region = SMOKE_REGION

    def deploy(self) -> None:
        self._step("deploy host")
        self._destroy_tagged_smoke_resources()
        env = os.environ.copy()
        env["PYTHONPATH"] = str(REPO_ROOT)
        env["AWS_REGION"] = self.region
        # The CLI takes only the password hash; the harness generates the
        # password and keeps the cleartext for its own admin API checks. The
        # result JSON is the deploy's stdout.
        admin_password = py_secrets.token_urlsafe(32)
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "host.cli.deploy",
                "--agent-name",
                SMOKE_AGENT_NAME,
                "--operator-ssh-public-key",
                self.public_key or "",
                "--admin-password-sha256",
                hashlib.sha256(admin_password.encode()).hexdigest(),
            ],
            cwd=self.workdir,
            env=env,
            check=True,
            stdout=subprocess.PIPE,
            text=True,
            timeout=DEPLOY_TIMEOUT,
        )
        self.result = json.loads(proc.stdout)
        self.result["admin_password"] = admin_password
        self._ok(f"instance {self.result['instance_id']} at {self.result['public_dns']}")

    def open_tunnel(self) -> None:
        self._step("open SSH tunnel to the admin API")
        self._start_tunnel()
        self._ok("tunnel established")

    def _start_tunnel(self) -> None:
        target = f"kern-operator@{self.result['public_dns']}"
        subprocess.run(
            [
                "ssh", "-fN", "-M", "-S", str(self.control_socket),
                "-i", self.ssh_key,
                "-o", "ExitOnForwardFailure=yes",
                "-o", "StrictHostKeyChecking=accept-new",
                "-o", f"UserKnownHostsFile={self.workdir / 'known_hosts'}",
                "-o", "ConnectTimeout=15",
                # Keep the tunnel alive across the interactive login wait — an
                # idle NAT/firewall would otherwise drop it and the next request
                # would see connection-refused on the local forward.
                "-o", "ServerAliveInterval=15",
                "-o", "ServerAliveCountMax=8",
                "-o", "TCPKeepAlive=yes",
                "-L", f"{ADMIN_PORT}:127.0.0.1:{ADMIN_PORT}",
                target,
            ],
            check=True,
        )
        self.tunnel_open = True

    def _reopen_tunnel(self) -> None:
        """Tear down a dead control master and start a fresh tunnel."""
        subprocess.run(
            ["ssh", "-S", str(self.control_socket), "-O", "exit",
             f"kern-operator@{self.result['public_dns']}"],
            capture_output=True,
        )
        self.tunnel_open = False
        self._start_tunnel()

    def teardown(self) -> None:
        if self.tunnel_open and self.result:
            subprocess.run(
                ["ssh", "-S", str(self.control_socket), "-O", "exit", f"kern-operator@{self.result['public_dns']}"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
            )
        try:
            self._destroy_tagged_smoke_resources()
        finally:
            shutil.rmtree(self.workdir, ignore_errors=True)

    def _destroy_tagged_smoke_resources(self) -> None:
        print("\nTearing down tagged smoke AWS resources...")
        instance_ids = self._tagged_instance_ids("pending,running,stopping,stopped")
        if instance_ids:
            print(f"  terminating instances: {', '.join(instance_ids)}", flush=True)
            self._aws("ec2", "terminate-instances", "--instance-ids", *instance_ids)
            self._aws("ec2", "wait", "instance-terminated", "--instance-ids", *instance_ids)
        shutting_down_ids = self._tagged_instance_ids("shutting-down")
        if shutting_down_ids:
            print(f"  waiting for already-shutting-down instances: {', '.join(shutting_down_ids)}", flush=True)
            self._aws("ec2", "wait", "instance-terminated", "--instance-ids", *shutting_down_ids)

        volume_ids = self._tagged_volume_ids()
        deleted_volumes: list[str] = []
        for volume_id in volume_ids:
            try:
                self._aws("ec2", "wait", "volume-available", "--volume-ids", volume_id)
                self._aws("ec2", "delete-volume", "--volume-id", volume_id)
                self._aws("ec2", "wait", "volume-deleted", "--volume-ids", volume_id)
                deleted_volumes.append(volume_id)
            except subprocess.CalledProcessError as exc:
                print(f"warning: could not delete volume {volume_id}: {exc}", file=sys.stderr)

        group_ids = self._tagged_security_group_ids()
        deleted_groups: list[str] = []
        for group_id in group_ids:
            try:
                self._aws("ec2", "delete-security-group", "--group-id", group_id)
                deleted_groups.append(group_id)
            except subprocess.CalledProcessError as exc:
                print(f"warning: could not delete security group {group_id}: {exc}", file=sys.stderr)

        remaining = {
            "instances": self._tagged_instance_ids(),
            "volumes": self._tagged_volume_ids(),
            "security_groups": self._tagged_security_group_ids(),
        }
        if any(remaining.values()):
            raise AssertionError(f"tagged smoke AWS resources remain after teardown: {remaining}")
        print(
            "  destroyed tagged smoke resources"
            f" (instances={len(instance_ids)}, volumes={len(deleted_volumes)}, security_groups={len(deleted_groups)})",
            flush=True,
        )

    def _smoke_tag_filters(self) -> list[str]:
        return [
            f"Name=tag:kern-host-agent-name,Values={SMOKE_AGENT_NAME}",
            "Name=tag:kern-host,Values=true",
        ]

    def _tagged_instance_ids(
        self,
        states: str = "pending,running,stopping,stopped,shutting-down",
    ) -> list[str]:
        response = self._aws(
            "ec2",
            "describe-instances",
            "--filters",
            *self._smoke_tag_filters(),
            f"Name=instance-state-name,Values={states}",
        )
        ids: list[str] = []
        for reservation in response.get("Reservations", []):
            for instance in reservation.get("Instances", []):
                instance_id = instance.get("InstanceId")
                if isinstance(instance_id, str):
                    ids.append(instance_id)
        return sorted(ids)

    def _tagged_volume_ids(self) -> list[str]:
        response = self._aws("ec2", "describe-volumes", "--filters", *self._smoke_tag_filters())
        ids: list[str] = []
        for volume in response.get("Volumes", []):
            if volume.get("State") in {"deleted", "deleting"}:
                continue
            volume_id = volume.get("VolumeId")
            if isinstance(volume_id, str):
                ids.append(volume_id)
        return sorted(ids)

    def _tagged_security_group_ids(self) -> list[str]:
        response = self._aws("ec2", "describe-security-groups", "--filters", *self._smoke_tag_filters())
        ids: list[str] = []
        for group in response.get("SecurityGroups", []):
            group_id = group.get("GroupId")
            if isinstance(group_id, str):
                ids.append(group_id)
        return sorted(ids)

    # --- checks ------------------------------------------------------------

    def check_health(self) -> None:
        self._step("admin API health (waiting for bootstrap)")
        start = time.time()
        deadline = start + HEALTH_TIMEOUT
        last: dict | None = None
        last_error: str | None = None
        while time.time() < deadline:
            try:
                last = self._api("GET", "/v1/health")
                last_error = None
                if last["network_controls"]["status"] == "active":
                    break
            except Exception as exc:  # noqa: BLE001 - surface the failure mode below
                last = None
                last_error = f"{type(exc).__name__}: {exc}"
            elapsed = int(time.time() - start)
            if elapsed % 30 < 10:
                detail = last_error or (last and f"network_controls={last['network_controls']['status']}")
                print(f"[health] waiting ({elapsed}s): {detail}", flush=True)
            time.sleep(10)
        if not last or last["network_controls"]["status"] != "active":
            raise AssertionError(
                f"network controls never became active (last health: {last}, last error: {last_error})"
            )
        for key in ("cpu", "memory", "filesystem", "swap"):
            if key not in last["host_runtime"]:
                raise AssertionError(f"health missing host_runtime.{key}")
        mounts = last["host_runtime"]["filesystem"].get("mounts", {})
        for mount_name in ("root", "admin", "agent"):
            mount = mounts.get(mount_name)
            if not isinstance(mount, dict) or mount.get("total_bytes", 0) <= 0:
                raise AssertionError(f"health missing filesystem mount {mount_name}: {last['host_runtime']['filesystem']}")
        runtime_states = {
            runtime: self.runtime_status_record(last["agent_runtime"], runtime)["status"]
            for runtime in SMOKE_RUNTIMES
        }
        for runtime in SMOKE_RUNTIMES:
            record = self.runtime_status_record(last["agent_runtime"], runtime)
            if not isinstance(record.get("active_thread_ids"), list) or "active_task_ids" in record:
                raise AssertionError(f"health runtime record should carry active_thread_ids only: {record}")
        self._ok(
            "healthy; "
            + ", ".join(f"{runtime} runtime {status}" for runtime, status in runtime_states.items())
            + ", swap and all storage mounts reported"
        )

    def check_host_config_schema(self) -> None:
        self._step("deployed host config schema")
        config = json.loads(self._ssh_code(
            "sudo -u postgres psql -tA -d kern_admin -c \""
            "SELECT json_build_object("
            "'agent_name', c.agent_name,"
            " 'admin_password_sha256', c.admin_password_sha256,"
            " 'operator_connections', COALESCE((SELECT json_agg(json_strip_nulls(json_build_object("
            "'mode', o.mode, 'ssh_public_key', o.ssh_public_key,"
            " 'hostname', o.hostname, 'tunnel_token', o.tunnel_token)) ORDER BY o.mode)"
            " FROM operator_connections o), '[]'::json))::text FROM config c\""
        ))
        if config.get("agent_name") != self.expected_agent_name:
            raise AssertionError(f"host config has wrong agent_name: {config}")
        if not config.get("admin_password_sha256"):
            raise AssertionError(f"host config missing password hash: {config}")
        if self.ssh_key is None:
            raise AssertionError("smoke SSH key was not prepared")
        expected_connections = [{"mode": "ssh", "ssh_public_key": Path(f"{self.ssh_key}.pub").read_text().strip()}]
        if config.get("operator_connections") != expected_connections:
            raise AssertionError(f"host config has wrong operator connections: {config}")
        # The config schema is typed columns now; deployment-only inputs
        # cannot exist as stray keys, pinned by the exact column set.
        config_columns = set(self._ssh_code(
            "sudo -u postgres psql -tA -d kern_admin -c "
            "\"SELECT column_name FROM information_schema.columns WHERE table_name = 'config'\""
        ).split())
        if config_columns != {"singleton", "agent_name", "admin_password_sha256"}:
            raise AssertionError(f"config table has unexpected columns: {sorted(config_columns)}")
        # Proxy account pins live in the proxy_provider_pins table. Clearing a
        # pin upserts the row with a null account_id rather than deleting it,
        # so a fresh host carries one row per provider whose runtime refresh
        # has run. The property to hold is that none of them names an account
        # before a login lands -- asserting which providers may have a row
        # instead just goes stale the next time one is added.
        pinned = self._ssh_code(
            "sudo -u postgres psql -tA -d kern_admin "
            "-c \"SELECT provider FROM proxy_provider_pins WHERE account_id IS NOT NULL\""
        ).strip()
        if pinned:
            raise AssertionError(f"proxy_provider_pins names an account before any login: {pinned}")
        # Admin-side provider account records live in the database, in the
        # provider_accounts table (empty or explicit-null records until login).
        admin_accounts = self._ssh_code(
            "sudo -u postgres psql -tA -d kern_admin "
            "-c \"SELECT provider FROM provider_accounts WHERE provider NOT IN ('openai', 'claude', 'bedrock')\""
        ).strip()
        if admin_accounts:
            raise AssertionError(f"unexpected provider_accounts rows: {admin_accounts}")
        storage_layout = json.loads(self._ssh_code(
            "sudo python3 - <<'PY'\n"
            "import grp, json, os, pwd\n"
            "paths = [\n"
            "    '/mnt/kern-admin',\n"
            "    '/mnt/kern-admin/postgres',\n"
            "    '/mnt/kern-admin/postgres/14/main',\n"
            "    '/mnt/kern-admin/proxy-state',\n"
            "    '/mnt/kern-admin/proxy-state/network_proxy_ca.key',\n"
            "    '/mnt/kern-admin/proxy-state/network_proxy_ca.crt',\n"
            "]\n"
            "result = {}\n"
            "for path in paths:\n"
            "    st = os.lstat(path)\n"
            "    result[path] = {'owner': pwd.getpwuid(st.st_uid).pw_name,\n"
            "                    'group': grp.getgrgid(st.st_gid).gr_name,\n"
            "                    'mode': oct(st.st_mode & 0o777),\n"
            "                    'symlink': os.path.islink(path)}\n"
            "print(json.dumps(result))\n"
            "PY"
        ))
        service_ids = json.loads(self._ssh_code(
            "sudo python3 - <<'PY'\n"
            "import grp, json, pwd\n"
            "names = ['kern-admin', 'kern-proxy', 'kern-agent', 'cloudflared', 'postgres']\n"
            "print(json.dumps({name: {'uid': pwd.getpwnam(name).pw_uid, 'gid': grp.getgrnam(name).gr_gid} for name in names}))\n"
            "PY"
        ))
        expected_service_ids = {
            "kern-admin": {"uid": 47741, "gid": 47741},
            "kern-proxy": {"uid": 47742, "gid": 47742},
            "kern-agent": {"uid": 47743, "gid": 47743},
            "cloudflared": {"uid": 47744, "gid": 47744},
            "postgres": {"uid": 47745, "gid": 47745},
        }
        if service_ids != expected_service_ids:
            raise AssertionError(f"service IDs are not stable: {service_ids}")
        agent_ca_access = self._ssh_code(
            "sudo -u kern-agent bash -c "
            "'test -r /usr/local/share/ca-certificates/kern-network-proxy.crt && "
            "! test -r /mnt/kern-admin/proxy-state/network_proxy_ca.crt && echo ok'"
        ).strip()
        if agent_ca_access != "ok":
            raise AssertionError("agent must read only the installed proxy CA copy, not proxy-state directly")
        partition_access = self._ssh_code(
            "sudo -u kern-admin bash -c '! test -r /mnt/kern-admin/proxy-state/network_proxy_ca.key' && "
            "sudo -u kern-proxy bash -c '! test -r /mnt/kern-admin/postgres/14/main/pg_hba.conf' && "
            "echo ok"
        ).strip()
        if partition_access != "ok":
            raise AssertionError("proxy-state and the Postgres data directory must be unreadable across service users")
        # The admin role has full access. The proxy role can read the shared
        # Bedrock row but cannot mutate it or create objects; the agent has no
        # database role at all.
        database_access = self._ssh_code(
            "sudo -u kern-admin psql -tA -d kern_admin -c 'SELECT 1' && "
            "sudo -u kern-proxy psql -tA -d kern_admin -c 'SELECT 2' && "
            "sudo -u kern-proxy psql -tA -d kern_admin -c 'SELECT count(*) FROM bedrock_credentials' && "
            "sudo -u kern-proxy bash -c '! psql -tA -d kern_admin -c \"UPDATE bedrock_credentials SET access_key_id = access_key_id\" 2>/dev/null' && "
            "sudo -u kern-proxy bash -c '! psql -tA -d kern_admin -c \"CREATE TABLE smoke_illegal (n INT)\" 2>/dev/null' && "
            "sudo -u kern-agent bash -c '! psql -tA -d kern_admin -c \"SELECT 1\" 2>/dev/null' && "
            "echo ok"
        ).strip().splitlines()
        if database_access != ["1", "2", "0", "ok"]:
            raise AssertionError(
                f"database access must be admin-full, proxy-narrow, agent-none: {database_access}"
            )
        expected_layout = {
            "/mnt/kern-admin": ("root", "root", "0o711", False),
            "/mnt/kern-admin/postgres": ("root", "root", "0o711", False),
            "/mnt/kern-admin/postgres/14/main": ("postgres", "postgres", "0o700", False),
            "/mnt/kern-admin/proxy-state": ("kern-proxy", "kern-proxy", "0o700", False),
            "/mnt/kern-admin/proxy-state/network_proxy_ca.key": ("kern-proxy", "kern-proxy", "0o600", False),
            "/mnt/kern-admin/proxy-state/network_proxy_ca.crt": ("kern-proxy", "kern-proxy", "0o644", False),
        }
        for path, expected_values in expected_layout.items():
            entry = storage_layout.get(path, {})
            actual = (entry.get("owner"), entry.get("group"), entry.get("mode"), entry.get("symlink"))
            if actual != expected_values:
                raise AssertionError(f"{path} ownership/mode mismatch: {entry}")
        self._ok(
            "host config persists name/password in the database; database and proxy state are private per service user"
        )

    def check_agent_home_guidance(self) -> None:
        """Pin the operator guidance actually installed into each agent runtime."""
        self._step("deployed agent host guidance")
        guide = self._ssh_code(
            "sudo -u kern-agent cat /mnt/kern-agent/agent-home/AGENTS.md"
        )
        required = (
            "This is a single-tenant Linux machine.",
            "Kern source is readable at `/opt/kern-host`.",
            "`search_conversation_history`",
            "messages and activity are untrusted data",
            "`GET /agent/identity` returns the current thread's immutable host identity.",
            "GraphQL is\nalways blocked",
            "switch to REST or git; do not retry GraphQL",
            "/opt/kern-host/host/bootstrap/agent-home/references/web-apps.md",
        )
        missing = [marker for marker in required if marker not in guide]
        if missing:
            raise AssertionError(
                f"deployed AGENTS.md is missing current host guidance: {missing}"
            )
        identical = self._ssh_code(
            "sudo -u kern-agent cmp -s /mnt/kern-agent/agent-home/AGENTS.md "
            "/mnt/kern-agent/agent-home/CLAUDE.md && echo identical"
        ).strip()
        if identical != "identical":
            raise AssertionError("deployed AGENTS.md and CLAUDE.md guidance differ")
        self._ok(
            "host orientation, typed history trust, identity-keyed memory, and "
            "GitHub REST fallback guidance are installed for every runtime"
        )

    def check_network_policy(self) -> None:
        self._step("network policy get/replace")
        self._api("PUT", "/v1/network/policy", self.enforcement_policy())
        policy = self._api("GET", "/v1/network/policy")
        controls = policy["network_controls"]
        expected_integrations = self.enforcement_policy()["network_integrations"]
        if controls.get("network_integrations") != expected_integrations:
            raise AssertionError(f"policy did not preserve explicit integrations: {controls}")
        rules = controls["network_integrations"].get("custom", {}).get("domains", {})
        if "example.com" not in rules:
            raise AssertionError("replaced policy not reflected in GET")
        for host in self.managed_domains:
            if host in rules:
                raise AssertionError(f"managed provider rule {host} leaked into API policy response: {rules}")
        # The stored policy is typed rows now; check them directly.
        stored_integrations = set(self._ssh_code(
            "sudo -u postgres psql -tA -d kern_admin -c 'SELECT integration FROM managed_integrations'"
        ).split())
        expected_enabled = {
            name for name, config in expected_integrations.items()
            if name != "custom" and config.get("enabled") is True
        }
        if stored_integrations != expected_enabled:
            raise AssertionError(
                f"stored policy did not preserve enabled integrations: {sorted(stored_integrations)}"
            )
        stored_repos = set(self._ssh_code(
            "sudo -u postgres psql -tA -d kern_admin -c "
            "\"SELECT owner || '/' || repo FROM github_repositories\""
        ).split())
        expected_repos = {
            f"{repo['owner']}/{repo['repo']}" for repo in SMOKE_GITHUB_INTEGRATION["write_repositories"]
        }
        if stored_repos != expected_repos:
            raise AssertionError(f"stored GitHub write-repository list mismatch: {sorted(stored_repos)}")
        stored_domains = set(self._ssh_code(
            "sudo -u postgres psql -tA -d kern_admin -c 'SELECT domain FROM allowed_domains'"
        ).split())
        if "example.com" not in stored_domains:
            raise AssertionError(f"replaced policy not reflected in stored rows: {sorted(stored_domains)}")
        for host in self.managed_domains:
            if host in stored_domains:
                raise AssertionError(f"managed integration rule {host} leaked into stored policy: {sorted(stored_domains)}")
        self._check_github_credential_lifecycle()
        self._ok("policy read back and stored user-facing; proxy dispatches typed integration config directly")

    def _check_github_credential_lifecycle(self) -> None:
        smoke_token = f"github_pat_smoke_{time.time_ns()}"
        metadata = self._api("GET", "/v1/network-tools/github-credential")
        if metadata.get("configured") is not False:
            raise AssertionError(f"initial GitHub credential should be unconfigured: {metadata}")
        saved = self._api("PUT", "/v1/network-tools/github-credential", {"mode": "pat", "token": smoke_token})
        if saved.get("configured") is not True or smoke_token in json.dumps(saved):
            raise AssertionError(f"GitHub credential PUT should return metadata only: {saved}")
        # The working token lives in the proxy-readable row as secretbox
        # ciphertext — encrypted at rest like every other stored secret, and
        # never on disk anywhere.
        published = self._ssh_code(
            "sudo -u postgres psql -tA -d kern_admin -c 'SELECT token FROM proxy_github_token'"
        ).strip()
        if not published.startswith("enc:v1:") or smoke_token in published:
            raise AssertionError("the published proxy_github_token row must hold ciphertext, not the token")
        no_file = self._ssh_code("sudo bash -c '! test -e /etc/kern-github && echo absent'").strip()
        if no_file != "absent":
            raise AssertionError("the agent token-file directory should not exist in the injection era")
        # The proxy injects the stored (fake) PAT into agent GitHub requests:
        # gh runs with a fixed placeholder GH_TOKEN, the proxy strips it and
        # injects the smoke token, and GitHub answers 401 for the bad
        # credential — proof the injected token (not the placeholder) reached
        # GitHub. gh's own "not logged in" refusal would mean a broken shim.
        proxy = f"http://127.0.0.1:{PROXY_PORT}"
        gh_injected = self._ssh_code(
            f"sudo -u kern-agent env HTTPS_PROXY={proxy} https_proxy={proxy} "
            "gh api repos/infiloop2/kern 2>&1 || true"
        )
        if "401" not in gh_injected or "gh auth login" in gh_injected:
            raise AssertionError(f"proxy did not inject the stored token upstream: {gh_injected!r}")
        # GraphQL is denied at the proxy regardless of credentials, so gh's
        # GraphQL-backed path fails with the proxy's 403, not a GitHub 401.
        gh_graphql = self._ssh_code(
            f"sudo -u kern-agent env HTTPS_PROXY={proxy} https_proxy={proxy} "
            "gh api graphql -f query='query{viewer{login}}' 2>&1 || true"
        )
        if "403" not in gh_graphql:
            raise AssertionError(f"gh graphql should be denied by the proxy: {gh_graphql!r}")
        deleted = self._api("DELETE", "/v1/network-tools/github-credential")
        if deleted.get("configured") is not False:
            raise AssertionError(f"GitHub credential DELETE should clear metadata: {deleted}")
        rows = self._ssh_code(
            "sudo -u postgres psql -tA -d kern_admin -c 'SELECT count(*) FROM proxy_github_token'"
        ).strip()
        if rows != "0":
            raise AssertionError("proxy_github_token should be cleared after DELETE")
        # With the row cleared, the same request goes upstream with the
        # placeholder stripped and nothing injected: the public repo read
        # succeeds unauthenticated — revocation is instant, and agent-supplied
        # credentials demonstrably never pass through.
        gh_uninjected = self._ssh_code(
            f"sudo -u kern-agent env HTTPS_PROXY={proxy} https_proxy={proxy} "
            "gh api repos/infiloop2/kern 2>&1 || true"
        )
        if '"full_name"' not in gh_uninjected or "401" in gh_uninjected:
            raise AssertionError(f"unauthenticated public read should succeed after DELETE: {gh_uninjected!r}")

    def check_initial_disabled_provider_deploy(self) -> None:
        self._step("initial deploy with managed providers disabled")
        expected_empty_policy = {"network_integrations": {}}
        stored_rows = self._ssh_code(
            "sudo -u postgres psql -tA -d kern_admin -c 'SELECT count(*) FROM network_policy'"
        ).strip()
        if stored_rows != "0":
            raise AssertionError(
                f"a fresh deploy must not seed a policy row (missing row = fail-closed empty default): {stored_rows}"
            )
        policy = self._api("GET", "/v1/network/policy")["network_controls"]
        if policy != expected_empty_policy:
            raise AssertionError(f"initial API policy should be empty: {policy}")
        health = self._api("GET", "/v1/health")
        if health["network_controls"]["status"] != "active":
            raise AssertionError(f"empty valid policy should report derived active network status: {health}")
        if policy.get("network_integrations", {}) != {}:
            raise AssertionError(f"initial policy should be empty: {policy}")
        bedrock_rows = self._ssh_code(
            "sudo -u postgres psql -tA -d kern_admin -c "
            "'SELECT count(*) FROM bedrock_credentials'"
        ).strip()
        if bedrock_rows != "0":
            raise AssertionError(
                f"fresh host seeded a Bedrock credential: {bedrock_rows}"
            )
        for runtime in SMOKE_RUNTIMES:
            status = self._wait_for_runtime_status({"deactivated"}, runtime=runtime, timeout=90)
            if status != "deactivated":
                raise AssertionError(f"{runtime} should start deactivated when its provider is disabled, got {status}")
            account = self._agent_account(runtime)
            if account.get("status") != "deactivated":
                raise AssertionError(f"{runtime} account summary should report deactivated: {account}")
            if "account_id" in account or "email" in account:
                raise AssertionError(f"{runtime} account summary leaked account identity while deactivated: {account}")
        for label, login_path in (
            ("initial-codex-disabled-login", "/v1/agent-runtime/codex-oauth-login"),
            ("initial-claude-disabled-login", "/v1/agent-runtime/claude-oauth-login"),
        ):
            status, body = self._api_status("POST", login_path)
            if status != 409:
                raise AssertionError(f"{login_path} while initially deactivated returned {status}, expected 409: {body}")
        self._api("PUT", "/v1/network/policy", self.enforcement_policy())
        for runtime in SMOKE_RUNTIMES:
            status = self._wait_for_runtime_status({"awaiting_login"}, runtime=runtime, timeout=120)
            if status != "awaiting_login":
                raise AssertionError(
                    f"{runtime} should await its connection after enabling provider access, got {status}"
                )
        self._ok("first boot creates an empty derived-active policy; providers omitted keep runtimes deactivated")

    def check_ui_page(self) -> None:
        self._step("admin UI page served at / without auth")
        request = urllib.request.Request(f"http://127.0.0.1:{ADMIN_PORT}/")
        with urllib.request.urlopen(request, timeout=30) as response:
            content_type = response.headers.get("Content-Type", "")
            self._assert_admin_ui_security_headers(response.headers)
            page = response.read().decode()
        if "text/html" not in content_type:
            raise AssertionError(f"UI page content type is {content_type!r}")
        if "Kern" not in page:
            raise AssertionError("UI page does not look like the admin UI")
        for path, expected_type in (
            ("/admin_ui.css", "text/css"),
            ("/admin_ui/app.js", "application/javascript"),
            ("/admin_ui/api.js", "application/javascript"),
            ("/admin_ui/helpers.js", "application/javascript"),
            ("/admin_ui/health.js", "application/javascript"),
            ("/admin_ui/files.js", "application/javascript"),
            ("/admin_ui/processes.js", "application/javascript"),
            ("/admin_ui/logs.js", "application/javascript"),
            ("/admin_ui/network.js", "application/javascript"),
            ("/admin_ui/tools.js", "application/javascript"),
            ("/admin_ui/integration_catalog.js", "application/javascript"),
            ("/admin_ui/connection_guide.js", "application/javascript"),
        ):
            request = urllib.request.Request(f"http://127.0.0.1:{ADMIN_PORT}{path}")
            with urllib.request.urlopen(request, timeout=30) as response:
                asset_type = response.headers.get("Content-Type", "")
                self._assert_admin_ui_security_headers(response.headers)
                asset_body = response.read().decode()
            if expected_type not in asset_type:
                raise AssertionError(f"UI asset {path} content type is {asset_type!r}")
            page += "\n" + asset_body
        for path in (
            "/guide-assets/google-auth-app-information.png",
            "/guide-assets/google-auth-data-access.png",
            "/guide-assets/google-auth-web-client.png",
        ):
            request = urllib.request.Request(f"http://127.0.0.1:{ADMIN_PORT}{path}")
            with urllib.request.urlopen(request, timeout=30) as response:
                asset_type = response.headers.get("Content-Type", "")
                self._assert_admin_ui_security_headers(response.headers)
                asset_body = response.read()
            if "image/png" not in asset_type or not asset_body.startswith(b"\x89PNG\r\n\x1a\n"):
                raise AssertionError(f"UI guide asset {path} is not a PNG ({asset_type!r})")
        for expected in (
            '<link rel="stylesheet" href="/admin_ui.css">',
            '<script type="module" src="/admin_ui/app.js"></script>',
            "button[data-action]",
            "active_thread_ids",
            'id="panel-home"',
            'id="panel-processes"',
            'id="panel-network"',
            'id="processes"',
            "/v1/agent-processes",
            "refreshAgentProcesses",
            'id="runtime-overview"',
            'data-integration-message="custom_domain"',
            'data-action="start-login"',
            'Start ${esc(runtimeLabel)} login',
            'data-action="reset-linked-account"',
            'Disconnect</button>',
            "usageRing",
            'id="ai-inference-integrations"',
            'id="tools"',
            "AI Inference",
            "Manual",
            'id="github-repos"',
            'id="domain-rules"',
            'id="github-repo"',
            'id="github-token"',
            'id="github-credential-mode"',
            'id="github-app-fields"',
            'id="github-app-id"',
            'id="github-app-installation-id"',
            'id="github-app-private-key"',
            'id="github-credential-status"',
            'id="home-integration-groups"',
            'data-action="open-home-integration"',
            'data-action="open-home-view"',
            'data-action="home-back"',
            "Reboot host",
            "Custom Domain Access",
            "Add domain rule",
            "MANAGED_INTEGRATIONS",
            "integration_catalog.js",
            "objectValue",
            "!Array.isArray(value)",
            "activeNetworkPolicy",
            "clonePolicy",
            "publishPolicy",
            "setIntegrationEnabled",
            "renderNetworkControls",
            "renderManagedIntegrations",
            "renderGithubRepos",
            "renderDomainRules",
            "addDomainRule",
            "removeDomainRule",
            'data-action="enable-integration"',
            'data-action="disable-integration"',
            'data-action="remove-github-repo"',
            'data-action="remove-domain-rule"',
            'data-action="add-github-repo"',
            'data-action="set-github-credential"',
            'data-action="delete-github-credential"',
            'data-action="add-domain-rule"',
            'id="github-expansion"',
            'id="github-credential-clear"',
            'data-action="recheck-github-audit"',
            "renderGithubAudit",
            "recheckGithubAudit",
            "audit-banner",
            "/v1/network-tools/github-audit",
            "Connect your OpenAI subscription and let your agent use Codex for tasks and cached web search.",
            "Connect your Anthropic subscription and let your agent use Claude Code for tasks. Web search is optional and off by default.",
            "OpenAI's cached web search",
            "Reads can reach any public repository",
            "api.openai.com",
            "auth.openai.com",
            "chatgpt.com",
            "api.anthropic.com",
            "platform.claude.com",
            "api.github.com",
            "uploads.github.com",
            "codeload.github.com",
            "objects.githubusercontent.com",
            "github-cloud.githubusercontent.com",
            "raw.githubusercontent.com",
            "release-assets.githubusercontent.com",
            "pypi.org",
            "files.pythonhosted.org",
            "nodejs.org",
            "registry.npmjs.org",
            "Add domain rule",
            'id="tools"',
            'id="integration-detail-title"',
            "selectToolDetail",
            "selectIntegrationDetail",
            "tool-approvals-table",
            "refreshTools",
            "refreshExpandedToolApprovals",
            "decideToolApproval",
            'data-action="enable-tool"',
            'data-action="save-tool-config"',
            "connectTool",
            "completeToolConnect",
            "/oauth/callback",
            "/v1/tools",
            'id="panel-tool-log"',
            'id="tool-events"',
            "Tool audit log",
            "/v1/tools/events",
            "tool-page",
            'id="connection-guide-content"',
            "refreshConnectionGuide",
            "Integration guide",
            "setup_steps",
        ):
            if expected not in page:
                raise AssertionError(f"UI page is missing expected admin UI fragment {expected!r}")
        for removed in (
            "onclick=",
            "oninput=",
            "/v1/tasks",
            "task_id",
            "active_task_ids",
            "showTaskEvents",
            "loadFinishedTasks",
            "loadAllTaskEvents",
            "retained_task_count",
            "ssh_port_opened",
            "editWebsiteRule",
            "removeWebsiteRule",
            'id="policy-provider-openai"',
            'id="policy-provider-claude"',
            'id="policy-websites"',
            "Policy preset applied",
            "Network policy replaced",
        ):
            if removed in page:
                raise AssertionError(f"UI page still contains removed task-era fragment {removed!r}")
        self._ok("static UI page served unauthenticated; thread history and network policy controls present; API routes still require auth")

    @staticmethod
    def _assert_admin_ui_security_headers(headers) -> None:
        csp = headers.get("Content-Security-Policy", "")
        required_directives = (
            "default-src 'self'",
            "connect-src 'self'",
            "script-src 'self'",
            "style-src 'self'",
            "img-src 'self' data:",
            "frame-ancestors 'none'",
        )
        missing = [directive for directive in required_directives if directive not in csp]
        if missing:
            raise AssertionError(f"admin UI Content-Security-Policy missing {missing}: {csp!r}")
        expected_headers = {
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
        }
        for name, expected in expected_headers.items():
            actual = headers.get(name, "")
            if actual != expected:
                raise AssertionError(f"admin UI {name} header is {actual!r}, expected {expected!r}")

    def check_admin_auth(self) -> None:
        self._step("admin API authentication")
        status, _ = self._api_status("GET", "/v1/agent-runtime/status", cookie=None)
        if status != 401:
            raise AssertionError(f"request without credentials returned {status}, expected 401")
        status, _ = self._api_status("GET", "/v1/agent-runtime/status", cookie="wrong-session-token")
        if status != 401:
            raise AssertionError(f"request with a wrong session returned {status}, expected 401")
        # The UI page is the one unauthenticated route.
        request = urllib.request.Request(f"http://127.0.0.1:{ADMIN_PORT}/")
        with urllib.request.urlopen(request, timeout=30) as response:
            page = response.read()
        if b"<html" not in page.lower():
            raise AssertionError("GET / did not serve the admin UI page")
        auth = f"Cookie: tc_admin_session={self._admin_cookie()}\r\nX-Kern-Csrf: 1\r\n".encode()
        malformed = self._raw_local_http(
            ADMIN_PORT,
            b"POST /v1/threads/thread-smoke-auth/messages HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            + auth
            + b"Content-Length: nope\r\n\r\n",
        )
        huge = self._raw_local_http(
            ADMIN_PORT,
            b"POST /v1/threads/thread-smoke-auth/messages HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            + auth
            + b"Content-Length: 1048577\r\n\r\n",
        )
        if b"400" not in malformed or b"malformed Content-Length" not in malformed:
            raise AssertionError(f"admin API malformed Content-Length was not rejected cleanly: {malformed[:300]!r}")
        if b"413" not in huge or b"request body too large" not in huge:
            raise AssertionError(f"admin API huge Content-Length was not rejected cleanly: {huge[:300]!r}")
        self._ok("401 without/with wrong credentials; UI served unauthenticated; malformed admin bodies fail closed")

    def check_workspace_backends_without_providers(self) -> None:
        """Exercise every provider-free Workspace path on a fresh real host."""
        self._step("Workspace resources work before provider login")
        workspace_options: dict[str, dict] = {}
        for base in ("/v1/workspace/chat", "/v1/workspace/web-apps"):
            options = self._api(
                "GET", f"{base}/session-options"
            ).get("session_options")
            if not isinstance(options, dict) or set(options) != set(OFFERED_RUNTIMES):
                raise AssertionError(
                    f"{base} returned invalid session options: {options}"
                )
            workspace_options[base] = options

        agent_threads = self._api(
            "GET", "/v1/workspace/chat/threads"
        ).get("threads")
        archived_agent_threads = self._api(
            "GET", "/v1/workspace/chat/threads?archived=true"
        ).get("threads")
        if agent_threads != [] or archived_agent_threads != []:
            raise AssertionError(
                "fresh Agent Chat should have no current or archived threads: "
                f"{agent_threads}, {archived_agent_threads}"
            )

        base = "/v1/workspace/web-apps"
        created = self._api("POST", f"{base}/apps").get("app")
        if not isinstance(created, dict) or not isinstance(created.get("app_id"), str):
            raise AssertionError(f"App Builder returned invalid created app: {created}")
        app_id = created["app_id"]
        encoded_id = quote(app_id, safe="")
        state = self._api("GET", f"{base}/apps/{encoded_id}/state").get("app")
        if (
            not isinstance(state, dict)
            or state.get("revision") != 0
        ):
            raise AssertionError(f"new App Builder state is invalid: {state}")
        if any(state.get(field) for field in ("html", "css", "javascript")):
            raise AssertionError(f"new App Builder UI should be empty: {state}")
        if state.get("data") != {}:
            raise AssertionError(f"new App Builder data should be empty: {state}")

        conversation = self._api(
            "GET", f"{base}/apps/{encoded_id}/conversation"
        )
        if conversation != {"session": None, "status": "idle"}:
            raise AssertionError(
                f"new App Builder conversation is invalid: {conversation}"
            )
        events = self._api(
            "GET", f"{base}/apps/{encoded_id}/conversation/events"
        ).get("events")
        if events != []:
            raise AssertionError(
                f"new App Builder conversation should be empty: {events}"
            )

        renamed = self._api(
            "PUT",
            f"{base}/apps/{encoded_id}/name",
            {"name": "Provider-free smoke app"},
        ).get("app")
        if not isinstance(renamed, dict) or renamed.get("name") != "Provider-free smoke app":
            raise AssertionError(f"App Builder rename failed: {renamed}")

        listed = self._api("GET", f"{base}/apps").get("apps")
        if not any(
            isinstance(app, dict)
            and app.get("app_id") == app_id
            and app.get("name") == "Provider-free smoke app"
            and app.get("archived") is False
            for app in (listed or [])
        ):
            raise AssertionError(f"App Builder list omitted renamed app: {listed}")

        memory_base = "/v1/workspace/memory"
        if self._api("GET", memory_base).get("pages") != []:
            raise AssertionError("fresh Workspace memory should be empty")
        memory_page = self._api(
            "PUT",
            f"{memory_base}/pages/provider-free-smoke",
            {
                "description": "Provider-free smoke memory",
                "content": "Fresh Workspace memory persists without a model.",
                "expected_revision": 0,
            },
        ).get("page")
        if (
            not isinstance(memory_page, dict)
            or memory_page.get("page_id") != "provider-free-smoke"
            or memory_page.get("revision") != 1
        ):
            raise AssertionError(f"Workspace memory create returned invalid data: {memory_page}")
        memory_matches = self._api(
            "GET", f"{memory_base}/search?q={quote('provider free', safe='')}"
        ).get("pages")
        if not isinstance(memory_matches, list) or not any(
            isinstance(page, dict) and page.get("page_id") == "provider-free-smoke"
            for page in memory_matches
        ):
            raise AssertionError(f"Workspace memory search missed its saved page: {memory_matches}")
        deleted_memory = self._api(
            "DELETE",
            f"{memory_base}/pages/provider-free-smoke?expected_revision=1",
        )
        if deleted_memory != {"ok": True, "revision": 2}:
            raise AssertionError(f"Workspace memory delete returned invalid data: {deleted_memory}")

        schedules_base = "/v1/workspace/schedules"
        if self._api("GET", schedules_base).get("schedules") != []:
            raise AssertionError("fresh Workspace schedules should be empty")
        schedule_options = self._api(
            "GET", f"{schedules_base}/session-options"
        ).get("session_options")
        if not isinstance(schedule_options, dict):
            raise AssertionError(
                f"Workspace schedules returned invalid session options: {schedule_options}"
            )
        managed_schedule_options = {
            runtime: schedule_options.get(runtime) for runtime in OFFERED_RUNTIMES
        }
        if managed_schedule_options != workspace_options["/v1/workspace/chat"]:
            raise AssertionError(
                "Workspace schedules returned inconsistent managed-runtime "
                f"session options: {schedule_options}"
            )
        if (
            set(schedule_options) != {*OFFERED_RUNTIMES, "script"}
            or schedule_options.get("script") != {"bash": ["fixed"]}
        ):
            raise AssertionError(
                f"Workspace schedules returned invalid script session options: {schedule_options}"
            )
        runtime = SMOKE_RUNTIMES[0]
        runtime_models = schedule_options.get(runtime)
        if not isinstance(runtime_models, dict) or not runtime_models:
            raise AssertionError(f"Workspace schedules omitted {runtime} models: {schedule_options}")
        model, efforts = next(iter(runtime_models.items()))
        if not isinstance(efforts, list) or not efforts:
            raise AssertionError(f"Workspace schedules returned invalid efforts: {runtime_models}")
        created_schedule = self._api(
            "POST",
            schedules_base,
            {
                "name": "Provider-free smoke schedule",
                "message": "This future task must not run during fresh smoke.",
                "cadence": "interval",
                "interval_minutes": 7 * 24 * 60,
                "agent_runtime": runtime,
                "model": model,
                "effort": efforts[0],
            },
        ).get("schedule")
        if (
            not isinstance(created_schedule, dict)
            or not isinstance(created_schedule.get("id"), int)
            or created_schedule.get("revision") != 1
            or created_schedule.get("thread_id") != f"schedule-{created_schedule.get('id')}"
        ):
            raise AssertionError(
                f"Workspace schedule create returned invalid data: {created_schedule}"
            )
        schedule_id = created_schedule["id"]
        scheduled = self._api("GET", schedules_base).get("schedules")
        if not isinstance(scheduled, list) or not any(
            isinstance(item, dict) and item.get("id") == schedule_id
            for item in scheduled
        ):
            raise AssertionError(f"Workspace schedule list missed its saved task: {scheduled}")
        deleted_schedule = self._api(
            "DELETE", f"{schedules_base}/{schedule_id}?expected_revision=1"
        )
        if deleted_schedule != {
            "ok": True,
            "revision": 2,
            "thread_id": created_schedule["thread_id"],
        }:
            raise AssertionError(
                f"Workspace schedule delete returned invalid data: {deleted_schedule}"
            )

        self._ok(
            "Chat options/history, Web App create/state/rename, and global "
            "Memory and Schedules create/search/list/delete paths worked without inference"
        )

    def check_embedding_index_resource_load(self) -> None:
        """Exercise real local inference while proving the host remains responsive."""
        self._step("local embedding backfill resource load")
        page_count = 24
        revised_count = 4
        memory_base = "/v1/workspace/memory"
        latencies: list[float] = []

        def embedding_counts() -> tuple[int, int]:
            raw = self._ssh_code(
                "sudo -u postgres psql -tA -d kern_admin -F '|' -c \""
                "SELECT count(*), count(*) FILTER (WHERE embeddings.revision = pages.revision)"
                " FROM memory_page_embeddings AS embeddings"
                " JOIN memory_pages AS pages ON pages.page_id = embeddings.page_id"
                " WHERE pages.page_id LIKE 'embedding-load-%'\""
            )
            total, separator, current = raw.strip().partition("|")
            if not separator:
                raise AssertionError(f"invalid embedding count result: {raw!r}")
            return int(total), int(current)

        def wait_for_current(expected: int, timeout: float) -> float:
            started = time.monotonic()
            deadline = started + timeout
            last = (-1, -1)
            while time.monotonic() < deadline:
                health_started = time.monotonic()
                health = self._api("GET", "/v1/health")
                latencies.append(time.monotonic() - health_started)
                if health.get("network_controls", {}).get("status") != "active":
                    raise AssertionError(f"host health degraded during embedding load: {health}")
                last = embedding_counts()
                if last[1] == expected:
                    return time.monotonic() - started
                time.sleep(0.5)
            raise AssertionError(
                f"embedding backfill did not reach {expected} current rows; last={last}"
            )

        def agent_memory_search(search_query: str) -> tuple[int, dict]:
            request = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "workspace_api",
                        "arguments": {
                            "method": "GET",
                            "path": (
                                "/agent/memory/search?q="
                                f"{quote(search_query, safe='')}&limit=100"
                            ),
                        },
                    },
                }
            )
            raw = self._ssh_code(
                "printf '%s\\n' "
                f"{shlex.quote(request)} | sudo -u kern-agent env "
                "PYTHONPATH=/opt/kern-host python3 -m host.runtime.agent_shim.mcp_shim"
            )
            try:
                rpc = json.loads(raw)
                result = rpc["result"]
                content = result["content"]
                response = json.loads(content[0]["text"])
                status = response["status"]
                body = response["body"]
            except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
                raise AssertionError(
                    f"agent memory search returned an invalid MCP response: {raw!r}"
                ) from exc
            if not isinstance(status, int) or not isinstance(body, dict):
                raise AssertionError(
                    f"agent memory search returned an invalid response: {response!r}"
                )
            return status, body

        def wait_for_agent_match(
            search_query: str,
            *,
            page_id: str,
            revision: int,
            present: bool,
            timeout: float,
        ) -> tuple[float, int]:
            started = time.monotonic()
            deadline = started + timeout
            conflicts = 0
            last: dict = {}
            while time.monotonic() < deadline:
                status, last = agent_memory_search(search_query)
                if status == 409:
                    conflicts += 1
                    time.sleep(0.5)
                    continue
                if status != 200:
                    raise AssertionError(
                        f"agent memory search returned HTTP {status}: {last}"
                    )
                if last.get("search_mode") != "hybrid":
                    raise AssertionError(
                        f"agent memory search did not use hybrid retrieval: {last}"
                    )
                pages = last.get("pages")
                if not isinstance(pages, list):
                    raise AssertionError(
                        f"agent memory search omitted its pages array: {last}"
                    )
                matches = [
                    page
                    for page in pages
                    if isinstance(page, dict) and page.get("page_id") == page_id
                ]
                if present and any(page.get("revision") == revision for page in matches):
                    return time.monotonic() - started, conflicts
                if not present and not matches:
                    return time.monotonic() - started, conflicts
                time.sleep(0.5)
            expectation = "appear" if present else "disappear"
            raise AssertionError(
                f"{page_id} did not {expectation} for {search_query!r}; last={last}"
            )

        content_suffix = " ".join(["bounded local semantic indexing"] * 55)
        for index in range(page_count):
            if index == 0:
                description = "Deployment recovery procedure"
                content = (
                    "If the deployment fails, restore the previous release artifact "
                    "and redirect traffic to it."
                )
            else:
                description = f"Embedding load fixture {index:02d}"
                content = f"Fixture {index:02d}. {content_suffix}"
            started = time.monotonic()
            page = self._api(
                "PUT",
                f"{memory_base}/pages/embedding-load-{index:02d}",
                {
                    "description": description,
                    "content": content,
                    "expected_revision": 0,
                },
            ).get("page")
            latencies.append(time.monotonic() - started)
            if not isinstance(page, dict) or page.get("revision") != 1:
                raise AssertionError(f"embedding load page create failed: {page}")

        initial_seconds = wait_for_current(page_count, 90)
        initial_retrieval_seconds, initial_conflicts = wait_for_agent_match(
            "How should we undo a broken launch?",
            page_id="embedding-load-00",
            revision=1,
            present=True,
            timeout=90,
        )
        for index in range(revised_count):
            if index == 0:
                description = "Office access procedure"
                content = (
                    "Visitors must obtain a temporary badge from the reception desk "
                    "before entering."
                )
            else:
                description = f"Revised embedding load fixture {index:02d}"
                content = f"Updated fixture {index:02d}. {content_suffix}"
            self._api(
                "PUT",
                f"{memory_base}/pages/embedding-load-{index:02d}",
                {
                    "description": description,
                    "content": content,
                    "expected_revision": 1,
                },
            )
        replacement_seconds = wait_for_current(page_count, 45)
        replacement_retrieval_seconds, replacement_conflicts = wait_for_agent_match(
            "How can a guest get into the building?",
            page_id="embedding-load-00",
            revision=2,
            present=True,
            timeout=90,
        )
        wait_for_agent_match(
            "How should we undo a broken launch?",
            page_id="embedding-load-00",
            revision=2,
            present=False,
            timeout=10,
        )

        relation_bytes = int(
            self._ssh_code(
                "sudo -u postgres psql -tA -d kern_admin -c "
                "\"SELECT pg_total_relation_size('memory_page_embeddings')\""
            )
        )
        if relation_bytes > 32 * 1024 * 1024:
            raise AssertionError(
                f"{page_count} memory embeddings used an unexpected {relation_bytes} bytes"
            )

        properties = {}
        for line in self._ssh_code(
            "systemctl show kern-embedding.service "
            "-p ActiveState -p NRestarts -p MemoryCurrent -p MemoryPeak "
            "-p MemoryMax -p TasksCurrent -p CPUWeight -p IOWeight -p Nice"
        ).splitlines():
            key, separator, value = line.partition("=")
            if separator:
                properties[key] = value
        if properties.get("ActiveState") != "active" or properties.get("NRestarts") != "0":
            raise AssertionError(f"embedding service restarted or stopped under load: {properties}")
        if properties.get("MemoryMax") != str(1024 * 1024 * 1024):
            raise AssertionError(f"embedding service lost its 1 GiB hard limit: {properties}")
        if (
            properties.get("CPUWeight") != "25"
            or properties.get("IOWeight") != "25"
            or properties.get("Nice") != "10"
        ):
            raise AssertionError(f"embedding service lost its low-priority controls: {properties}")
        for field in ("MemoryCurrent", "MemoryPeak"):
            value = properties.get(field, "")
            if value.isdigit() and int(value) > 1024 * 1024 * 1024:
                raise AssertionError(f"embedding service exceeded MemoryMax: {properties}")
        tasks = properties.get("TasksCurrent", "")
        if tasks.isdigit() and int(tasks) > 64:
            raise AssertionError(f"embedding service exceeded TasksMax: {properties}")

        ordered_latencies = sorted(latencies)
        p95_latency = ordered_latencies[int(0.95 * (len(ordered_latencies) - 1))]
        if p95_latency > 3.0:
            raise AssertionError(
                f"admin API p95 latency under embedding load was {p95_latency:.2f}s"
            )

        for index in range(page_count):
            expected_revision = 2 if index < revised_count else 1
            self._api(
                "DELETE",
                f"{memory_base}/pages/embedding-load-{index:02d}"
                f"?expected_revision={expected_revision}",
            )
        if embedding_counts() != (0, 0):
            raise AssertionError("soft-deleted memory pages retained derived embeddings")
        wait_for_agent_match(
            "How can a guest get into the building?",
            page_id="embedding-load-00",
            revision=2,
            present=False,
            timeout=10,
        )

        current_memory = properties.get("MemoryCurrent", "unknown")
        self._ok(
            f"{page_count} pages indexed in {initial_seconds:.1f}s, "
            f"{revised_count} replacements in {replacement_seconds:.1f}s; "
            f"semantic retrieval in {initial_retrieval_seconds:.1f}s/"
            f"{replacement_retrieval_seconds:.1f}s with "
            f"{initial_conflicts + replacement_conflicts} cursor conflicts; "
            f"API p95 {p95_latency:.2f}s, service memory {current_memory} bytes, "
            f"index {relation_bytes} bytes, no restart, cleanup complete"
        )

    def check_policy_validation_and_concurrency(self) -> None:
        self._step("policy validation and concurrent replaces")
        # SSH is deployment config, not runtime network policy.
        pinned = network_policy(SMOKE_MANAGED_PROVIDERS)
        pinned["ssh_port_opened"] = False
        status, body = self._api_status("PUT", "/v1/network/policy", pinned)
        if status != 400:
            raise AssertionError(f"runtime ssh_port_opened field returned {status}, expected 400: {body}")
        invalid = network_policy(SMOKE_MANAGED_PROVIDERS, {"api.example.com": {"allow_http_methods": ["BOGUS"]}})
        status, body = self._api_status("PUT", "/v1/network/policy", invalid)
        if status != 400:
            raise AssertionError(f"invalid policy returned {status}, expected 400: {body}")
        disabled_provider_policy = {"network_integrations": {}}
        status, body = self._api_status(
            "PUT", "/v1/network/policy", disabled_provider_policy
        )
        if status != 200:
            raise AssertionError(f"disabling all managed providers returned {status}, expected 200: {body}")
        for runtime in SMOKE_RUNTIMES:
            disabled_status = self._wait_for_runtime_status({"deactivated"}, runtime=runtime, timeout=60)
            if disabled_status != "deactivated":
                raise AssertionError(f"{runtime} did not deactivate after provider access was disabled")
        for label, login_path in (
            ("codex-provider-disabled-login", "/v1/agent-runtime/codex-oauth-login"),
            ("claude-provider-disabled-login", "/v1/agent-runtime/claude-oauth-login"),
        ):
            status, _ = self._api_status("POST", login_path)
            if status != 409:
                raise AssertionError(f"{login_path} while provider is deactivated returned {status}, expected 409")
        status, body = self._api_status(
            "POST",
            "/v1/agent-runtime/bedrock-credentials",
            {
                "access_key_id": "AKIA0000000000000000",
                "secret_access_key": "not-a-real-secret",
                "region": SMOKE_BEDROCK_REGION,
            },
        )
        if status != 400:
            raise AssertionError(
                f"invalid Bedrock credentials while disabled returned {status}, expected 400: {body}"
            )
        metadata = self._api("GET", "/v1/agent-runtime/bedrock-credentials")
        if metadata != {"connected": False}:
            raise AssertionError(f"rejected Bedrock credential remained stored: {metadata}")

        for label, providers, enabled_runtime, disabled_runtime, disabled_login_path in (
            (
                "openai-only",
                {"openai": True},
                "codex",
                "claude_code",
                "/v1/agent-runtime/claude-oauth-login",
            ),
            (
                "claude-only",
                {"claude": True},
                "claude_code",
                "codex",
                "/v1/agent-runtime/codex-oauth-login",
            ),
        ):
            policy = network_policy(providers)
            status, body = self._api_status("PUT", "/v1/network/policy", policy)
            if status != 200:
                raise AssertionError(f"{label} provider policy returned {status}, expected 200: {body}")
            enabled_status = self._wait_for_runtime_status(
                {"loading", "awaiting_login", "active"}, runtime=enabled_runtime, timeout=120
            )
            if enabled_status not in {"loading", "awaiting_login", "active"}:
                raise AssertionError(f"{label} should enable {enabled_runtime}, got {enabled_status}")
            disabled_status = self._wait_for_runtime_status({"deactivated"}, runtime=disabled_runtime, timeout=60)
            if disabled_status != "deactivated":
                raise AssertionError(f"{label} should deactivate {disabled_runtime}, got {disabled_status}")
            status, _ = self._api_status("POST", disabled_login_path)
            if status != 409:
                raise AssertionError(f"{label} disabled runtime login returned {status}, expected 409")
            print(
                f"  {label}: {enabled_runtime} status {enabled_status}; {disabled_runtime} deactivated",
                flush=True,
            )

        status, body = self._api_status(
            "PUT", "/v1/network/policy", network_policy({"bedrock": True})
        )
        if status != 200:
            raise AssertionError(f"Bedrock-only policy returned {status}, expected 200: {body}")
        enabled_status = self._wait_for_runtime_status(
            {"awaiting_login"}, runtime="hermes", timeout=120
        )
        if enabled_status != "awaiting_login":
            raise AssertionError(f"Bedrock should await one connection for Hermes: {enabled_status}")
        for runtime in ("codex", "claude_code"):
            disabled_status = self._wait_for_runtime_status(
                {"deactivated"}, runtime=runtime, timeout=60
            )
            if disabled_status != "deactivated":
                raise AssertionError(f"Bedrock-only policy should deactivate {runtime}")

        for label, bad_policy, expected_error in (
            (
                "self-managed-openai-domain",
                network_policy({"openai": True, "claude": True}, {"chatgpt.com": {"allow_http_methods": ["GET"]}}),
                "network_integrations.openai",
            ),
            (
                "self-managed-claude-domain",
                network_policy({"openai": True, "claude": True}, {"api.anthropic.com": {"allow_http_methods": ["POST"]}}),
                "network_integrations.claude",
            ),
            (
                "unsupported-bedrock-region",
                {"network_integrations": {"bedrock": {"enabled": True, "region": "eu-west-1"}}},
                "unsupported fields",
            ),
            (
                "user-openai-managed-flag",
                network_policy(SMOKE_MANAGED_PROVIDERS, {
                    "api.example.com": {
                        "allow_http_methods": ["GET"],
                        "openai_external_url_request_guard": True,
                    }
                }),
                "unsupported fields",
            ),
            (
                "user-openai-account-guard-flag",
                network_policy(SMOKE_MANAGED_PROVIDERS, {
                    "api.example.com": {
                        "allow_http_methods": ["GET"],
                        "openai_account_guard": True,
                    }
                }),
                "unsupported fields",
            ),
        ):
            status, body = self._api_status("PUT", "/v1/network/policy", bad_policy)
            if status != 400:
                raise AssertionError(f"{label} policy returned {status}, expected 400: {body}")
            error = body.get("error", {})
            message = error.get("message", "") if isinstance(error, dict) else str(error)
            if expected_error not in message:
                raise AssertionError(f"{label} error should mention {expected_error}, got: {body}")

        # Concurrent replaces must serialize: each one either succeeds or is
        # turned away with 409 (the lock wait is bounded), the final policy is
        # exactly one of the successful requests, and enforcement ends active.
        # A torn or interleaved write would leave a policy nobody requested.
        variants = [
            network_policy(SMOKE_MANAGED_PROVIDERS, {f"smoke-{index}.example.com": {"allow_http_methods": ["GET"]}})
            for index in range(4)
        ]
        results = self._parallel(
            len(variants),
            lambda index: self._api_status(
                "PUT", "/v1/network/policy", variants[index]
            ),
        )
        succeeded = []
        for index, (status, body) in enumerate(results):
            if status == 200:
                succeeded.append(variants[index])
            elif status != 409:
                raise AssertionError(f"concurrent policy replace {index} returned {status}: {body}")
        if not succeeded:
            raise AssertionError(f"no concurrent policy replace succeeded: {results}")
        final = self._api("GET", "/v1/network/policy")["network_controls"]
        if final not in succeeded:
            raise AssertionError(f"final policy matches none of the successful replacements: {final}")
        health = self._api("GET", "/v1/health")
        if health["network_controls"]["status"] != "active":
            raise AssertionError(f"network status not active after concurrent replaces: {health}")
        # Leave the enforcement policy in place for the checks that follow.
        self._api("PUT", "/v1/network/policy", self.enforcement_policy())
        self._ok(
            f"ssh pin, provider schema, and validation enforced; "
            f"asymmetric provider activation checked; {len(succeeded)}/4 concurrent replaces applied, rest 409, status active"
        )

    def check_turn_admission_contract(self) -> None:
        """There is no queue: pre-login, a first message is rejected outright
        with the runtime status in the error, the rejection rolls back
        whole-cloth (no thread row, no events), and the message/thread
        validation 400s and 404s hold on the thread-only surface."""
        self._step("thread message admission and 4xx contract (pre-login)")
        status, body = self._api_status(
            "POST", "/v1/threads/thread-smoke-lifecycle/messages",
            self.message_body("lifecycle check (smoke)"),
        )
        if status != 409:
            raise AssertionError(f"pre-login message returned {status}, expected 409: {body}")
        if RUNTIME_INACTIVE_MARKER not in self._error_message(body):
            raise AssertionError(f"admission error should name the runtime status: {body}")

        # Exercise every runtime (and every Bedrock model) through the real
        # admission path. Hermes cannot run a paid turn in credential-free
        # smoke, but its runtime dispatch must reject cleanly per model.
        for runtime in (item for item in SMOKE_RUNTIMES if item != self.agent_runtime):
            models = (
                SMOKE_BEDROCK_MODELS
                if runtime == "hermes"
                else (SMOKE_RUNTIME_MODELS[runtime],)
            )
            for model in models:
                thread_id = (
                    f"thread-smoke-lifecycle-{self.thread_id_component(runtime)}-"
                    f"{self.thread_id_component(model)}"
                )
                status, body = self._api_status(
                    "POST",
                    f"/v1/threads/{thread_id}/messages",
                    self.message_body(
                        f"{runtime} {model} lifecycle check (smoke)", runtime=runtime, model=model
                    ),
                )
                if status != 409 or RUNTIME_INACTIVE_MARKER not in self._error_message(body):
                    raise AssertionError(
                        f"{runtime} {model} did not reject cleanly before login: {status} {body}"
                    )

        status, _ = self._api_status("GET", "/v1/threads/thread-999999")
        if status != 404:
            raise AssertionError(f"unknown thread returned {status}, expected 404")
        status, _ = self._api_status("POST", "/v1/threads/thread-999999/stop")
        if status != 404:
            raise AssertionError(f"stopping an unknown thread returned {status}, expected 404")

        # Message and config validation run before the admission gate, so the
        # 400 contract holds even while the runtime is not active.
        for label, bad_body in (
            ("empty-message", self.message_body("x") | {"message": ""}),
            ("oversized-message", self.message_body("x") | {"message": "x" * (MESSAGE_LIMIT + 1)}),
            ("no-config", self.follow_up_body("missing config (smoke)")),
            ("bad-runtime", self.message_body("bad runtime (smoke)") | {"agent_runtime": "bad"}),
            ("partial-config", {"message": "partial config (smoke)", "agent_runtime": self.agent_runtime}),
        ):
            status, _ = self._api_status("POST", "/v1/threads/thread-smoke-bad-config/messages", bad_body)
            if status != 400:
                raise AssertionError(f"message with {label} returned {status}, expected 400")
        # An invalid thread id shape never reaches a handler.
        status, _ = self._api_status(
            "POST", "/v1/threads/not.valid/messages", self.message_body("bad thread (smoke)")
        )
        if status != 404:
            raise AssertionError(f"invalid thread id returned {status}, expected 404")
        status, _ = self._api_status("GET", "/v1/threads/thread-smoke-lifecycle?verbose=1")
        if status != 400:
            raise AssertionError(f"thread detail with query params returned {status}, expected 400")
        status, _ = self._api_status("GET", "/v1/threads/thread-smoke-lifecycle/events?since=0&before=9")
        if status != 400:
            raise AssertionError(f"combining since and before returned {status}, expected 400")

        # The rejected admissions rolled back whole-cloth: no thread row and
        # no retained turn events.
        listed = {item["thread_id"] for item in self._api("GET", "/v1/threads")["threads"]}
        leaked = {thread_id for thread_id in listed if "smoke-lifecycle" in thread_id}
        if leaked:
            raise AssertionError(f"rejected admissions left thread rows behind: {leaked}")
        status, _ = self._api_status("GET", "/v1/threads/thread-smoke-lifecycle")
        if status != 404:
            raise AssertionError(f"rejected thread should stay unknown, got {status}")
        events = self._api("GET", "/v1/threads/thread-smoke-lifecycle/events?since=0")["events"]
        if events:
            raise AssertionError(f"rejected admission left turn events behind: {events}")

        # The task API is gone from the admin surface.
        for method, path in (
            ("GET", "/v1/tasks"),
            ("POST", "/v1/tasks"),
            ("GET", "/v1/tasks/task_999999"),
            ("GET", "/v1/tasks/finished"),
            ("GET", "/v1/threads/thread-smoke-lifecycle/tasks"),
        ):
            status, _ = self._api_status(method, path)
            if status != 404:
                raise AssertionError(f"removed task route {method} {path} returned {status}, expected 404")

        # Runtime status carries live thread ids, never task ids.
        record = self.runtime_status_record(self._api("GET", "/v1/agent-runtime/status"))
        if record.get("active_thread_ids") != [] or "active_task_ids" in record:
            raise AssertionError(f"runtime status should report empty active_thread_ids: {record}")
        self._ok(
            "all three pre-login runtimes rejected messages with the runtime status, "
            "Hermes rejected all three Bedrock models, rejections left no thread state; "
            "validation 400s and unknown-thread 404s honored; task routes gone"
        )

    def check_admin_concurrency(self) -> None:
        self._step("concurrent message admissions with interleaved health reads")
        # Pre-login, every admission is rejected with a clean 409 (never a
        # 5xx), interleaved with health reads that must never block or fail,
        # and no rejected admission may leave a thread row behind.
        creates = 8

        def create_or_health(index: int) -> tuple[int, dict]:
            if index >= creates:
                return self._api_status("GET", "/v1/health")
            return self._api_status(
                "POST", f"/v1/threads/thread-smoke-cc-{index}/messages",
                self.message_body(f"concurrent admission {index} (smoke)"),
            )

        results = self._parallel(creates + 3, create_or_health)
        for index, (status, body) in enumerate(results):
            if index >= creates:
                if status != 200:
                    raise AssertionError(f"concurrent health read {index} returned {status}: {body}")
            elif status != 409 or RUNTIME_INACTIVE_MARKER not in self._error_message(body):
                raise AssertionError(f"concurrent admission {index} returned {status}: {body}")
        listed = {item["thread_id"] for item in self._api("GET", "/v1/threads")["threads"]}
        leaked = {thread_id for thread_id in listed if "smoke-cc-" in thread_id}
        if leaked:
            raise AssertionError(f"rejected concurrent admissions left thread rows: {leaked}")
        self._ok(f"{creates} parallel admissions rejected cleanly; health reads never blocked")

    def check_state_transactions(self) -> None:
        """Edge cases of the admin-state read-modify-write transaction under
        real concurrency: racing admissions on one thread all roll back
        whole-cloth (no thread row, no events — check-then-act shares the
        message's transaction), racing stops answer cleanly, reads stay fast
        and consistent mid-storm, and event seqs stay unique across parallel
        writers."""
        self._step("state transaction edge cases (atomic admission rollback, racing stops, seq uniqueness)")

        # 1. Concurrent first messages racing over ONE thread pre-login. The
        # admission check and the thread/event writes share one transaction,
        # so every racer sees the clean 409 and the rollback leaves no
        # partial thread state a later racer (or reader) could observe.
        sends = self._parallel(
            6,
            lambda i: self._api_status(
                "POST", "/v1/threads/thread-smoke-tx-send/messages",
                self.message_body("admission race (smoke)"),
            ),
        )
        statuses = sorted(status for status, _ in sends)
        if any(status != 409 for status in statuses):
            raise AssertionError(f"racing admissions must all yield 409, got {statuses}")
        status, _ = self._api_status("GET", "/v1/threads/thread-smoke-tx-send")
        if status != 404:
            raise AssertionError(f"racing admissions left a thread row behind ({status})")
        events = self._api("GET", "/v1/threads/thread-smoke-tx-send/events?since=0")["events"]
        if events:
            raise AssertionError(f"racing admissions left turn events behind: {events}")

        # 2. Concurrent stops on a thread that does not exist: every racer
        # sees the clean 404 (never a 5xx or a phantom accepted stop).
        stops = self._parallel(
            5, lambda i: self._api_status("POST", "/v1/threads/thread-smoke-tx-stop/stop")
        )
        bad = [status for status, _ in stops if status != 404]
        if bad:
            raise AssertionError(f"racing stops on an unknown thread returned {bad}, expected 404s")

        # 3. Mixed messages, stops, and reads racing over one thread: writers
        # see their contract status and the reads never block or fail.
        def message_stop_or_read(index: int) -> tuple[int, dict]:
            if index % 3 == 0:
                return self._api_status("GET", "/v1/health")
            if index % 3 == 1:
                return self._api_status(
                    "POST", "/v1/threads/thread-smoke-tx-mixed/messages",
                    self.message_body(f"mixed racer {index} (smoke)"),
                )
            return self._api_status("POST", "/v1/threads/thread-smoke-tx-mixed/stop")

        mixed = self._parallel(9, message_stop_or_read)
        for index, (status, body) in enumerate(mixed):
            expected = (200,) if index % 3 == 0 else (409,) if index % 3 == 1 else (404,)
            if status not in expected:
                raise AssertionError(f"mixed storm request {index} returned {status}: {body}")

        # 4. Parallel writers allocated event seqs through the transaction, so
        # the agent event log must hold no duplicate seq anywhere.
        seqs = [int(event["seq"]) for event in self._agent_events()]
        if len(seqs) != len(set(seqs)):
            duplicates = sorted({seq for seq in seqs if seqs.count(seq) > 1})
            raise AssertionError(f"agent event log has duplicate seqs after the storms: {duplicates}")

        self._ok("admission rollback atomic, racing stops clean, mixed storm consistent, event seqs unique")

    def check_event_pagination(self) -> None:
        self._step("agent event pagination (newest-first cursor pages, strict seq ordering)")
        # Rejected admissions roll back their events, so the pre-login log
        # holds only agent_runtime.* transitions from the policy toggles.
        events = self._agent_events()
        if len(events) < 4:
            raise AssertionError(f"expected the earlier checks to leave >3 events, found {len(events)}")
        seqs = [int(event["seq"]) for event in events]
        if sorted(seqs) != seqs or len(set(seqs)) != len(seqs):
            raise AssertionError(f"event seqs are not strictly increasing/unique: {seqs}")
        limited = self._api("GET", "/v1/events?limit=3")["events"]
        if [int(event["seq"]) for event in limited] != seqs[-1:-4:-1]:
            raise AssertionError(f"limit=3 did not return the newest three events: {limited}")
        self._ok(f"{len(seqs)} events drained through the cursor with unique seqs")

    def check_enforcement(self) -> None:
        self._step("network enforcement (proxy + nftables, as the agent user)")
        proxy = f"http://127.0.0.1:{PROXY_PORT}"
        agent = "sudo -u kern-agent env"
        allowed = self._ssh_code(f"{agent} HTTPS_PROXY={proxy} curl -s -o /dev/null -w '%{{http_code}}' --max-time 20 https://example.com/")
        denied = self._ssh_code(f"{agent} HTTPS_PROXY={proxy} curl -s -o /dev/null -w '%{{http_code}}' --max-time 20 https://example.com/denied")
        direct = self._ssh_code(f"{agent} curl -s -o /dev/null -w '%{{http_code}}' --max-time 12 https://example.com/ || true")
        loopback_admin = self._ssh_code(
            f"{agent} curl -s -o /dev/null -w '%{{http_code}}' --connect-timeout 2 --max-time 5 "
            f"http://127.0.0.1:{ADMIN_PORT}/v1/health || true"
        )
        # Loopback is not an identity boundary by itself. Exercise the admin
        # listener's complete uid boundary on real nftables here, in the
        # opt-in billable smoke rather than adding probes to every user deploy
        # and upgrade. Allowed identities reach the unauthenticated 401 gate;
        # egress-capable service identities receive no connection at all.
        def admin_probe(user: str) -> str:
            return self._ssh_code(
                f"sudo -u {user} curl -s -o /dev/null -w '%{{http_code}}' "
                f"--connect-timeout 2 --max-time 5 http://127.0.0.1:{ADMIN_PORT}/v1/health || true"
            )

        admin_uid_results = {
            "kern-operator": self._ssh_code(
                f"curl -s -o /dev/null -w '%{{http_code}}' --connect-timeout 2 --max-time 5 "
                f"http://127.0.0.1:{ADMIN_PORT}/v1/health || true"
            ),
            "kern-admin": admin_probe("kern-admin"),
            "cloudflared": admin_probe("cloudflared"),
            "kern-tools": admin_probe("kern-tools"),
            "kern-proxy": admin_probe("kern-proxy"),
        }
        # The preview-port carve-out and its boundary, exercised on the live
        # host's firewall in one orchestrated command (run as kern-operator, who
        # has sudo). A kern-agent HTTP server serves a known file on the base
        # preview port as a tracked background child (its PID captured in $srv);
        # then we fetch it as kern-agent — which must succeed (200: the dport
        # accept + the established reply accept both work) — and as kern-tools,
        # an egress-capable service account that must be blocked (no code: the
        # range is default-deny, so a compromised service can neither dial a
        # preview server nor be reached by the agent). Cleanup kills the exact
        # PID; a `pkill -f http.server` pattern would also match this probe's own
        # `bash -c` command lines and kill the shell before it reports.
        preview_port = AGENT_PREVIEW_PORT_BASE
        # -w %{http_code} is left unquoted on purpose: this whole probe is a
        # single-quoted `bash -c '...'`, so an inner single quote would close it;
        # bash does not brace-expand a single-element {http_code}.
        curl = (
            "curl -s -o /dev/null -w %{http_code} --connect-timeout 2 --max-time 5 "
            f"http://127.0.0.1:{preview_port}/probe.txt"
        )
        preview_probe = self._ssh_code(
            "bash -c '"
            "d=$(sudo -u kern-agent mktemp -d); "
            "sudo -u kern-agent tee \"$d/probe.txt\" >/dev/null <<<preview-ok; "
            f"sudo -u kern-agent python3 -m http.server {preview_port} --bind 127.0.0.1 "
            "--directory \"$d\" >/dev/null 2>&1 & "
            "srv=$!; "
            "sleep 1; "
            f"a=$(sudo -u kern-agent {curl} || true); "
            f"t=$(sudo -u kern-tools {curl} || true); "
            "sudo kill \"$srv\" 2>/dev/null; "
            "sudo rm -rf \"$d\"; "
            "echo \"agent=$a tools=$t\"' || true"
        )
        preview_self = ""
        preview_tools = ""
        for token in preview_probe.split():
            if token.startswith("agent="):
                preview_self = token.split("=", 1)[1]
            elif token.startswith("tools="):
                preview_tools = token.split("=", 1)[1]
        if allowed != "200":
            raise AssertionError(f"allowed request through proxy returned {allowed!r}, expected 200")
        if denied != "403":
            raise AssertionError(f"denied path through proxy returned {denied!r}, expected 403")
        if direct == "200":
            raise AssertionError("direct (un-proxied) request succeeded; nftables is not blocking the agent")
        if loopback_admin not in ("", "000"):
            raise AssertionError(
                f"agent reached loopback admin API directly ({loopback_admin}); nftables should allow only the proxy port"
            )
        for user in ("kern-operator", "kern-admin", "cloudflared"):
            if admin_uid_results[user] != "401":
                raise AssertionError(
                    f"{user} did not reach the admin login gate "
                    f"(got {admin_uid_results[user]!r}, expected 401)"
                )
        for user in ("kern-tools", "kern-proxy"):
            if admin_uid_results[user] not in ("", "000"):
                raise AssertionError(
                    f"{user} reached the admin listener ({admin_uid_results[user]}); "
                    "the per-service loopback boundary is not effective"
                )
        if preview_self != "200":
            raise AssertionError(
                f"agent could not serve and reach its own preview port {preview_port} "
                f"(got {preview_self!r}, expected 200); the preview-range firewall rules are not effective"
            )
        if preview_tools not in ("", "000"):
            raise AssertionError(
                f"a service account (kern-tools) reached the agent preview port {preview_port} "
                f"(got {preview_tools!r}, expected no connection); the per-service preview drop is not effective"
            )
        # GitHub reads are universal: both the configured repository and a
        # foreign public repository are forwarded and served, while GraphQL
        # (which can mutate and cannot be parsed) fails closed at the proxy.
        gh_allowed = self._ssh_code(
            f"{agent} HTTPS_PROXY={proxy} curl -s -o /dev/null -w '%{{http_code}}' --max-time 20 "
            f"https://github.com/infiloop2/kern"
        )
        gh_foreign = self._ssh_code(
            f"{agent} HTTPS_PROXY={proxy} curl -s -o /dev/null -w '%{{http_code}}' --max-time 20 "
            f"https://github.com/torvalds/linux"
        )
        gh_graphql = self._ssh_code(
            f"{agent} HTTPS_PROXY={proxy} curl -s -o /dev/null -w '%{{http_code}}' --max-time 20 "
            "-X POST -d '{\"query\":\"{viewer{login}}\"}' https://api.github.com/graphql || true"
        )
        if gh_allowed != "200":
            raise AssertionError(f"configured GitHub repo through proxy returned {gh_allowed!r}, expected 200")
        if gh_foreign != "200":
            raise AssertionError(f"foreign GitHub repo through proxy returned {gh_foreign!r}, expected 200 (reads are universal)")
        if gh_graphql != "403":
            raise AssertionError(f"GitHub GraphQL through proxy returned {gh_graphql!r}, expected proxy 403")
        # End-to-end: a real git clone of the configured repository rides
        # smart-HTTP through the proxy (git and gh are installed by bootstrap;
        # git trusts the proxy CA via the system store).
        tool_versions = self._ssh_code("git --version && gh --version | head -1")
        if "git version" not in tool_versions or "gh version" not in tool_versions:
            raise AssertionError(f"git/gh missing on the host: {tool_versions!r}")
        self._ssh_code("sudo rm -rf /tmp/kern-smoke-clone /tmp/kern-smoke-foreign")
        clone_ok = self._ssh_code(
            f"{agent} HTTPS_PROXY={proxy} https_proxy={proxy} "
            "git clone --depth 1 https://github.com/infiloop2/kern /tmp/kern-smoke-clone "
            ">/dev/null 2>&1 && echo cloned; sudo rm -rf /tmp/kern-smoke-clone"
        ).strip()
        if clone_ok != "cloned":
            raise AssertionError("git clone of the configured repository through the proxy failed")
        # Reads are universal, so a foreign public repo clones too (a small one
        # keeps the smoke fast); the denied network events come from the
        # GraphQL and write denials above.
        foreign_clone = self._ssh_code(
            f"{agent} HTTPS_PROXY={proxy} https_proxy={proxy} "
            "git clone --depth 1 https://github.com/octocat/Hello-World /tmp/kern-smoke-foreign "
            ">/dev/null 2>&1 && echo cloned || echo denied; sudo rm -rf /tmp/kern-smoke-foreign"
        ).strip()
        if foreign_clone != "cloned":
            raise AssertionError("git clone of a foreign public repository should be allowed (reads are universal)")
        events = self._network_events()
        decisions = {event["decision"] for event in events}
        if not {"allowed", "denied"} <= decisions:
            raise AssertionError(f"expected both allowed and denied network events, saw {decisions}")
        self._ok(
            f"proxy allowed=200 denied=403, direct blocked ({direct or 'no connection'}), "
            f"admin loopback blocked ({loopback_admin or 'no connection'}), "
            f"preview self-serve={preview_self} service-blocked={preview_tools or 'no connection'}, "
            f"github reads listed={gh_allowed} foreign={gh_foreign} graphql-denied={gh_graphql}, events logged"
        )

    def check_package_client_headers(self) -> None:
        """Drive real pip and npm clients through the proxy.

        The package integrations exist to make `pip install` and `npm install`
        work, and the proxy removes credential headers and replaces User-Agent
        before forwarding. Only a real client proves the resulting request is
        still accepted by the public registry.

        Public registries only, no credential, so it runs in ordinary smoke.
        """
        self._step("package clients (real pip and npm through the proxy)")
        proxy = f"http://127.0.0.1:{PROXY_PORT}"
        env = (
            "sudo -u kern-agent env HOME=/mnt/kern-agent/agent-home "
            f"HTTPS_PROXY={proxy} https_proxy={proxy} "
            "SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt "
            "REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt "
            "NODE_EXTRA_CA_CERTS=/usr/local/share/ca-certificates/kern-network-proxy.crt"
        )

        def agent_command(command: str) -> str:
            return (
                f"{env} sh -c "
                + shlex.quote(f"cd /mnt/kern-agent/agent-home && {command}")
            )

        curl = "curl -s -o /dev/null -w '%{http_code}' --max-time 30"
        baseline_seq = max((event["seq"] for event in self._network_events()), default=0)

        # Kern deliberately has no system-wide pip module. Use the installed uv
        # to create a seeded venv, exercising uv's proxy-CA path, then use that
        # venv's real pip client for the registry request.
        workdir = f"/tmp/kern-smoke-pip-{time.time_ns()}"
        try:
            venv_result = self._ssh_code(
                agent_command(f"uv venv --seed {workdir}/venv")
                + " 2>&1 && echo __KERN_VENV_OK__"
            ).strip()
            if not venv_result.endswith("__KERN_VENV_OK__"):
                raise AssertionError(
                    "could not create the temporary pip venv: "
                    f"{venv_result[-2000:]!r}"
                )
            pip_result = self._ssh_code(
                agent_command(
                    f"{workdir}/venv/bin/python -m pip download --quiet --no-deps "
                    f"--disable-pip-version-check --dest {workdir}/downloads certifi"
                )
                + " 2>&1 && echo __KERN_PIP_OK__"
            ).strip()
        finally:
            self._ssh_code(f"sudo -u kern-agent rm -rf {workdir}")
        if not pip_result.endswith("__KERN_PIP_OK__"):
            raise AssertionError(
                "pip download through the proxy failed: "
                f"{pip_result[-2000:]!r}"
            )

        # npm's own client, when the host has it, plus a plain curl fetch so the
        # check still exercises the registry when it does not.
        # "npm is absent" and "npm ran and failed" must not collapse into the
        # same answer: the second is the regression this check exists to catch.
        npm_result = self._ssh_code(
            agent_command(
                "if command -v npm >/dev/null 2>&1; then "
                "if npm view lodash version >/dev/null 2>&1; "
                "then echo ok; else echo failed; fi; "
                "else echo absent; fi"
            )
        ).strip()
        if npm_result == "failed":
            raise AssertionError(
                "npm is installed on this host but `npm view` failed through the proxy; "
                "the npm client's own request no longer works"
            )
        registry_status = self._ssh_code(
            agent_command(f"{curl} https://registry.npmjs.org/lodash")
        ).strip()
        if registry_status not in {"200", "304"}:
            raise AssertionError(
                f"npm registry metadata read returned {registry_status!r} through the proxy"
            )
        tarball_status = self._ssh_code(
            agent_command(
                f"{curl} -r 0-1023 https://registry.npmjs.org/lodash/-/lodash-4.17.21.tgz"
            )
        ).strip()
        if tarball_status not in {"200", "206"}:
            raise AssertionError(
                f"npm ranged tarball download returned {tarball_status!r}; the Range shape "
                "in the allowlist may be narrower than a real client's request"
            )

        # The URL guard is the control that remains, so it must still bite.
        guarded = self._ssh_code(
            agent_command(f"{curl} https://pypi.org/simple/alice%40example.com/")
        ).strip()
        if guarded != "403":
            raise AssertionError(
                f"a package name carrying an identifier returned {guarded!r}, expected a proxy 403"
            )

        denials = [
            event
            for event in self._network_events(baseline_seq)
            if str(event.get("reason_code") or "").startswith("request_")
            and str(event.get("host") or "") != "pypi.org"
        ]
        if denials:
            detail = ", ".join(
                f"{event.get('method')} {event.get('host')}{event.get('path')} -> {event.get('reason_code')}"
                for event in denials[:5]
            )
            raise AssertionError(f"real package-client traffic was denied: {detail}")
        self._ok(
            f"pip=ok npm={npm_result} registry={registry_status} "
            f"ranged={tarball_status} guarded-url=403"
        )

    def check_github_read_paths(self) -> None:
        """Every GitHub guard branch, unauthenticated — the guard decides
        without a credential, so this covers the whole surface without one. When
        GitHub is enabled reads are universal, so foreign repos and non-repo
        paths (search) are forwarded and answer with GitHub's own status; writes
        are gated on the write repo (infiloop2/kern), so a write to an
        unlisted repo is denied by the proxy while a write to the listed repo
        reaches upstream and returns GitHub's 401 (no credential installed).
        Repository administration is denied even on the write repo, at the guard
        before any credential, so the full admin-write denylist (forks, hooks,
        keys, pages, actions secrets/permissions/oidc, protections, statuses,
        run approval/deletion, ...) is exercised here as proxy 403s. GraphQL and
        non-repo writes (create repo/gist) are denied too. HEAD refs keep the
        checks default-branch-agnostic."""
        self._step("github guard matrix (universal reads, scoped writes)")
        proxy = f"http://127.0.0.1:{PROXY_PORT}"
        curl = (
            f"sudo -u kern-agent env HTTPS_PROXY={proxy} "
            "curl -s -o /dev/null -w '%{http_code}' --max-time 20"
        )
        # These unauthenticated API reads can be forwarded correctly while
        # GitHub rate-limits the shared egress IP or its search/read edge times
        # out. The network-event assertion still proves the proxy allowed the
        # request. Keep proxy-denial checks below exact; only upstream read
        # responses get this tolerance.
        read_ok = {"200", "403", "429", "502", "504"}
        unauthenticated_read_ok = {"401", "403", "429", "502", "504"}
        checks = [
            # Reads are universal: the listed repo, a foreign public repo, and
            # non-repo paths (search) are all forwarded to GitHub.
            ("api listed repo read", f"{curl} https://api.github.com/repos/infiloop2/kern", read_ok),
            ("api foreign repo read", f"{curl} https://api.github.com/repos/torvalds/linux", read_ok),
            ("api search read", f"{curl} 'https://api.github.com/search/repositories?q=kern'", read_ok),
            ("api rate_limit read", f"{curl} https://api.github.com/rate_limit", read_ok),
            # /user needs auth; the proxy forwards it and GitHub's own 401 (no
            # credential installed) comes back — a proxy denial would be 403.
            ("api /user reaches upstream", f"{curl} https://api.github.com/user || true", unauthenticated_read_ok),
            ("raw listed file", f"{curl} https://raw.githubusercontent.com/infiloop2/kern/HEAD/README.md", read_ok),
            ("raw foreign file", f"{curl} https://raw.githubusercontent.com/torvalds/linux/HEAD/README", read_ok),
            ("codeload listed tarball", f"{curl} https://codeload.github.com/infiloop2/kern/tar.gz/HEAD", read_ok),
            # Keep this a guard check, not a bandwidth test: the Linux kernel
            # tarball can exceed the smoke's fixed request deadline.
            ("codeload foreign tarball", f"{curl} https://codeload.github.com/octocat/Hello-World/tar.gz/HEAD", read_ok),
            ("github.com web read", f"{curl} https://github.com/torvalds/linux", read_ok),
            # The API tarball endpoint 302s to codeload; following the
            # redirect exercises both domains in one read chain.
            (
                "api tarball redirect to codeload",
                f"{curl} -L https://api.github.com/repos/infiloop2/kern/tarball/HEAD",
                read_ok,
            ),
            # Writes to an unlisted repo are denied by the proxy before any
            # credential question arises.
            (
                "receive-pack discovery on unlisted denied",
                f"{curl} 'https://github.com/torvalds/linux/info/refs?service=git-receive-pack' || true",
                "403",
            ),
            (
                "api write to unlisted denied",
                f"{curl} -X POST -d '{{}}' https://api.github.com/repos/torvalds/linux/issues || true",
                "403",
            ),
            # A write to the listed repo passes the proxy and reaches upstream,
            # which answers 401 without a credential (a proxy denial is 403).
            (
                "api write to listed reaches upstream",
                f"{curl} -X POST -d '{{}}' https://api.github.com/repos/infiloop2/kern/issues || true",
                "401",
            ),
            # GraphQL is denied outright (can mutate, cannot be parsed).
            (
                "api graphql denied",
                f"{curl} -X POST -d '{{\"query\":\"{{viewer{{login}}}}\"}}' https://api.github.com/graphql || true",
                "403",
            ),
            # Writes that target no repository at all (create a repo, create a
            # gist) are never a configured write repo.
            (
                "api create-repo denied",
                f"{curl} -X POST -d '{{\"name\":\"x\"}}' https://api.github.com/user/repos || true",
                "403",
            ),
            (
                "api create-gist denied",
                f"{curl} -X POST -d '{{\"files\":{{}}}}' https://api.github.com/gists || true",
                "403",
            ),
            # Encoded traversal: %2e%2e decodes to .. and collapses a write path
            # onto an unlisted repo, which the canonicalizing guard must deny.
            (
                "encoded traversal write denied",
                f"{curl} -X POST -d '{{}}' 'https://api.github.com/repos/infiloop2/kern/%2e%2e/%2e%2e/%2e%2e/repos/torvalds/linux/issues' || true",
                "403",
            ),
            # github.com web mutations are denied everywhere: the API is the
            # only mutation surface.
            (
                "github.com web mutation denied",
                f"{curl} -X POST https://github.com/infiloop2/kern/issues || true",
                "403",
            ),
            (
                "uploads to unlisted denied",
                f"{curl} -X POST https://uploads.github.com/repos/torvalds/linux/releases/1/assets || true",
                "403",
            ),
            # LFS batch: upload is denied on any repo; an unparseable body
            # fails closed.
            (
                "lfs upload denied",
                f"{curl} -X POST -H 'Content-Type: application/json' "
                f"-d '{{\"operation\":\"upload\",\"objects\":[]}}' "
                f"https://github.com/infiloop2/kern.git/info/lfs/objects/batch || true",
                "403",
            ),
            (
                "lfs garbage body fails closed",
                f"{curl} -X POST -d 'not-json' https://github.com/infiloop2/kern.git/info/lfs/objects/batch || true",
                "403",
            ),
        ]
        for name, command, expected in checks:
            got = self._ssh_code(command).strip()
            if isinstance(expected, set):
                if got not in expected:
                    raise AssertionError(f"{name}: expected one of {sorted(expected)}, got {got!r}")
                continue
            if got != expected:
                raise AssertionError(f"{name}: expected {expected}, got {got!r}")
        # Repository administration is denied even on the listed write repo, and
        # the proxy denies it at the guard before any credential — so the full
        # denylist is exercised here without a token (a proxy 403, not GitHub's
        # 401/404). One representative method per sub-resource; the unit tests
        # cover the exhaustive matrix.
        admin_writes = [
            ("PUT", ""),                                   # repo resource (settings/visibility)
            ("POST", "forks"),
            ("POST", "generate"),
            ("POST", "transfer"),
            ("PUT", "collaborators/attacker"),
            ("POST", "keys"),
            ("POST", "hooks"),
            ("POST", "pages"),
            ("POST", "releases"),
            ("PUT", "environments/prod"),
            ("PUT", "actions/secrets/TOKEN"),
            ("PUT", "actions/variables/NAME"),
            ("PUT", "actions/permissions"),
            ("PUT", "actions/oidc/customization/sub"),
            ("POST", "actions/runners/registration-token"),
            ("PUT", "actions/workflows/ci.yml/disable"),
            ("POST", "dispatches"),
            ("PUT", "branches/main/protection"),
            ("POST", "statuses/abc123"),
            ("POST", "check-runs"),
            ("POST", "deployments"),
            ("POST", "actions/runs/1/approve"),
            ("DELETE", "actions/runs/1"),
            ("PATCH", "code-scanning/alerts/1"),
            ("PUT", "vulnerability-alerts"),
        ]
        for method, sub in admin_writes:
            suffix = f"/{sub}" if sub else ""
            code = self._ssh_code(
                f"{curl} -X {method} -d '{{}}' https://api.github.com/repos/infiloop2/kern{suffix} || true"
            ).strip()
            if code != "403":
                raise AssertionError(f"admin write {method} {sub or '<repo>'}: expected proxy 403, got {code!r}")
        # Real git: ls-remote of any repo rides the (now universal) read leg,
        # while a push to an unlisted repo must be denied by the proxy at the
        # receive-pack discovery leg.
        agentenv = f"sudo -u kern-agent env HTTPS_PROXY={proxy} https_proxy={proxy}"
        ls_remote = self._ssh_code(
            f"{agentenv} git ls-remote https://github.com/torvalds/linux HEAD "
            ">/dev/null 2>&1 && echo ok || echo failed"
        ).strip()
        if ls_remote != "ok":
            raise AssertionError("git ls-remote of a public repo failed through the proxy")
        workdir = "/tmp/kern-smoke-push-denial"
        self._ssh_code(f"sudo rm -rf {workdir}")
        push_denied = self._ssh_code(
            f"{agentenv} git clone --depth 1 https://github.com/infiloop2/kern {workdir} >/dev/null 2>&1 && "
            f"{agentenv} git -C {workdir} push https://github.com/torvalds/linux HEAD:refs/heads/smoke-denied >/dev/null 2>&1 "
            "&& echo pushed || echo denied"
        ).strip()
        self._ssh_code(f"sudo rm -rf {workdir}")
        if push_denied != "denied":
            raise AssertionError("git push to an unlisted repo should be denied by the proxy")
        self._ok(
            f"{len(checks)} guard-branch checks + {len(admin_writes)} admin-write denials "
            "+ git ls-remote/push-denial across the github domains"
        )

    def check_proxy_edge_cases(self) -> None:
        self._step("proxy protocol edge cases (ports, hosts, encodings, wildcards)")
        baseline = max((event["seq"] for event in self._network_events()), default=0)
        proxy = f"http://127.0.0.1:{PROXY_PORT}"
        agent = "sudo -u kern-agent env"

        # CONNECT to a non-443 port and to an unlisted host: both denied before
        # any DNS or dial; curl reports a proxy CONNECT failure (not an HTTP code).
        self._ssh_code(f"{agent} HTTPS_PROXY={proxy} curl -s -o /dev/null --max-time 12 https://example.com:444/ || true")
        self._ssh_code(f"{agent} HTTPS_PROXY={proxy} curl -s -o /dev/null --max-time 12 https://iana.org/ || true")
        # Host header that does not match the CONNECT host: denied inside TLS.
        mismatch = self._ssh_code(
            f"{agent} HTTPS_PROXY={proxy} curl -s -o /dev/null -w '%{{http_code}}' --max-time 20 "
            f"-H 'Host: evil.example' https://example.com/"
        )
        # Percent-encoded path: the guard must match the decoded form —
        # /%7A%65%6E decodes to /zen, which ^/zen$ allows, while the raw
        # encoded path matches no guard. So a 403 here means the guard failed
        # to decode. The upstream's own status is not asserted: the proxy
        # forwards the path as sent, and GitHub routes the encoded form to 404.
        encoded = self._ssh_code(
            f"{agent} HTTPS_PROXY={proxy} curl -s -o /dev/null -w '%{{http_code}}' --max-time 20 "
            f"https://example.com/%7A%65%6E || true"
        )
        # Wildcard rule (*.example.com) must admit a subdomain.
        wildcard = self._ssh_code(
            f"{agent} HTTPS_PROXY={proxy} curl -s -o /dev/null -w '%{{http_code}}' --max-time 20 "
            f"https://www.example.com/ || true"
        )
        # Plain HTTP is not supported: even a policy-allowed domain gets a
        # logged 403 (curl only honors the lowercase http_proxy variable for
        # http:// URLs).
        plain = self._ssh_code(
            f"{agent} http_proxy={proxy} curl -s -o /dev/null -w '%{{http_code}}' --max-time 20 "
            f"http://example.com/ || true"
        )

        if mismatch != "403":
            raise AssertionError(f"Host header mismatch returned {mismatch!r}, expected 403")
        if encoded in ("403", "", "000"):
            raise AssertionError(f"percent-encoded path matching a decoded guard returned {encoded!r}, expected the proxy to allow it")
        if wildcard == "403" or wildcard == "":
            raise AssertionError(f"wildcard-allowed host returned {wildcard!r}, expected non-403")
        if plain != "403":
            raise AssertionError(f"plain HTTP through the proxy returned {plain!r}, expected 403")

        events = self._network_events(since=baseline)
        reasons = {event.get("reason_code") for event in events if event["decision"] == "denied"}
        if "connect_port_denied" not in reasons:
            raise AssertionError(f"non-443 CONNECT denial not logged; denied reason codes: {reasons}")
        if "host_not_allowed" not in reasons:
            raise AssertionError(f"unknown-host denial not logged; denied reason codes: {reasons}")
        if "plain_http_denied" not in reasons:
            raise AssertionError(f"plain HTTP denial not logged; denied reason codes: {reasons}")
        if not any(event["host"] == "www.example.com" and event["decision"] == "allowed" for event in events):
            raise AssertionError("wildcard-matched request did not produce an allowed event")
        if not any(event["path"] == "/%7A%65%6E" and event["decision"] == "allowed" for event in events):
            raise AssertionError("percent-encoded request was not logged as an allowed decision")
        self._ok("port pin, unknown host, Host mismatch, plain HTTP denied; decoded guard and wildcard allowed")

    def check_proxy_concurrency(self) -> None:
        self._step("parallel proxy traffic with consistent event sequencing")
        self._api(
            "PUT",
            "/v1/network/policy",
            {
                "network_integrations": {
                    "custom": {"domains": {
                        "example.com": {
                            "allow_http_methods": ["GET"],
                            "path_guards": ["^/$"],
                        },
                    }},
                },
            },
        )
        for runtime in SMOKE_RUNTIMES:
            status = self._wait_for_runtime_status({"deactivated"}, runtime=runtime, timeout=60)
            if status != "deactivated":
                raise AssertionError(f"{runtime} should be deactivated before proxy concurrency check, got {status}")
        baseline = max((event["seq"] for event in self._network_events()), default=0)
        proxy = f"http://127.0.0.1:{PROXY_PORT}"
        curl = "curl -s -o /dev/null -w '%{http_code}\\n' --max-time 25"
        script = " ".join(
            [f"{curl} https://example.com/ &" for _ in range(6)]
            + [f"{curl} https://example.com/denied-{index} &" for index in range(6)]
            + ["wait"]
        )
        codes = self._ssh_code(f'sudo -u kern-agent env HTTPS_PROXY={proxy} bash -c "{script}"')
        lines = [line.strip() for line in codes.splitlines() if line.strip()]
        if len(lines) != 12:
            raise AssertionError(f"expected 12 parallel responses, got {len(lines)}: {lines}")
        if lines.count("403") != 6:
            raise AssertionError(f"expected exactly 6 denied responses, got {lines}")

        # Every one of the 12 decisions must be logged with a unique seq: lost
        # or duplicated entries under parallel load mean the proxy's event
        # serialization (in-process lock + file lock + derived seq) is broken.
        events = [event for event in self._network_events(since=baseline) if event["host"] == "example.com"]
        seqs = [int(event["seq"]) for event in events]
        if len(events) != 12 or len(set(seqs)) != 12:
            raise AssertionError(f"expected 12 uniquely-sequenced events, got {len(events)} (seqs {sorted(seqs)})")
        decisions = [event["decision"] for event in events]
        if decisions.count("allowed") != 6 or decisions.count("denied") != 6:
            raise AssertionError(f"expected 6 allowed + 6 denied events, got {decisions}")
        self._ok("12 parallel requests all decided and logged with unique, ordered seqs")

    def check_pre_login_provider_guards(self) -> None:
        self._step("all managed provider data planes fail closed before login")
        baseline = max((event["seq"] for event in self._network_events()), default=0)
        self._api(
            "PUT",
            "/v1/network/policy",
            network_policy(SMOKE_MANAGED_PROVIDERS),
        )
        proxy = f"http://127.0.0.1:{PROXY_PORT}"

        openai_url = "https://chatgpt.com/backend-api/codex/responses"
        openai_payload = '{"input":"hello"}'
        print(f"  POST {openai_url} before account id is known", flush=True)
        openai_response = self._ssh_code(
            f"sudo -u kern-agent env HTTPS_PROXY={proxy} "
            f"curl -s --max-time 20 -X POST -H 'Content-Type: application/json' "
            f"--data {shlex.quote(openai_payload)} {shlex.quote(openai_url)}"
        )
        print(f"  -> {openai_response[:200]!r}", flush=True)
        if "openai_account_unavailable" not in openai_response:
            raise AssertionError(f"OpenAI data-plane request did not fail closed; proxy returned {openai_response!r}")

        claude_hello = self._ssh_code(
            f"sudo -u kern-agent env HTTPS_PROXY={proxy} "
            "curl -s -o /dev/null -w '%{http_code}' --max-time 20 "
            "https://api.anthropic.com/api/hello"
        )
        if claude_hello == "403" or claude_hello == "000" or claude_hello == "":
            raise AssertionError(f"Claude unauthenticated readiness path returned {claude_hello!r}, expected proxy allow")

        claude_url = "https://api.anthropic.com/v1/messages"
        claude_payload = '{"model":"claude-sonnet-4-5","max_tokens":8,"messages":[{"role":"user","content":"hello"}]}'
        print(f"  POST {claude_url} before Claude account identity is known", flush=True)
        claude_response = self._ssh_code(
            f"sudo -u kern-agent env HTTPS_PROXY={proxy} "
            f"curl -s --max-time 20 -X POST -H 'Content-Type: application/json' "
            f"--data {shlex.quote(claude_payload)} {shlex.quote(claude_url)}"
        )
        print(f"  -> {claude_response[:200]!r}", flush=True)
        if "anthropic_account_unavailable" not in claude_response:
            raise AssertionError(f"Anthropic API request did not fail closed before login; proxy returned {claude_response!r}")

        def post_bedrock(
            *,
            region: str,
            access_key_id: str,
            query: str = "",
            session_token: bool = False,
        ) -> str:
            url = (
                f"https://bedrock-runtime.{region}.amazonaws.com/model/"
                f"deepseek.v3.2/converse{query}"
            )
            authorization = (
                "AWS4-HMAC-SHA256 "
                f"Credential={access_key_id}/20260718/{region}/bedrock/aws4_request, "
                "SignedHeaders=content-type;host;x-amz-date, "
                f"Signature={'0' * 64}"
            )
            token_header = " -H 'X-Amz-Security-Token: smuggled-session-token'" if session_token else ""
            return self._ssh_code(
                f"sudo -u kern-agent env HTTPS_PROXY={proxy} "
                "curl -s --max-time 20 -X POST -H 'Content-Type: application/json' "
                "-H 'X-Amz-Date: 20260718T000000Z' "
                f"-H {shlex.quote(f'Authorization: {authorization}')}"
                f"{token_header} --data '{{\"messages\":[]}}' {shlex.quote(url)}"
            )

        bedrock_cases = (
            (
                "hermes-no-credential",
                post_bedrock(
                    region=SMOKE_BEDROCK_REGION,
                    access_key_id=ROUTING_ACCESS_KEY_ID,
                ),
                "bedrock_credentials_unavailable",
            ),
            (
                "foreign-aws-key",
                post_bedrock(
                    region=SMOKE_BEDROCK_REGION,
                    access_key_id="AKIA0000000000000000",
                ),
                "bedrock_access_key_mismatch",
            ),
            (
                "presigned-query",
                post_bedrock(
                    region=SMOKE_BEDROCK_REGION,
                    access_key_id=ROUTING_ACCESS_KEY_ID,
                    query="?X-Amz-Credential=smuggled",
                ),
                "bedrock_query_auth_denied",
            ),
            (
                "session-credential",
                post_bedrock(
                    region=SMOKE_BEDROCK_REGION,
                    access_key_id=ROUTING_ACCESS_KEY_ID,
                    session_token=True,
                ),
                "bedrock_session_credentials_denied",
            ),
        )
        for label, response, expected_reason in bedrock_cases:
            if expected_reason not in response:
                raise AssertionError(
                    f"{label} should fail with {expected_reason}; proxy returned {response!r}"
                )

        # Without a stored credential there is no selected region to enforce
        # at CONNECT time. The supported Bedrock host reaches the request
        # guard, which fails closed before any upstream AWS connection.
        post_bedrock(region="us-west-2", access_key_id=ROUTING_ACCESS_KEY_ID)

        events = self._network_events(since=baseline)
        if not any(
            event["host"] == "chatgpt.com"
            and event["decision"] == "denied"
            and event.get("reason_code") == "openai_account_unavailable"
            for event in events
        ):
            raise AssertionError("no account-id-missing chatgpt.com network denial was logged")
        if not any(
            event["host"] == "api.anthropic.com"
            and event["decision"] == "denied"
            and event.get("reason_code") == "anthropic_account_unavailable"
            for event in events
        ):
            raise AssertionError("no account-missing api.anthropic.com network denial was logged")
        if not any(
            event["host"] == "api.anthropic.com"
            and event["path"] == "/api/hello"
            and event["decision"] == "allowed"
            for event in events
        ):
            raise AssertionError("Claude unauthenticated readiness request was not logged as allowed")
        for expected_reason in {
            "bedrock_credentials_unavailable",
            "bedrock_access_key_mismatch",
            "bedrock_query_auth_denied",
            "bedrock_session_credentials_denied",
        }:
            if not any(event.get("reason_code") == expected_reason for event in events):
                raise AssertionError(f"no live Bedrock denial was logged for {expected_reason}")
        if not any(
            event["host"] == "bedrock-runtime.us-west-2.amazonaws.com"
            and event["method"] == "POST"
            and event["decision"] == "denied"
            and event.get("reason_code") == "bedrock_credentials_unavailable"
            for event in events
        ):
            raise AssertionError("no cross-region Bedrock request reached the local missing-credential denial")
        self._ok(
            "OpenAI and Claude failed closed before login; Hermes routing identity, regions, "
            "query auth, session credentials, and missing proxy credentials all denied live"
        )

    def check_precredential_bedrock_harness_launchers(self) -> None:
        """Start Hermes without a working AWS credential.

        This is the deepest fail-closed fresh-host probe available without a
        paid provider call: the real admin sudo path, systemd scope, package,
        harness config, stdin protocol, dummy SDK identity, CA, and proxy all
        run. The proxy must stop the request locally before an AWS connection
        because no re-signing credential exists.
        """
        self._step("installed Hermes launcher reaches the local Bedrock credential boundary")
        self._api(
            "PUT",
            "/v1/network/policy",
            network_policy(SMOKE_MANAGED_PROVIDERS),
        )
        credential_rows = self._ssh_code(
            "sudo -u postgres psql -tA -d kern_admin -c "
            + shlex.quote("SELECT count(*) FROM bedrock_credentials")
        ).strip()
        if credential_rows != "0":
            raise AssertionError(
                "credential-free launcher probes require no stored Bedrock credential; "
                f"found {credential_rows}"
            )

        probes = (
            (
                "hermes",
                "smoke-hermes-launch",
                "qwen.qwen3-coder-next",
                f"printf %s {shlex.quote('Reply with exactly OK.')} | "
                "sudo -u kern-admin -- timeout 90 sudo -n "
                "/usr/local/lib/kern-host/run-hermes "
                f"region={SMOKE_BEDROCK_REGION} --thread-scope smoke-hermes-launch "
                "--model qwen.qwen3-coder-next 2>&1 | tail -c 4000",
            ),
        )
        for runtime, thread_scope, model, command in probes:
            baseline = max((event["seq"] for event in self._network_events()), default=0)
            output = self._ssh_code(command)
            expected_path = f"/model/{model}/"
            expected_host = f"bedrock-runtime.{SMOKE_BEDROCK_REGION}.amazonaws.com"

            def reached_missing_credential_boundary(events: list[dict]) -> bool:
                return any(
                    event["host"] == expected_host
                    and expected_path in event.get("path", "")
                    and event["decision"] == "denied"
                    and event.get("reason_code") == "bedrock_credentials_unavailable"
                    for event in events
                )

            # Allow the real event log to catch up before stopping the scope
            # and judging the probe.
            events: list[dict] = []
            for _ in range(15):
                events = self._network_events(since=baseline)
                if reached_missing_credential_boundary(events):
                    break
                time.sleep(1)
            # A timeout or client retry must not leave a named harness scope
            # behind. Stopping an already collected scope is a harmless no-op.
            self._ssh_code(
                f"sudo systemctl stop kern-agent-thread-{thread_scope}.scope "
                ">/dev/null 2>&1 || true"
            )
            if not reached_missing_credential_boundary(events):
                raise AssertionError(
                    f"installed {runtime} launcher did not reach the local missing-credential "
                    f"Bedrock boundary for {model}; output={output!r}, events={events}"
                )

        final_rows = self._ssh_code(
            "sudo -u postgres psql -tA -d kern_admin -c "
            + shlex.quote("SELECT count(*) FROM bedrock_credentials")
        ).strip()
        if final_rows != "0":
            raise AssertionError(f"launcher probes changed Bedrock credential state: {final_rows}")
        self._ok(
            "the real Hermes launch path reached the proxy's local missing-credential denial; "
            "no AWS credential was stored and no upstream model call was possible"
        )

    def check_installed_agent_script_launcher(self) -> None:
        """Run a real agent-home script through the installed launcher.

        The script runtime needs no provider credential, so unlike the model
        runtimes its whole production path can be proven on a fresh host: the
        admin sudo entry, root's path validation, the systemd scope, the
        demotion to kern-agent, the demoted file checks, and the script's own
        output and exit status.
        """
        self._step("installed script launcher runs an agent-home script and confines its path")
        script = "/mnt/kern-agent/agent-home/kern-smoke-script.sh"
        missing = "/mnt/kern-agent/agent-home/kern-smoke-absent.sh"
        self._ssh_code(
            f"sudo -u kern-agent tee {shlex.quote(script)} >/dev/null <<'KERNSMOKE'\n"
            "echo kern-smoke-script-ok\n"
            "id -un\n"
            "KERNSMOKE"
        )
        try:
            output = self._ssh_code(
                "sudo -u kern-admin -- timeout 60 sudo -n "
                "/usr/local/lib/kern-host/run-agent-script "
                f"--thread-scope smoke-agent-script {shlex.quote(script)} 2>&1 | tail -c 400"
            )
            if "kern-smoke-script-ok" not in output:
                raise AssertionError(
                    f"installed script launcher did not run the script; output={output!r}"
                )
            if "kern-agent" not in output:
                raise AssertionError(
                    f"the script did not run as the agent user; output={output!r}"
                )

            # Root's spelling check and the demoted side's file check, with
            # their distinct exit statuses: a usage rejection never becomes a
            # process, and a missing script is only discovered as kern-agent.
            probes = (
                ("/etc/hostname", 64),
                ("/mnt/kern-agent/agent-home/../../etc/hostname", 64),
                ("/mnt/kern-agent/agent-home/kern-smoke-script.txt", 64),
                (missing, 66),
            )
            for path, expected in probes:
                status = self._ssh_code(
                    "sudo -u kern-admin -- timeout 60 sudo -n "
                    "/usr/local/lib/kern-host/run-agent-script "
                    f"{shlex.quote(path)} >/dev/null 2>&1; echo status=$?"
                ).strip()
                if status != f"status={expected}":
                    raise AssertionError(
                        f"script launcher accepted or misreported {path!r}: {status}"
                    )
        finally:
            self._ssh_code(
                "sudo systemctl stop kern-agent-thread-smoke-agent-script.scope "
                ">/dev/null 2>&1 || true"
            )
            self._ssh_code(f"sudo -u kern-agent rm -f {shlex.quote(script)}")
        self._ok(
            "the real script launch path ran an agent-home script as kern-agent and "
            "refused paths outside the agent home"
        )

    def check_tools_surface(self) -> None:
        """Every bundled action on a fresh host with no tool configuration.

        Credentialed actions and OAuth starts must fail closed; all six public
        Polymarket reads must execute. The same pass covers MCP discovery,
        local image/video upload, exact audit arguments, approvals, peer
        credentials, and the tools-service-only egress boundary.
        """
        self._step("bundled tools: listing, enablement, agent shim, approvals surface")
        listing = self._api("GET", "/v1/tools")
        tool_ids = sorted(entry["tool_id"] for entry in listing["tools"])
        expected_tool_ids = sorted(BUNDLED_TOOLS)
        if tool_ids != expected_tool_ids:
            raise AssertionError(f"unexpected bundled tools: {tool_ids}")
        gmail = next(entry for entry in listing["tools"] if entry["tool_id"] == "gmail")
        if gmail["enabled"] or gmail["connection_status"] != {"connected": False}:
            raise AssertionError(f"gmail must start disabled and disconnected: {gmail}")

        # Enablement is not gated on config: enabling before the key is set succeeds.
        enabled = self._api("POST", "/v1/tools/brave_search/enable", {})
        if enabled != {"tool_id": "brave_search", "enabled": True}:
            raise AssertionError(f"brave_search enable without config should succeed: {enabled}")

        # The agent-facing path end to end: the MCP shim runs as
        # kern-agent and reaches the tools socket by peer credentials.
        shim_command = (
            "sudo -u kern-agent env PYTHONPATH=/opt/kern-host "
            "python3 -m host.runtime.agent_shim.mcp_shim"
        )
        next_request_id = 10

        def shim_tool_call(name: str, arguments: dict) -> tuple[dict, object]:
            nonlocal next_request_id
            request = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": next_request_id,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                }
            )
            next_request_id += 1
            response_text = self._ssh_code(
                f"printf '%s\\n' {shlex.quote(request)} | {shim_command}"
            )
            rpc = json.loads(response_text)
            result = rpc.get("result")
            if not isinstance(result, dict):
                raise AssertionError(f"{name} returned an invalid MCP response: {rpc}")
            content = result.get("content")
            text = content[0].get("text", "") if isinstance(content, list) and content and isinstance(content[0], dict) else ""
            parsed: object = None
            if not result.get("isError"):
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError as exc:
                    raise AssertionError(f"{name} returned non-JSON success text: {text!r}") from exc
            return result, parsed

        def shim_bundled_call(tool_id: str, action_id: str, arguments: dict) -> tuple[dict, object]:
            """Invoke a bundled action the way the agent now does."""
            return shim_tool_call(
                "call_tool",
                {"tool_id": tool_id, "action_id": action_id, "input": arguments},
            )

        list_request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        shim_listing = self._ssh_code(f"printf '%s\\n' {shlex.quote(list_request)} | {shim_command}")
        print(f"  shim tools/list -> {shim_listing[:200]!r}", flush=True)
        for expected in ("list_bundled_tools", "describe_tool", "call_tool", "check_tool_approval"):
            if expected not in shim_listing:
                raise AssertionError(f"MCP shim listing missing {expected}: {shim_listing!r}")
        if "list_network_integrations" not in shim_listing or "recent_network_denials" not in shim_listing:
            raise AssertionError(f"MCP shim listing missing network introspection: {shim_listing!r}")
        if "search_conversation_history" not in shim_listing or "read_thread_history" not in shim_listing:
            raise AssertionError(
                f"MCP shim listing missing typed conversation history tools: {shim_listing!r}"
            )
        # The listing never enumerates the catalog, enabled or not: it heads the
        # model prompt and must be identical for every session.
        if "brave_search_search_web" in shim_listing or "gmail_search_messages" in shim_listing:
            raise AssertionError(f"MCP shim enumerated bundled actions: {shim_listing!r}")

        # A fresh host has no retained messages, but both typed history tools
        # must still traverse the deployed shim -> Workspace socket -> admin
        # API path and return their bounded, explicitly untrusted contract.
        search_result, empty_search = shim_tool_call(
            "search_conversation_history", {"query": "fresh smoke absent phrase"}
        )
        expected_history_metadata = {
            "provenance": "retained_conversation_history",
            "trust": "untrusted",
            "instruction_authority": "none",
        }
        if search_result.get("isError") or not isinstance(empty_search, dict):
            raise AssertionError(
                f"fresh conversation search failed through the MCP path: {search_result}"
            )
        if any(
            empty_search.get(key) != value
            for key, value in expected_history_metadata.items()
        ) or empty_search.get("matches") != [] or empty_search.get("next_cursor") is not None:
            raise AssertionError(
                f"fresh conversation search returned an invalid contract: {empty_search}"
            )
        missing_thread_id = "thread-fresh-smoke-missing-thread"
        read_result, empty_read = shim_tool_call(
            "read_thread_history", {"thread_id": missing_thread_id}
        )
        if read_result.get("isError") or not isinstance(empty_read, dict):
            raise AssertionError(
                f"fresh conversation read failed through the MCP path: {read_result}"
            )
        if any(
            empty_read.get(key) != value
            for key, value in expected_history_metadata.items()
        ) or empty_read.get("events") != [] or empty_read.get("thread") != {
            "thread_id": missing_thread_id
        }:
            raise AssertionError(
                f"fresh conversation read returned an invalid contract: {empty_read}"
            )
        hostile_result, _ = shim_tool_call(
            "search_conversation_history", {"query": "x" * 513}
        )
        hostile_content = hostile_result.get("content")
        hostile_text = (
            hostile_content[0].get("text", "")
            if isinstance(hostile_content, list)
            and hostile_content
            and isinstance(hostile_content[0], dict)
            else ""
        )
        if not hostile_result.get("isError") or "at most 512 UTF-8 bytes" not in hostile_text:
            raise AssertionError(
                f"oversized conversation search did not fail closed: {hostile_result}"
            )

        network_result, network_listing = shim_tool_call("list_network_integrations", {})
        if network_result.get("isError") or not isinstance(network_listing, dict):
            raise AssertionError(
                f"list_network_integrations failed through the agent-network service: {network_result}"
            )
        listed_integrations = {
            entry.get("integration_id"): entry
            for entry in network_listing.get("network_integrations", [])
            if isinstance(entry, dict)
        }
        entry = listed_integrations.get("bedrock")
        if not isinstance(entry, dict) or entry.get("enabled") is not True:
            raise AssertionError(f"agent-network introspection omitted active Bedrock: {entry}")
        # Region is part of the encrypted credential connection, not the
        # enablement-only network policy. A fresh smoke host has no credential,
        # so the agent-facing policy introspection must not expose a region.
        if "region" in (entry.get("options") or {}):
            raise AssertionError(f"agent-network introspection exposed unconnected Bedrock region: {entry}")

        # The catalog built-in shows disabled tools too, so the agent can ask
        # the operator to enable an existing tool instead of rebuilding it.
        catalog_request = json.dumps(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "list_bundled_tools", "arguments": {}}}
        )
        catalog_response = self._ssh_code(f"printf '%s\\n' {shlex.quote(catalog_request)} | {shim_command}")
        print(f"  shim list_bundled_tools -> {catalog_response[:200]!r}", flush=True)
        if '"isError": true' in catalog_response:
            raise AssertionError(f"list_bundled_tools failed: {catalog_response!r}")
        # The result text is JSON-escaped inside the MCP content; parse it back
        # out and assert the disabled gmail tool appears with enabled false.
        catalog_text = json.loads(catalog_response)["result"]["content"][0]["text"]
        catalog_tools = {entry["tool_id"]: entry for entry in json.loads(catalog_text)["tools"]}
        if not catalog_tools["brave_search"]["enabled"]:
            raise AssertionError(f"catalog must show brave_search enabled: {catalog_text!r}")
        if catalog_tools["gmail"]["enabled"]:
            raise AssertionError(f"catalog must show gmail disabled: {catalog_text!r}")

        # The egress boundary is split: the tools service holds internet egress,
        # the admin service holds none. nftables drops the admin uid's outbound
        # TCP while the tools uid reaches 443 (to a raw IP, so no DNS is needed).
        admin_egress = self._ssh_code(
            "sudo -u kern-admin timeout 6 bash -c 'exec 3<>/dev/tcp/1.1.1.1/443' 2>&1 "
            "&& echo TC_OPEN || echo TC_BLOCKED"
        )
        if "TC_BLOCKED" not in admin_egress:
            raise AssertionError(f"admin uid must have no internet egress: {admin_egress!r}")
        tools_egress = self._ssh_code(
            "sudo -u kern-tools timeout 6 bash -c 'exec 3<>/dev/tcp/1.1.1.1/443' 2>&1 "
            "&& echo TC_OPEN || echo TC_BLOCKED"
        )
        if "TC_OPEN" not in tools_egress:
            raise AssertionError(f"tools uid must reach the internet for tool APIs: {tools_egress!r}")
        network_egress = self._ssh_code(
            "sudo -u kern-agent-network timeout 6 bash -c 'exec 3<>/dev/tcp/1.1.1.1/443' 2>&1 "
            "&& echo TC_OPEN || echo TC_BLOCKED"
        )
        if "TC_BLOCKED" not in network_egress:
            raise AssertionError(
                f"agent-network uid must have no internet egress: {network_egress!r}"
            )
        network_proxy_loopback = self._ssh_code(
            f"sudo -u kern-agent-network timeout 6 bash -c "
            f"'exec 3<>/dev/tcp/127.0.0.1/{PROXY_PORT}' 2>&1 "
            "&& echo TC_OPEN || echo TC_BLOCKED"
        )
        if "TC_BLOCKED" not in network_proxy_loopback:
            raise AssertionError(
                "agent-network uid must not reach the loopback policy proxy: "
                f"{network_proxy_loopback!r}"
            )

        # The agent-facing tools socket is owned by the dedicated tools service.
        service_active = self._ssh_code("systemctl is-active kern-tools.service 2>&1 || true")
        if service_active.strip() != "active":
            raise AssertionError(f"kern-tools.service must be active: {service_active!r}")
        socket_owner = self._ssh_code("stat -c '%U' /run/kern-tools/tools.sock 2>&1 || true")
        if "kern-tools" not in socket_owner:
            raise AssertionError(f"tools socket must be owned by kern-tools: {socket_owner!r}")
        network_service = self._ssh_code(
            "systemctl is-active kern-agent-network.service 2>&1 || true"
        )
        if network_service.strip() != "active":
            raise AssertionError(
                f"kern-agent-network.service must be active: {network_service!r}"
            )
        network_socket_owner = self._ssh_code(
            "stat -c '%U' /run/kern-agent-network/agent-network.sock 2>&1 || true"
        )
        if "kern-agent-network" not in network_socket_owner:
            raise AssertionError(
                f"network socket must be owned by kern-agent-network: {network_socket_owner!r}"
            )

        # Peers are scoped strictly by path: other service users are rejected
        # outright, and even the admin uid is rejected on the agent MCP routes
        # (it holds only the /operator/... delegation routes).
        probe_script = (
            "from host.runtime.agent_shim.mcp_shim import UnixHTTPConnection; "
            "c = UnixHTTPConnection('/run/kern-tools/tools.sock'); "
            "c.request('GET', '/tools'); print(c.getresponse().status)"
        )
        for probe_user in ("kern-proxy", "kern-admin"):
            peer_probe = self._ssh_code(
                f"sudo -u {probe_user} env PYTHONPATH=/opt/kern-host "
                f"python3 -c {shlex.quote(probe_script)}"
            )
            if peer_probe.strip() != "403":
                raise AssertionError(
                    f"tools socket must reject {probe_user} on agent routes, got {peer_probe!r}"
                )

        network_probe_script = (
            "from host.runtime.agent_shim.mcp_shim import UnixHTTPConnection; "
            "c = UnixHTTPConnection('/run/kern-agent-network/agent-network.sock'); "
            "c.request('GET', '/tools'); print(c.getresponse().status)"
        )
        for probe_user in ("kern-tools", "kern-proxy", "kern-admin"):
            peer_probe = self._ssh_code(
                f"sudo -u {probe_user} env PYTHONPATH=/opt/kern-host "
                f"python3 -c {shlex.quote(network_probe_script)}"
            )
            if peer_probe.strip() != "403":
                raise AssertionError(
                    f"network socket must reject {probe_user}, got {peer_probe!r}"
                )

        network_roles = self._ssh_code(
            "sudo -u kern-agent-network psql -tA -d kern_admin "
            "-c 'SELECT count(*) >= 0 FROM network_events' && "
            "sudo -u kern-agent-network bash -c "
            "'! psql -tA -d kern_admin -c \"DELETE FROM network_events\" 2>/dev/null' && "
            "sudo -u kern-agent-network bash -c "
            "'! psql -tA -d kern_admin -c \"SELECT count(*) FROM tool_events\" 2>/dev/null' && "
            "sudo -u kern-tools bash -c "
            "'! psql -tA -d kern_admin -c \"SELECT count(*) FROM network_events\" 2>/dev/null' && "
            "echo ok"
        ).strip().splitlines()
        if network_roles != ["t", "ok"]:
            raise AssertionError(
                f"network/tools database roles are not isolated: {network_roles}"
            )

        # The fresh host starts with no approvals or tool config whatsoever.
        approvals = self._api("GET", "/v1/tools/gmail/approvals")
        if approvals["approvals"]:
            raise AssertionError(f"expected no approvals on a fresh host: {approvals}")
        status, body = self._api_status("POST", "/v1/tools/gmail/approvals/approval_1/approve", {})
        if status != 404:
            raise AssertionError(f"deciding a missing approval must 404, got {status} {body}")

        # Enable every package without setting even a dummy value. OAuth starts
        # must fail locally on the absent client config; they never contact the
        # provider or create a connection.
        for tool_id in BUNDLED_TOOLS:
            self._api("POST", f"/v1/tools/{tool_id}/enable", {})
        empty_config_listing = self._api("GET", "/v1/tools")["tools"]
        for entry in empty_config_listing:
            configured = [item["key"] for item in entry.get("config", []) if item.get("set")]
            if configured:
                raise AssertionError(
                    f"fresh smoke must not configure {entry['tool_id']}, found {configured}"
                )
            if entry.get("connection") == "oauth":
                if (entry.get("connection_status") or {}).get("connected") is True:
                    raise AssertionError(f"fresh smoke unexpectedly connected {entry['tool_id']}: {entry}")
                status, body = self._api_status(
                    "POST",
                    f"/v1/tools/{entry['tool_id']}/oauth_connect/start",
                    {"redirect_uri": f"http://127.0.0.1:{ADMIN_PORT}/oauth/callback"},
                )
                if status != 400 or "not set" not in str(body).lower():
                    raise AssertionError(
                        f"{entry['tool_id']} OAuth start without config must fail locally: {status} {body}"
                    )

        all_listed = self._ssh_code(f"printf '%s\\n' {shlex.quote(list_request)} | {shim_command}")
        all_tool_names = {
            entry["name"] for entry in json.loads(all_listed)["result"]["tools"]
        }
        # Enabling every bundled tool must not change the listing: that
        # invariant is the whole point of the static surface.
        if all_tool_names != set(STATIC_SHIM_TOOLS):
            raise AssertionError(
                f"MCP shim listing moved with enablement: {sorted(all_tool_names)}"
            )
        # Every bundled action must instead be reachable through discovery.
        describe_requests = [
            shlex.quote(json.dumps({
                "jsonrpc": "2.0",
                "id": index,
                "method": "tools/call",
                "params": {"name": "describe_tool", "arguments": {"tool_id": tool_id}},
            }))
            for index, tool_id in enumerate(BUNDLED_TOOLS, start=1)
        ]
        described = self._ssh_code(
            f"printf '%s\\n' {' '.join(describe_requests)} | {shim_command}"
        )
        described_actions: set[str] = set()
        for line in described.splitlines():
            if not line.strip():
                continue
            response = json.loads(line)
            if response["result"]["isError"]:
                raise AssertionError(f"describe_tool failed: {line!r}")
            body = json.loads(response["result"]["content"][0]["text"])
            described_actions.update(
                f"{body['tool_id']}_{action['id']}" for action in body["actions"]
            )
        expected_actions = {
            f"{tool_id}_{action.id}"
            for tool_id, tool in BUNDLED_TOOLS.items()
            for action in tool.manifest.actions
        }
        missing_actions = expected_actions - described_actions
        if missing_actions:
            raise AssertionError(f"describe_tool omitted bundled actions: {sorted(missing_actions)}")

        # Exercise both local media uploads without provider config. The files
        # live in the agent workspace, are opened by the agent-side shim, and
        # are removed immediately after the private tool-scoped copies exist.
        media_root = "/mnt/kern-agent/agent-home"
        image_path = "/kern-smoke.png"
        video_path = "/kern-smoke.mp4"
        image_local = f"{media_root}{image_path}"
        video_local = f"{media_root}{video_path}"
        create_media = (
            "umask 077; "
            f"dd if=/dev/zero of={shlex.quote(image_local)} bs=512 count=1 status=none; "
            f"dd if=/dev/zero of={shlex.quote(video_local)} bs=512 count=1 status=none"
        )
        self._ssh_code(f"sudo -u kern-agent sh -c {shlex.quote(create_media)}")
        try:
            _, image_stage = shim_tool_call(
                "stage_image", {"path": image_path, "for_tool": "runway"}
            )
            _, runway_video_stage = shim_tool_call(
                "stage_video", {"path": video_path, "for_tool": "runway"}
            )
            _, instagram_video_stage = shim_tool_call(
                "stage_video", {"path": video_path, "for_tool": "instagram"}
            )
        finally:
            self._ssh_code(
                "sudo -u kern-agent rm -f "
                f"{shlex.quote(image_local)} {shlex.quote(video_local)}"
            )
        if (
            not isinstance(image_stage, dict)
            or not isinstance(runway_video_stage, dict)
            or not isinstance(instagram_video_stage, dict)
        ):
            raise AssertionError("local media staging returned an invalid result")
        asset_ids = {
            "$RUNWAY_IMAGE": image_stage.get("image_asset_id"),
            "$RUNWAY_VIDEO": runway_video_stage.get("video_asset_id"),
            "$INSTAGRAM_VIDEO": instagram_video_stage.get("video_asset_id"),
        }
        if not all(isinstance(value, str) and value for value in asset_ids.values()):
            raise AssertionError(f"local media staging returned missing asset ids: {asset_ids}")

        spool = "/mnt/kern-admin/tools-state/assets"
        spool_mode = self._ssh_code(f"sudo stat -c '%U:%G:%a' {spool}")
        if spool_mode.strip() != "kern-tools:kern-tools:700":
            raise AssertionError(f"tool asset spool has unsafe ownership or mode: {spool_mode!r}")

        for label, asset_id in asset_ids.items():
            if not isinstance(asset_id, str):
                raise AssertionError(f"local media staging returned invalid {label}: {asset_id!r}")
            asset_stat = self._ssh_code(
                f"sudo stat -c '%U:%G:%a:%s' {shlex.quote(f'{spool}/{asset_id}')}"
            )
            if asset_stat.strip() != "kern-tools:kern-tools:600:512":
                raise AssertionError(
                    f"staged media is not private on the admin volume: {asset_stat!r}"
                )

        # Invoke every declared action. Credentialed packages must fail closed
        # on absent config/connection, while the public Polymarket package must
        # execute. Static Polymarket listing supplies ids for its three
        # dependent reads below.
        triggered_actions: set[str] = set()
        public_results: dict[str, dict] = {}
        for tool_id, calls in SMOKE_TOOL_CALLS.items():
            for action_id, arguments_template in calls:
                arguments = {
                    key: asset_ids.get(value, value) if isinstance(value, str) else value
                    for key, value in arguments_template.items()
                }
                name = f"{tool_id}_{action_id}"
                response, parsed = shim_bundled_call(tool_id, action_id, arguments)
                # Polymarket and Web Fetch need no credential or config, so
                # they must execute on the fresh host rather than fail closed.
                if tool_id in ("polymarket", "web_fetch"):
                    if response.get("isError") or not isinstance(parsed, dict):
                        raise AssertionError(f"credential-free {name} failed: {response} {parsed}")
                    public_results[action_id] = parsed
                else:
                    content = response.get("content") or [{}]
                    text = str(content[0].get("text", "")) if isinstance(content[0], dict) else ""
                    if not response.get("isError") or not any(
                        phrase in text.lower() for phrase in ("not set", "not connected", "reconnect")
                    ):
                        raise AssertionError(
                            f"{name} without config/connection did not fail closed: {response}"
                        )
                triggered_actions.add(name)

        markets = public_results.get("list_markets", {}).get("markets")
        market = next(
            (
                item for item in markets if isinstance(item, dict)
                and item.get("id") and item.get("clob_token_ids")
            ),
            None,
        ) if isinstance(markets, list) else None
        if not isinstance(market, dict):
            raise AssertionError(f"Polymarket listing returned no usable active market: {markets}")
        try:
            token_ids = json.loads(str(market["clob_token_ids"]))
        except (json.JSONDecodeError, KeyError) as exc:
            raise AssertionError(f"Polymarket returned invalid token ids: {market}") from exc
        token_id = next(
            (value for value in token_ids if isinstance(value, str) and value.isdigit()),
            None,
        ) if isinstance(token_ids, list) else None
        if token_id is None:
            raise AssertionError(f"Polymarket returned no decimal outcome token id: {market}")
        dependent_public_calls = (
            ("get_market", {"market_id": market["id"]}),
            ("get_order_book", {"token_id": token_id}),
            ("price_history", {"token_id": token_id, "interval": "1d"}),
        )
        for action_id, arguments in dependent_public_calls:
            name = f"polymarket_{action_id}"
            response, parsed = shim_bundled_call("polymarket", action_id, arguments)
            if response.get("isError") or not isinstance(parsed, dict):
                raise AssertionError(f"credential-free {name} failed: {response} {parsed}")
            triggered_actions.add(name)

        if triggered_actions != expected_actions:
            raise AssertionError(
                "fresh smoke did not trigger every bundled action: "
                f"missing={sorted(expected_actions - triggered_actions)}, "
                f"extra={sorted(triggered_actions - expected_actions)}"
            )

        # Every action call, including local failures, is recorded with
        # expandable exact arguments in the tool audit log.
        events = self._api("GET", "/v1/tools/events?limit=100")["events"]
        action_events = {
            f"{event['tool_id']}_{event['action_id']}": event
            for event in events
            if f"{event['tool_id']}_{event['action_id']}" in expected_actions
        }
        if set(action_events) != expected_actions:
            raise AssertionError(
                f"tool audit log missed actions: {sorted(expected_actions - set(action_events))}"
            )
        if not all(event.get("has_arguments") is True for event in action_events.values()):
            raise AssertionError("tool audit log did not mark every action's arguments as expandable")
        brave_event = action_events["brave_search_search_web"]
        brave_detail = self._api("GET", f"/v1/tools/events/{brave_event['seq']}")["event"]
        if brave_detail.get("arguments") != {"query": "Kern"}:
            raise AssertionError(f"tool audit detail lost exact arguments: {brave_detail}")

        for tool_id in BUNDLED_TOOLS:
            pending = self._api("GET", f"/v1/tools/{tool_id}/approvals")["approvals"]
            if pending:
                raise AssertionError(f"{tool_id} queued an approval without credentials: {pending}")

        for tool_id in BUNDLED_TOOLS:
            self._api("POST", f"/v1/tools/{tool_id}/disable", {})
        self._ok(
            "every bundled action discoverable, triggered, and audited with no tool config; OAuth starts and "
            "credentialed actions failed closed, all public Polymarket reads completed, local image/video "
            "uploads worked, non-agent peers were rejected, and no approval was queued"
        )

    def check_all_runtimes_active(self) -> None:
        self._step("all three agent runtimes active together")
        statuses = {}
        for runtime in SMOKE_RUNTIMES:
            statuses[runtime] = self._wait_for_runtime_status({"active"}, runtime=runtime, timeout=120)
        if statuses != {runtime: "active" for runtime in SMOKE_RUNTIMES}:
            raise AssertionError(f"all three runtimes should be active before mixed turns: {statuses}")
        accounts = {runtime: self._agent_account(runtime) for runtime in SMOKE_RUNTIMES}
        for runtime, account in accounts.items():
            if account.get("status") != "active" or not account.get("account_id"):
                raise AssertionError(f"{runtime} account should be active before mixed turns: {account}")
            self._assert_provider_metadata(runtime, account)
        bedrock_keys = self._ssh_code(
            "sudo -u postgres psql -tA -d kern_admin -c "
            "\"SELECT count(*) || ':' || count(DISTINCT access_key_id) FROM bedrock_credentials\""
        ).strip()
        if bedrock_keys != "1:1":
            raise AssertionError(f"Hermes must have one durable IAM access key in stage: {bedrock_keys}")
        self._assert_provider_account_anchors(live_pins=True)
        self._ok("Codex, Claude Code, and Hermes are active together with account metadata available")

    def check_runtime_deactivation_stops_running_turns(self) -> None:
        self._step("runtime deactivation closes running turns for all three harnesses")
        specs = [
            ("codex", "smoke-deactivate-codex", "CODEX_SHOULD_NOT_FINISH"),
            ("claude_code", "smoke-deactivate-claude", "CLAUDE_SHOULD_NOT_FINISH"),
            ("hermes", "smoke-deactivate-hermes", "HERMES_SHOULD_NOT_FINISH"),
        ]
        turns: dict[str, tuple[str, int]] = {}
        for runtime, thread_id, token in specs:
            baseline = self._latest_thread_event_seq(thread_id)
            started = self.send_message(
                thread_id,
                (
                    "Use the terminal tool to run `sleep 300` now. Only after that command exits, "
                    f"reply with exactly the word {token} and nothing else."
                ),
                runtime=runtime,
            )
            if started.get("status") != "accepted":
                raise AssertionError(f"{runtime} deactivation target was not started: {started}")
            turns[thread_id] = (runtime, baseline)
        for thread_id, (runtime, baseline) in turns.items():
            self._wait_for_turn_activity(thread_id, since=baseline, timeout=180)

        self._api("PUT", "/v1/network/policy", {"network_integrations": {}})
        for runtime in SMOKE_RUNTIMES:
            status = self._wait_for_runtime_status({"deactivated"}, runtime=runtime, timeout=90)
            if status != "deactivated":
                raise AssertionError(f"{runtime} did not deactivate after provider disable: {status}")
        self._assert_provider_account_anchors(live_pins=False)
        bedrock_rows = self._ssh_code(
            "sudo -u postgres psql -tA -d kern_admin -c "
            "'SELECT count(*) FROM bedrock_credentials'"
        ).strip()
        if bedrock_rows != "1":
            raise AssertionError(
                "Bedrock deactivation must preserve the one validated credential row: "
                f"{bedrock_rows}"
            )
        for thread_id, (runtime, baseline) in turns.items():
            done = self._wait_for_turn(thread_id, since=baseline, timeout=90)
            if done["status"] != "failed":
                raise AssertionError(f"{runtime} running turn was not failed by deactivation: {done}")
            if "deactivated" not in (done.get("error_message") or ""):
                raise AssertionError(f"{runtime} failed with unexpected deactivation reason: {done}")

        self._api("PUT", "/v1/network/policy", self.enforcement_policy())
        for runtime in SMOKE_RUNTIMES:
            status = self._wait_for_runtime_status({"active"}, runtime=runtime, timeout=240)
            if status != "active":
                raise AssertionError(f"{runtime} did not recover to active after provider re-enable: {status}")
        self._assert_provider_account_anchors(live_pins=True)
        bedrock_rows = self._ssh_code(
            "sudo -u postgres psql -tA -d kern_admin -c "
            "'SELECT count(*) FROM bedrock_credentials'"
        ).strip()
        if bedrock_rows != "1":
            raise AssertionError(
                f"Bedrock reactivation did not retain the validated credential: {bedrock_rows}"
            )
        self._ok("disabling providers failed running turns, closed all three runtimes, and each recovered after re-enable")

    def check_agent_parallelism(self) -> None:
        """Mixed-runtime parallelism on the live host: three Codex turns and
        three Claude Code turns run at the same time within their independent
        per-runtime admission pools, then all are steered to completion."""
        self._step("mixed OAuth harness parallelism: 3 Codex + 3 Claude turns")
        specs = [
            ("codex", "smoke-codex-par-a", "CODEX_ALPHA"),
            ("claude_code", "smoke-claude-par-a", "CLAUDE_ALPHA"),
            ("codex", "smoke-codex-par-b", "CODEX_BRAVO"),
            ("claude_code", "smoke-claude-par-b", "CLAUDE_BRAVO"),
            ("codex", "smoke-codex-par-c", "CODEX_CHARLIE"),
            ("claude_code", "smoke-claude-par-c", "CLAUDE_CHARLIE"),
        ]
        # api thread id -> (runtime, harness thread id, token, event baseline)
        created: dict[str, tuple[str, str, str, int]] = {}
        for runtime, thread_id, token in specs:
            baseline = self._latest_thread_event_seq(thread_id)
            started = self.send_message(
                thread_id,
                (
                    "Do not finish yet. Wait for a follow-up instruction. "
                    f"When you receive it, reply with exactly the word {token} and nothing else."
                ),
                runtime=runtime,
            )
            if started.get("status") != "accepted":
                raise AssertionError(f"{runtime} parallel turn on {thread_id} was not started: {started}")
            created[self.api_thread_id(thread_id)] = (runtime, thread_id, token, baseline)
        print(f"  created {', '.join(sorted(created))}", flush=True)

        max_running_total = 0
        max_running_by_runtime = {runtime: 0 for runtime in SMOKE_OAUTH_RUNTIMES}
        all_running_seen = False
        deadline = time.time() + 300
        while time.time() < deadline:
            running = self._running_thread_ids() & set(created)
            max_running_total = max(max_running_total, len(running))
            runtime_status = self._api("GET", "/v1/agent-runtime/status")
            active_by_runtime = {}
            for runtime in SMOKE_OAUTH_RUNTIMES:
                active_thread_ids = [
                    thread_id
                    for thread_id in self.runtime_status_record(runtime_status, runtime).get("active_thread_ids", [])
                    if thread_id in created
                ]
                active_by_runtime[runtime] = active_thread_ids
                max_running_by_runtime[runtime] = max(max_running_by_runtime[runtime], len(active_thread_ids))
                if len(active_thread_ids) > 3:
                    raise AssertionError(f"more than 3 {runtime} turns reported running: {runtime_status}")
            if sum(len(ids) for ids in active_by_runtime.values()) > 6:
                raise AssertionError(f"more than 6 mixed turns reported running: {runtime_status}")
            if all(len(active_by_runtime[runtime]) == 3 for runtime in SMOKE_OAUTH_RUNTIMES):
                all_running_seen = True
                break
            time.sleep(2)

        if not all_running_seen:
            snapshot = self._api("GET", "/v1/agent-runtime/status")
            raise AssertionError(
                "all six mixed runtime turns never ran together; "
                f"max total={max_running_total}, max by runtime={max_running_by_runtime}, last={snapshot}"
            )

        for _, (_, thread_id, token, _) in sorted(created.items()):
            steered = self.send_follow_up(
                thread_id, f"Now reply with exactly the word {token} and nothing else."
            )
            if steered.get("status") != "accepted":
                raise AssertionError(f"steer on running thread {thread_id} was not delivered: {steered}")

        for api_id, (runtime, thread_id, token, baseline) in created.items():
            done = self._wait_for_turn(thread_id, since=baseline, timeout=300)
            if done["status"] != "completed":
                raise AssertionError(
                    f"mixed {runtime} turn on {api_id} ended {done['status']}: "
                    f"{self._thread_failure_detail(thread_id)}"
                )
            if token not in (done.get("output_message") or "").upper():
                raise AssertionError(f"mixed {runtime} turn on {api_id} answered {done.get('output_message')!r}, expected {token}")

        print(
            "  live thread state reached total=6, codex=3, claude_code=3",
            flush=True,
        )
        self.parallel_threads = {
            runtime: (thread_id, token)
            for runtime, thread_id, token in specs
            if thread_id.endswith("-par-a")
        }

        for runtime, (thread_id, token) in self.parallel_threads.items():
            baseline = self._latest_thread_event_seq(thread_id)
            follow_up = self.send_follow_up(
                thread_id,
                (
                    "Earlier in this conversation you replied with a single uppercase token. "
                    "Reply with exactly that token again and nothing else."
                ),
            )
            if follow_up.get("status") != "accepted":
                raise AssertionError(f"{runtime} follow-up on idle {thread_id} was not started: {follow_up}")
            done = self._wait_for_turn(thread_id, since=baseline, timeout=240)
            if done["status"] != "completed":
                raise AssertionError(
                    f"{runtime} follow-up turn ended {done['status']}: {self._thread_failure_detail(thread_id)}"
                )
            if token not in (done.get("output_message") or "").upper():
                raise AssertionError(f"{runtime} thread context lost across turns: {done.get('output_message')!r}")

        self._ok("6 mixed OAuth turns ran together at 3 per runtime; both kept thread context")

    def check_agent_steering(self) -> None:
        """Mid-turn steering through the admin API: a second message posted
        while the turn is running must be synchronously flushed to the runtime
        as a steer."""
        self._step(f"{self.agent_runtime} steering: redirect a running turn mid-turn")
        thread_id = f"smoke-steer-{self.thread_id_component(self.agent_runtime)}"
        if self.agent_runtime == "claude_code":
            # Do not wait for activity: this pins cancel_queued handling when
            # the initial message is still queued or pending dispatch.
            startup_thread_id = f"{thread_id}-startup"
            startup_baseline = self._latest_thread_event_seq(startup_thread_id)
            startup = self.send_message(
                startup_thread_id,
                "Write a detailed 5000-word essay about distributed systems.",
            )
            if startup.get("status") != "accepted":
                raise AssertionError(
                    f"startup steer target was not started: {startup}"
                )
            startup_deadline = time.time() + 30
            while True:
                # Measure only this POST. Time spent behind the host's normal
                # STARTING fence is startup latency, not steering-delivery
                # latency, and is bounded independently by startup_deadline.
                startup_steer_started = time.monotonic()
                try:
                    startup_steer = self.send_follow_up(
                        startup_thread_id,
                        "Immediate update: reply with exactly STARTUP_STEERED.",
                    )
                    startup_elapsed = time.monotonic() - startup_steer_started
                    break
                except AssertionError as exc:
                    if (
                        "agent is starting; retry shortly" in str(exc)
                        and time.time() < startup_deadline
                    ):
                        # Retry the host's STARTING fence only; deliberately do
                        # not wait for provider activity, because queued/pending
                        # dispatch is the race this check must exercise.
                        time.sleep(0.05)
                        continue
                    raise
            if startup_steer.get("status") != "accepted":
                raise AssertionError(
                    "immediate Claude startup steer was not delivered: "
                    f"{startup_steer}"
                )
            if startup_elapsed >= 8:
                raise AssertionError(
                    "immediate Claude startup steer delivery was delayed: "
                    f"{startup_elapsed:.2f}s"
                )
            startup_done = self._wait_for_turn(
                startup_thread_id,
                since=startup_baseline,
                timeout=240,
            )
            if startup_done["status"] != "completed" or "STARTUP_STEERED" not in (
                startup_done.get("output_message") or ""
            ).upper():
                raise AssertionError(
                    "immediate Claude startup steer did not supersede the "
                    f"initial prompt: {startup_done}"
                )
        baseline = self._latest_thread_event_seq(thread_id)
        slow_prompt = (
            "Use the terminal tool to run "
            "`python3 -c 'import time; [(print(i, flush=True), time.sleep(0.05)) "
            "for i in range(400)]'`, then write a 300-word essay about bananas."
            if self.agent_runtime == "codex"
            else "Use the terminal tool to run `sleep 20`, then write a 300-word essay about bananas."
        )
        slow = self.send_message(
            thread_id,
            slow_prompt,
        )
        if slow.get("status") != "accepted":
            raise AssertionError(f"steer target was not started: {slow}")
        self._wait_for_turn_activity(thread_id, since=baseline, timeout=120)
        steer_started = time.monotonic()
        steered = self.send_follow_up(
            thread_id, "Task update: stop the essay and reply with exactly the word STEERED."
        )
        steer_elapsed = time.monotonic() - steer_started
        if steered.get("status") != "accepted":
            raise AssertionError(f"message on the running thread was not delivered as a steer: {steered}")
        if self.agent_runtime in ("codex", "claude_code", "grok") and steer_elapsed >= 8:
            raise AssertionError(
                f"{self.agent_runtime} steer delivery was delayed "
                "behind activity: "
                f"{steer_elapsed:.2f}s"
            )
        expected = "STEERED"
        if self.agent_runtime == "claude_code":
            # Exercise the queue/abort race that a single steer cannot cover:
            # the second pair may reach Claude before it has begun processing
            # the first replacement message.
            second_started = time.monotonic()
            second = self.send_follow_up(
                thread_id,
                "Final task update: reply with exactly the word DOUBLE_STEERED.",
            )
            second_elapsed = time.monotonic() - second_started
            if second.get("status") != "accepted":
                raise AssertionError(
                    "second rapid Claude steer was not delivered: "
                    f"{second}"
                )
            if second_elapsed >= 8:
                raise AssertionError(
                    "second rapid Claude steer delivery was delayed: "
                    f"{second_elapsed:.2f}s"
                )
            expected = "DOUBLE_STEERED"
        done = self._wait_for_turn(thread_id, since=baseline, timeout=240)
        if done["status"] != "completed":
            raise AssertionError(f"steered turn ended {done['status']}: {self._thread_failure_detail(thread_id)}")
        if expected not in (done.get("output_message") or "").upper():
            raise AssertionError(f"steer did not take effect, output: {done.get('output_message')!r}")
        self._ok(
            f"{self.agent_runtime} steer redirected the running turn "
            f"(delivered in {steer_elapsed:.2f}s)"
        )

    def check_agent_kill_and_thread_survival(self, *, expect_steering_denied: bool = False) -> None:
        """Stop a running turn (its runtime process is terminated mid-turn),
        then send another message on the same thread: the stop must not corrupt
        the persisted runtime thread/session. A runtime without mid-turn
        steering can prove that API boundary against the same running turn."""
        self._step(f"{self.agent_runtime} stop: cancel a running turn, then reuse its thread")
        thread_id = f"smoke-kill-{self.thread_id_component(self.agent_runtime)}"
        baseline = self._latest_thread_event_seq(thread_id)
        slow = self.send_message(
            thread_id,
            "Use the terminal tool to run `sleep 300`, then write a 500-word essay about bananas.",
        )
        if slow.get("status") != "accepted":
            raise AssertionError(f"slow turn was not started ({slow}); cannot test stop")
        self._wait_for_turn_activity(thread_id, since=baseline, timeout=120)
        if expect_steering_denied:
            status, body = self._api_status(
                "POST",
                f"/v1/threads/{self.api_thread_id(thread_id)}/messages",
                self.follow_up_body("change direction"),
            )
            expected_error = "Hermes cannot accept another message while running; wait for it to finish"
            if status != 409 or self._error_message(body) != expected_error:
                raise AssertionError(f"unsupported steering returned {status}: {body}")
        start = time.time()
        status, body = self._api_status(
            "POST", f"/v1/threads/{self.api_thread_id(thread_id)}/stop"
        )
        if status != 200 or body.get("status") != "accepted":
            raise AssertionError(f"stop returned {status}: {body}")
        killed = self._wait_for_turn(thread_id, since=baseline, timeout=60)
        if killed["status"] != "cancelled":
            raise AssertionError(f"stopped turn ended {killed['status']}, expected cancelled")
        print(f"  stop settled in {time.time() - start:.1f}s", flush=True)

        # The stop must also free the thread's transient scope on the host:
        # close() stops kern-agent-thread-<id>.scope through the root
        # stop-agent-thread helper (SIGKILLing any process the runtime left in
        # the cgroup, such as the sleep above) and reset-failed clears any
        # failed remnant, so systemd forgets the unit entirely. The turn is
        # marked cancelled before that close completes, so poll briefly. This
        # pins the mechanism the follow-up turn below depends on.
        scope_unit = f"kern-agent-thread-{self.api_thread_id(thread_id)}.scope"
        deadline = time.time() + 30
        while True:
            load_state = self._ssh_code(
                f"systemctl show -p LoadState --value {shlex.quote(scope_unit)}"
            ).strip()
            if load_state == "not-found":
                break
            if time.time() > deadline:
                raise AssertionError(
                    f"stopped thread scope {scope_unit} still known to systemd: {load_state!r}"
                )
            time.sleep(1)

        follow_baseline = self._latest_thread_event_seq(thread_id)
        follow = self.send_follow_up(
            thread_id, "Stop the essay. Reply with exactly the word SURVIVED and nothing else."
        )
        if follow.get("status") != "accepted":
            raise AssertionError(f"follow-up on the stopped thread was not started: {follow}")
        done = self._wait_for_turn(thread_id, since=follow_baseline, timeout=240)
        if done["status"] != "completed":
            raise AssertionError(
                f"follow-up on the stopped thread ended {done['status']}: {self._thread_failure_detail(thread_id)}"
            )
        if "SURVIVED" not in (done.get("output_message") or "").upper():
            raise AssertionError(f"follow-up on stopped thread answered {done.get('output_message')!r}")
        steering = " and rejected unsupported steering" if expect_steering_denied else ""
        self._ok(
            f"{self.agent_runtime} stop cancelled the running turn{steering}; "
            "a later turn resumed the same thread"
        )

    def check_agent_thread_recall(self) -> None:
        """Thread context must survive runtime process recycling. By now the
        earlier parallel threads have cycled through the runtime pools, so these
        recalls exercise persisted thread/session resume."""
        self._step("agent thread recall after process recycling")
        if not self.parallel_threads:
            raise AssertionError("no completed parallel threads recorded; recall check must run after parallelism")
        for runtime, (thread_id, token) in self.parallel_threads.items():
            baseline = self._latest_thread_event_seq(thread_id)
            recall = self.send_follow_up(
                thread_id,
                (
                    "Earlier in this conversation you replied with a single uppercase word. "
                    "Reply with exactly that word again and nothing else."
                ),
            )
            if recall.get("status") != "accepted":
                raise AssertionError(f"{runtime} recall on {thread_id} was not started: {recall}")
            done = self._wait_for_turn(thread_id, since=baseline, timeout=240)
            if done["status"] != "completed":
                raise AssertionError(
                    f"{runtime} recall on {thread_id} ended {done['status']}: {self._thread_failure_detail(thread_id)}"
                )
            if token not in (done.get("output_message") or "").upper():
                raise AssertionError(f"{runtime} {thread_id} lost its context: {done.get('output_message')!r}")
        self._ok("Codex and Claude threads recalled their context after pool eviction and reuse")

    def check_reboot_recovery(self) -> None:
        """POST /v1/host-runtime/reboot, then prove the host comes back with
        everything intact: the admin API and proxy restart enabled, turn
        history and the thread map survive on the EBS volume, provider login
        persists, and a post-reboot turn resumes a pre-reboot thread."""
        self._step("host reboot: services, state, login, and threads survive")
        if not self.parallel_threads:
            raise AssertionError("no completed parallel threads recorded; reboot check must run after parallelism")
        status, body = self._api_status("POST", "/v1/host-runtime/reboot")
        if status != 200 or body.get("status") != "accepted":
            raise AssertionError(f"reboot returned {status}: {body}")
        print("  reboot accepted; waiting for the host to go down and come back", flush=True)
        # Admin sessions live in the admin API process, so the reboot clears them.
        # Drop the cached cookie so the first post-reboot call re-logs in over the
        # loopback instead of retrying a now-dead session and reading every 401 as
        # "still booting" until the wait loop times out.
        self._session_cookie = None
        time.sleep(20)  # let the host actually drop before reconnecting

        deadline = time.time() + 420
        health = None
        while time.time() < deadline:
            try:
                self._reopen_tunnel()
                health = self._api("GET", "/v1/health")
                if health["network_controls"]["status"] == "active":
                    break
            except Exception as exc:  # noqa: BLE001 - ssh/api both fail until boot completes
                print(f"  still waiting ({type(exc).__name__})", flush=True)
            time.sleep(10)
        if not health or health["network_controls"]["status"] != "active":
            raise AssertionError(f"host did not come back healthy after reboot (last health: {health})")

        # Pre-reboot history survived on disk: the parallel threads keep their
        # rows (idle, no phantom running work) and their retained messages.
        for runtime, (thread_id, _) in self.parallel_threads.items():
            survivor = self._api(
                "GET", f"/v1/threads/{self.api_thread_id(thread_id)}"
            )["thread"]
            if survivor.get("status") != "idle":
                raise AssertionError(f"{runtime} thread changed across reboot: {survivor}")
            events = self._thread_events(thread_id)
            if not any(event["event_type"] == "thread.message" for event in events):
                raise AssertionError(f"{runtime} thread lost its message history across reboot")

        # All provider credentials persisted: every runtime re-derives active
        # without a new login or credential connection.
        for runtime in SMOKE_RUNTIMES:
            wanted = (
                {"active", "error"}
                if runtime == "hermes"
                else {"active", "awaiting_login", "error"}
            )
            status = self._wait_for_runtime_status(wanted, runtime=runtime, timeout=180)
            if status != "active":
                raise AssertionError(
                    f"{runtime} is {status} after reboot; expected its provider connection to survive"
                )

        # And pre-reboot threads resume with their context for both runtimes.
        for runtime, (thread_id, token) in self.parallel_threads.items():
            baseline = self._latest_thread_event_seq(thread_id)
            recall = self.send_follow_up(
                thread_id,
                (
                    "Earlier in this conversation you replied with a single uppercase token. "
                    "Reply with exactly that token again and nothing else."
                ),
            )
            if recall.get("status") != "accepted":
                raise AssertionError(f"{runtime} post-reboot recall was not started: {recall}")
            done = self._wait_for_turn(thread_id, since=baseline, timeout=300)
            if done["status"] != "completed":
                raise AssertionError(
                    f"{runtime} post-reboot recall ended {done['status']}: {self._thread_failure_detail(thread_id)}"
                )
            if token not in (done.get("output_message") or "").upper():
                raise AssertionError(f"{runtime} thread context lost across reboot: {done.get('output_message')!r}")
        self._ok("host rebooted clean; history, all three provider credentials, and retained runtime thread contexts survived")

    # --- helpers -----------------------------------------------------------

    def check_network_event_prune_race(self) -> None:
        """Storm denied CONNECTs through the proxy while the admin API
        concurrently pages the network event table. Two real processes, two
        database roles: the proxy inserts rows under its narrow grant (its
        amortized prune fires every PRUNE_EVERY-th insert, a no-op below the
        cap), the admin reads them, and paging must stay unique and ordered
        throughout. Also pins the role isolation only a live host can show:
        the proxy role can write exactly the network_events table and nothing
        else."""
        self._step("network event storm under concurrent reads (two database roles)")
        baseline_row = self._ssh_code(
            "sudo -u postgres psql -tA -d kern_admin -c 'SELECT COALESCE(max(seq), 0) FROM network_events'"
        ).strip()
        baseline = int(baseline_row or 0)
        needed = 2 * PRUNE_EVERY + 50  # cross at least two amortized-prune boundaries
        print(f"  pushing {needed} denied requests through the proxy (seq baseline {baseline})", flush=True)

        reader_failures: list[str] = []
        reader_reads = {"count": 0}
        stop = threading.Event()

        def reader() -> None:
            while not stop.is_set():
                status, body = self._api_status("GET", "/v1/network/events")
                if status != 200:
                    reader_failures.append(f"GET /v1/network/events -> {status}: {body}")
                    return
                reader_reads["count"] += 1
                page_seqs = [int(event["seq"]) for event in body["events"]]
                if page_seqs:
                    if len(set(page_seqs)) != len(page_seqs) or page_seqs != sorted(page_seqs, reverse=True):
                        reader_failures.append(f"inconsistent network event page: {page_seqs}")
                        return
                else:
                    time.sleep(0.2)

        reader_thread = threading.Thread(target=reader, daemon=True)
        reader_thread.start()
        generator = (
            "sudo -u kern-agent python3 - <<'PY'\n"
            "import socket, threading\n"
            f"count, workers = {needed}, 8\n"
            "def worker(n):\n"
            "    for _ in range(n):\n"
            "        try:\n"
            f"            s = socket.create_connection((\"127.0.0.1\", {PROXY_PORT}), timeout=10)\n"
            "            s.sendall(b\"CONNECT denied.smoke.invalid:443 HTTP/1.1\\r\\n"
            "Host: denied.smoke.invalid:443\\r\\n\\r\\n\")\n"
            "            s.recv(4096)\n"
            "            s.close()\n"
            "        except OSError:\n"
            "            pass\n"
            "threads = [threading.Thread(target=worker, args=(-(-count // workers),)) for _ in range(workers)]\n"
            "[t.start() for t in threads]\n"
            "[t.join() for t in threads]\n"
            "print(\"generated\")\n"
            "PY"
        )
        try:
            output = self._ssh_code(generator)
        finally:
            stop.set()
            reader_thread.join(timeout=30)
        if "generated" not in output:
            raise AssertionError(f"event generator did not finish cleanly: {output!r}")
        if reader_failures:
            raise AssertionError(f"concurrent reader failed during the storm: {reader_failures[0]}")
        if reader_reads["count"] < 10:
            raise AssertionError(f"reader only completed {reader_reads['count']} reads; the storm outpaced it entirely")

        verdict = json.loads(self._ssh_code(
            "sudo -u postgres psql -tA -d kern_admin -c "
            "\"SELECT json_build_object('rows', count(*), 'max_seq', COALESCE(max(seq), 0),"
            " 'unique', count(*) = count(DISTINCT seq))::text FROM network_events WHERE seq > "
            f"{baseline}\""
        ))
        if verdict["rows"] < needed:
            raise AssertionError(f"storm generated {needed} denials but only {verdict['rows']} events landed")
        if not verdict["unique"]:
            raise AssertionError(f"duplicate event seqs after the storm: {verdict}")
        # Role isolation: the proxy can append audit events and read the one
        # Bedrock credential, but cannot mutate credentials or read admin state.
        isolation = self._ssh_code(
            "sudo -u kern-proxy psql -tA -d kern_admin -c 'SELECT count(*) >= 0 FROM network_events' && "
            "sudo -u kern-proxy psql -tA -d kern_admin -c 'SELECT count(*) >= 0 FROM bedrock_credentials' && "
            "sudo -u kern-proxy bash -c '! psql -tA -d kern_admin -c \"UPDATE bedrock_credentials SET access_key_id = access_key_id\" 2>/dev/null' && "
            "sudo -u kern-proxy bash -c '! psql -tA -d kern_admin -c \"SELECT count(*) FROM thread_sessions\" 2>/dev/null' && "
            "sudo -u kern-proxy bash -c '! psql -tA -d kern_admin -c \"SELECT agent_name FROM config\" 2>/dev/null' && "
            "echo ok"
        ).strip().splitlines()
        if isolation != ["t", "t", "ok"]:
            raise AssertionError(f"proxy database role exceeded its narrow table grants: {isolation}")
        final = self._api("GET", "/v1/network/events")
        if not final["events"]:
            raise AssertionError("admin API cannot read the network events after the storm")
        self._ok(
            f"{needed} events stormed; {reader_reads['count']} concurrent reads stayed consistent; "
            "proxy role confined to network_events"
        )

    def _raw_local_http(self, port: int, request: bytes) -> bytes:
        import socket

        with socket.create_connection(("127.0.0.1", port), timeout=30) as sock:
            sock.sendall(request)
            sock.shutdown(socket.SHUT_WR)
            chunks: list[bytes] = []
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks)

    def _admin_cookie(self) -> str:
        """Log in once over the SSH-forwarded loopback and cache the session
        cookie, the credential every admin API call carries (the admin password
        is only ever presented at /v1/login)."""
        cookie = getattr(self, "_session_cookie", None)
        if cookie:
            return cookie
        data = json.dumps({"password": self.result["admin_password"]}).encode()
        request = urllib.request.Request(f"http://127.0.0.1:{ADMIN_PORT}/v1/login", data=data, method="POST")
        request.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(request, timeout=30) as response:
            response.read()
            for value in response.headers.get_all("Set-Cookie") or []:
                name, _, token = value.split(";", 1)[0].strip().partition("=")
                if name == "tc_admin_session" and token:
                    self._session_cookie = token
                    return token
        raise AssertionError("admin login did not return a session cookie")

    def _auth_headers(self) -> dict:
        return {"Cookie": f"tc_admin_session={self._admin_cookie()}", "X-Kern-Csrf": "1"}

    def _api(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None

        def attempt() -> dict:
            request = urllib.request.Request(f"http://127.0.0.1:{ADMIN_PORT}{path}", data=data, method=method)
            for name, value in self._auth_headers().items():
                request.add_header(name, value)
            if body is not None:
                request.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read())

        try:
            return attempt()
        except (urllib.error.URLError, ConnectionError) as exc:
            # The tunnel can drop during a long idle; the failure then hits the
            # connect of the NEXT request, which never reached the server, so
            # one reopen-and-retry is safe. (A response lost mid-flight would
            # retry a mutation that already executed; that is vanishingly rare
            # and fails the run visibly rather than silently.)
            reason = getattr(exc, "reason", exc)
            if isinstance(exc, urllib.error.HTTPError):
                payload = exc.read()
                try:
                    detail = json.loads(payload)
                except json.JSONDecodeError:
                    detail = payload.decode(errors="replace")
                raise AssertionError(f"{method} {path} returned HTTP {exc.code}: {detail}") from exc
            print(f"  (admin API unreachable: {reason}; reopening tunnel and retrying)", flush=True)
            self._reopen_tunnel()
            return attempt()

    def _api_status(
        self, method: str, path: str, body: dict | None = None, *,
        cookie: str | None = "__default__",
    ) -> tuple[int, dict]:
        """One-shot request returning (status, body) instead of raising on HTTP
        errors, for checks that assert specific 4xx behavior or run from
        threads (no tunnel-reopen side effects). ``cookie=None`` sends no session
        cookie; a wrong cookie value exercises rejected sessions."""
        if cookie == "__default__":
            cookie = self._admin_cookie()
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(f"http://127.0.0.1:{ADMIN_PORT}{path}", data=data, method=method)
        if cookie is not None:
            request.add_header("Cookie", f"tc_admin_session={cookie}")
            request.add_header("X-Kern-Csrf", "1")
        if body is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            try:
                return exc.code, json.loads(payload)
            except json.JSONDecodeError:
                return exc.code, {"raw": payload.decode(errors="replace")}

    def _parallel(self, count: int, fn) -> list:
        """Run fn(0..count-1) on real threads and return results in order; a
        worker's exception is re-raised after all workers finish."""
        results: list = [None] * count

        def run(index: int) -> None:
            try:
                results[index] = fn(index)
            except Exception as exc:  # noqa: BLE001 - surfaced after join below
                results[index] = exc

        threads = [threading.Thread(target=run, args=(index,)) for index in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        for result in results:
            if isinstance(result, Exception):
                raise AssertionError(f"parallel request failed: {result}") from result
        return results

    def _running_thread_ids(self) -> set[str]:
        """API-side ids of threads with a live turn, from the thread list."""
        return {
            thread["thread_id"]
            for thread in self._api("GET", "/v1/threads")["threads"]
            if thread.get("status") == "running"
        }

    @staticmethod
    def _error_message(body: object) -> str:
        error = body.get("error") if isinstance(body, dict) else None
        return error.get("message", "") if isinstance(error, dict) else str(error or "")

    def _network_events(self, since: int = 0) -> list[dict]:
        """Drain `/v1/network/events` cursor pages into events after ``since``."""
        return self._drain_event_pages("/v1/network/events", since)

    def _agent_events(self, since: int = 0) -> list[dict]:
        """Drain `/v1/events` cursor pages into events after ``since``."""
        return self._drain_event_pages("/v1/events", since)

    def _drain_event_pages(self, endpoint: str, since: int) -> list[dict]:
        """Walk an audit log's newest-first cursor pages, asserting the page
        contract, and return events after ``since`` oldest-first."""
        events: list[dict] = []
        before: int | None = None
        while True:
            query = "?limit=100" if before is None else f"?before={before}&limit=100"
            page = self._api("GET", f"{endpoint}{query}")["events"]
            if not page:
                return sorted(events, key=lambda event: int(event["seq"]))
            if len(page) > 100:
                raise AssertionError(f"{endpoint} page holds {len(page)} events, expected at most 100")
            page_seqs = [int(event["seq"]) for event in page]
            if page_seqs != sorted(page_seqs, reverse=True):
                raise AssertionError(f"{endpoint} page is not sorted by descending seq: {page_seqs}")
            if before is not None and any(seq >= before for seq in page_seqs):
                raise AssertionError(f"{endpoint} page contains seq >= before cursor {before}: {page_seqs}")
            if len(set(page_seqs)) != len(page_seqs):
                raise AssertionError(f"{endpoint} page contains duplicate seqs: {page_seqs}")
            events.extend(event for event in page if int(event["seq"]) > since)
            if min(page_seqs) <= since:
                return sorted(events, key=lambda event: int(event["seq"]))
            before = min(page_seqs)

    def _agent_account(self, runtime_type: str) -> dict:
        accounts = self._api("GET", "/v1/agent-runtime/account")["accounts"]
        for account in accounts:
            if account.get("agent_runtime") == runtime_type:
                return account
            if runtime_type == "hermes" and account.get("provider") == "bedrock":
                return account
        raise AssertionError(f"account summary did not include {runtime_type}: {accounts}")

    def _assert_provider_metadata(self, runtime_type: str, account: dict) -> None:
        forbidden_fragments = ("token", "secret", "key", "authorization", "bearer", "sha256")
        allowed_keys = {"agent_runtime", "provider", "status", "account_id", "email", "plan_type"}
        if runtime_type == "codex":
            allowed_keys.add("codex_usage")
        elif runtime_type == "claude_code":
            allowed_keys.add("claude_usage")
        elif runtime_type == "grok":
            allowed_keys.update(
                {
                    "grok_usage",
                    "coding_data_retention_opt_out",
                    "zdr_enabled",
                }
            )
            for key in ("coding_data_retention_opt_out", "zdr_enabled"):
                if key in account and not isinstance(account[key], bool):
                    raise AssertionError(
                        f"Grok account metadata {key} is not boolean: {account}"
                    )
        elif runtime_type == "hermes":
            allowed_keys = {
                "provider", "agent_runtimes", "status", "account_id", "arn", "bedrock_usage"
            }
        unexpected_keys = sorted(set(account) - allowed_keys)
        if unexpected_keys:
            raise AssertionError(f"{runtime_type} account metadata exposed unexpected key(s) {unexpected_keys}: {account}")

        # The live Bedrock usage counters legitimately count tokens; every
        # other secret-shaped key name stays forbidden.
        usage_counter_keys = {"input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"}

        def check_no_secretish_keys(value: object) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    lowered = str(key).lower()
                    if any(fragment in lowered for fragment in forbidden_fragments) and key not in usage_counter_keys:
                        raise AssertionError(f"{runtime_type} account metadata leaked secret-like key {key!r}: {account}")
                    check_no_secretish_keys(item)
            elif isinstance(value, list):
                for item in value:
                    check_no_secretish_keys(item)

        check_no_secretish_keys(account)

    def _assert_provider_account_anchors(self, *, live_pins: bool) -> None:
        snapshot = self._provider_account_pin_snapshot()
        openai = snapshot.get("openai")
        claude = snapshot.get("claude")
        if not isinstance(openai, dict) or not openai.get("admin_account_id"):
            raise AssertionError(f"OpenAI admin account anchor is missing: {snapshot}")
        if not isinstance(claude, dict) or not claude.get("admin_account_id") or not claude.get("admin_token_sha256"):
            raise AssertionError(f"Claude admin account anchor is missing: {snapshot}")
        if live_pins:
            if openai.get("pin_account_id") != openai.get("admin_account_id"):
                raise AssertionError(f"OpenAI proxy pin does not match admin account anchor: {snapshot}")
            if claude.get("pin_account_id") != claude.get("admin_account_id"):
                raise AssertionError(f"Claude proxy account pin does not match admin account anchor: {snapshot}")
        else:
            if openai.get("pin_account_id"):
                raise AssertionError(f"OpenAI live proxy pin survived provider deactivation: {snapshot}")
            if claude.get("pin_account_id"):
                raise AssertionError(f"Claude live proxy pin survived provider deactivation: {snapshot}")

    def _provider_account_pin_snapshot(self) -> dict:
        query = """
SELECT jsonb_object_agg(
    providers.provider,
    jsonb_build_object(
        'admin_account_id', provider_accounts.account_id,
        'admin_token_sha256', provider_accounts.metadata->>'access_token_sha256',
        'pin_account_id', proxy_provider_pins.account_id
    )
)::text
FROM (VALUES ('openai'), ('claude')) AS providers(provider)
LEFT JOIN provider_accounts USING (provider)
LEFT JOIN proxy_provider_pins USING (provider)
"""
        output = self._ssh_code(
            "sudo -u postgres psql -tA -d kern_admin -c "
            + shlex.quote(query)
        )
        return json.loads(output) if output else {}

    def print_network_events(self, label: str, *, since: int = 0) -> None:
        try:
            events = self._network_events(since=since)
        except Exception as exc:  # noqa: BLE001 - best-effort debug output
            print(f"  {label}: could not read network events: {type(exc).__name__}: {exc}", flush=True)
            return
        print(f"  {label}: {len(events)} event(s) after seq {since}", flush=True)
        for event in events:
            reason = event.get("reason_code")
            suffix = f" reason_code={reason!r}" if reason else ""
            print(
                f"    seq={event.get('seq')} {event.get('decision')} "
                f"{event.get('method')} {event.get('protocol')}://{event.get('host')}{event.get('path')}{suffix}",
                flush=True,
            )

    def _thread_failure_detail(self, thread_id: str) -> str:
        """Failure context for assertions: the thread's last few events (which
        carry agent messages and failure payloads)."""
        try:
            tail = "; ".join(
                f"{event['event_type']}: {event['payload'].get('error_message') or event['payload'].get('message', '')}"
                for event in self._thread_events(thread_id)[-4:]
            )
        except Exception as exc:  # noqa: BLE001 - best-effort debug output
            return f"<could not read thread events: {type(exc).__name__}: {exc}>"
        return f"recent events: {tail or '<none>'}"

    def _thread_events(self, thread_id: str, since: int = 0) -> list[dict]:
        """Drain the thread's events after ``since``, oldest-first."""
        events: list[dict] = []
        cursor = since
        while True:
            page = self._api(
                "GET",
                f"/v1/threads/{self.api_thread_id(thread_id)}/events?since={cursor}",
            )["events"]
            if not page:
                return events
            events.extend(page)
            cursor = max(int(event["seq"]) for event in page)

    def _latest_thread_event_seq(self, thread_id: str) -> int:
        """The baseline seq before a send, used to isolate later events."""
        return max((int(event["seq"]) for event in self._thread_events(thread_id)), default=0)

    @staticmethod
    def _turn_result(events: list[dict], thread_status: str = "running") -> dict | None:
        """Fold new events and durable thread status into a terminal result.

        Errors and stops are explicit events. Successful work is complete when
        the thread is idle; ``output_message`` is its latest agent message.
        """
        output: str | None = None
        for event in events:
            payload = event.get("payload") or {}
            if event.get("event_type") == "thread.message" and payload.get("source") == "agent":
                output = payload.get("message")
            terminal = TURN_TERMINAL_STATUSES.get(event.get("event_type"))
            if terminal is not None:
                return {
                    "status": terminal,
                    "output_message": output,
                    "error_message": payload.get("error_message"),
                }
        if thread_status == "idle":
            return {
                "status": "completed",
                "output_message": output,
                "error_message": None,
            }
        return None

    def _wait_for_turn(self, thread_id: str, *, timeout: float, since: int = 0) -> dict:
        """Wait until the turn whose events land after ``since`` finishes and
        return {"status", "output_message", "error_message"}."""
        deadline = time.time() + timeout
        while True:
            thread_status = self._api(
                "GET", f"/v1/threads/{self.api_thread_id(thread_id)}"
            )["thread"]["status"]
            result = self._turn_result(
                self._thread_events(thread_id, since=since),
                thread_status,
            )
            if result is not None:
                return result
            if time.time() >= deadline:
                raise AssertionError(
                    f"turn on thread {thread_id} did not finish within {timeout}s; "
                    f"{self._thread_failure_detail(thread_id)}"
                )
            time.sleep(2)

    def _wait_for_turn_activity(self, thread_id: str, *, since: int, timeout: float) -> None:
        """Wait until the running turn shows agent activity, so mid-turn
        steering/stop checks exercise a genuinely running runtime process."""
        deadline = time.time() + timeout
        while True:
            thread_status = self._api(
                "GET", f"/v1/threads/{self.api_thread_id(thread_id)}"
            )["thread"]["status"]
            events = self._thread_events(thread_id, since=since)
            if any(event.get("event_type") == "thread.activity" for event in events):
                return
            result = self._turn_result(events, thread_status)
            if result is not None:
                raise AssertionError(
                    f"turn on thread {thread_id} finished before showing activity: {result}"
                )
            if time.time() >= deadline:
                raise AssertionError(f"turn on thread {thread_id} showed no activity within {timeout}s")
            time.sleep(2)

    def _wait_for_runtime_status(self, wanted: set[str], *, timeout: float, runtime: str | None = None) -> str:
        runtime = runtime or self.agent_runtime
        deadline = time.time() + timeout
        record = self.runtime_status_record(self._api("GET", "/v1/agent-runtime/status"), runtime)
        status = record["status"]
        print(
            self._runtime_status_line(runtime, record, wanted),
            flush=True,
        )
        previous_detail = record.get("error_message")
        while time.time() < deadline and status not in wanted:
            time.sleep(5)
            previous = status
            record = self.runtime_status_record(
                self._api("GET", "/v1/agent-runtime/status"), runtime
            )
            status = record["status"]
            detail = record.get("error_message")
            if status != previous or detail != previous_detail:
                print(self._runtime_status_line(runtime, record), flush=True)
            previous_detail = detail
        return status

    def _runtime_status_line(self, runtime: str, record: dict, wanted: set[str] | None = None) -> str:
        status = record["status"]
        suffix = f" (waiting for {'/'.join(sorted(wanted))})" if wanted else ""
        detail = record.get("error_message")
        if isinstance(detail, str) and detail:
            return f"  {runtime} runtime status: {status}{suffix}; error_message={detail!r}"
        return f"  {runtime} runtime status: {status}{suffix}"

    def _ssh_code(self, remote_command: str) -> str:
        result = subprocess.run(
            [
                "ssh", "-S", str(self.control_socket),
                "-o", f"UserKnownHostsFile={self.workdir / 'known_hosts'}",
                f"kern-operator@{self.result['public_dns']}", remote_command,
            ],
            capture_output=True, text=True,
        )
        return result.stdout.strip()

    def _aws(self, *args: str) -> dict:
        proc = subprocess.run(
            ["aws", *args, "--region", self.region],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        return json.loads(proc.stdout) if proc.stdout.strip() else {}

    def _step(self, name: str) -> None:
        self.total += 1
        self._current = name
        print(f"[ .. ] {name}", flush=True)

    def _ok(self, detail: str) -> None:
        self.passed += 1
        print(f"[ OK ] {self._current}: {detail}\n", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
