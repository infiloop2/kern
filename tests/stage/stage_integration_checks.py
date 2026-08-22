"""Provider and GitHub checks for the persistent stage harness."""

from __future__ import annotations

import base64
import json
from pathlib import Path
import shlex
import time
from typing import TYPE_CHECKING

from host.constants import PROXY_PORT
from tests.smoke.smoke_aws import SMOKE_RUNTIMES, AwsSmoke
from tests.stage.stage_support import (
    CHEAP_EFFORT,
    CHEAP_MODELS,
    diagnostic_ref,
    integration_label as _integration_label,
    selected_integrations as _selected_integrations,
)


# The operator-facing suite that runs all four chat runtimes in one invocation
# (four focused suites otherwise). It is a
# stage_aws CLI choice, not a stage_support suite: tool selection and
# integration labels never see it.
ALL_RUNTIMES_SUITE = "all_runtimes"
RUNTIME_INTEGRATIONS = ("codex", "claude", "grok", "hermes")


class StageIntegrationChecks(AwsSmoke):
    """Provider and GitHub checks layered on the shared smoke primitives."""

    if TYPE_CHECKING:
        github_app_config: dict[str, str] | None
        github_secret_error: str | None
        bedrock_secret_error: str | None
        stage_github_repositories: list[dict]

        def enforcement_policy(self) -> dict: ...
        def _tool_credential_failures(self, tool_id: str) -> list[str]: ...

    @staticmethod
    def _print_agent_turn(runtime: str, purpose: str, phase: str, **fields: object) -> None:
        """Print turn identity and state without logging prompts or output."""
        detail = " ".join(f"{key}={value}" for key, value in fields.items())
        print(f"    [agent turn {phase}] runtime={runtime} purpose={purpose} {detail}", flush=True)

    @staticmethod
    def _account_shape(account: dict) -> dict[str, object]:
        """Provider-account diagnostics without identity values."""
        return {
            "status": account.get("status"),
            "provider": account.get("provider"),
            "has_account_id": bool(account.get("account_id")),
            "has_email": "email" in account,
            "keys": sorted(account),
        }

    def autoconfigure_github(self, suite: str) -> None:
        """When the stage secrets supply a GitHub App credential and sandbox
        write repo, install them on the host so the GitHub checks need no manual
        operator setup: store the App credential, and add the write repo to the
        stored policy (exactly the state an operator would have configured). A
        no-op when the secrets are absent or the suite excludes GitHub."""
        if suite not in ("github", "all"):
            return
        config = self.github_app_config
        if config is None:
            return
        repo = {"owner": config["owner"], "repo": config["repo"]}
        self._step(f"configure GitHub App credential and write repo {config['owner']}/{config['repo']} from stage secrets")
        self._api(
            "PUT",
            "/v1/network-tools/github-credential",
            {
                "mode": "app",
                "app_id": config["app_id"],
                "installation_id": config["installation_id"],
                "private_key_pem": config["private_key_pem"],
            },
        )
        # Fully manage the GitHub integration from the secret: the sandbox repo
        # is the only configured write repo, replacing whatever was on the host,
        # so the preflight and the write e2e can never see a stale entry. The GET
        # response wraps the controls; the PUT takes them directly. Other
        # integrations and domain rules are preserved.
        controls = self._api("GET", "/v1/network/policy").get("network_controls") or {}
        integrations = dict(controls.get("network_integrations") or {})
        integrations["github"] = {"enabled": True, "write_repositories": [repo]}
        controls["network_integrations"] = integrations
        self._api("PUT", "/v1/network/policy", controls)
        self._ok(f"GitHub App credential stored and {config['owner']}/{config['repo']} set as the sole write repo")

    def integration_availability(self, suite: str) -> dict[str, str | None]:
        """Check every selected credential before any integration test runs.

        ``None`` means ready. A string is an operator-facing reason the
        integration is unavailable. The all-suite runner records those as
        skips and continues; focused suites turn their one unavailable result
        into a failure so they remain useful for setup and debugging.
        """
        self._step(f"integration credential preflight (suite: {suite})")
        results: dict[str, str | None] = {}
        selected = (
            RUNTIME_INTEGRATIONS
            if suite == ALL_RUNTIMES_SUITE
            else _selected_integrations(suite)
        )
        for integration in selected:
            failures: list[str]
            if integration in {"codex", "claude", "grok", "hermes"}:
                runtime = "claude_code" if integration == "claude" else integration
                status = self._wait_for_runtime_status(
                    {"active", "awaiting_login", "deactivated", "error"},
                    runtime=runtime,
                    timeout=180,
                )
                if runtime == "hermes":
                    failures = []
                    if self.bedrock_secret_error:
                        failures.append(self.bedrock_secret_error)
                    if status != "active":
                        failures.append(
                            f"runtime is {status!r}; set both STAGE_BEDROCK_AWS_* secrets "
                            "or connect the AWS Bedrock credential in the stage admin UI"
                        )
                else:
                    failures = [] if status == "active" else [
                        f"runtime is {status!r}; open the stage admin UI and complete OAuth"
                    ]
            elif integration == "github":
                failures = self._github_config_failures()
            else:
                failures = self._tool_credential_failures(integration)
            reason = "; ".join(failures) if failures else None
            results[integration] = reason
            if reason is None:
                print(f"  [available] {_integration_label(integration)}", flush=True)
            else:
                print(f"  [unavailable] {_integration_label(integration)}: {reason}", flush=True)
        ready = sum(reason is None for reason in results.values())
        self._ok(f"credential preflight completed: {ready} available, {len(results) - ready} unavailable")
        return results

    def _github_config_failures(self) -> list[str]:
        """GitHub configuration checks for the preflight. Returns remediation
        strings for whatever is missing; prints an [ok] line for each item that
        is present. A non-empty write-repository list implies the integration is
        enabled (policy validation rejects write repos while disabled), so the
        two operator-provided pieces to confirm are the credential and at least
        one sandbox write repository."""
        failures: list[str] = []
        if self.github_secret_error:
            failures.append(self.github_secret_error)
        stored = self._api("GET", "/v1/network/policy").get("network_controls") or {}
        github = ((stored.get("network_integrations") or {}).get("github")) or {}
        write_repos = [repo for repo in (github.get("write_repositories") or []) if isinstance(repo, dict)]
        metadata = self._api("GET", "/v1/network-tools/github-credential")
        if metadata.get("configured") is True:
            validation = (metadata.get("validation") or {}).get("status")
            if validation == "not_checked" and write_repos:
                deadline = time.time() + 30
                while time.time() < deadline and validation == "not_checked":
                    time.sleep(2)
                    metadata = self._api("GET", "/v1/network-tools/github-credential")
                    validation = (metadata.get("validation") or {}).get("status")
            if validation == "ok":
                print(
                    f"  [ok] GitHub credential configured (mode={metadata.get('mode')}, validation=ok)",
                    flush=True,
                )
            else:
                message = (metadata.get("validation") or {}).get("message")
                failures.append(
                    f"GitHub credential validation is {validation!r}"
                    + (f": {message}" if message else "")
                )
        else:
            failures.append(
                "no GitHub credential is configured; set the STAGE_GITHUB_* stage secrets "
                "to auto-configure, or store a write-capable PAT or App credential in the "
                "admin UI (Home > Integrations)"
            )
        if write_repos:
            listed = ", ".join(f"{repo.get('owner')}/{repo.get('repo')}" for repo in write_repos)
            print(f"  [ok] GitHub write repositories in policy: {listed}", flush=True)
        else:
            failures.append(
                "the network policy lists no GitHub write repository; set the STAGE_GITHUB_* "
                "stage secrets to auto-configure, or add a dedicated sandbox write repo in "
                "the admin UI (Home > Integrations)"
            )
        return failures

    def print_configuration_snapshot(self) -> None:
        """Best-effort dump of the operator-facing configuration: runtime
        statuses, the managed-integration enable flags with the GitHub write
        repos, and the GitHub credential state. Printed on any failure so a red
        run shows what was (and was not) configured without another round trip."""
        print("  configuration snapshot:", flush=True)
        try:
            status = self._api("GET", "/v1/agent-runtime/status")
            for runtime in (*SMOKE_RUNTIMES, "grok"):
                try:
                    record = self.runtime_status_record(status, runtime)
                except AssertionError:
                    print(f"    runtime {runtime}: <not present>", flush=True)
                    continue
                detail = record.get("error_message")
                extra = f", error_message={detail!r}" if detail else ""
                print(f"    runtime {runtime}: {record.get('status')}{extra}", flush=True)
        except Exception as exc:  # noqa: BLE001 - best-effort debug output
            print(f"    runtimes: could not read status: {type(exc).__name__}: {exc}", flush=True)
        try:
            policy = self._api("GET", "/v1/network/policy")
            controls = policy.get("network_controls") or {}
            integrations = controls.get("network_integrations") or {}
            enabled = {
                name: (value.get("enabled") if isinstance(value, dict) else None)
                for name, value in integrations.items()
            }
            github = integrations.get("github") or {}
            repos = [
                f"{repo.get('owner')}/{repo.get('repo')}"
                for repo in (github.get("write_repositories") or [])
                if isinstance(repo, dict)
            ]
            print(f"    policy updated_at: {policy.get('updated_at')}", flush=True)
            print(f"    managed integrations enabled: {enabled or '<none>'}", flush=True)
            print(f"    github write repositories: {repos or '<none>'}", flush=True)
        except Exception as exc:  # noqa: BLE001 - best-effort debug output
            print(f"    network policy: could not read: {type(exc).__name__}: {exc}", flush=True)
        try:
            credential = self._api("GET", "/v1/network-tools/github-credential")
            validation = (credential.get("validation") or {}).get("status")
            print(
                f"    github credential configured: {credential.get('configured')} "
                f"(mode={credential.get('mode')}, validation={validation})",
                flush=True,
            )
        except Exception as exc:  # noqa: BLE001 - best-effort debug output
            print(f"    github credential: could not read: {type(exc).__name__}: {exc}", flush=True)

    def require_runtime_active(self, runtime: str) -> None:
        status = self._wait_for_runtime_status(
            {"active", "awaiting_login", "deactivated", "error"},
            runtime=runtime,
            timeout=180,
        )
        if status != "active":
            if runtime == "hermes":
                raise AssertionError(
                    f"{runtime} runtime is {status}; connect the AWS Bedrock credential, then rerun stage"
                )
            raise AssertionError(
                f"{runtime} runtime is {status}; manually open the stage admin UI, complete OAuth, then rerun stage"
            )
        print(f"    [provider status] runtime={runtime} status=active", flush=True)

    def prepare_grok_integration(self, suite: str) -> None:
        """Enable xAI before preflight without disturbing any other stage state.

        Grok's credential is still an operator-completed device login stored on
        the persistent volume. This only makes a first focused run settle at
        ``awaiting_login`` (with useful setup guidance) rather than remaining
        deactivated because an older stage policy predates the integration.
        """
        if suite not in {"all", ALL_RUNTIMES_SUITE, "grok"}:
            return
        controls = self._api("GET", "/v1/network/policy").get("network_controls") or {}
        integrations = dict(controls.get("network_integrations") or {})
        xai = integrations.get("xai")
        if isinstance(xai, dict) and xai.get("enabled") is True:
            return
        integrations["xai"] = {"enabled": True}
        controls["network_integrations"] = integrations
        self._api("PUT", "/v1/network/policy", controls)
        print("  [configured] xAI enabled; Grok web search is not offered", flush=True)

    def check_grok_connection_and_guards(self) -> None:
        """Exercise the live Grok connection and its complete proxy boundary.

        The first half proves real ACP auth, entitlement, and billing. Synthetic
        account-bound JWTs then drive every local proxy decision without
        exposing the persistent OAuth token; the separate turn check exercises
        real subscription inference through the runtime adapter.
        """
        self._step("Grok live connection, account binding, routes, and hosted-tool guards")
        baseline_policy = self.enforcement_policy()
        self._api("PUT", "/v1/network/policy", baseline_policy)
        self.require_runtime_active("grok")

        bootstrap = (Path(__file__).resolve().parents[2] / "host/bootstrap/bootstrap.sh").read_text(
            encoding="utf-8"
        )
        version_line = next(
            (line for line in bootstrap.splitlines() if line.startswith("GROK_CLI_VERSION=")),
            "",
        )
        expected_version = version_line.partition("=")[2]
        if not expected_version:
            raise AssertionError("bootstrap does not declare GROK_CLI_VERSION")
        cli_check = self._ssh_code(
            "test \"$(stat -c '%U:%a' /usr/local/bin/grok)\" = root:755"
            " && sudo -u kern-agent env HOME=/mnt/kern-agent/agent-home"
            " GROK_HOME=/mnt/kern-agent/agent-home/.grok"
            " /usr/local/bin/grok --version"
            f" | grep -qF {shlex.quote(expected_version)}"
            " && printf grok-cli-ok"
        )
        if cli_check != "grok-cli-ok":
            raise AssertionError("the root-owned Grok CLI is missing, mutable, or not the pinned version")

        for method in ("POST", "GET"):
            code, _ = self._api_status(method, "/v1/agent-runtime/grok-oauth-login")
            if code != 409:
                raise AssertionError(
                    f"{method} grok-oauth-login while active returned {code}, expected 409"
                )

        probe_baseline = max((event["seq"] for event in self._network_events()), default=0)
        refreshed = self._api(
            "POST", "/v1/agent-runtime/refresh", {"agent_runtime": "grok"}
        )
        account = next(
            (
                item
                for item in refreshed.get("accounts", [])
                if isinstance(item, dict) and item.get("agent_runtime") == "grok"
            ),
            {},
        )
        if (
            account.get("status") != "active"
            or account.get("provider") != "xai"
            or not account.get("account_id")
        ):
            raise AssertionError(
                "forced Grok provider probe did not return the approved active account: "
                f"{self._account_shape(account)}"
            )
        self._assert_provider_metadata("grok", account)
        account_id = str(account["account_id"])
        probe_events = [
            event
            for event in self._network_events(since=probe_baseline)
            if event.get("host") == "cli-chat-proxy.grok.com"
        ]
        decision_cursor = max(
            [probe_baseline, *(int(event["seq"]) for event in probe_events)]
        )
        if not any(
            event.get("decision") == "allowed" and event.get("path") == "/v1/user"
            for event in probe_events
        ):
            raise AssertionError(f"forced Grok entitlement probe had no allowed /v1/user request: {probe_events}")
        if not any(
            event.get("decision") == "allowed" and event.get("path") == "/v1/billing"
            for event in probe_events
        ):
            raise AssertionError(f"forced Grok usage probe had no allowed /v1/billing request: {probe_events}")
        print(
            "    [provider connection] runtime=grok auth=active entitlement=allowed "
            "billing=allowed metadata=valid",
            flush=True,
        )

        def encoded(value: dict[str, object]) -> str:
            raw = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
            return base64.urlsafe_b64encode(raw).decode().rstrip("=")

        matching_claims = {
            "sub": account_id,
            "principal_id": account_id,
            "principal_type": "User",
        }
        foreign_claims = {
            "sub": account_id + "-foreign",
            "principal_id": account_id + "-foreign",
            "principal_type": "User",
        }
        matching_token = f"{encoded({'alg': 'RS256'})}.{encoded(matching_claims)}.stage"
        foreign_token = f"{encoded({'alg': 'RS256'})}.{encoded(foreign_claims)}.stage"
        matching_headers = [
            ("Authorization", f"Bearer {matching_token}"),
            ("Content-Type", "application/json"),
        ]

        def proxy_request(
            *,
            host: str = "cli-chat-proxy.grok.com",
            path: str = "/v1/responses",
            method: str = "POST",
            headers: list[tuple[str, str]] | None = None,
            body: str | None = "{}",
        ) -> str:
            header_args = " ".join(
                f"-H {shlex.quote(f'{name}: {value}')}" for name, value in (headers or [])
            )
            data_arg = "" if body is None else f" --data-binary {shlex.quote(body)}"
            proxy = f"http://127.0.0.1:{PROXY_PORT}"
            return self._ssh_code(
                f"sudo -u kern-agent env HTTPS_PROXY={proxy} "
                f"curl -sS --path-as-is --max-time 20 -X {shlex.quote(method)} "
                f"{header_args}{data_arg} {shlex.quote(f'https://{host}{path}')}"
            )

        # Start once, then advance after each request. Re-reading the complete
        # retained audit log for every matrix row made this focused check
        # quadratic in host age (about 17 minutes at 27,000 retained events).
        def assert_decision(
            label: str,
            decision: str,
            reason: str | None,
            **request: object,
        ) -> None:
            nonlocal decision_cursor
            host = str(request.get("host", "cli-chat-proxy.grok.com"))
            path = str(request.get("path", "/v1/responses"))
            method = str(request.get("method", "POST"))
            # A disallowed host is rejected at CONNECT, before the proxy sees
            # an inner HTTP method or path.
            event_method = "CONNECT" if reason == "host_not_allowed" else method
            event_path = "" if event_method == "CONNECT" else path
            response = proxy_request(**request)  # type: ignore[arg-type]
            events = [
                event
                for event in self._network_events(since=decision_cursor)
                if event.get("host") == host
                and event.get("path") == event_path
                and event.get("method") == event_method
            ]
            decision_cursor = max(
                [decision_cursor, *(int(event["seq"]) for event in events)]
            )
            matching = [event for event in events if event.get("decision") == decision]
            if not matching:
                raise AssertionError(f"{label} produced no {decision} proxy event: {events}")
            observed_reason = matching[-1].get("reason_code")
            if observed_reason != reason:
                raise AssertionError(
                    f"{label} recorded reason {observed_reason!r}, expected {reason!r}: {matching[-1]}"
                )
            # curl reports a rejected CONNECT on stderr without the proxy's
            # reason body. The persisted event above is the exact-code proof.
            if decision == "denied" and event_method != "CONNECT" and reason not in response:
                raise AssertionError(f"{label} did not return {reason!r} to the client: {response!r}")

        bearer_only = [("Authorization", f"Bearer {matching_token}"), ("Content-Type", "application/json")]
        wrong_token = [
            ("Authorization", f"Bearer {foreign_token}"),
            ("Content-Type", "application/json"),
        ]
        duplicate_token = [
            ("Authorization", f"Bearer {matching_token}"),
            ("Authorization", f"Bearer {matching_token}"),
            ("Content-Type", "application/json"),
        ]

        denial_cases: list[tuple[str, str, dict[str, object]]] = [
            ("missing bearer", "xai_token_account_mismatch", {"headers": [("Content-Type", "application/json")]}),
            ("foreign bearer", "xai_token_account_mismatch", {"headers": wrong_token}),
            # The proxy rejects security-sensitive duplicate headers before a
            # provider guard runs, so this correctly uses the global code.
            ("duplicate bearer", "duplicate_header_denied", {"headers": duplicate_token}),
            ("unsupported method", "network_policy_denied", {"headers": matching_headers, "method": "DELETE"}),
            ("settings mutation", "network_policy_denied", {"headers": matching_headers, "path": "/v1/settings"}),
            ("storage upload", "network_policy_denied", {"headers": matching_headers, "path": "/v1/storage/batch_upload"}),
            ("session sync", "network_policy_denied", {"headers": matching_headers, "path": "/v1/sessions/register"}),
            ("workspace sync", "network_policy_denied", {"headers": matching_headers, "path": "/v1/rest/workspaces"}),
            ("path traversal", "network_policy_denied", {"headers": matching_headers, "path": "/v1/responses/../storage/batch_upload"}),
            ("metered developer API", "host_not_allowed", {"headers": matching_headers, "host": "api.x.ai"}),
            ("cloud session host", "host_not_allowed", {"headers": matching_headers, "host": "code.grok.com"}),
            ("malformed JSON", "xai_body_not_json", {"headers": matching_headers, "body": "{not json"}),
            ("undecodable body", "xai_body_undecodable", {"headers": matching_headers + [("Content-Encoding", "gzip")], "body": "not-gzip"}),
            # Web search is denied with no policy that can turn it on. Both of
            # xAI's ways of asking for one are covered, and the corpus named
            # decides nothing because none is reachable.
            ("Web search tool", "xai_web_search_denied", {"headers": matching_headers, "body": '{"tools":[{"type":"web_search"}]}' }),
            ("search parameters", "xai_web_search_denied", {"headers": matching_headers, "body": '{"search_parameters":{"mode":"on"}}'}),
            ("web-source search parameters", "xai_web_search_denied", {"headers": matching_headers, "body": '{"search_parameters":{"mode":"auto","sources":[{"type":"web"}]}}'}),
            ("news-source search parameters", "xai_web_search_denied", {"headers": matching_headers, "body": '{"search_parameters":{"mode":"on","sources":[{"type":"news"}]}}'}),
            ("X search", "xai_server_tool_denied", {"headers": matching_headers, "body": '{"tools":[{"type":"x_search"}]}' }),
            ("code execution", "xai_server_tool_denied", {"headers": matching_headers, "body": '{"tools":[{"type":"code_execution"}]}' }),
            ("collections search", "xai_server_tool_denied", {"headers": matching_headers, "body": '{"tools":[{"type":"file_search"}]}' }),
            ("remote MCP", "xai_remote_mcp_denied", {"headers": matching_headers, "body": '{"tools":[{"type":"mcp","server_url":"https://example.com"}]}' }),
            ("unknown hosted tool", "xai_server_tool_denied", {"headers": matching_headers, "body": '{"tools":[{"type":"future_hosted_thing"}]}' }),
            ("untyped hosted tool", "xai_server_tool_denied", {"headers": matching_headers, "body": '{"tools":[{"name":"future_hosted_thing"}]}' }),
        ]
        for label, reason, request in denial_cases:
            assert_decision(label, "denied", reason, **request)

        allowed_cases: list[tuple[str, dict[str, object]]] = [
            (
                "responses inference",
                {"headers": bearer_only, "body": '{"model":"grok-code-fast-1","input":"stage"}'},
            ),
            (
                "chat completions inference",
                {
                    "headers": bearer_only,
                    "path": "/v1/chat/completions",
                    "body": '{"model":"grok-code-fast-1","messages":[]}',
                },
            ),
            # A follow-up turn replays what already ran. Its subtrees must not
            # be read as fresh declarations, or multi-turn Grok breaks.
            (
                "replayed hosted-call history",
                {
                    "headers": matching_headers,
                    "body": '{"input":[{"type":"web_search_call","action":{"type":"open_page",'
                            '"url":"https://example.com"}},{"type":"mcp_list_tools",'
                            '"tools":[{"name":"call_tool"}]}]}',
                },
            ),
        ]
        for label, request in allowed_cases:
            assert_decision(label, "allowed", None, **request)

        self.require_runtime_active("grok")
        print(
            f"    [provider guards] runtime=grok denial_cases={len(denial_cases)} "
            f"allowed_cases={len(allowed_cases)} web_search=unavailable",
            flush=True,
        )
        self._ok(
            f"live auth, entitlement, and billing passed; {len(denial_cases)} fail-closed edge "
            f"cases and {len(allowed_cases)} allowed payloads held; Web search is not offered"
        )

    def check_grok_task(self) -> None:
        """Run and resume a real Grok ACP session through the admin API."""
        self._step("Grok ACP turn, activity, and session resume")
        self._api("PUT", "/v1/network/policy", self.enforcement_policy())
        self.require_runtime_active("grok")
        baseline_network = max(
            (event["seq"] for event in self._network_events()), default=0
        )
        baseline = self._latest_thread_event_seq("grok-session")
        started = self.send_message(
            "grok-session",
            (
                "Use the terminal exactly once to run `printf GROK_ACTIVITY_OK`. "
                "Then reply with exactly GROK_STAGE_OK and nothing else."
            ),
            runtime="grok",
            model=CHEAP_MODELS["grok"],
            effort=CHEAP_EFFORT,
        )
        thread = started.get("thread") or {}
        if started.get("status") != "accepted" or (
            thread.get("model"), thread.get("effort")
        ) != (CHEAP_MODELS["grok"], CHEAP_EFFORT):
            raise AssertionError(
                f"Grok turn did not start with the selected session options: {started}"
            )
        done = self._wait_for_turn("grok-session", since=baseline, timeout=300)
        if done.get("status") != "completed" or "GROK_STAGE_OK" not in str(
            done.get("output_message") or ""
        ).upper():
            raise AssertionError(
                f"Grok turn failed or returned unexpected output: {self._thread_failure_detail('grok-session')}"
            )
        activities = [
            event
            for event in self._thread_events("grok-session", since=baseline)
            if event.get("event_type") == "thread.activity"
        ]
        if not activities:
            raise AssertionError("Grok tool turn persisted no activity events")

        follow_up_baseline = self._latest_thread_event_seq("grok-session")
        follow_up = self.send_follow_up(
            "grok-session",
            "Reply with exactly the same uppercase token you returned in the previous turn.",
        )
        if follow_up.get("status") != "accepted":
            raise AssertionError(f"Grok follow-up was not accepted: {follow_up}")
        resumed = self._wait_for_turn(
            "grok-session", since=follow_up_baseline, timeout=300
        )
        if resumed.get("status") != "completed" or "GROK_STAGE_OK" not in str(
            resumed.get("output_message") or ""
        ).upper():
            raise AssertionError(
                f"Grok resumed turn lost its session context: {resumed}"
            )

        provider_events = [
            event
            for event in self._network_events(since=baseline_network)
            if event.get("host") == "cli-chat-proxy.grok.com"
        ]
        inference = [
            event
            for event in provider_events
            if event.get("path") in {"/v1/responses", "/v1/chat/completions"}
        ]
        if not inference or any(event.get("decision") != "allowed" for event in inference):
            raise AssertionError(
                f"Grok turns had no clean allowed inference path: {provider_events}"
            )
        self._ok(
            "real Grok turn completed with activity, resumed its ACP session, and inference stayed inside the xAI guard"
        )

    def check_task(self) -> None:
        self._step("Codex account guard + real web-search turn")
        # Publish the full enforcement policy (not just the provider bundle) so a
        # provider-only run keeps the GitHub integration and its write
        # repositories in the stored policy instead of erasing them.
        self._api("PUT", "/v1/network/policy", self.enforcement_policy())
        self.require_runtime_active("codex")
        for method in ("POST", "GET"):
            code, _ = self._api_status(method, "/v1/agent-runtime/codex-oauth-login")
            if code != 409:
                raise AssertionError(f"{method} codex-oauth-login while active returned {code}, expected 409")
        account = self._agent_account("codex")
        if account.get("status") != "active":
            raise AssertionError(
                f"GET account while active did not report active: {self._account_shape(account)}"
            )
        account_id = account.get("account_id")
        if not account_id:
            raise AssertionError(
                f"GET account while active did not include account_id: {self._account_shape(account)}"
            )
        self._assert_provider_metadata("codex", account)
        print("    [provider account] runtime=codex status=active metadata=valid", flush=True)

        proxy = f"http://127.0.0.1:{PROXY_PORT}"
        url = "https://chatgpt.com/backend-api/codex/responses"
        cached = '{"tools":[{"type":"web_search","external_web_access":false}]}'

        def post_openai(payload: str, account_header: str | None = account_id) -> str:
            header = "" if account_header is None else f" -H {shlex.quote(f'ChatGPT-Account-Id: {account_header}')}"
            return self._ssh_code(
                f"sudo -u kern-agent env HTTPS_PROXY={proxy} "
                f"curl -s --max-time 20 -X POST -H 'Content-Type: application/json' "
                f"{header} --data {shlex.quote(payload)} {shlex.quote(url)}"
            )

        missing_account_response = post_openai(cached, account_header=None)
        wrong_account_response = post_openai(cached, account_header=f"{account_id}-wrong")
        missing_token_response = post_openai(cached)
        if "openai_account_header_required" not in missing_account_response:
            raise AssertionError(f"missing account header was not blocked; proxy returned {missing_account_response!r}")
        if "openai_account_mismatch" not in wrong_account_response:
            raise AssertionError(f"wrong account header was not blocked; proxy returned {wrong_account_response!r}")
        if "openai_token_account_mismatch" not in missing_token_response:
            raise AssertionError(
                "request without the pinned account token was not blocked; "
                f"proxy returned {missing_token_response!r}"
            )
        print(
            "    [provider guards] runtime=codex missing-account=denied "
            "wrong-account=denied missing-token=denied",
            flush=True,
        )

        baseline_seq = max((event["seq"] for event in self._network_events()), default=0)
        turn_baseline = self._latest_thread_event_seq("codex-web")
        prompt = "Use your web search tool to check today's date, then reply with the word DONE."
        started = self.send_message("codex-web", prompt)
        thread = started.get("thread") or {}
        if started.get("status") != "accepted" or (thread.get("model"), thread.get("effort")) != (
            CHEAP_MODELS["codex"],
            CHEAP_EFFORT,
        ):
            raise AssertionError(f"Codex turn did not start with the selected session options: {started}")
        self._print_agent_turn(
            "codex", "web-search", "started",
            thread_id=thread.get("thread_id"), model=thread.get("model"), effort=thread.get("effort"),
        )
        current = self._wait_for_turn("codex-web", since=turn_baseline, timeout=240)
        self._print_agent_turn("codex", "web-search", "finished", status=current["status"])
        events = self._network_events(since=baseline_seq)
        chatgpt = [event for event in events if event["host"].endswith("chatgpt.com")]
        denied = [event for event in chatgpt if event["decision"] == "denied"]
        fatal = [event for event in denied if event["path"].startswith("/backend-api/codex/responses")]
        if current["status"] != "completed":
            raise AssertionError(f"turn did not complete: {current}; denied chatgpt.com events: {denied}")
        if fatal:
            raise AssertionError(f"the guard denied agent ChatGPT turn traffic: {fatal}")
        if not any(event["decision"] == "allowed" for event in chatgpt):
            raise AssertionError(f"no allowed chatgpt.com traffic was observed for the turn: {events}")
        print(
            f"    [provider network] runtime=codex chatgpt_events={len(chatgpt)} "
            f"allowed={sum(event['decision'] == 'allowed' for event in chatgpt)} "
            f"denied={len(denied)} fatal_denials={len(fatal)}",
            flush=True,
        )
        self._ok("web search turn completed; account and external URL request guards held")

    def check_claude_auth_and_task(self) -> None:
        self._step("Claude account guard + real turn")
        # Publish the full enforcement policy (not just the provider bundle) so a
        # provider-only run keeps the GitHub integration and its write
        # repositories in the stored policy instead of erasing them.
        self._api("PUT", "/v1/network/policy", self.enforcement_policy())
        self.require_runtime_active("claude_code")
        for method in ("POST", "GET"):
            code, _ = self._api_status(method, "/v1/agent-runtime/claude-oauth-login")
            if code != 409:
                raise AssertionError(f"{method} claude-oauth-login while active returned {code}, expected 409")
        account = self._agent_account("claude_code")
        if (
            account.get("status") != "active"
            or account.get("provider") != "claude"
            or not account.get("account_id")
            or "email" not in account
        ):
            raise AssertionError(
                "GET account while Claude is active returned unexpected shape: "
                f"{self._account_shape(account)}"
            )
        self._assert_provider_metadata("claude_code", account)
        print("    [provider account] runtime=claude_code status=active metadata=valid", flush=True)

        proxy = f"http://127.0.0.1:{PROXY_PORT}"
        url = "https://api.anthropic.com/v1/messages"
        payload = '{"model":"claude-sonnet-4-5","max_tokens":8,"messages":[{"role":"user","content":"hello"}]}'
        missing = self._ssh_code(
            f"sudo -u kern-agent env HTTPS_PROXY={proxy} "
            f"curl -s --max-time 20 -X POST -H 'Content-Type: application/json' "
            f"--data {shlex.quote(payload)} {shlex.quote(url)}"
        )
        wrong = self._ssh_code(
            f"sudo -u kern-agent env HTTPS_PROXY={proxy} "
            f"curl -s --max-time 20 -X POST -H 'Content-Type: application/json' "
            f"-H 'Authorization: Bearer stage-wrong-token' "
            f"--data {shlex.quote(payload)} {shlex.quote(url)}"
        )
        if "anthropic_token_required" not in missing:
            raise AssertionError(f"missing Claude bearer was not blocked; proxy returned {missing!r}")
        if "anthropic_token_mismatch" not in wrong:
            raise AssertionError(f"wrong Claude bearer was not blocked; proxy returned {wrong!r}")
        print(
            "    [provider guards] runtime=claude_code missing-token=denied wrong-token=denied",
            flush=True,
        )

        task_baseline_seq = max((event["seq"] for event in self._network_events()), default=0)
        turn_baseline = self._latest_thread_event_seq("claude")
        prompt = "Reply with exactly the word CLAUDE_STAGE_OK and nothing else."
        started = self.send_message("claude", prompt)
        thread = started.get("thread") or {}
        if started.get("status") != "accepted" or (thread.get("model"), thread.get("effort")) != (
            CHEAP_MODELS["claude_code"],
            CHEAP_EFFORT,
        ):
            raise AssertionError(f"Claude turn did not start with the selected session options: {started}")
        self._print_agent_turn(
            "claude_code", "session", "started",
            thread_id=thread.get("thread_id"), model=thread.get("model"), effort=thread.get("effort"),
        )
        done = self._wait_for_turn("claude", since=turn_baseline, timeout=240)
        self._print_agent_turn("claude_code", "session", "finished", status=done["status"])
        if done["status"] != "completed":
            raise AssertionError(f"Claude turn ended {done['status']}: {self._thread_failure_detail('claude')}")
        if "CLAUDE_STAGE_OK" not in (done.get("output_message") or ""):
            raise AssertionError(f"Claude turn output did not contain expected token: {done.get('output_message')!r}")
        events = self._network_events(since=task_baseline_seq)
        anthropic = [event for event in events if event["host"] == "api.anthropic.com"]
        if not any(event["decision"] == "allowed" for event in anthropic):
            raise AssertionError(f"Claude turn completed without an allowed api.anthropic.com event: {events}")
        fatal = [
            event for event in anthropic
            if event["decision"] == "denied" and event["path"].startswith("/v1/messages")
        ]
        if fatal:
            raise AssertionError(f"Claude turn had denied message traffic: {fatal}")
        print(
            f"    [provider network] runtime=claude_code anthropic_events={len(anthropic)} "
            f"allowed={sum(event['decision'] == 'allowed' for event in anthropic)} "
            f"fatal_denials={len(fatal)}",
            flush=True,
        )

        follow_up_prompt = (
            "Earlier in this Claude conversation you replied with one uppercase token. "
            "Reply with exactly that token again and nothing else."
        )
        follow_up_baseline = self._latest_thread_event_seq("claude")
        follow_up = self.send_follow_up("claude", follow_up_prompt)
        if follow_up.get("status") != "accepted":
            raise AssertionError(f"Claude follow-up was not started on the idle thread: {follow_up}")
        self._print_agent_turn("claude_code", "session-follow-up", "started", thread_id=self.api_thread_id("claude"))
        follow_up_done = self._wait_for_turn("claude", since=follow_up_baseline, timeout=240)
        self._print_agent_turn("claude_code", "session-follow-up", "finished", status=follow_up_done["status"])
        if follow_up_done["status"] != "completed":
            raise AssertionError(
                f"Claude follow-up ended {follow_up_done['status']}: "
                f"{self._thread_failure_detail('claude')}"
            )
        if "CLAUDE_STAGE_OK" not in (follow_up_done.get("output_message") or ""):
            raise AssertionError(
                "Claude follow-up did not resume the persisted session context: "
                f"{follow_up_done.get('output_message')!r}"
            )
        self._ok("Claude account guard passed; real turn completed and resumed through the proxy")

    def check_package_client_headers_e2e(self) -> None:
        """Drive the real pip and npm-registry clients through the proxy.

        The proxy sees whatever a real client chooses to send — conditional
        revalidation, ranged downloads, and provider-issued URLs — while a
        unit test can only assert the traffic someone thought to invent. This
        check exists so an integration tightened past real client traffic
        fails here rather than in an operator's ``pip install``.

        The assertion is deliberately the absence of a denial: any
        ``request_param_*`` reason recorded against these hosts while the
        clients ran means the guard rejected traffic a normal client emits.

        Not covered here, and still unit-tested only: custom-domain
        ``If-Match`` writes and the WebSocket handshake nonce, neither of
        which a stage host emits from a real client.
        """
        self._step("package clients e2e (pip and npm registry through the proxy)")
        policy = self.enforcement_policy()
        policy["network_integrations"]["python_packages"] = {"enabled": True}
        policy["network_integrations"]["npm_packages"] = {"enabled": True}
        self._api("PUT", "/v1/network/policy", policy)
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

        workdir = f"/tmp/kern-stage-packages-{time.time_ns()}"
        baseline_seq = max((event["seq"] for event in self._network_events()), default=0)
        try:
            # Kern installs uv, not a system-wide pip module. Seed a venv with
            # uv (also testing its proxy-CA path), then drive the venv's pip.
            venv_result = self._ssh_code(
                agent_command(f"uv venv --seed {workdir}/venv")
                + " 2>&1 && echo __KERN_VENV_OK__"
            ).strip()
            if not venv_result.endswith("__KERN_VENV_OK__"):
                raise AssertionError(
                    "could not create the temporary pip venv: "
                    f"{venv_result[-2000:]!r}"
                )
            # Twice: the first pass resolves and downloads, the second lets
            # pip's cache layer revalidate, which is what puts a real
            # provider-issued validator on the wire.
            for attempt in (1, 2):
                done = self._ssh_code(
                    agent_command(
                        f"{workdir}/venv/bin/python -m pip download --quiet --no-deps "
                        f"--disable-pip-version-check --dest {workdir}/downloads-{attempt} "
                        "certifi"
                    )
                    + " 2>&1 && echo __KERN_PIP_OK__"
                ).strip()
                if not done.endswith("__KERN_PIP_OK__"):
                    raise AssertionError(
                        f"pip download through the proxy failed on pass {attempt}; "
                        f"output: {done[-2000:]!r}"
                    )
                print(f"    [pip download] pass={attempt} result=success", flush=True)
            # npm metadata, then a ranged fetch: curl stands in for the npm
            # binary, which a stage host is not guaranteed to carry, while
            # still exercising the same forwarded Range and validator fields.
            etag = self._ssh_code(
                agent_command(
                    "curl -sS -D- -o /dev/null https://registry.npmjs.org/lodash "
                    "| tr -d '\\r' | awk 'tolower($1)==\"etag:\"{print $2}'"
                )
            ).strip()
            if not etag:
                raise AssertionError("npm registry metadata read through the proxy returned no ETag")
            revalidated = self._ssh_code(
                agent_command(
                    "curl -sS -o /dev/null -w '%{http_code}' "
                    f"-H 'If-None-Match: {etag}' https://registry.npmjs.org/lodash"
                )
            ).strip()
            if revalidated not in {"200", "304"}:
                raise AssertionError(
                    f"npm registry revalidation returned {revalidated!r}; the guard may be "
                    "denying a provider-issued validator"
                )
            ranged = self._ssh_code(
                agent_command(
                    "curl -sS -o /dev/null -w '%{http_code}' -r 0-1023 "
                    "https://registry.npmjs.org/lodash/-/lodash-4.17.21.tgz"
                )
            ).strip()
            if ranged not in {"200", "206"}:
                raise AssertionError(
                    f"npm tarball ranged download returned {ranged!r}; the bounded Range "
                    "exemption may be narrower than a real client's request"
                )
            print(f"    [npm registry] revalidate={revalidated} ranged={ranged}", flush=True)
        finally:
            self._ssh_code(f"sudo -u kern-agent rm -rf {workdir}")
        denied = [
            event
            for event in self._network_events(baseline_seq)
            if str(event.get("reason_code") or "").startswith("request_param_")
        ]
        if denied:
            detail = ", ".join(
                f"{event.get('method')} {event.get('host')}{event.get('path')} -> {event.get('reason_code')}"
                for event in denied[:5]
            )
            raise AssertionError(
                "the parameter guard denied ordinary package-client traffic, so a guard rule "
                f"is tighter than real clients: {detail}"
            )
        self._ok("pip and npm registry clients completed through the proxy")

    def check_github_write_e2e(self) -> None:
        """End-to-end exercise of the authenticated GitHub paths with a real,
        operator-installed write credential: clone, a real branch push
        (receive-pack), authenticated gh API read and write, and the write
        denial on an unlisted repo. Like the provider OAuth logins, this
        requires one-time stage-host configuration and fails with instructions
        until it is done: a write-capable credential stored and at least one
        sandbox write repository in the policy (real branches are pushed and
        deleted there). The pushed branch is deleted through the API."""
        write_repos = [
            repo for repo in getattr(self, "stage_github_repositories", []) if isinstance(repo, dict)
        ]
        if not write_repos:
            raise AssertionError(
                "the stage host's network policy lists no GitHub write repository; "
                "add a dedicated sandbox write repo in the admin UI (Home > Integrations), "
                "then rerun; like the provider logins, this is one-time "
                "stage-host configuration"
            )
        metadata = self._api("GET", "/v1/network-tools/github-credential")
        if metadata.get("configured") is not True:
            raise AssertionError(
                "no GitHub credential is configured on the stage host; store a write-capable "
                "PAT or App credential in the admin UI (Home > Integrations), then rerun; "
                "like the provider logins, this is one-time stage-host configuration"
            )
        write_repo = f"{write_repos[0]['owner']}/{write_repos[0]['repo']}"
        self._step(f"github write e2e against {write_repo} (operator-installed credential)")
        # Self-contained regardless of suite or ordering: the provider checks
        # reset the policy to a GitHub-less bundle, so publish the GitHub-enabled
        # enforcement policy (with the stage write repositories) before
        # exercising the authenticated paths.
        self._api("PUT", "/v1/network/policy", self.enforcement_policy())
        proxy = f"http://127.0.0.1:{PROXY_PORT}"
        env = f"sudo -u kern-agent env HTTPS_PROXY={proxy} https_proxy={proxy}"
        branch = f"stage-e2e-{time.time_ns()}"
        workdir = f"/tmp/kern-stage-github-{time.time_ns()}"
        baseline_seq = max((event["seq"] for event in self._network_events()), default=0)
        try:
            cloned = self._ssh_code(
                f"{env} git clone --depth 1 https://github.com/{write_repo} {workdir} "
                ">/dev/null 2>&1 && echo cloned"
            ).strip()
            if cloned != "cloned":
                raise AssertionError(f"authenticated clone of {write_repo} through the proxy failed")
            print(f"    [github clone] repo={write_repo} result=success", flush=True)
            pushed = self._ssh_code(
                f"{env} sh -c 'cd {workdir} && git config user.email stage@kern.invalid && "
                f"git config user.name kern-stage && echo {branch} > STAGE_E2E.txt && "
                f"git add STAGE_E2E.txt && git commit -q -m stage-e2e && "
                f"git push -q origin HEAD:refs/heads/{branch} && echo pushed' 2>/dev/null"
            ).strip()
            if pushed != "pushed":
                raise AssertionError(f"git push of {branch} to {write_repo} failed")
            print(f"    [github push] repo={write_repo} branch={branch} result=success", flush=True)
            # Authenticated API read through the gh shim proves the pushed
            # branch is real on GitHub, not just accepted by the proxy.
            seen = self._ssh_code(
                f"{env} gh api repos/{write_repo}/branches/{branch} --jq .name 2>/dev/null"
            ).strip()
            if seen != branch:
                raise AssertionError(f"pushed branch not visible via gh api: {seen!r}")
            print(f"    [github read] branch={branch} visible=true", flush=True)
            # Authenticated API write: delete the ref (also the cleanup). Capture
            # the outcome so a real write failure (e.g. a missing permission)
            # reads distinctly from the branch merely lingering below.
            delete_result = self._ssh_code(
                f"{env} sh -c 'gh api -X DELETE repos/{write_repo}/git/refs/heads/{branch} 2>&1 "
                "&& echo DELETE_OK || echo DELETE_FAILED'"
            ).strip()
            if not delete_result.endswith("DELETE_OK"):
                raise AssertionError(f"gh api DELETE of {branch} on {write_repo} failed: {delete_result!r}")
            # GitHub's branches REST endpoint can briefly still report a ref that
            # was just deleted through the git data plane, so confirm the branch
            # is gone with a few short retries instead of a single racy read.
            deleted = "present"
            for _ in range(6):
                deleted = self._ssh_code(
                    f"{env} sh -c 'gh api repos/{write_repo}/branches/{branch} >/dev/null 2>&1 "
                    "&& echo present || echo deleted'"
                ).strip()
                if deleted == "deleted":
                    break
                time.sleep(2)
            if deleted != "deleted":
                raise AssertionError(f"gh api DELETE did not remove {branch} from {write_repo} after retries")
            print(f"    [github cleanup] branch={branch} deleted=true", flush=True)
            # The same credential must not be able to push to an unlisted repo:
            # the proxy denies receive-pack before GitHub sees it.
            denied = self._ssh_code(
                f"{env} git -C {workdir} push -q https://github.com/torvalds/linux "
                f"HEAD:refs/heads/{branch} >/dev/null 2>&1 && echo pushed || echo denied"
            ).strip()
            if denied != "denied":
                raise AssertionError("push to an unlisted repo was not denied by the proxy")
            print("    [github guard] unlisted_repo_push=denied", flush=True)
            self._check_github_dot_github_approval_e2e(write_repo, env, baseline_seq)
        except Exception:
            self._print_denied_github_events(baseline_seq)
            raise
        finally:
            self._ssh_code(f"sudo rm -rf {workdir}")
        self._ok(
            f"clone + push + gh api read/write on {write_repo}, branch {branch} cleaned up, "
            "unlisted push denied, .github approval queued and approved"
        )

    def _check_github_dot_github_approval_e2e(self, write_repo: str, env: str, baseline_seq: int) -> None:
        """Real stage coverage for the .github approval gate: enable the toggle,
        prove REST bypasses are denied, queue a .github-changing push, approve it,
        confirm it lands on GitHub, then delete the branch through git."""
        owner, repo = write_repo.split("/", 1)
        branch = f"stage-dotgithub-{time.time_ns()}"
        ref = f"refs/heads/{branch}"
        workdir = f"/tmp/kern-stage-dotgithub-{time.time_ns()}"
        self._api("PUT", "/v1/network/policy", self.enforcement_policy())
        branch_landed = False
        pending_id = None
        try:
            seed_script = f"""
rm -rf {shlex.quote(workdir)}
git clone --depth 1 https://github.com/{shlex.quote(write_repo)} {shlex.quote(workdir)} >/dev/null 2>&1 || {{ echo CLONE_FAILED; exit 0; }}
cd {shlex.quote(workdir)} || {{ echo CD_FAILED; exit 0; }}
git config user.email stage@kern.invalid
git config user.name kern-stage
git checkout -q -b {shlex.quote(branch)}
echo {shlex.quote(branch)} > STAGE_DOTGITHUB_BASE.txt
git add STAGE_DOTGITHUB_BASE.txt
git commit -q -m stage-dotgithub-base
git push -q origin HEAD:{shlex.quote(ref)} >/dev/null 2>&1 && echo SEED_PUSHED || echo SEED_FAILED
"""
            seed_output = self._ssh_code(f"{env} sh -c {shlex.quote(seed_script)}").strip()
            if seed_output != "SEED_PUSHED":
                raise AssertionError(f"setup branch for .github approval push failed: {seed_output!r}")
            branch_landed = True
            print(f"    [github approval seed] branch={branch} result=success", flush=True)

            approval_policy = self.enforcement_policy()
            github = dict(approval_policy["network_integrations"]["github"])
            github["require_dot_github_approval"] = True
            approval_policy["network_integrations"]["github"] = github
            self._api("PUT", "/v1/network/policy", approval_policy)

            rest_code = self._ssh_code(
                f"{env} curl -s -o /dev/null -w '%{{http_code}}' --max-time 20 "
                f"-X PUT -d '{{\"message\":\"stage\",\"content\":\"eA==\"}}' "
                f"https://api.github.com/repos/{write_repo}/contents/.github/CODEOWNERS || true"
            ).strip()
            if rest_code != "403":
                raise AssertionError(f".github REST contents write should be denied by approval gate, got {rest_code!r}")
            print("    [github approval guard] dot_github_rest_status=403", flush=True)

            script = f"""
cd {shlex.quote(workdir)} || {{ echo CD_FAILED; exit 0; }}
mkdir -p .github
printf 'stage * @kern-stage\\n' > .github/CODEOWNERS
git add .github/CODEOWNERS
git commit -q -m stage-dotgithub-approval
git push origin HEAD:{shlex.quote(ref)} > /tmp/kern-stage-dotgithub-push.out 2>&1
status=$?
cat /tmp/kern-stage-dotgithub-push.out
echo PUSH_STATUS:$status
"""
            push_output = self._ssh_code(f"{env} sh -c {shlex.quote(script)}")
            if "CD_FAILED" in push_output:
                raise AssertionError(f"setup for .github approval push failed: {push_output!r}")
            if "PUSH_STATUS:0" in push_output:
                raise AssertionError(".github-changing push succeeded instead of being queued for approval")
            if "queued for approval" not in push_output:
                raise AssertionError(f".github-changing push did not report approval queue: {push_output!r}")

            pending = None
            deadline = time.time() + 30
            while time.time() < deadline:
                pushes = self._api("GET", "/v1/network-tools/github-pending-pushes").get("pending_pushes") or []
                for candidate in pushes:
                    updates = candidate.get("ref_updates") or []
                    if (
                        candidate.get("owner") == owner.lower()
                        and candidate.get("repo") == repo.lower()
                        and candidate.get("status") == "pending"
                        and any(update.get("ref") == ref for update in updates if isinstance(update, dict))
                    ):
                        pending = candidate
                        break
                if pending is not None:
                    break
                time.sleep(2)
            if pending is None:
                raise AssertionError(f"no pending .github push found for {ref}")
            pending_id = pending["id"]
            changed_paths = pending.get("changed_paths") or []
            if ".github/CODEOWNERS" not in changed_paths:
                raise AssertionError(f"pending push did not record .github/CODEOWNERS: {pending}")
            print(
                f"    [github approval queued] pending_ref={diagnostic_ref(pending_id)} ref={ref} "
                f"changed_paths={changed_paths}",
                flush=True,
            )

            approved = self._api("POST", f"/v1/network-tools/github-pending-pushes/{pending_id}/approve")
            if (approved.get("pending_push") or {}).get("status") != "approved":
                raise AssertionError(f"approval did not mark push approved: {approved}")
            print(
                f"    [github approval result] pending_ref={diagnostic_ref(pending_id)} "
                "status=approved",
                flush=True,
            )
            pending_id = None
            seen = self._ssh_code(
                f"{env} gh api repos/{write_repo}/branches/{branch} --jq .name 2>/dev/null"
            ).strip()
            if seen != branch:
                raise AssertionError(f"approved .github branch not visible via gh api: {seen!r}")

            deleted = self._ssh_code(
                f"{env} git -C {workdir} push -q origin :{shlex.quote(ref)} >/dev/null 2>&1 "
                "&& echo deleted || echo delete_failed"
            ).strip()
            if deleted != "deleted":
                raise AssertionError(f"approved branch cleanup via git delete failed: {deleted!r}")
            branch_landed = False
            print(f"    [github approval cleanup] branch={branch} deleted=true", flush=True)
            reasons = {
                reason
                for event in self._network_events(since=baseline_seq)
                if event.get("host") in {"github.com", "api.github.com"}
                for reason in [event.get("reason_code")]
                if isinstance(reason, str)
            }
            for reason in ("github_dot_github_rest_write_denied", "github_push_queued_for_approval"):
                if reason not in reasons:
                    raise AssertionError(f"missing network event reason code {reason!r} after approval e2e: {sorted(reasons)}")
        finally:
            if pending_id:
                try:
                    self._api("POST", f"/v1/network-tools/github-pending-pushes/{pending_id}/reject")
                except Exception:
                    pass
            try:
                self._api("PUT", "/v1/network/policy", self.enforcement_policy())
            except Exception:
                pass
            if branch_landed:
                self._ssh_code(
                    f"{env} gh api -X DELETE repos/{write_repo}/git/refs/heads/{branch} >/dev/null 2>&1 || true"
                )
            self._ssh_code(f"sudo rm -rf {workdir} /tmp/kern-stage-dotgithub-push.out")

    def _print_denied_github_events(self, since: int) -> None:
        """Dump denied GitHub network events since ``since`` with their reason
        codes: what separates an unloadable policy ('network_policy_unavailable')
        from a host that simply is not allowed ('network_policy_denied')."""
        github_hosts = {
            "github.com", "api.github.com", "uploads.github.com",
            "codeload.github.com", "raw.githubusercontent.com",
        }
        try:
            events = [
                event for event in self._network_events(since=since)
                if event.get("host") in github_hosts and event.get("decision") == "denied"
            ]
        except Exception as exc:  # noqa: BLE001 - best-effort debug output
            print(f"  denied GitHub events: could not read: {type(exc).__name__}: {exc}", flush=True)
            return
        if not events:
            print("  denied GitHub events during write e2e: <none>", flush=True)
            return
        print(f"  denied GitHub events during write e2e ({len(events)}):", flush=True)
        for event in events:
            print(
                f"    seq={event.get('seq')} {event.get('method')} "
                f"{event.get('host')}{event.get('path')} reason={event.get('reason')!r}",
                flush=True,
            )
