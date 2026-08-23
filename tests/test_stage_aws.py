"""Focused tests for the persistent stage matrix orchestration."""

from __future__ import annotations

import ast
import json
from http import HTTPStatus
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from host.network_integrations.xai import guard as xai_guard
from host.network_integrations.xai.manifest import XaiIntegration
from host.runtime.network_proxy.service import duplicate_header_denial
from host.runtime.tools.tools_host import BUNDLED_TOOLS
from tests.stage.stage_tool_checks import _result_shape, _safe_arguments
from tests.stage.stage_aws import STAGE_SUITE_CHOICES, StageAwsSmoke
from tests.stage.stage_integration_checks import ALL_RUNTIMES_SUITE
from tests.stage.stage_support import (
    CHEAP_EFFORT,
    CHEAP_MODELS,
    CHECK_LABELS,
    CredentialUnavailable,
    STAGE_BEDROCK_ENV,
    STAGE_GITHUB_APP_ENV,
    TOOL_SUITES,
    StageReport,
    agent_catalog_tool_ids as _agent_catalog_tool_ids,
    bedrock_credential_from_env as _bedrock_credential_from_env,
    github_app_config_from_env as _github_app_config_from_env,
    integration_label as _integration_label,
    record_check as _record_check,
    selected_integrations as _selected_integrations,
    write_action_summary as _write_action_summary,
)


