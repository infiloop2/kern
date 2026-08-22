#!/usr/bin/env python3
"""Persistent staging test for an already deployed Kern host.

Unlike the smoke test, this does not deploy or tear down the host. It assumes a
stage host was upgraded/recovered with stable admin and agent data volumes.
The ``all`` suite checks every integration's credentials before testing: an
unconfigured or expired integration is reported and skipped without hiding
results for configured integrations. Focused suites remain strict and fail
when their selected integration is unavailable.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from urllib.parse import quote

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from host.session_options import public_session_options
from tests.stage.stage_bedrock_checks import StageBedrockChecks
from tests.stage.stage_integration_checks import ALL_RUNTIMES_SUITE, StageIntegrationChecks
from tests.stage.stage_tool_checks import StageToolChecks
from tests.stage.stage_support import (
    CHEAP_EFFORT,
    CHEAP_MODELS,
    RUNTIME_LABELS as _RUNTIME_LABELS,
    STAGE_AGENT_NAME,
    STAGE_SUITES,
    TOOL_SUITES,
    StageReport,
    bedrock_credential_from_env as _bedrock_credential_from_env,
    github_app_config_from_env as _github_app_config_from_env,
    integration_label as _integration_label,
    record_check as _record_check,
    suite_tools,
    write_action_summary as _write_action_summary,
)

# The operator-selectable suites: everything stage_support knows, plus the
# all_runtimes convenience suite that runs the four runtime suites at once.
STAGE_SUITE_CHOICES = (STAGE_SUITES[0], ALL_RUNTIMES_SUITE, *STAGE_SUITES[1:])
STAGE_CHAT_RUNTIMES = ("codex", "claude_code", "grok", "hermes")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--result-file", required=True, help="Result JSON from the stage deploy, upgrade, recover, or start run.")
    ssh_key_group = parser.add_mutually_exclusive_group(required=True)
    ssh_key_group.add_argument("--ssh-key", help="Private SSH key path for the persistent stage operator key.")
    ssh_key_group.add_argument(
        "--ssh-key-env",
        help="Environment variable containing the private SSH key path for the persistent stage operator key.",
    )
    parser.add_argument(
        "--admin-password-env",
        help="Environment variable containing the stage admin password when the result file omits it.",
    )
    parser.add_argument(
        "--summary-file",
        type=Path,
        help="Write the integration result table here for the workflow's final Actions summary step.",
    )
    parser.add_argument(
        "--suite",
        choices=STAGE_SUITE_CHOICES,
        default="all",
        help=(
            "Which test suite to run. 'claude', 'codex', 'grok', 'hermes', or 'github' run that integration's "
            "checks only (plus the shared preamble); 'all_runtimes' runs the Codex, Claude Code, Grok, and "
            "Hermes runtime checks in one invocation; each bundled tool id runs that tool's "
            "live check; 'all' (default) checks credentials for every integration first, "
            "skips unavailable integrations, and runs every available integration "
            "independently. A focused suite still fails when its required credential is absent "
            "('all_runtimes' fails when any of the four runtimes is unavailable, but still runs "
            "the available ones)."
        ),
    )
    args = parser.parse_args(argv)

    ssh_key = Path(args.ssh_key) if args.ssh_key is not None else _required_env_path(args.ssh_key_env)
    suite = args.suite
    report = StageReport(suite)
    stage = StageAwsSmoke(Path(args.result_file), ssh_key, args.admin_password_env)
    selected_tools = suite_tools(suite)
    run_network_baseline = 0
    try:
        stage.open_tunnel()
        # One optional credential configures the Bedrock provider. The
        # POST validates it synchronously before the provider is enabled.
        stage.autoconfigure_bedrock(suite)
        # Install the GitHub App credential and sandbox write repo from CI
        # secrets when present, so the GitHub checks need no manual operator
        # setup. A no-op when the secrets are absent or GitHub is out of scope.
        stage.autoconfigure_github(suite)
        # xAI has no CI credential: enable its managed integration before
        # preflight, then use the operator-completed login retained by stage.
        stage.prepare_grok_integration(suite)
        stage.autoconfigure_tools(selected_tools)
        availability = stage.integration_availability(suite)
        unavailable = {name: reason for name, reason in availability.items() if reason is not None}
        if suite != "all":
            # Focused suites stay strict: an unavailable integration is a
            # failure. all_runtimes records each unavailable runtime as failed
            # but still runs the available runtimes' checks below.
            for integration, reason in unavailable.items():
                assert reason is not None
                report.add(_integration_label(integration), "unavailable", "failed", reason)
            if unavailable and suite != ALL_RUNTIMES_SUITE:
                raise AssertionError(next(iter(unavailable.values())))
        else:
            for integration, reason in unavailable.items():
                assert reason is not None
                report.add(_integration_label(integration), "unavailable", "skipped", reason)

        ready_runtimes = tuple(
            runtime
            for integration, runtime in (
                ("codex", "codex"),
                ("claude", "claude_code"),
                ("grok", "grok"),
                ("hermes", "hermes"),
            )
            if availability.get(integration) is None and integration in availability
        )
        stage.recover_baseline(ready_runtimes)
        run_network_baseline = max(
            (event["seq"] for event in stage._network_events()),
            default=0,
        )

        try:
            stage.check_health()
            stage.check_ui_page()
            stage.check_admin_auth()
            stage.check_agent_file_explorer()
            stage.check_network_event_prune_race()
        except Exception as exc:
            report.add("Core host", "n/a", "failed", f"{type(exc).__name__}: {exc}")
            for integration, reason in availability.items():
                if reason is None:
                    report.add(
                        _integration_label(integration),
                        "available",
                        "skipped",
                        "not run because the shared host checks failed",
                    )
            raise
        report.add("Core host", "n/a", "passed", "health, UI, auth, files, and event pruning")

        passed_runtimes: list[str] = []
        if availability.get("codex") is None and "codex" in availability:
            def check_codex() -> None:
                stage.agent_runtime = "codex"
                stage.check_task()
                stage.check_agent_mcp_catalog("codex")
                stage.check_agent_steering()
                stage.check_agent_kill_and_thread_survival()

            if _record_check(report, "codex", check_codex, "guards, MCP catalog, turns, steering, and stop recovery"):
                passed_runtimes.append("codex")
        if availability.get("claude") is None and "claude" in availability:
            def check_claude() -> None:
                stage.agent_runtime = "claude_code"
                stage.check_claude_auth_and_task()
                stage.check_agent_mcp_catalog("claude_code")
                stage.check_agent_steering()
                stage.check_agent_kill_and_thread_survival()

            if _record_check(report, "claude", check_claude, "guards, MCP catalog, turns, steering, and stop recovery"):
                passed_runtimes.append("claude_code")
        if availability.get("grok") is None and "grok" in availability:
            def check_grok() -> None:
                stage.agent_runtime = "grok"
                stage.check_grok_connection_and_guards()
                stage.check_grok_task()
                stage.check_agent_mcp_catalog("grok")
                stage.check_agent_workspace_mcp("grok")
                stage.check_agent_steering()
                stage.check_agent_kill_and_thread_survival()

            if _record_check(
                report,
                "grok",
                check_grok,
                "auth, guards, ACP turns/resume, MCP catalog/Workspace identity, steering, and stop recovery",
            ):
                passed_runtimes.append("grok")
        if availability.get("hermes") is None and "hermes" in availability:
            def check_hermes() -> None:
                stage.agent_runtime = "hermes"
                stage.check_bedrock_auth_and_task()
                stage.check_agent_mcp_catalog("hermes")
                stage.check_agent_kill_and_thread_survival(expect_steering_denied=True)

            if _record_check(
                report,
                "hermes",
                check_hermes,
                "credential boundary, real turn, MCP catalog, session resume, steering denial, and stop recovery",
            ):
                passed_runtimes.append("hermes")

        if passed_runtimes:
            _record_check(
                report,
                "thread_admin_api",
                stage.check_thread_admin_api_contract,
                "flat events, pagination, validation, and stop behavior",
            )
            _record_check(
                report,
                "thread_session_switch",
                lambda: stage.check_idle_session_switch_handoff(
                    tuple(passed_runtimes)
                ),
                (
                    "cross-provider switch with summarized activity handoff"
                    if len(passed_runtimes) > 1
                    else "same-provider model switch with summarized activity handoff"
                ),
            )
            _record_check(
                report,
                "stable_apps",
                lambda: stage.check_stable_app_basics(passed_runtimes[0]),
                "Agent Chat messaging, typed retained history and memory clear, plus App Builder generation through the cheapest configured model",
            )

        if suite == "all":
            if "hermes" in passed_runtimes:
                _record_check(
                    report,
                    "bedrock",
                    stage.check_bedrock_disable_stages_credential_for_reenable,
                    "the provider toggle deactivated and restored Hermes",
                )
            else:
                report.add(
                    "AWS Bedrock",
                    "unavailable",
                    "skipped",
                    "requires a successful Hermes integration check",
                )

        if suite == "all":
            if passed_runtimes == list(STAGE_CHAT_RUNTIMES):
                def check_cross_runtime() -> None:
                    stage.check_all_runtimes_active()
                    stage.check_agent_parallelism()
                    stage.check_agent_thread_recall()
                    stage.check_runtime_deactivation_stops_running_turns()
                    stage.check_reboot_recovery()

                _record_check(
                    report,
                    "runtime_interoperability",
                    check_cross_runtime,
                    "mixed concurrency, recall, deactivation, and reboot recovery",
                )
            else:
                report.add(
                    "Runtime interoperability",
                    "unavailable",
                    "skipped",
                    "requires successful Codex, Claude Code, Grok, and Hermes integration checks",
                )

        if availability.get("github") is None and "github" in availability:
            _record_check(report, "github", stage.check_github_write_e2e, "authenticated read/write and fail-closed guards")

        # Needs no provider credential — real package clients against public
        # registries — so it runs whenever the host is up, and is what catches
        # a forwarded-header rule tightened past ordinary client traffic.
        _record_check(
            report,
            "package_clients",
            stage.check_package_client_headers_e2e,
            "pip and npm registry traffic through the forwarded-header guard",
        )

        for tool_id in selected_tools:
            if availability.get(tool_id) is not None:
                continue
            _record_check(
                report,
                tool_id,
                lambda tool_id=tool_id: stage.check_tool_live(tool_id),
                "deterministic live MCP coverage",
                skip_unavailable=suite == "all",
            )

        print(f"\n{stage.passed}/{stage.total} checks passed")
        print(f"suite: {suite}")
        failed = report.failed() or stage.passed != stage.total
        if failed:
            stage.print_configuration_snapshot()
            stage.print_network_events(
                "Network events during failed integration run",
                since=run_network_baseline,
            )
        return 1 if failed else 0
    except Exception as exc:  # noqa: BLE001 - report failure with network + config context
        stage.print_configuration_snapshot()
        stage.print_network_events("Network events before failure", since=run_network_baseline)
        print(f"\n[FAIL] {exc}", file=sys.stderr)
        return 1
    finally:
        _write_action_summary(report, args.summary_file)
        stage.close_tunnel()


def _required_env_path(env_name: str) -> Path:
    value = os.environ.get(env_name)
    if not value:
        raise SystemExit(f"{env_name} is not set or empty")
    return Path(value)


class StageAwsSmoke(StageToolChecks, StageBedrockChecks, StageIntegrationChecks):
    def __init__(self, result_file: Path, ssh_key: Path, admin_password_env: str | None = None) -> None:
        super().__init__()
        result = json.loads(result_file.read_text())
        if not isinstance(result, dict):
            raise AssertionError("stage result file must contain a JSON object")
        if result.get("agent_name") != STAGE_AGENT_NAME:
            raise AssertionError(f"stage result file is for {result.get('agent_name')!r}, expected {STAGE_AGENT_NAME!r}")
        if "admin_password" not in result and admin_password_env is not None:
            admin_password = os.environ.get(admin_password_env)
            if not admin_password:
                raise AssertionError(f"{admin_password_env} is not set or empty")
            result["admin_password"] = admin_password
        self.result = result
        self.ssh_key = str(ssh_key)
        self.region = str(result["region"])
        self.workdir = Path(tempfile.mkdtemp(prefix="stage-aws-"))
        self.control_socket = self.workdir / "ssh-control"
        self.thread_prefix = f"thread-stage-{int(time.time())}-"
        self.github_app_config, self.github_secret_error = _github_app_config_from_env()
        self.stage_bedrock_credential, self.bedrock_secret_error = _bedrock_credential_from_env()

    def enforcement_policy(self) -> dict:
        policy = super().enforcement_policy()
        # Grok is an offered runtime; stage enables its provider while retaining
        # the operator-completed subscription login on the persistent volume.
        policy["network_integrations"]["xai"] = {"enabled": True}
        github = policy["network_integrations"]["github"]
        base_repos = [repo for repo in github["write_repositories"] if isinstance(repo, dict)]
        # Stage repositories (the CI-secret sandbox repo, or the operator's) go
        # first, ahead of the hardcoded public read repo, so
        # check_github_write_e2e always targets the sandbox at
        # write_repositories[0] and never a stale or public entry.
        ordered = [repo for repo in getattr(self, "stage_github_repositories", []) if isinstance(repo, dict)]
        listed = {(repo.get("owner"), repo.get("repo")) for repo in ordered}
        for repo in base_repos:
            if (repo.get("owner"), repo.get("repo")) not in listed:
                ordered.append(repo)
        github["write_repositories"] = ordered
        return policy

    def message_body(
        self,
        message: str,
        *,
        runtime: str | None = None,
        model: str | None = None,
        effort: str | None = None,
    ) -> dict:
        selected_runtime = runtime or self.agent_runtime
        return super().message_body(
            message,
            runtime=selected_runtime,
            model=model or CHEAP_MODELS[selected_runtime],
            effort=effort or CHEAP_EFFORT,
        )

    def check_thread_admin_api_contract(self) -> None:
        """Pin thread-only API edge cases against the real stage host.

        Runtime checks above already exercise live concurrency, synchronous
        steering, stop/fence cleanup, and ordinary session resume. This adds
        deterministic history, status, pagination, and rejection checks.
        """
        self._step("thread admin API: flat history, pagination, and edge cases")

        first_page = self._api("GET", "/v1/threads?limit=1")
        first_threads = first_page.get("threads")
        if not isinstance(first_threads, list) or len(first_threads) != 1:
            raise AssertionError(f"thread limit=1 returned an invalid page: {first_page}")
        cursor = first_page.get("next_before")
        if not isinstance(cursor, str) or not cursor:
            raise AssertionError(f"thread keyset page omitted next_before: {first_page}")
        second_page = self._api(
            "GET", f"/v1/threads?limit=1&before={quote(cursor, safe='')}"
        )
        second_threads = second_page.get("threads")
        if not isinstance(second_threads, list) or len(second_threads) != 1:
            raise AssertionError(f"second thread keyset page is invalid: {second_page}")
        if first_threads[0].get("thread_id") == second_threads[0].get("thread_id"):
            raise AssertionError(f"thread keyset pages overlap: {first_page}, {second_page}")

        status, body = self._api_status("GET", "/v1/threads?before=not-a-cursor")
        if status != 400 or "valid thread list cursor" not in self._error_message(body):
            raise AssertionError(f"invalid thread cursor returned {status}: {body}")

        recent = self._api("GET", "/v1/threads?limit=100").get("threads") or []
        candidates = [
            thread
            for thread in recent
            if isinstance(thread, dict)
            and str(thread.get("thread_id") or "").startswith(self.thread_prefix)
            and thread.get("status") == "idle"
        ]
        if not candidates:
            raise AssertionError(f"stage checks left no recent idle thread: {recent}")
        thread = candidates[0]
        thread_id = str(thread["thread_id"])
        encoded_id = quote(thread_id, safe="")

        detail = self._api("GET", f"/v1/threads/{encoded_id}")["thread"]
        if detail != thread:
            raise AssertionError(f"thread detail differs from list entry: {thread}, {detail}")

        all_events = self._api(
            "GET", f"/v1/threads/{encoded_id}/events?limit=100"
        ).get("events") or []
        if not all_events:
            raise AssertionError(f"stage thread has no retained events: {thread}")
        public_types = {
            "thread.message",
            "thread.activity",
            "thread.error",
            "thread.stopped",
        }
        unexpected = sorted(
            {
                str(event.get("event_type"))
                for event in all_events
                if event.get("event_type") not in public_types
            }
        )
        if unexpected:
            raise AssertionError(f"thread history exposed non-public lifecycle events: {unexpected}")

        latest = self._api(
            "GET", f"/v1/threads/{encoded_id}/events?limit=1"
        ).get("events") or []
        if len(latest) != 1 or latest[0]["seq"] != all_events[-1]["seq"]:
            raise AssertionError(f"latest event page is inconsistent: {all_events}, {latest}")
        if len(all_events) > 1:
            older = self._api(
                "GET",
                f"/v1/threads/{encoded_id}/events?limit=1&before={latest[0]['seq']}",
            ).get("events") or []
            if len(older) != 1 or int(older[0]["seq"]) >= int(latest[0]["seq"]):
                raise AssertionError(f"older event page is inconsistent: {latest}, {older}")
        caught_up = self._api(
            "GET",
            f"/v1/threads/{encoded_id}/events?since={all_events[-1]['seq']}&limit=1",
        )
        if caught_up.get("events") != []:
            raise AssertionError(f"caught-up since cursor returned events: {caught_up}")
        status, body = self._api_status(
            "GET",
            f"/v1/threads/{encoded_id}/events?since=0&before={all_events[-1]['seq']}",
        )
        if status != 400 or "cannot be combined" not in self._error_message(body):
            raise AssertionError(f"mixed event cursors returned {status}: {body}")

        before_events = list(all_events)
        status, body = self._api_status(
            "POST",
            f"/v1/threads/{encoded_id}/messages",
            {"message": "must be rejected", "model": thread["model"]},
        )
        if status != 400 or "must be provided together" not in self._error_message(body):
            raise AssertionError(f"partial session config returned {status}: {body}")
        after_events = self._api(
            "GET", f"/v1/threads/{encoded_id}/events?limit=100"
        ).get("events") or []
        if after_events != before_events:
            raise AssertionError("rejected message mutated thread history")

        status, body = self._api_status("POST", f"/v1/threads/{encoded_id}/stop")
        if status != 409 or self._error_message(body) != "the thread has no running work":
            raise AssertionError(f"idle stop returned {status}: {body}")
        missing = quote(f"{self.thread_prefix}definitely-missing", safe="")
        status, body = self._api_status("POST", f"/v1/threads/{missing}/stop")
        if status != 404 or self._error_message(body) != "thread not found":
            raise AssertionError(f"unknown stop returned {status}: {body}")

        self._ok(
            "thread API exposed only four public event types; status, pagination, "
            "validation, and stop errors were consistent"
        )

    def check_idle_session_switch_handoff(
        self,
        available_runtimes: tuple[str, ...],
    ) -> None:
        if not available_runtimes:
            raise AssertionError("session switch stage check requires an active runtime")
        source_runtime = available_runtimes[0]
        source_model = CHEAP_MODELS[source_runtime]
        source_effort = CHEAP_EFFORT
        target_runtime, target_model, target_effort = self._replacement_session_config(
            source_runtime,
            available_runtimes,
        )
        thread_name = "session-switch-handoff"
        thread_id = self.api_thread_id(thread_name)
        encoded_id = quote(thread_id, safe="")

        started = self._api(
            "POST",
            f"/v1/threads/{encoded_id}/messages",
            self.message_body(
                (
                    "Use the terminal exactly once to run `cat /proc/sys/kernel/random/uuid`. "
                    "After the command finishes, reply exactly "
                    "STAGE_SWITCH_SOURCE_READY and nothing else."
                ),
                runtime=source_runtime,
                model=source_model,
                effort=source_effort,
            ),
        )
        if started.get("status") != "accepted":
            raise AssertionError(f"source session was not accepted: {started}")
        source_done = self._wait_for_turn(thread_name, timeout=240)
        if source_done.get("status") != "completed":
            raise AssertionError(
                f"source session failed: {self._thread_failure_detail(thread_name)}"
            )

        source_events = self._thread_events(thread_name)
        uuid_pattern = re.compile(
            r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
            r"[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
            re.IGNORECASE,
        )
        activity_uuid = None
        source_activity_seq = None
        for event in source_events:
            if event.get("event_type") != "thread.activity":
                continue
            activity = (event.get("payload") or {}).get("activity") or {}
            output = activity.get("output")
            match = uuid_pattern.search(output) if isinstance(output, str) else None
            if match:
                activity_uuid = match.group(0)
                source_activity_seq = int(event["seq"])
                break
        if activity_uuid is None or source_activity_seq is None:
            raise AssertionError(
                f"source turn retained no UUID-bearing activity: {source_events}"
            )
        agent_messages = [
            str((event.get("payload") or {}).get("message") or "")
            for event in source_events
            if event.get("event_type") == "thread.message"
            and (event.get("payload") or {}).get("source") == "agent"
        ]
        if any(activity_uuid in message for message in agent_messages):
            raise AssertionError(
                "source agent copied the UUID into a message, so activity-only handoff was not isolated"
            )

        baseline = max(int(event["seq"]) for event in source_events)
        switched = self._api(
            "POST",
            f"/v1/threads/{encoded_id}/messages",
            {
                "message": (
                    "Do not use tools. Repeat the UUID you see in the earlier activity."
                ),
                "agent_runtime": target_runtime,
                "model": target_model,
                "effort": target_effort,
            },
        )
        switched_thread = switched.get("thread") or {}
        if switched.get("status") != "accepted" or (
            switched_thread.get("thread_id"),
            switched_thread.get("agent_runtime"),
            switched_thread.get("model"),
            switched_thread.get("effort"),
        ) != (thread_id, target_runtime, target_model, target_effort):
            raise AssertionError(f"replacement session returned an invalid response: {switched}")

        replacement_done = self._wait_for_turn(
            thread_name,
            since=baseline,
            timeout=240,
        )
        if replacement_done.get("status") != "completed":
            raise AssertionError(
                f"replacement session failed: {self._thread_failure_detail(thread_name)}"
            )
        if activity_uuid.lower() not in str(
            replacement_done.get("output_message") or ""
        ).lower():
            raise AssertionError(
                "replacement provider did not recover the UUID from summarized activity: "
                f"{replacement_done.get('output_message')!r}"
            )

        replacement_events = self._thread_events(thread_name, since=baseline)
        changes = [
            (event.get("payload") or {}).get("activity") or {}
            for event in replacement_events
            if event.get("event_type") == "thread.activity"
            and str(((event.get("payload") or {}).get("activity") or {}).get("activity_id", ""))
            .endswith(":session-change")
        ]
        if len(changes) != 1:
            raise AssertionError(
                f"replacement recorded {len(changes)} session-change activities: {replacement_events}"
            )
        change = changes[0]
        expected_title = (
            "Agent provider changed"
            if source_runtime != target_runtime
            else "Agent session changed"
        )
        if change.get("title") != expected_title or target_model not in str(
            change.get("detail") or ""
        ):
            raise AssertionError(f"session-change activity is incomplete: {change}")

        final_events = self._thread_events(thread_name)
        if not any(int(event["seq"]) == source_activity_seq for event in final_events):
            raise AssertionError("provider replacement lost the source activity history")

    @staticmethod
    def _replacement_session_config(
        source_runtime: str,
        available_runtimes: tuple[str, ...],
    ) -> tuple[str, str, str]:
        target_runtime = next(
            (runtime for runtime in available_runtimes if runtime != source_runtime),
            source_runtime,
        )
        if target_runtime != source_runtime:
            return target_runtime, CHEAP_MODELS[target_runtime], CHEAP_EFFORT

        options = public_session_options().get(source_runtime) or {}
        for model, efforts in options.items():
            if model != CHEAP_MODELS[source_runtime] and CHEAP_EFFORT in efforts:
                return source_runtime, model, CHEAP_EFFORT
        for model, efforts in options.items():
            for effort in efforts:
                candidate = (source_runtime, model, effort)
                if candidate != (
                    source_runtime,
                    CHEAP_MODELS[source_runtime],
                    CHEAP_EFFORT,
                ):
                    return candidate
        raise AssertionError(
            f"{source_runtime} has no alternate offered session configuration"
        )

    def check_stable_app_basics(self, runtime: str) -> None:
        """Run real app flows and pin retained-history behavior."""
        model = CHEAP_MODELS[runtime]
        self._step(
            f"stable app basics with {runtime} ({model}, {CHEAP_EFFORT})"
        )
        public_types = {
            "thread.message",
            "thread.activity",
            "thread.error",
            "thread.stopped",
        }

        agent_base = "/v1/workspace/chat"
        history_probe = f"stagehistory{time.time_ns()}"
        agent_result = self._api(
            "POST",
            f"{agent_base}/messages",
            {
                "input_message": (
                    "The emergency release reversal procedure is filed in the blue binder. "
                    f"Remember marker {history_probe}, then reply with exactly "
                    "STAGE_AGENT_CHAT_OK. Do not use tools."
                ),
                "agent_runtime": runtime,
                "model": model,
                "effort": CHEAP_EFFORT,
            },
        )
        agent_thread = agent_result.get("thread_id")
        if (
            agent_result.get("action") != "accepted"
            or not isinstance(agent_thread, str)
            or not agent_thread
        ):
            raise AssertionError(
                f"Agent Chat did not accept its stage message: {agent_result}"
            )
        encoded_agent_thread = quote(agent_thread, safe="")
        try:
            agent_events = self._wait_for_app_thread_idle(
                status_path=f"{agent_base}/threads",
                events_path=f"{agent_base}/threads/{encoded_agent_thread}/events",
                thread_id=agent_thread,
                list_key="threads",
                timeout=240,
            )
            agent_types = {event.get("event_type") for event in agent_events}
            if not agent_types.issubset(public_types):
                raise AssertionError(
                    f"Agent Chat exposed non-public event types: {agent_events}"
                )
            agent_messages = [
                str((event.get("payload") or {}).get("message") or "")
                for event in agent_events
                if event.get("event_type") == "thread.message"
                and (event.get("payload") or {}).get("source") == "agent"
            ]
            if not any("STAGE_AGENT_CHAT_OK" in message for message in agent_messages):
                raise AssertionError(
                    f"Agent Chat did not retain the expected reply: {agent_events}"
                )
            self._check_agent_history_and_clear_memory(
                agent_base, agent_thread, history_probe
            )
        finally:
            self._api_status(
                "POST", f"{agent_base}/threads/{encoded_agent_thread}/archive"
            )

        builder_base = "/v1/workspace/web-apps"
        created = self._api("POST", f"{builder_base}/apps").get("app")
        builder_app = (
            created.get("app_id") if isinstance(created, dict) else None
        )
        if not isinstance(builder_app, str) or not builder_app:
            raise AssertionError(f"App Builder did not create a workspace: {created}")
        encoded_builder_app = quote(builder_app, safe="")
        sent = self._api(
            "POST",
            f"{builder_base}/apps/{encoded_builder_app}/messages",
            {
                "content": (
                    "Create the smallest possible app whose visible heading is "
                    "STAGE_APP_BUILDER_OK. Keep the UI and data minimal."
                ),
                "agent_runtime": runtime,
                "model": model,
                "effort": CHEAP_EFFORT,
            },
        )
        if sent.get("status") != "accepted":
            raise AssertionError(
                f"App Builder did not accept its stage message: {sent}"
            )
        builder_events = self._wait_for_app_thread_idle(
            status_path=(
                f"{builder_base}/apps/{encoded_builder_app}/conversation"
            ),
            events_path=(
                f"{builder_base}/apps/{encoded_builder_app}/conversation/events"
            ),
            thread_id=builder_app,
            list_key=None,
            timeout=300,
        )
        builder_types = {event.get("event_type") for event in builder_events}
        if not builder_types.issubset(public_types):
            raise AssertionError(
                f"App Builder exposed non-public event types: {builder_events}"
            )
        if any(
            event.get("event_type") in {"thread.error", "thread.stopped"}
            for event in builder_events
        ):
            raise AssertionError(
                f"App Builder agent did not complete successfully: {builder_events}"
            )
        state = self._api(
            "GET", f"{builder_base}/apps/{encoded_builder_app}/state"
        ).get("app")
        if (
            not isinstance(state, dict)
            or int(state.get("revision") or 0) < 1
        ):
            raise AssertionError(
                f"App Builder agent did not revise app state: {state}"
            )
        generated = " ".join(
            str(state.get(field) or "")
            for field in ("html", "css", "javascript")
        ) + json.dumps(state.get("data") or {}, sort_keys=True)
        if "STAGE_APP_BUILDER_OK" not in generated:
            raise AssertionError(
                f"App Builder output omitted the requested heading: {state}"
            )

        self._ok(
            "Agent Chat retained a real reply, typed history stayed bounded and "
            "untrusted across a working-memory clear, and App Builder used its "
            "agent API to generate and persist a minimal app"
        )

    def _check_agent_history_and_clear_memory(
        self,
        agent_base: str,
        thread_id: str,
        probe: str,
    ) -> None:
        """Prove the deployed typed MCP tools read retained app history safely."""
        expected_metadata = {
            "provenance": "retained_conversation_history",
            "trust": "untrusted",
            "instruction_authority": "none",
        }
        search = self._shim_tool_result(
            "search_conversation_history",
            {"query": probe, "roles": ["user"], "limit": 1},
        )
        if any(search.get(key) != value for key, value in expected_metadata.items()):
            raise AssertionError(f"conversation search lost provenance metadata: {search}")
        matches = search.get("matches")
        match = next(
            (
                item
                for item in matches
                if isinstance(item, dict)
                and item.get("thread_id") == thread_id
                and item.get("role") == "user"
                and probe in str(item.get("excerpt") or "")
            ),
            None,
        ) if isinstance(matches, list) else None
        if match is None or not isinstance(match.get("event_id"), str):
            raise AssertionError(
                f"conversation search did not find the unique retained user message: {search}"
            )

        semantic_query = "Where is the plan for undoing a failed production launch?"
        semantic_deadline = time.time() + 90
        semantic_search: dict = {}
        while time.time() < semantic_deadline:
            semantic_search = self._shim_tool_result(
                "search_conversation_history",
                {"query": semantic_query, "roles": ["user"], "limit": 5},
            )
            semantic_matches = semantic_search.get("matches")
            semantic_match = next(
                (
                    item
                    for item in semantic_matches
                    if isinstance(item, dict)
                    and item.get("thread_id") == thread_id
                    and probe in str(item.get("excerpt") or "")
                ),
                None,
            ) if isinstance(semantic_matches, list) else None
            if semantic_search.get("search_mode") == "hybrid" and semantic_match is not None:
                break
            time.sleep(2)
        else:
            raise AssertionError(
                "conversation semantic index did not retrieve the paraphrased retained "
                f"message within 90s: {semantic_search}"
            )

        read_arguments = {
            "thread_id": thread_id,
            "around_event_id": match["event_id"],
            "include_activity": True,
            "limit": 50,
        }
        history = self._shim_tool_result("read_thread_history", read_arguments)
        if any(history.get(key) != value for key, value in expected_metadata.items()):
            raise AssertionError(f"conversation read lost provenance metadata: {history}")
        if history.get("thread") != {"thread_id": thread_id}:
            raise AssertionError(f"conversation read returned the wrong thread: {history}")
        events = history.get("events")
        if not isinstance(events, list):
            raise AssertionError(f"conversation read returned invalid events: {history}")
        messages = [
            event
            for event in events
            if isinstance(event, dict) and event.get("type") == "message"
        ]
        if not any(
            event.get("role") == "user" and probe in str(event.get("content") or "")
            for event in messages
        ) or not any(
            event.get("role") == "assistant"
            and "STAGE_AGENT_CHAT_OK" in str(event.get("content") or "")
            for event in messages
        ):
            raise AssertionError(
                f"conversation read omitted retained user/assistant messages: {history}"
            )
        retained_message_ids = {
            event.get("event_id") for event in messages if isinstance(event.get("event_id"), str)
        }

        encoded_thread = quote(thread_id, safe="")
        cleared = self._api(
            "POST", f"{agent_base}/threads/{encoded_thread}/clear-memory"
        )
        if cleared != {"status": "cleared"}:
            raise AssertionError(f"Agent Chat working-memory clear failed: {cleared}")
        display_events = self._api(
            "GET", f"{agent_base}/threads/{encoded_thread}/events"
        ).get("events")
        if not isinstance(display_events, list) or not any(
            isinstance(event, dict)
            and event.get("event_type") == "thread.memory_cleared"
            for event in display_events
        ):
            raise AssertionError(
                f"Agent Chat did not expose the working-memory boundary: {display_events}"
            )

        retained = self._shim_tool_result("read_thread_history", read_arguments)
        retained_events = retained.get("events")
        retained_ids = {
            event.get("event_id")
            for event in retained_events
            if isinstance(event, dict)
            and event.get("type") == "message"
            and isinstance(event.get("event_id"), str)
        } if isinstance(retained_events, list) else set()
        if not retained_message_ids or not retained_message_ids.issubset(retained_ids):
            raise AssertionError(
                f"working-memory clear removed retained conversation messages: {retained}"
            )

    def _wait_for_app_thread_idle(
        self,
        *,
        status_path: str,
        events_path: str,
        thread_id: str,
        list_key: str | None,
        timeout: float,
    ) -> list[dict]:
        deadline = time.time() + timeout
        while True:
            status_response = self._api("GET", status_path)
            if list_key is None:
                status = status_response.get("status")
            else:
                rows = status_response.get(list_key)
                if not isinstance(rows, list):
                    raise AssertionError(
                        f"app status list has the wrong shape: {status_response}"
                    )
                row = next(
                    (
                        item
                        for item in rows
                        if isinstance(item, dict)
                        and item.get("thread_id") == thread_id
                    ),
                    None,
                )
                if row is None:
                    raise AssertionError(
                        f"app status list omitted thread {thread_id}: {rows}"
                    )
                status = row.get("status")
            events = self._api("GET", events_path).get("events")
            if not isinstance(events, list) or not all(
                isinstance(event, dict) for event in events
            ):
                raise AssertionError(
                    f"app event stream has the wrong shape: {events}"
                )
            terminal = next(
                (
                    event
                    for event in events
                    if event.get("event_type")
                    in {"thread.error", "thread.stopped"}
                ),
                None,
            )
            if terminal is not None:
                raise AssertionError(
                    f"app thread {thread_id} terminated unsuccessfully: {terminal}"
                )
            if status == "idle":
                return events
            if status != "running":
                raise AssertionError(
                    f"app thread {thread_id} returned invalid status {status!r}"
                )
            if time.time() >= deadline:
                raise AssertionError(
                    f"app thread {thread_id} did not become idle within {timeout}s"
                )
            time.sleep(2)

    def close_tunnel(self) -> None:
        if self.tunnel_open and self.result:
            self.tunnel_open = False
            import subprocess

            subprocess.run(
                [
                    "ssh",
                    "-S",
                    str(self.control_socket),
                    "-O",
                    "exit",
                    f"kern-operator@{self.result['public_dns']}",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )

    def teardown(self) -> None:
        self.close_tunnel()

    def recover_baseline(self, runtimes: tuple[str, ...] = ()) -> None:
        self._step("stage baseline recovery")
        # GitHub write repositories must survive the baseline policy reset. When
        # the run is auto-configured from CI secrets, the sandbox repo from those
        # secrets is authoritative and fully replaces any host state, so a stale
        # or manually added entry can never become the write target. Otherwise
        # the operator-configured repos are captured from the stored policy and
        # merged into every policy this run publishes.
        if self.github_app_config is not None:
            self.stage_github_repositories = [
                {"owner": self.github_app_config["owner"], "repo": self.github_app_config["repo"]}
            ]
        else:
            stored = self._api("GET", "/v1/network/policy").get("network_controls") or {}
            integrations = stored.get("network_integrations") or {}
            github = integrations.get("github") or {}
            self.stage_github_repositories = [
                repo for repo in (github.get("write_repositories") or []) if isinstance(repo, dict)
            ]
        self._api("PUT", "/v1/network/policy", self.enforcement_policy())
        # Stop any leftover running turn (there is no queue to drain); the
        # thread stays fenced briefly while its process closes, so poll until
        # nothing is live.
        deadline = time.time() + 90
        while time.time() < deadline:
            running = sorted(self._running_thread_ids())
            if not running:
                break
            for thread_id in running:
                code, _ = self._api_status("POST", f"/v1/threads/{thread_id}/stop")
                if code not in {200, 202, 409, 404}:
                    raise AssertionError(f"baseline stop of running thread {thread_id} returned {code}")
            time.sleep(3)
        else:
            raise AssertionError(
                f"stage still has running turns after cleanup: {sorted(self._running_thread_ids())}"
            )
        for runtime in runtimes:
            self.require_runtime_active(runtime)
        if any(runtime in {"codex", "claude_code"} for runtime in runtimes):
            self._assert_provider_account_anchors(live_pins=True)
        active_note = (
            ", ".join(_RUNTIME_LABELS[runtime] for runtime in runtimes) + " active"
            if runtimes
            else "no provider runtime required for this suite"
        )
        self._ok(f"policy reset, running turns cleared, {active_note}")

    @staticmethod
    def suite_runtimes(suite: str) -> tuple[str, ...]:
        """Provider runtimes the selected suite exercises (and therefore needs
        available). 'all_runtimes' covers all four thread-capable runtimes,
        while 'github' and tool suites need none."""
        if suite == "codex":
            return ("codex",)
        if suite == "claude":
            return ("claude_code",)
        if suite == "hermes":
            return (suite,)
        if suite == "grok":
            return (suite,)
        if suite == "github" or suite in TOOL_SUITES:
            return ()
        return STAGE_CHAT_RUNTIMES

    def check_agent_file_explorer(self) -> None:
        self._step("agent file explorer API on real agent home")
        directory_name = f".stage-file-explorer-{int(time.time())}"
        file_name = 'quote"file.txt'
        html_file_name = '<img src=x onerror="window.__stageFileNameXss=1">.txt'
        symlink_name = "outside-link"
        internal_symlink_name = "inside-file-link"
        dir_symlink_name = "outside-dir-link"
        file_content = f"stage file explorer content {self.thread_prefix}"
        html_file_content = '<script>window.__stageFileContentXss=1</script>\n'
        create_script = "\n".join([
            "from pathlib import Path",
            "home = Path('/mnt/kern-agent/agent-home')",
            f"directory = home / {directory_name!r}",
            "directory.mkdir(mode=0o700, exist_ok=True)",
            f"(directory / {file_name!r}).write_text({file_content!r})",
            f"(directory / {html_file_name!r}).write_text({html_file_content!r})",
            f"(directory / {symlink_name!r}).symlink_to('/etc/passwd')",
            f"(directory / {internal_symlink_name!r}).symlink_to({file_name!r})",
            f"(directory / {dir_symlink_name!r}).symlink_to('/tmp', target_is_directory=True)",
        ])
        cleanup = (
            "sudo -u kern-agent python3 - <<'PY'\n"
            "import shutil\n"
            "from pathlib import Path\n"
            f"shutil.rmtree(Path('/mnt/kern-agent/agent-home') / {directory_name!r}, ignore_errors=True)\n"
            "PY"
        )
        try:
            self._ssh_code(f"sudo -u kern-agent python3 - <<'PY'\n{create_script}\nPY")
            root = self._api("GET", "/v1/agent-files?path=/")
            if not isinstance(root.get("truncated"), bool) or "max_entries" in root:
                raise AssertionError(f"agent file list did not report expected listing metadata: {root}")
            root_names = {entry.get("name") for entry in root.get("entries", [])}
            if directory_name not in root_names:
                raise AssertionError(f"agent file root did not include hidden stage directory: {root}")

            directory_path = f"/{directory_name}"
            listed = self._api("GET", f"/v1/agent-files?path={quote(directory_path, safe='')}")
            entries = listed.get("entries", [])
            match = next((entry for entry in entries if entry.get("name") == file_name), None)
            if not match or match.get("type") != "file" or match.get("path") != f"{directory_path}/{file_name}":
                raise AssertionError(f"agent file directory did not include expected file: {listed}")
            html_match = next((entry for entry in entries if entry.get("name") == html_file_name), None)
            if not html_match or html_match.get("type") != "file" or html_match.get("path") != f"{directory_path}/{html_file_name}":
                raise AssertionError(f"agent file directory did not include expected HTML-looking file: {listed}")
            symlink_names = {symlink_name, internal_symlink_name, dir_symlink_name}
            listed_symlinks = symlink_names & {entry.get("name") for entry in entries}
            if listed_symlinks:
                raise AssertionError(f"agent file directory exposed symlinks {listed_symlinks}: {listed}")

            file_path = f"{directory_path}/{file_name}"
            read = self._api("GET", f"/v1/agent-files/read?path={quote(file_path, safe='')}")
            if read.get("content") != file_content or read.get("truncated") is not False:
                raise AssertionError(f"agent file read returned unexpected payload: {read}")
            html_file_path = f"{directory_path}/{html_file_name}"
            html_read = self._api("GET", f"/v1/agent-files/read?path={quote(html_file_path, safe='')}")
            if html_read.get("content") != html_file_content or html_read.get("truncated") is not False:
                raise AssertionError(f"agent file HTML-looking read returned unexpected payload: {html_read}")

            status, body = self._api_status("GET", "/v1/agent-files?path=..")
            if status != 400 or "escapes the agent home" not in json.dumps(body):
                raise AssertionError(f"agent file path escape returned {status}: {body}")
            for name in (symlink_name, internal_symlink_name):
                symlink_path = f"{directory_path}/{name}"
                status, body = self._api_status("GET", f"/v1/agent-files/read?path={quote(symlink_path, safe='')}")
                if status != 400 or "symlinks are not supported" not in json.dumps(body):
                    raise AssertionError(f"agent file symlink read returned {status}: {body}")
            dir_symlink_path = f"{directory_path}/{dir_symlink_name}"
            status, body = self._api_status("GET", f"/v1/agent-files?path={quote(dir_symlink_path, safe='')}")
            if status != 400 or "symlinks are not supported" not in json.dumps(body):
                raise AssertionError(f"agent file symlink list returned {status}: {body}")
        finally:
            self._ssh_code(cleanup)
        self._ok("hidden directory listed, hostile filenames read as text, and path/symlink escapes rejected")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