class StageOrchestrationTests(unittest.TestCase):
    def test_app_stage_wait_uses_flat_events_and_durable_status(self) -> None:
        stage = StageAwsSmoke.__new__(StageAwsSmoke)
        statuses = iter(("running", "idle"))

        def fake_api(method: str, path: str, body: dict | None = None) -> dict:
            self.assertEqual(method, "GET")
            if path == "/app/status":
                return {
                    "threads": [
                        {"thread_id": "thread-1", "status": next(statuses)}
                    ]
                }
            if path == "/app/events":
                return {
                    "events": [
                        {
                            "event_type": "thread.message",
                            "payload": {"source": "agent", "message": "done"},
                        }
                    ]
                }
            raise AssertionError((method, path, body))

        with (
            patch.object(stage, "_api", side_effect=fake_api),
            patch("tests.stage.stage_aws.time.sleep"),
        ):
            events = stage._wait_for_app_thread_idle(
                status_path="/app/status",
                events_path="/app/events",
                thread_id="thread-1",
                list_key="threads",
                timeout=5,
            )
        self.assertEqual(events[0]["event_type"], "thread.message")

    def test_stage_history_check_uses_typed_tools_and_preserves_messages_after_clear(self) -> None:
        stage = StageAwsSmoke.__new__(StageAwsSmoke)
        metadata = {
            "provenance": "retained_conversation_history",
            "trust": "untrusted",
            "instruction_authority": "none",
        }
        search = {
            **metadata,
            "matches": [
                {
                    "thread_id": "thread-7",
                    "event_id": "event_10",
                    "role": "user",
                    "excerpt": "remember stagehistory123",
                }
            ],
            "next_cursor": None,
        }
        semantic_search = {**search, "search_mode": "hybrid"}
        history = {
            **metadata,
            "thread": {"thread_id": "thread-7"},
            "events": [
                {
                    "event_id": "event_10",
                    "type": "message",
                    "role": "user",
                    "content": "remember stagehistory123",
                },
                {
                    "event_id": "event_11",
                    "type": "message",
                    "role": "assistant",
                    "content": "STAGE_AGENT_CHAT_OK",
                },
            ],
        }

        def fake_api(method: str, path: str, body: dict | None = None) -> dict:
            if (method, path) == (
                "POST",
                "/v1/workspace/chat/threads/thread-7/clear-memory",
            ):
                self.assertIsNone(body)
                return {"status": "cleared"}
            if (method, path) == (
                "GET",
                "/v1/workspace/chat/threads/thread-7/events",
            ):
                return {"events": [{"event_type": "thread.memory_cleared"}]}
            raise AssertionError((method, path, body))

        with (
            patch.object(
                stage,
                "_shim_tool_result",
                side_effect=[search, semantic_search, history, history],
            ) as tool,
            patch.object(stage, "_api", side_effect=fake_api),
        ):
            stage._check_agent_history_and_clear_memory(
                "/v1/workspace/chat", "thread-7", "stagehistory123"
            )

        self.assertEqual(
            [call.args[0] for call in tool.call_args_list],
            [
                "search_conversation_history",
                "search_conversation_history",
                "read_thread_history",
                "read_thread_history",
            ],
        )
        self.assertEqual(
            tool.call_args_list[0].args[1],
            {"query": "stagehistory123", "roles": ["user"], "limit": 1},
        )
        self.assertEqual(
            tool.call_args_list[1].args[1],
            {
                "query": "Where is the plan for undoing a failed production launch?",
                "roles": ["user"],
                "limit": 5,
            },
        )

    def test_stage_harness_import_does_not_require_playwright(self) -> None:
        script = """
import builtins

real_import = builtins.__import__

def import_without_playwright(name, *args, **kwargs):
    if name == "playwright" or name.startswith("playwright."):
        raise ModuleNotFoundError("playwright intentionally unavailable")
    return real_import(name, *args, **kwargs)

builtins.__import__ = import_without_playwright
import tests.stage.stage_aws
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_all_selects_every_runtime_github_and_bundled_tool(self) -> None:
        selected = _selected_integrations("all")
        self.assertEqual(selected[:5], ("codex", "claude", "grok", "hermes", "github"))
        self.assertEqual(selected[5:], TOOL_SUITES)
        self.assertEqual(set(TOOL_SUITES), set(BUNDLED_TOOLS))

    def test_all_runtimes_suite_is_offered_and_selects_the_four_runtimes(self) -> None:
        self.assertIn(ALL_RUNTIMES_SUITE, STAGE_SUITE_CHOICES)
        self.assertEqual(STAGE_SUITE_CHOICES[0], "all")
        stage = StageAwsSmoke.__new__(StageAwsSmoke)
        stage.total = 0
        stage.passed = 0
        stage.bedrock_secret_error = None
        with patch.object(
            stage,
            "_wait_for_runtime_status",
            side_effect=["active", "active", "active", "active"],
        ) as wait:
            availability = stage.integration_availability(ALL_RUNTIMES_SUITE)
        self.assertEqual(list(availability), ["codex", "claude", "grok", "hermes"])
        self.assertTrue(all(reason is None for reason in availability.values()))
        self.assertEqual(
            [call.kwargs["runtime"] for call in wait.call_args_list],
            ["codex", "claude_code", "grok", "hermes"],
        )

    def test_all_runtimes_bedrock_autoconfiguration_targets_hermes(self) -> None:
        stage = StageAwsSmoke.__new__(StageAwsSmoke)
        stage.total = 0
        stage.passed = 0
        stage.stage_bedrock_credential = ("AKIASTAGE", "stage-secret")
        stage.bedrock_secret_error = None

        def fake_api(method: str, path: str, body: dict | None = None) -> dict:
            if (method, path) == ("POST", "/v1/agent-runtime/bedrock-credentials"):
                return {"status": "accepted"}
            if (method, path) == ("GET", "/v1/network/policy"):
                return {"network_controls": {"network_integrations": {}}}
            if (method, path) == ("PUT", "/v1/network/policy"):
                return {}
            raise AssertionError((method, path, body))

        with (
            patch.object(stage, "_api", side_effect=fake_api),
            patch.object(stage, "_wait_for_runtime_status", return_value="active") as wait,
        ):
            stage.autoconfigure_bedrock(ALL_RUNTIMES_SUITE)
        self.assertEqual(
            [call.kwargs["runtime"] for call in wait.call_args_list],
            ["hermes"],
        )

    def test_grok_preflight_enable_preserves_policy_and_defaults_search_off(self) -> None:
        stage = StageAwsSmoke.__new__(StageAwsSmoke)
        stored = {
            "network_integrations": {
                "github": {"enabled": True, "write_repositories": [{"owner": "o", "repo": "r"}]},
                "custom": {"domains": {"example.com": {"allow_http_methods": ["GET"]}}},
            }
        }
        writes: list[dict] = []

        def fake_api(method: str, path: str, body: dict | None = None) -> dict:
            if (method, path) == ("GET", "/v1/network/policy"):
                return {"network_controls": stored}
            if (method, path) == ("PUT", "/v1/network/policy"):
                assert body is not None
                writes.append(body)
                return {}
            raise AssertionError((method, path, body))

        with patch.object(stage, "_api", side_effect=fake_api):
            stage.prepare_grok_integration("grok")
        self.assertEqual(writes[0]["network_integrations"]["xai"], {"enabled": True})
        self.assertEqual(writes[0]["network_integrations"]["github"], stored["network_integrations"]["github"])
        self.assertEqual(writes[0]["network_integrations"]["custom"], stored["network_integrations"]["custom"])

    def test_grok_stage_matrix_matches_the_xai_guard(self) -> None:
        stage = StageAwsSmoke.__new__(StageAwsSmoke)
        stage.total = 0
        stage.passed = 0
        account_id = "stage-grok-account"
        events: list[dict] = []
        network_queries: list[int] = []
        policies: list[dict] = []
        baseline = {
            "network_integrations": {
                "xai": {"enabled": True},
                "github": {"enabled": True, "write_repositories": []},
            }
        }

        def append_event(
            method: str,
            host: str,
            path: str,
            decision: str,
            reason: str | None,
        ) -> None:
            events.append(
                {
                    "seq": len(events) + 1,
                    "method": method,
                    "host": host,
                    "path": path,
                    "decision": decision,
                    "reason_code": reason,
                }
            )

        def fake_api(method: str, path: str, body: dict | None = None) -> dict:
            if (method, path) == ("PUT", "/v1/network/policy"):
                assert body is not None
                policies.append(json.loads(json.dumps(body)))
                return {}
            if (method, path) == ("POST", "/v1/agent-runtime/refresh"):
                append_event("GET", "cli-chat-proxy.grok.com", "/v1/user", "allowed", None)
                append_event("GET", "cli-chat-proxy.grok.com", "/v1/billing", "allowed", None)
                return {
                    "accounts": [
                        {
                            "agent_runtime": "grok",
                            "provider": "xai",
                            "status": "active",
                            "account_id": account_id,
                            "grok_usage": {"usage_percent": 12},
                            "coding_data_retention_opt_out": True,
                            "zdr_enabled": False,
                        }
                    ]
                }
            raise AssertionError((method, path, body))

        def fake_ssh(command: str) -> str:
            if "curl" not in command:
                return "grok-cli-ok"
            args = shlex.split(command)
            args = args[args.index("curl") + 1 :]
            method = args[args.index("-X") + 1]
            headers: list[tuple[str, str]] = []
            for index, arg in enumerate(args):
                if arg == "-H":
                    name, _, value = args[index + 1].partition(":")
                    headers.append((name, value.strip()))
            body = args[args.index("--data-binary") + 1].encode() if "--data-binary" in args else b""
            parsed = urlsplit(args[-1])
            event_method = method
            event_path = parsed.path
            integration = XaiIntegration(enabled=True)
            if not xai_guard.host_allowed(integration, parsed.hostname or ""):
                reason = "host_not_allowed"
                event_method = "CONNECT"
                event_path = ""
            else:
                reason = duplicate_header_denial(headers)
            if reason is None:
                with patch.object(
                    xai_guard, "read_proxy_xai_account_id", return_value=account_id
                ):
                    reason = xai_guard.request_denied(
                        integration,
                        method,
                        parsed.hostname or "",
                        parsed.path,
                        parsed.query,
                        headers,
                        body,
                    )
            append_event(
                event_method,
                parsed.hostname or "",
                event_path,
                "allowed" if reason is None else "denied",
                reason,
            )
            # Real curl writes only a generic CONNECT 403 to stderr, while
            # _ssh_code returns stdout. Keep the mock faithful to that shape.
            if event_method == "CONNECT":
                return ""
            return reason or "upstream rejected synthetic credential"

        def fake_network_events(since: int = 0) -> list[dict]:
            network_queries.append(since)
            return [event for event in events if event["seq"] > since]

        with (
            patch.object(stage, "enforcement_policy", side_effect=lambda: json.loads(json.dumps(baseline))),
            patch.object(stage, "require_runtime_active"),
            patch.object(stage, "_api", side_effect=fake_api),
            patch.object(stage, "_api_status", return_value=(409, {})),
            patch.object(stage, "_ssh_code", side_effect=fake_ssh),
            patch.object(stage, "_network_events", side_effect=fake_network_events),
        ):
            stage.check_grok_connection_and_guards()

        self.assertEqual((stage.passed, stage.total), (1, 1))
        self.assertEqual(policies[-1]["network_integrations"]["xai"], {"enabled": True})
        # Every denial case in the stage matrix, and the six allowed shapes
        # plus the two refresh reads.
        self.assertEqual(sum(event["decision"] == "denied" for event in events), 24)
        self.assertEqual(sum(event["decision"] == "allowed" for event in events), 8)
        # One baseline, one provider-refresh read, then one read per matrix
        # row. The old implementation added a second full-history read for
        # every row, which made the live check quadratic in retained events.
        self.assertEqual(len(network_queries), 32)
        self.assertLessEqual(network_queries.count(0), 2)

    def test_agent_catalog_parser_requires_unique_string_tool_ids(self) -> None:
        output = '```json\n{"tools":["twitter","gmail"]}\n```'
        self.assertEqual(_agent_catalog_tool_ids(output), ("gmail", "twitter"))
        with self.assertRaisesRegex(AssertionError, "repeated tool ids"):
            _agent_catalog_tool_ids('{"tools":["gmail","gmail"]}')
        with self.assertRaisesRegex(AssertionError, "string tools list"):
            _agent_catalog_tool_ids('{"tools":[1]}')

    def test_tool_diagnostics_redact_credentials_but_keep_domain_ids(self) -> None:
        safe = _safe_arguments(
            {
                "api_key": "top-secret",
                "access_token": "also-secret",
                "token_id": "12345",
                "query": "public query",
            }
        )
        self.assertEqual(
            safe,
            {
                "api_key": "<redacted>",
                "access_token": "<redacted>",
                "token_id": "12345",
                "query": "public query",
            },
        )

    def test_tool_result_diagnostics_report_shape_without_values(self) -> None:
        self.assertEqual(
            _result_shape(
                {
                    "message": "provider detail",
                    "reels": [{"url": "https://example.com/private-result"}],
                    "metadata": {"cursor": "secret-ish-provider-value"},
                }
            ),
            "message,metadata{1},reels[1]",
        )

    def test_brave_stage_retries_one_provider_5xx(self) -> None:
        stage = StageAwsSmoke.__new__(StageAwsSmoke)
        with patch.object(
            stage,
            "_successful_tool_call",
            side_effect=[AssertionError("Brave Search API returned HTTP 500."), {}],
        ) as call:
            detail = stage._check_brave_live()
        self.assertEqual(detail, "live search completed")
        self.assertEqual(call.call_count, 2)

    def test_brave_stage_does_not_retry_non_5xx_failures(self) -> None:
        stage = StageAwsSmoke.__new__(StageAwsSmoke)
        with patch.object(
            stage,
            "_successful_tool_call",
            side_effect=AssertionError("Brave Search API rate limit was reached."),
        ) as call:
            with self.assertRaisesRegex(AssertionError, "rate limit"):
                stage._check_brave_live()
        self.assertEqual(call.call_count, 1)

    def test_search_console_stage_selects_a_writable_property(self) -> None:
        stage = StageAwsSmoke.__new__(StageAwsSmoke)
        calls: list[tuple[str, dict]] = []

        def responder(name: str, arguments: dict) -> dict:
            calls.append((name, arguments))
            if name == "google_search_console_list_properties":
                return {
                    "properties": [
                        {
                            "site_url": "https://restricted.example/",
                            "permission_level": "siteRestrictedUser",
                        },
                        {
                            "site_url": "https://writable.example/",
                            "permission_level": "siteFullUser",
                        },
                    ]
                }
            return {}

        with (
            patch.object(stage, "_successful_tool_call", side_effect=responder),
            patch.object(stage, "_queue_and_deny") as queue,
        ):
            detail = stage._check_search_console_live()

        self.assertIn("sitemap proposal denied", detail)
        for name, arguments in calls[1:]:
            with self.subTest(name=name):
                self.assertEqual(arguments["site_url"], "https://writable.example/")
        self.assertEqual(
            queue.call_args.args[2]["site_url"], "https://writable.example/"
        )

    def test_instagram_stage_prefers_trending_reel_for_dependent_reads(self) -> None:
        stage = StageAwsSmoke.__new__(StageAwsSmoke)
        calls: list[tuple[str, dict]] = []

        def responder(name: str, arguments: dict) -> dict:
            calls.append((name, arguments))
            if name == "instagram_discovery_get_trending_reels":
                return {
                    "reels": [
                        {
                            "url": "https://www.instagram.com/reel/Fresh/",
                            "audio_id": "123",
                        }
                    ]
                }
            if name == "instagram_discovery_search_reels":
                return {"reels": [{"url": "https://www.instagram.com/reel/Stale/"}]}
            return {}

        # Trending is the strict anchor read; the searches and derived reads go
        # through the best-effort _optional_tool_result path, so stub both.
        with patch.object(
            stage, "_successful_tool_call", side_effect=responder
        ), patch.object(stage, "_optional_tool_result", side_effect=responder):
            detail = stage._check_instagram_discovery_live()

        self.assertIn("2/2 provider-result-derived read(s)", detail)
        self.assertIn(
            (
                "instagram_discovery_get_reel_details",
                {"url": "https://www.instagram.com/reel/Fresh/"},
            ),
            calls,
        )

    def test_optional_tool_result_skips_no_content_but_fails_real_errors(self) -> None:
        stage = StageAwsSmoke.__new__(StageAwsSmoke)

        with patch.object(
            stage,
            "_shim_tool_response",
            return_value=(
                {"isError": True},
                "ScrapeCreators could not find public Instagram content for that request.",
            ),
        ):
            self.assertEqual(
                stage._optional_tool_result("instagram_discovery_search_reels", {}), {}
            )

        with patch.object(
            stage,
            "_shim_tool_response",
            return_value=(
                {"isError": True},
                "ScrapeCreators returned 429 Too Many Requests: credits exhausted.",
            ),
        ):
            with self.assertRaises(AssertionError):
                stage._optional_tool_result("instagram_discovery_search_reels", {})

        with patch.object(
            stage,
            "_shim_tool_response",
            return_value=({}, '{"reels": [{"url": "https://www.instagram.com/reel/A/"}]}'),
        ):
            self.assertEqual(
                stage._optional_tool_result("instagram_discovery_search_reels", {}),
                {"reels": [{"url": "https://www.instagram.com/reel/A/"}]},
            )

    def test_runway_stage_uses_uuid_shaped_missing_task(self) -> None:
        stage = StageAwsSmoke.__new__(StageAwsSmoke)
        with patch.object(
            stage,
            "_shim_tool_response",
            return_value=({"isError": True}, "Runway task was not found."),
        ) as call:
            detail = stage._check_runway_live()
        self.assertEqual(
            call.call_args.args,
            ("runway_get_task", {"task_id": "00000000-0000-4000-8000-000000000000"}),
        )
        self.assertIn("without generation spend", detail)

    def test_github_stage_secrets_are_optional_but_partial_sets_are_unavailable(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(_github_app_config_from_env(), (None, None))

        env = {STAGE_GITHUB_APP_ENV["app_id"]: "123"}
        with patch.dict(os.environ, env, clear=True):
            config, error = _github_app_config_from_env()
        self.assertIsNone(config)
        self.assertIn(STAGE_GITHUB_APP_ENV["private_key"], error or "")

    def test_complete_github_stage_secrets_are_parsed(self) -> None:
        env = {
            STAGE_GITHUB_APP_ENV["write_repo"]: "infiloop2/kern-stage",
            STAGE_GITHUB_APP_ENV["app_id"]: "123",
            STAGE_GITHUB_APP_ENV["installation_id"]: "456",
            STAGE_GITHUB_APP_ENV["private_key"]: "key",
        }
        with patch.dict(os.environ, env, clear=True):
            config, error = _github_app_config_from_env()
        self.assertIsNone(error)
        self.assertEqual(
            config,
            {
                "owner": "infiloop2",
                "repo": "kern-stage",
                "app_id": "123",
                "installation_id": "456",
                "private_key_pem": "key",
            },
        )

    def test_bedrock_stage_secret_is_one_optional_pair(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(_bedrock_credential_from_env(), (None, None))
        with patch.dict(os.environ, {STAGE_BEDROCK_ENV[0]: "AKIASTAGE"}, clear=True):
            credential, error = _bedrock_credential_from_env()
        self.assertIsNone(credential)
        self.assertIn(STAGE_BEDROCK_ENV[1], error or "")
        with patch.dict(
            os.environ,
            {
                STAGE_BEDROCK_ENV[0]: "AKIASTAGE",
                STAGE_BEDROCK_ENV[1]: "stage-secret",
            },
            clear=True,
        ):
            credential, error = _bedrock_credential_from_env()
        self.assertIsNone(error)
        self.assertEqual(credential, ("AKIASTAGE", "stage-secret"))

    def test_bedrock_autoconfiguration_validates_once_then_enables_hermes(self) -> None:
        stage = StageAwsSmoke.__new__(StageAwsSmoke)
        stage.total = 0
        stage.passed = 0
        stage.stage_bedrock_credential = ("AKIASTAGE", "stage-secret")
        stage.bedrock_secret_error = None
        calls: list[tuple[str, str, dict | None]] = []

        def fake_api(method: str, path: str, body: dict | None = None) -> dict:
            calls.append((method, path, body))
            if (method, path) == ("POST", "/v1/agent-runtime/bedrock-credentials"):
                return {"status": "accepted"}
            if (method, path) == ("GET", "/v1/network/policy"):
                return {
                    "network_controls": {
                        "network_integrations": {"github": {"enabled": True}}
                    }
                }
            if (method, path) == ("PUT", "/v1/network/policy"):
                return {}
            raise AssertionError((method, path, body))

        with (
            patch.object(stage, "_api", side_effect=fake_api),
            patch.object(stage, "_wait_for_runtime_status", return_value="active") as wait,
        ):
            stage.autoconfigure_bedrock("all")

        self.assertEqual(calls[0][:2], ("POST", "/v1/agent-runtime/bedrock-credentials"))
        self.assertEqual(
            calls[0][2],
            {
                "access_key_id": "AKIASTAGE",
                "secret_access_key": "stage-secret",
                "region": "us-east-1",
            },
        )
        self.assertEqual(calls[1][:2], ("GET", "/v1/network/policy"))
        policy = calls[2][2]
        assert policy is not None
        self.assertEqual(
            policy["network_integrations"]["bedrock"],
            {"enabled": True},
        )
        self.assertIn("github", policy["network_integrations"])
        self.assertEqual(
            [call.kwargs["runtime"] for call in wait.call_args_list],
            ["hermes"],
        )

    def test_bedrock_autoconfiguration_reports_rejected_candidate_to_preflight(self) -> None:
        stage = StageAwsSmoke.__new__(StageAwsSmoke)
        stage.total = 0
        stage.passed = 0
        stage.stage_bedrock_credential = ("AKIASTAGE", "invalid-secret")
        stage.bedrock_secret_error = None
        with patch.object(stage, "_api", side_effect=RuntimeError("STS denied")):
            stage.autoconfigure_bedrock("hermes")
        self.assertIn("STS denied", stage.bedrock_secret_error or "")

    def test_all_preflight_returns_each_unavailable_integration_without_raising(self) -> None:
        stage = StageAwsSmoke.__new__(StageAwsSmoke)
        stage.total = 0
        stage.passed = 0
        stage.bedrock_secret_error = None
        with (
            patch.object(
                stage,
                "_wait_for_runtime_status",
                side_effect=["active", "awaiting_login", "active", "active"],
            ) as wait,
            patch.object(stage, "_github_config_failures", return_value=["credential validation is 'error'"]),
            patch.object(stage, "_tool_credential_failures", side_effect=lambda tool: [] if tool == "polymarket" else [f"{tool}: missing"]),
        ):
            availability = stage.integration_availability("all")

        self.assertIsNone(availability["codex"])
        self.assertIn("awaiting_login", availability["claude"] or "")
        self.assertIsNone(availability["grok"])
        self.assertIsNone(availability["hermes"])
        self.assertIn("validation", availability["github"] or "")
        self.assertIsNone(availability["polymarket"])
        self.assertEqual(stage.passed, stage.total)
        self.assertTrue(
            all(
                "awaiting_login" in call.args[0]
                for call in wait.call_args_list
            )
        )

    def test_stage_message_body_always_defaults_to_the_cheapest_model_and_effort(self) -> None:
        self.assertEqual(
            CHEAP_MODELS,
            {
                "codex": "gpt-5.6-luna",
                "claude_code": "claude-sonnet-5",
                "grok": "grok-4.6",
                "hermes": "qwen.qwen3-coder-next",
            },
        )
        self.assertEqual(CHEAP_EFFORT, "high")
        stage = StageAwsSmoke.__new__(StageAwsSmoke)
        stage.thread_prefix = "thread-stage-test-"
        stage.agent_runtime = "codex"
        codex = stage.message_body("test")
        claude = stage.message_body("test", runtime="claude_code")
        hermes = stage.message_body("test", runtime="hermes")
        self.assertEqual((codex["model"], codex["effort"]), (CHEAP_MODELS["codex"], CHEAP_EFFORT))
        self.assertEqual(
            (claude["model"], claude["effort"]),
            (CHEAP_MODELS["claude_code"], CHEAP_EFFORT),
        )
        self.assertEqual(
            (hermes["model"], hermes["effort"]),
            (CHEAP_MODELS["hermes"], CHEAP_EFFORT),
        )
        # The message body carries no thread id; the per-run prefix lands on
        # the thread route through api_thread_id.
        self.assertEqual(codex["message"], "test")
        self.assertNotIn("thread_id", codex)
        self.assertEqual(stage.api_thread_id("codex"), "thread-stage-test-codex")

    def test_stage_session_switch_prefers_an_available_provider_then_an_alternate_model(
        self,
    ) -> None:
        self.assertEqual(
            StageAwsSmoke._replacement_session_config(
                "codex",
                ("codex", "claude_code"),
            ),
            ("claude_code", CHEAP_MODELS["claude_code"], CHEAP_EFFORT),
        )

        runtime, model, effort = StageAwsSmoke._replacement_session_config(
            "codex",
            ("codex",),
        )
        self.assertEqual(runtime, "codex")
        self.assertNotEqual(model, CHEAP_MODELS["codex"])
        self.assertEqual(effort, CHEAP_EFFORT)

    def test_hermes_stage_reuses_stopped_turn_to_prove_nested_steering_denial(self) -> None:
        stage = StageAwsSmoke.__new__(StageAwsSmoke)
        stage.total = 0
        stage.passed = 0
        stage.agent_runtime = "hermes"
        expected_error = (
            "Hermes cannot accept another message while running; wait for it to finish"
        )
        with (
            patch.object(stage, "_latest_thread_event_seq", return_value=0),
            patch.object(
                stage,
                "send_message",
                return_value={"status": "accepted", "thread": {"thread_id": "smoke-kill-hermes"}},
            ),
            patch.object(stage, "_wait_for_turn_activity"),
            patch.object(
                stage,
                "_api_status",
                side_effect=[
                    (HTTPStatus.CONFLICT, {"error": {"message": expected_error}}),
                    (HTTPStatus.OK, {"status": "accepted"}),
                ],
            ) as api_status,
            patch.object(
                stage,
                "_wait_for_turn",
                side_effect=[
                    {"status": "cancelled", "output_message": None, "error_message": None},
                    {"status": "completed", "output_message": "SURVIVED", "error_message": None},
                ],
            ),
            patch.object(stage, "send_follow_up", return_value={"status": "accepted"}),
            patch.object(stage, "_ssh_code", return_value="not-found") as ssh_code,
        ):
            stage.check_agent_kill_and_thread_survival(expect_steering_denied=True)

        self.assertEqual(
            api_status.call_args_list[0].args[1],
            "/v1/threads/thread-smoke-kill-hermes/messages",
        )
        self.assertEqual(
            api_status.call_args_list[1].args[1],
            "/v1/threads/thread-smoke-kill-hermes/stop",
        )
        # The stop check asserts the thread's scope unit is gone from systemd.
        self.assertIn(
            "kern-agent-thread-thread-smoke-kill-hermes.scope",
            ssh_code.call_args.args[0],
        )
        self.assertEqual((stage.passed, stage.total), (1, 1))

    def test_claude_stage_exercises_two_rapid_steers(self) -> None:
        stage = StageAwsSmoke.__new__(StageAwsSmoke)
        stage.total = 0
        stage.passed = 0
        stage.agent_runtime = "claude_code"
        with (
            patch.object(stage, "_latest_thread_event_seq", return_value=0),
            patch.object(stage, "send_message", return_value={"status": "accepted"}),
            patch.object(stage, "_wait_for_turn_activity"),
            # Nine seconds elapse between the rejected STARTING attempt and
            # the accepted attempt, but the accepted POST itself takes 0.1s.
            # Startup waiting must not be reported as steering latency.
            patch(
                "tests.smoke.smoke_aws.time.monotonic",
                side_effect=[0.0, 9.0, 9.1, 10.0, 10.1, 11.0, 11.1],
            ),
            patch.object(
                stage,
                "send_follow_up",
                side_effect=[
                    AssertionError(
                        "the agent is starting; retry shortly"
                    ),
                    {"status": "accepted"},
                    {"status": "accepted"},
                    {"status": "accepted"},
                ],
            ) as follow_up,
            patch.object(
                stage,
                "_wait_for_turn",
                side_effect=[
                    {
                        "status": "completed",
                        "output_message": "STARTUP_STEERED",
                    },
                    {
                        "status": "completed",
                        "output_message": "DOUBLE_STEERED",
                    },
                ],
            ),
        ):
            stage.check_agent_steering()

        self.assertEqual(follow_up.call_count, 4)
        self.assertIn("STARTUP_STEERED", follow_up.call_args_list[1].args[1])
        self.assertIn("DOUBLE_STEERED", follow_up.call_args_list[3].args[1])
        self.assertEqual((stage.passed, stage.total), (1, 1))

    def test_smoke_package_check_uses_the_venv_pip(self) -> None:
        stage = StageAwsSmoke.__new__(StageAwsSmoke)
        stage.total = 0
        stage.passed = 0
        with (
            patch.object(stage, "_network_events", side_effect=[[], []]),
            patch.object(
                stage,
                "_ssh_code",
                side_effect=[
                    "__KERN_VENV_OK__",
                    "__KERN_PIP_OK__",
                    "",
                    "absent",
                    "200",
                    "206",
                    "403",
                ],
            ) as ssh_code,
        ):
            stage.check_package_client_headers()

        commands = [call.args[0] for call in ssh_code.call_args_list]
        self.assertIn("uv venv --seed", commands[0])
        self.assertIn("HOME=/mnt/kern-agent/agent-home", commands[0])
        self.assertIn("cd /mnt/kern-agent/agent-home", commands[0])
        self.assertIn("SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt", commands[0])
        self.assertIn("REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt", commands[0])
        self.assertIn("NODE_EXTRA_CA_CERTS=/usr/local/share/ca-certificates/kern-network-proxy.crt", commands[0])
        self.assertIn("/venv/bin/python -m pip download", commands[1])
        self.assertNotIn("python3 -m pip download", commands[1])
        self.assertIn("sudo -u kern-agent rm -rf", commands[2])
        self.assertEqual((stage.passed, stage.total), (1, 1))

    def test_stage_package_check_uses_the_venv_pip_twice(self) -> None:
        stage = StageAwsSmoke.__new__(StageAwsSmoke)
        stage.total = 0
        stage.passed = 0
        policy = {"network_integrations": {}}
        with (
            patch.object(stage, "enforcement_policy", return_value=policy),
            patch.object(stage, "_api"),
            patch.object(stage, "_network_events", side_effect=[[], []]),
            patch.object(
                stage,
                "_ssh_code",
                side_effect=[
                    "__KERN_VENV_OK__",
                    "__KERN_PIP_OK__",
                    "__KERN_PIP_OK__",
                    '"registry-etag"',
                    "304",
                    "206",
                    "",
                ],
            ) as ssh_code,
        ):
            stage.check_package_client_headers_e2e()

        commands = [call.args[0] for call in ssh_code.call_args_list]
        self.assertIn("uv venv --seed", commands[0])
        self.assertIn("HOME=/mnt/kern-agent/agent-home", commands[0])
        self.assertIn("cd /mnt/kern-agent/agent-home", commands[0])
        self.assertIn("SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt", commands[0])
        self.assertIn("REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt", commands[0])
        self.assertIn("NODE_EXTRA_CA_CERTS=/usr/local/share/ca-certificates/kern-network-proxy.crt", commands[0])
        self.assertIn("/venv/bin/python -m pip download", commands[1])
        self.assertIn("/venv/bin/python -m pip download", commands[2])
        self.assertIn("downloads-1", commands[1])
        self.assertIn("downloads-2", commands[2])
        self.assertIn("sudo -u kern-agent rm -rf", commands[-1])
        self.assertEqual((stage.passed, stage.total), (1, 1))

    def test_every_recorded_check_name_has_an_explicit_label(self) -> None:
        """A check name with no label used to raise KeyError out of record_check.

        The label is resolved before record_check's try block, so an unlabelled
        name aborted the entire stage run instead of failing one integration.
        """
        source = Path(__file__).resolve().parents[1] / "tests" / "stage" / "stage_aws.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        recorded = {
            call.args[1].value
            for call in ast.walk(tree)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "_record_check"
            and len(call.args) > 1
            and isinstance(call.args[1], ast.Constant)
            and isinstance(call.args[1].value, str)
        }
        self.assertIn("thread_admin_api", recorded)
        self.assertIn("thread_session_switch", recorded)
        self.assertIn("stable_apps", recorded)
        unlabelled = sorted(
            name for name in recorded if name not in CHECK_LABELS and name not in BUNDLED_TOOLS
        )
        self.assertEqual(unlabelled, [], f"stage checks missing a display label: {unlabelled}")

    def test_unknown_check_name_is_labelled_instead_of_raising(self) -> None:
        self.assertEqual(_integration_label("some_future_check"), "Some future check")
        report = StageReport("all")
        self.assertTrue(_record_check(report, "some_future_check", lambda: None, "ok"))
        self.assertIn("| Some future check | available | passed | ok |", report.markdown())

    def test_report_distinguishes_failure_from_unavailable_skip(self) -> None:
        report = StageReport("all")
        self.assertTrue(_record_check(report, "codex", lambda: None, "ok"))

        def unavailable() -> None:
            raise CredentialUnavailable("expired")

        self.assertFalse(_record_check(report, "gmail", unavailable, "unused"))
        self.assertFalse(
            _record_check(
                report,
                "twitter",
                unavailable,
                "unused",
                skip_unavailable=False,
            )
        )

        def failed() -> None:
            raise AssertionError("provider broke")

        self.assertFalse(_record_check(report, "github", failed, "unused"))
        self.assertTrue(report.failed())
        markdown = report.markdown()
        self.assertIn("| Gmail | unavailable | skipped | expired |", markdown)
        self.assertIn("| X (Twitter) | unavailable | failed | expired |", markdown)
        self.assertIn("| GitHub | available | failed | AssertionError: provider broke |", markdown)

    def test_live_rejected_credential_is_reclassified_as_unavailable(self) -> None:
        stage = StageAwsSmoke.__new__(StageAwsSmoke)
        for name, message in (
            ("brave_search_search_web", "Brave Search API rejected the configured API key."),
            ("apify_search_businesses", "Apify rejected the configured API key or Actor access."),
            (
                "reddit_get_profile",
                "Reddit rejected the personal-use script credentials. Check the app type.",
            ),
        ):
            response = {
                "result": {
                    "isError": True,
                    "content": [{"text": message}],
                }
            }
            with self.subTest(name=name), patch.object(stage, "_shim_call", return_value=response):
                with self.assertRaises(CredentialUnavailable) as raised:
                    stage._shim_tool_result(name, {"query": "Kern"})
                self.assertIn(message, str(raised.exception))

    def test_action_summary_appends_markdown(self) -> None:
        report = StageReport("all")
        report.add("Tool | escaped", "available", "passed", "line one\nline | two")
        with tempfile.TemporaryDirectory() as directory:
            summary = Path(directory) / "summary.md"
            summary.write_text("existing\n", encoding="utf-8")
            with patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary)}):
                _write_action_summary(report)
            text = summary.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("existing\n## Stage integration results"))
        self.assertIn("Tool \\| escaped", text)
        self.assertIn("line one line \\| two", text)

    def test_explicit_summary_file_is_ready_for_the_final_workflow_step(self) -> None:
        report = StageReport("all")
        report.add("Codex", "available", "passed", "ok")
        with tempfile.TemporaryDirectory() as directory:
            summary = Path(directory) / "integration-summary.md"
            _write_action_summary(report, summary)
            text = summary.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("## Stage integration results"))


if __name__ == "__main__":
    unittest.main()
