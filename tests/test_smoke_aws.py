from __future__ import annotations

import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from host.runtime.tools.tools_host import BUNDLED_TOOLS, validate_against_schema
from tests.smoke.smoke_aws import OFFERED_RUNTIMES, SMOKE_RUNTIMES, SMOKE_TOOL_CALLS, AwsSmoke
from tests.stage.stage_aws import STAGE_SUITE_CHOICES, StageAwsSmoke, _required_env_path
from tests.stage.stage_integration_checks import ALL_RUNTIMES_SUITE
from tests.stage.stage_support import (
    STAGE_SUITES,
    TOOL_SUITES,
    github_app_config_from_env as _github_app_config_from_env,
    suite_tools,
)


class AwsSmokeTeardownTests(unittest.TestCase):
    def test_fresh_smoke_exercises_the_admin_listener_uid_boundary(self) -> None:
        source = Path(__file__).with_name("smoke").joinpath("smoke_aws.py").read_text()
        self.assertIn('admin_uid_results = {', source)
        for user in ("kern-operator", "kern-admin", "cloudflared", "kern-tools", "kern-proxy"):
            self.assertIn(f'"{user}"', source)
        self.assertIn('for user in ("kern-operator", "kern-admin", "cloudflared"):', source)
        self.assertIn('for user in ("kern-tools", "kern-proxy"):', source)

    def test_fresh_smoke_pins_current_deployed_agent_guidance(self) -> None:
        smoke = AwsSmoke()
        smoke.total = 0
        smoke.passed = 0
        guide = (
            Path(__file__).resolve().parents[1]
            / "host"
            / "bootstrap"
            / "agent-home"
            / "agents_claude.md"
        ).read_text()
        with patch.object(
            smoke, "_ssh_code", side_effect=[guide, "identical"]
        ) as ssh:
            smoke.check_agent_home_guidance()
        self.assertEqual((smoke.passed, smoke.total), (1, 1))
        self.assertEqual(
            ssh.call_args_list[0].args[0],
            "sudo -u kern-agent cat /mnt/kern-agent/agent-home/AGENTS.md",
        )
        self.assertIn("sudo -u kern-agent cmp", ssh.call_args_list[1].args[0])

    def test_provider_free_smoke_covers_all_workspace_resources(self) -> None:
        smoke = AwsSmoke()
        smoke.total = 0
        smoke.passed = 0
        builder_base = "/v1/workspace/web-apps"
        memory_base = "/v1/workspace/memory"
        schedules_base = "/v1/workspace/schedules"
        schedule_created = False
        workspace_session_options = {
            runtime: {f"{runtime}-model": ["high"]}
            for runtime in OFFERED_RUNTIMES
        }
        schedule_session_options = {
            **workspace_session_options,
            "script": {"bash": ["fixed"]},
        }

        def fake_api(method: str, path: str, body: dict | None = None) -> dict:
            nonlocal schedule_created
            if (method, path) == ("GET", f"{schedules_base}/session-options"):
                return {"session_options": schedule_session_options}
            if method == "GET" and path.endswith("/session-options"):
                return {"session_options": workspace_session_options}
            if method == "GET" and path.startswith("/v1/workspace/chat/threads"):
                return {"threads": []}
            if (method, path) == ("POST", f"{builder_base}/apps"):
                return {"app": {"app_id": "app-1"}}
            if (method, path) == ("GET", f"{builder_base}/apps/app-1/state"):
                return {
                    "app": {
                        "revision": 0,
                        "html": "",
                        "css": "",
                        "javascript": "",
                        "data": {},
                    }
                }
            if (method, path) == (
                "GET",
                f"{builder_base}/apps/app-1/conversation",
            ):
                return {"session": None, "status": "idle"}
            if (method, path) == (
                "GET",
                f"{builder_base}/apps/app-1/conversation/events",
            ):
                return {"events": []}
            if (method, path) == ("PUT", f"{builder_base}/apps/app-1/name"):
                self.assertEqual(body, {"name": "Provider-free smoke app"})
                return {"app": {"app_id": "app-1", "name": body["name"]}}
            if (method, path) == ("GET", f"{builder_base}/apps"):
                return {
                    "apps": [
                        {
                            "app_id": "app-1",
                            "name": "Provider-free smoke app",
                            "archived": False,
                        }
                    ]
                }
            if (method, path) == ("GET", memory_base):
                return {"pages": []}
            if (method, path) == (
                "PUT",
                f"{memory_base}/pages/provider-free-smoke",
            ):
                self.assertEqual(body and body.get("expected_revision"), 0)
                return {
                    "page": {
                        "page_id": "provider-free-smoke",
                        "revision": 1,
                    }
                }
            if method == "GET" and path.startswith(f"{memory_base}/search?"):
                return {"pages": [{"page_id": "provider-free-smoke"}]}
            if method == "DELETE" and path == (
                f"{memory_base}/pages/provider-free-smoke?expected_revision=1"
            ):
                return {"ok": True, "revision": 2}
            if (method, path) == ("GET", schedules_base):
                return {
                    "schedules": ([{"id": 7}] if schedule_created else [])
                }
            if (method, path) == ("POST", schedules_base):
                self.assertEqual(body and body.get("interval_minutes"), 7 * 24 * 60)
                schedule_created = True
                return {
                    "schedule": {
                        "id": 7,
                        "revision": 1,
                        "thread_id": "schedule-7",
                    }
                }
            if method == "DELETE" and path == f"{schedules_base}/7?expected_revision=1":
                return {"ok": True, "revision": 2, "thread_id": "schedule-7"}
            raise AssertionError((method, path, body))

        with patch.object(smoke, "_api", side_effect=fake_api):
            smoke.check_workspace_backends_without_providers()

        self.assertEqual(smoke.passed, 1)

    def test_script_launcher_probe_runs_the_real_launcher_and_confines_paths(self) -> None:
        smoke = AwsSmoke()
        smoke.total = 0
        smoke.passed = 0
        commands: list[str] = []

        def fake_ssh(command: str) -> str:
            commands.append(command)
            if "--thread-scope smoke-agent-script" in command:
                return "kern-smoke-script-ok\nkern-agent"
            if "echo status=$?" in command:
                # The demoted side owns the not-found status; root owns the
                # usage rejections.
                return "status=66" if "kern-smoke-absent.sh" in command else "status=64"
            return ""

        with patch.object(smoke, "_ssh_code", side_effect=fake_ssh):
            smoke.check_installed_agent_script_launcher()

        joined = "\n".join(commands)
        self.assertIn("sudo -u kern-admin", joined)
        self.assertIn("/usr/local/lib/kern-host/run-agent-script", joined)
        # The probe writes and removes its own script, and never leaves a
        # named scope behind for the next run of the same thread id.
        self.assertIn("sudo -u kern-agent tee", joined)
        self.assertIn("rm -f /mnt/kern-agent/agent-home/kern-smoke-script.sh", joined)
        self.assertIn("stop kern-agent-thread-smoke-agent-script.scope", joined)
        self.assertEqual(smoke.passed, 1)

    def test_script_launcher_probe_fails_when_a_confined_path_is_accepted(self) -> None:
        smoke = AwsSmoke()
        smoke.total = 0
        smoke.passed = 0

        def fake_ssh(command: str) -> str:
            if "--thread-scope smoke-agent-script" in command:
                return "kern-smoke-script-ok\nkern-agent"
            return "status=0" if "echo status=$?" in command else ""

        with (
            patch.object(smoke, "_ssh_code", side_effect=fake_ssh),
            self.assertRaises(AssertionError) as caught,
        ):
            smoke.check_installed_agent_script_launcher()
        self.assertIn("/etc/hostname", str(caught.exception))
        self.assertEqual(smoke.passed, 0)

    def test_precredential_bedrock_probe_runs_real_hermes_launcher(self) -> None:
        smoke = AwsSmoke()
        smoke.total = 0
        smoke.passed = 0
        commands: list[str] = []
        event_reads = 0

        def fake_ssh(command: str) -> str:
            commands.append(command)
            if "SELECT count(*) FROM bedrock_credentials" in command:
                return "0"
            return "expected credential failure"

        def fake_events(since: int = 0) -> list[dict]:
            nonlocal event_reads
            event_reads += 1
            if event_reads == 1:
                return []
            if event_reads == 2:
                return [
                    {
                        "seq": 1,
                        "host": "bedrock-runtime.us-east-1.amazonaws.com",
                        "path": "/model/qwen.qwen3-coder-next/converse",
                        "decision": "denied",
                        "reason_code": "bedrock_credentials_unavailable",
                    }
                ]
            if event_reads == 3:
                return [
                    {
                        "seq": 1,
                        "host": "bedrock-runtime.us-east-1.amazonaws.com",
                        "path": "/model/qwen.qwen3-coder-next/converse",
                        "decision": "denied",
                        "reason_code": "bedrock_credentials_unavailable",
                    }
                ]
            return [
                {
                    "seq": 2,
                    "host": "bedrock-runtime.us-east-1.amazonaws.com",
                    "path": "/model/qwen.qwen3-coder-next/converse",
                    "decision": "denied",
                    "reason_code": "bedrock_credentials_unavailable",
                }
            ]

        with (
            patch.object(smoke, "_api", return_value={}),
            patch.object(smoke, "_ssh_code", side_effect=fake_ssh),
            patch.object(smoke, "_network_events", side_effect=fake_events),
        ):
            smoke.check_precredential_bedrock_harness_launchers()

        joined = "\n".join(commands)
        self.assertIn("sudo -u kern-admin", joined)
        self.assertEqual(joined.count("--model qwen.qwen3-coder-next"), 1)
        self.assertIn("/usr/local/lib/kern-host/run-hermes", joined)
        self.assertIn("--model qwen.qwen3-coder-next", joined)
        self.assertEqual(smoke.passed, 1)

    def test_fresh_smoke_uses_strict_deploy_command_and_stdout_result(self) -> None:
        smoke = AwsSmoke()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            smoke.workdir = tmp_path
            smoke.public_key = "ssh-ed25519 AAAATEST operator@example"
            smoke.ssh_key = str(tmp_path / "operator_key")
            calls: list[list[str]] = []

            class _Proc:
                stdout = json.dumps(
                    {
                        "agent_name": "kern-smoke",
                        "instance_id": "i-smoke",
                        "region": "us-east-1",
                        "public_dns": "smoke.example.com",
                    }
                )

            def fake_run(args: list[str], **kwargs: object) -> object:
                calls.append(args)
                if kwargs.get("cwd") != tmp_path:
                    raise AssertionError(f"fresh smoke deploy used unexpected cwd: {kwargs.get('cwd')!r}")
                env = kwargs.get("env")
                assert isinstance(env, dict) and env.get("AWS_REGION") == "us-east-1"
                return _Proc()

            with (
                patch.object(smoke, "_destroy_tagged_smoke_resources"),
                patch("tests.smoke.smoke_aws.subprocess.run", side_effect=fake_run),
            ):
                smoke.deploy()

        self.assertEqual(calls[0][1:3], ["-m", "host.cli.deploy"])
        self.assertIn("--agent-name", calls[0])
        self.assertEqual(calls[0][calls[0].index("--agent-name") + 1], "kern-smoke")
        self.assertIn("--operator-ssh-public-key", calls[0])
        self.assertIn("--admin-password-sha256", calls[0])
        self.assertNotIn("--config", calls[0])
        self.assertNotIn("--result-file", calls[0])
        assert smoke.result is not None
        self.assertEqual(smoke.result["instance_id"], "i-smoke")
        # The harness injects its own generated password for admin API auth.
        self.assertIn("admin_password", smoke.result)

    def test_teardown_destroys_tagged_resources_without_deploy_result(self) -> None:
        smoke = AwsSmoke()
        calls: list[tuple[str, ...]] = []
        instances_terminated = False
        volumes_deleted = set()
        security_group_deleted = False

        def fake_aws(*args: str) -> dict:
            nonlocal instances_terminated, security_group_deleted
            calls.append(args)
            if args[:2] == ("ec2", "describe-instances"):
                states = next((arg for arg in args if arg.startswith("Name=instance-state-name,Values=")), "")
                if not instances_terminated and "shutting-down" not in states:
                    return {"Reservations": [{"Instances": [{"InstanceId": "i-smoke"}]}]}
                return {"Reservations": []}
            if args[:2] == ("ec2", "terminate-instances"):
                instances_terminated = True
                return {}
            if args[:2] == ("ec2", "describe-volumes"):
                volumes = [
                    {"VolumeId": "vol-root", "State": "available"},
                    {"VolumeId": "vol-admin", "State": "available"},
                    {"VolumeId": "vol-agent", "State": "available"},
                ]
                return {"Volumes": [volume for volume in volumes if volume["VolumeId"] not in volumes_deleted]}
            if args[:2] == ("ec2", "delete-volume"):
                volumes_deleted.add(args[args.index("--volume-id") + 1])
                return {}
            if args[:2] == ("ec2", "describe-security-groups"):
                if security_group_deleted:
                    return {"SecurityGroups": []}
                return {"SecurityGroups": [{"GroupId": "sg-smoke"}]}
            if args[:2] == ("ec2", "delete-security-group"):
                security_group_deleted = True
                return {}
            if args[:3] == ("ec2", "wait", "instance-terminated"):
                return {}
            if args[:3] == ("ec2", "wait", "volume-available"):
                return {}
            if args[:3] == ("ec2", "wait", "volume-deleted"):
                return {}
            raise AssertionError(f"unexpected AWS call: {args}")

        smoke._aws = fake_aws  # type: ignore[method-assign]
        smoke.teardown()

        self.assertIn(("ec2", "terminate-instances", "--instance-ids", "i-smoke"), calls)
        self.assertEqual(volumes_deleted, {"vol-root", "vol-admin", "vol-agent"})
        self.assertTrue(security_group_deleted)


class ThreadTurnHelperTests(unittest.TestCase):
    def test_wait_for_turn_reads_terminal_event_and_agent_output(self) -> None:
        smoke = AwsSmoke()
        events = [
            {"seq": 2, "event_type": "thread.message", "payload": {"message": "prompt", "source": "user"}},
            {"seq": 3, "event_type": "thread.message", "payload": {"message": "DONE", "source": "agent"}},
        ]
        with (
            patch.object(smoke, "_thread_events", return_value=events) as reader,
            patch.object(smoke, "_api", return_value={"thread": {"status": "idle"}}),
        ):
            done = smoke._wait_for_turn("smoke-turn", timeout=5, since=0)
        self.assertEqual(
            done, {"status": "completed", "output_message": "DONE", "error_message": None}
        )
        self.assertEqual(reader.call_args.kwargs.get("since"), 0)

    def test_wait_for_turn_surfaces_failure_and_cancellation(self) -> None:
        smoke = AwsSmoke()
        failed = [
            {"seq": 2, "event_type": "thread.error", "payload": {"error_message": "boom"}},
        ]
        with (
            patch.object(smoke, "_thread_events", return_value=failed),
            patch.object(smoke, "_api", return_value={"thread": {"status": "idle"}}),
        ):
            done = smoke._wait_for_turn("smoke-turn", timeout=5)
        self.assertEqual(done, {"status": "failed", "output_message": None, "error_message": "boom"})
        cancelled = [
            {"seq": 2, "event_type": "thread.stopped", "payload": {}},
        ]
        with (
            patch.object(smoke, "_thread_events", return_value=cancelled),
            patch.object(smoke, "_api", return_value={"thread": {"status": "idle"}}),
        ):
            done = smoke._wait_for_turn("smoke-turn", timeout=5)
        self.assertEqual(done["status"], "cancelled")
        # A running thread with no explicit terminal event is not a result.
        self.assertIsNone(smoke._turn_result([], "running"))

    def test_post_message_retries_only_the_thread_close_fence(self) -> None:
        smoke = AwsSmoke()
        smoke.thread_prefix = "thread-stage-test-"
        fence = AssertionError(
            "POST /v1/threads/thread-stage-test-smoke-fence/messages returned HTTP 409: "
            "{'error': {'message': 'the agent is finishing; retry shortly'}}"
        )
        with (
            patch.object(smoke, "_api", side_effect=[fence, {"status": "accepted"}]) as api,
            patch("tests.smoke.smoke_aws.time.sleep"),
        ):
            response = smoke.send_follow_up("smoke-fence", "again")
        self.assertEqual(response, {"status": "accepted"})
        self.assertEqual(api.call_count, 2)
        self.assertEqual(api.call_args.args[1], "/v1/threads/thread-stage-test-smoke-fence/messages")
        self.assertEqual(api.call_args.args[2], {"message": "again"})

        hermes = AssertionError(
            "POST /v1/threads/thread-stage-test-smoke-fence/messages returned HTTP 409: "
            "{'error': {'message': 'Hermes cannot accept another message while running; wait for it to finish'}}"
        )
        with patch.object(smoke, "_api", side_effect=hermes) as api:
            with self.assertRaisesRegex(AssertionError, "cannot accept another message"):
                smoke.send_message("smoke-fence", "again")
        self.assertEqual(api.call_count, 1)

    def test_message_body_carries_full_config_and_follow_up_none(self) -> None:
        smoke = AwsSmoke()
        body = smoke.message_body("hello", runtime="claude_code")
        self.assertEqual(
            body,
            {
                "message": "hello",
                "agent_runtime": "claude_code",
                "model": "claude-opus-5",
                "effort": "high",
            },
        )
        self.assertEqual(smoke.follow_up_body("again"), {"message": "again"})
        self.assertEqual(smoke.api_thread_id("smoke-x"), "thread-smoke-x")
        self.assertEqual(smoke.thread_id_component("claude_code"), "claude-code")
        self.assertEqual(
            smoke.thread_id_component("moonshotai.kimi-k2.5"),
            "moonshotai-kimi-k2-5",
        )


class StageAwsSmokeTests(unittest.TestCase):
    def test_stage_rejects_non_stage_result_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            result_path = tmp_path / "wrong.json"
            result_path.write_text(
                json.dumps(
                    {
                        "agent_name": "kern-smoke",
                        "region": "us-east-1",
                        "public_dns": "smoke.example.com",
                        "admin_password": "stable-admin",
                    }
                )
            )
            ssh_key = tmp_path / "stage_operator"
            ssh_key.write_text("private key")

            with self.assertRaisesRegex(AssertionError, "expected 'kern-stage'"):
                StageAwsSmoke(result_path, ssh_key)

    def test_stage_upgrade_result_requires_admin_password_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            result_path = tmp_path / "kern-stage.json"
            result_path.write_text(
                json.dumps(
                    {
                        "agent_name": "kern-stage",
                        "region": "us-east-1",
                        "public_dns": "stage.example.com",
                    }
                )
            )
            ssh_key = tmp_path / "stage_operator"
            ssh_key.write_text("private key")

            with (
                patch.dict("os.environ", {}, clear=True),
                self.assertRaisesRegex(AssertionError, "STAGE_ADMIN_PASSWORD is not set or empty"),
            ):
                StageAwsSmoke(result_path, ssh_key, "STAGE_ADMIN_PASSWORD")

    def test_stage_uses_admin_password_env_when_upgrade_result_omits_password(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            result_path = tmp_path / "kern-stage.json"
            result_path.write_text(
                json.dumps(
                    {
                        "agent_name": "kern-stage",
                        "region": "us-east-1",
                        "public_dns": "stage.example.com",
                    }
                )
            )
            ssh_key = tmp_path / "stage_operator"
            ssh_key.write_text("private key")
            with patch.dict("os.environ", {"STAGE_ADMIN_PASSWORD": "stable-admin"}):
                smoke = StageAwsSmoke(result_path, ssh_key, "STAGE_ADMIN_PASSWORD")

        self.assertEqual(smoke.result["admin_password"], "stable-admin")

    def test_stage_accepts_start_result_with_admin_password_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            result_path = tmp_path / "kern-stage.json"
            result_path.write_text(
                json.dumps(
                    {
                        "agent_name": "kern-stage",
                        "operation": "start",
                        "state": "running",
                        "region": "us-east-1",
                        "public_dns": "stage.example.com",
                    }
                )
            )
            ssh_key = tmp_path / "stage_operator"
            ssh_key.write_text("private key")
            with patch.dict("os.environ", {"STAGE_ADMIN_PASSWORD": "stable-admin"}):
                smoke = StageAwsSmoke(result_path, ssh_key, "STAGE_ADMIN_PASSWORD")

        self.assertEqual(smoke.result["admin_password"], "stable-admin")
        self.assertEqual(smoke.result["operation"], "start")

    def test_stage_ssh_key_path_can_come_from_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ssh_key = Path(tmp) / "stage_operator"
            ssh_key.write_text("private key")
            with patch.dict("os.environ", {"STAGE_SSH_KEY": str(ssh_key)}):
                self.assertEqual(_required_env_path("STAGE_SSH_KEY"), ssh_key)

    def test_stage_suite_runtimes_scope_each_suite(self) -> None:
        self.assertEqual(StageAwsSmoke.suite_runtimes("codex"), ("codex",))
        self.assertEqual(StageAwsSmoke.suite_runtimes("claude"), ("claude_code",))
        self.assertEqual(StageAwsSmoke.suite_runtimes("grok"), ("grok",))
        self.assertEqual(StageAwsSmoke.suite_runtimes("hermes"), ("hermes",))
        self.assertEqual(StageAwsSmoke.suite_runtimes("github"), ())
        self.assertEqual(StageAwsSmoke.suite_runtimes("brave_search"), ())
        self.assertEqual(StageAwsSmoke.suite_runtimes("gmail"), ())
        self.assertEqual(StageAwsSmoke.suite_runtimes("google_calendar"), ())
        self.assertEqual(
            StageAwsSmoke.suite_runtimes("all"),
            ("codex", "claude_code", "grok", "hermes"),
        )
        self.assertEqual(
            StageAwsSmoke.suite_runtimes(ALL_RUNTIMES_SUITE),
            ("codex", "claude_code", "grok", "hermes"),
        )
        self.assertIn(ALL_RUNTIMES_SUITE, STAGE_SUITE_CHOICES)
        self.assertNotIn(ALL_RUNTIMES_SUITE, TOOL_SUITES)
        self.assertTrue(set(TOOL_SUITES).issubset(STAGE_SUITES))
        self.assertEqual(suite_tools("all"), TOOL_SUITES)
        self.assertEqual(suite_tools(ALL_RUNTIMES_SUITE), ())
        self.assertEqual(suite_tools("linkedin"), ("linkedin",))
        self.assertEqual(suite_tools("github"), ())

    def test_stage_autoconfiguration_never_touches_oauth_tools(self) -> None:
        smoke = object.__new__(StageAwsSmoke)
        configured: set[tuple[str, str]] = set()
        calls: list[tuple[str, str, dict | None]] = []

        def fake_api(method: str, path: str, body: dict | None = None) -> dict:
            calls.append((method, path, body))
            if method == "PUT" and body is not None:
                configured.add((path.split("/")[3], body["key"]))
                return {}
            if method == "GET" and path == "/v1/tools":
                return {
                    "tools": [
                        {
                            "tool_id": tool_id,
                            "config": [
                                {"key": requirement.key, "set": (tool_id, requirement.key) in configured}
                                for requirement in BUNDLED_TOOLS[tool_id].manifest.config
                            ],
                        }
                        for tool_id in ("brave_search", "gmail")
                    ]
                }
            if method == "POST":
                return {}
            raise AssertionError((method, path, body))

        smoke._api = fake_api  # type: ignore[method-assign]
        environment = {
            "KERN_STAGE_BRAVE_SEARCH_API_KEY": "brave-key",
            "KERN_STAGE_GOOGLE_OAUTH_CLIENT_ID": "must-not-be-read",
            "KERN_STAGE_GOOGLE_OAUTH_CLIENT_SECRET": "must-not-be-read",
        }
        with patch.dict("os.environ", environment, clear=False):
            smoke.autoconfigure_tools(("brave_search", "gmail"))

        self.assertIn(
            ("PUT", "/v1/tools/brave_search/config", {"key": "BRAVE_SEARCH_API_KEY", "value": "brave-key"}),
            calls,
        )
        self.assertIn(("POST", "/v1/tools/brave_search/enable", {}), calls)
        self.assertFalse(any("/gmail/" in path for _, path, _ in calls))

    def test_stage_oauth_preflight_points_only_to_persistent_host_setup(self) -> None:
        smoke = object.__new__(StageAwsSmoke)
        smoke._api = lambda *_args, **_kwargs: {  # type: ignore[method-assign]
            "tools": [
                {
                    "tool_id": "gmail",
                    "enabled": False,
                    "config": [
                        {"key": "GOOGLE_OAUTH_CLIENT_ID", "set": False},
                        {"key": "GOOGLE_OAUTH_CLIENT_SECRET", "set": False},
                    ],
                    "connection_status": {"connected": False},
                }
            ]
        }
        failures = smoke._tool_credential_failures("gmail")
        self.assertEqual(len(failures), 1)
        self.assertIn("stage admin UI", failures[0])
        self.assertIn("connect its stage account once", failures[0])
        self.assertIn("enable the tool", failures[0])
        self.assertNotIn("KERN_STAGE_GOOGLE", failures[0])

    def test_fresh_smoke_has_valid_input_for_every_bundled_action(self) -> None:
        covered = {
            f"{tool_id}_{action_id}"
            for tool_id, calls in SMOKE_TOOL_CALLS.items()
            for action_id, _arguments in calls
        }
        covered.update(
            {
                "polymarket_get_market",
                "polymarket_get_order_book",
                "polymarket_price_history",
            }
        )
        declared = {
            f"{tool_id}_{action.id}"
            for tool_id, tool in BUNDLED_TOOLS.items()
            for action in tool.manifest.actions
        }
        self.assertEqual(covered, declared)
        for tool_id, calls in SMOKE_TOOL_CALLS.items():
            for action_id, arguments in calls:
                spec = BUNDLED_TOOLS[tool_id].manifest.action(action_id)
                self.assertIsNotNone(spec)
                assert spec is not None
                self.assertEqual(
                    validate_against_schema(arguments, spec.input_schema),
                    "",
                    f"invalid smoke input for {tool_id}_{action_id}",
                )
        for action_id, arguments in (
            ("get_market", {"market_id": "1"}),
            ("get_order_book", {"token_id": "1"}),
            ("price_history", {"token_id": "1", "interval": "1d"}),
        ):
            spec = BUNDLED_TOOLS["polymarket"].manifest.action(action_id)
            assert spec is not None
            self.assertEqual(
                validate_against_schema(arguments, spec.input_schema),
                "",
                f"invalid smoke input for polymarket_{action_id}",
            )

    def test_github_app_config_from_env_parses_or_requires_all(self) -> None:
        keys = (
            "STAGE_GITHUB_WRITE_REPO",
            "STAGE_GITHUB_APP_ID",
            "STAGE_GITHUB_APP_INSTALLATION_ID",
            "STAGE_GITHUB_APP_PRIVATE_KEY",
        )
        with patch.dict("os.environ", {key: "" for key in keys}, clear=False):
            self.assertEqual(_github_app_config_from_env(), (None, None))
        full = {
            "STAGE_GITHUB_WRITE_REPO": "infiloop2/sandbox",
            "STAGE_GITHUB_APP_ID": "123",
            "STAGE_GITHUB_APP_INSTALLATION_ID": "456",
            "STAGE_GITHUB_APP_PRIVATE_KEY": "-----BEGIN KEY-----\nx\n-----END KEY-----",
        }
        with patch.dict("os.environ", full, clear=False):
            config, error = _github_app_config_from_env()
        self.assertIsNone(error)
        assert config is not None
        self.assertEqual(config["owner"], "infiloop2")
        self.assertEqual(config["repo"], "sandbox")
        self.assertEqual(config["app_id"], "123")
        self.assertEqual(config["installation_id"], "456")
        with patch.dict("os.environ", {**full, "STAGE_GITHUB_APP_ID": ""}, clear=False):
            partial, partial_error = _github_app_config_from_env()
        self.assertIsNone(partial)
        self.assertIn("STAGE_GITHUB_APP_ID", partial_error or "")

    def test_stage_enforcement_policy_lists_stage_repo_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result_path = Path(tmp) / "kern-stage.json"
            result_path.write_text(
                json.dumps({"agent_name": "kern-stage", "region": "us-east-1", "public_dns": "stage.example.com"})
            )
            ssh_key = Path(tmp) / "stage_operator"
            ssh_key.write_text("private key")
            smoke = StageAwsSmoke(result_path, ssh_key)
        smoke.stage_github_repositories = [{"owner": "sandbox-owner", "repo": "sandbox"}]
        integrations = smoke.enforcement_policy()["network_integrations"]
        repos = integrations["github"]["write_repositories"]
        self.assertEqual(repos[0], {"owner": "sandbox-owner", "repo": "sandbox"})
        self.assertIn({"owner": "infiloop2", "repo": "kern"}, repos)
        self.assertEqual(integrations["xai"], {"enabled": True})


class WorkflowSmokeTests(unittest.TestCase):
    def test_stage_workflows_use_first_class_power_cli(self) -> None:
        stage = Path(".github/workflows/kern-stage.yml").read_text()
        stage_start = Path(".github/workflows/kern-stage-start.yml").read_text()
        stage_stop = Path(".github/workflows/kern-stage-stop.yml").read_text()

        self.assertIn("python3 -m host.cli.start", stage)
        self.assertIn("python3 -m host.cli.stop", stage)
        self.assertIn("same_version_failure=${same_version_failure}", stage)
        self.assertIn(
            "steps.upgrade_stage.outcome == 'failure' && steps.upgrade_stage.outputs.same_version_failure == 'true'",
            stage,
        )
        self.assertIn("steps.upgrade_stage.outcome == 'failure' && steps.start_current.outcome != 'success'", stage)
        self.assertIn("2>&1 > kern-stage.json | tee stage-upgrade.log >&2", stage)
        self.assertIn("2>&1 > kern-stage.json | tee stage-recover.log >&2", stage)
        self.assertNotIn("> >(tee stage-", stage)
        self.assertIn("first_deploy=true", stage)
        self.assertIn("else\n              exit 1", stage)
        self.assertIn("python3 -m host.cli.start", stage_start)
        self.assertIn("python3 -m host.cli.stop", stage_stop)
        # The CLI takes flags and prints its result to stdout; the workflows
        # write no config files and redirect stdout to the step artifact.
        for workflow in (stage, stage_start, stage_stop):
            self.assertNotIn("--config", workflow)
            self.assertNotIn("config.json", workflow)
        self.assertIn("--agent-name kern-stage", stage_start)
        self.assertIn("--agent-name kern-stage", stage_stop)
        self.assertIn("--operator-ssh-public-key", stage)
        self.assertIn("> kern-stage.json", stage)
        removed_action = "start-stage" + "-instance"
        self.assertNotIn(removed_action, stage)
        self.assertNotIn(removed_action, stage_start)
        self.assertNotIn(removed_action, stage_stop)

    def test_stage_workflow_exposes_only_enable_only_tool_secrets(self) -> None:
        stage = Path(".github/workflows/kern-stage.yml").read_text()
        for option in ("all", *TOOL_SUITES, "claude", "codex", "grok", "github"):
            self.assertIn(f"- {option}", stage)
        self.assertIn("--suite", stage)
        self.assertIn("--summary-file stage-integration-summary.md", stage)
        self.assertIn('cat stage-integration-summary.md >> "${GITHUB_STEP_SUMMARY}"', stage)
        for env_name in (
            "STAGE_GITHUB_WRITE_REPO",
            "STAGE_GITHUB_APP_ID",
            "STAGE_GITHUB_APP_INSTALLATION_ID",
            "STAGE_GITHUB_APP_PRIVATE_KEY",
        ):
            self.assertIn(env_name, stage)
        for env_name in (
            "STAGE_BEDROCK_AWS_ACCESS_KEY_ID",
            "STAGE_BEDROCK_AWS_SECRET_ACCESS_KEY",
            "KERN_STAGE_BEDROCK_AWS_ACCESS_KEY_ID",
            "KERN_STAGE_BEDROCK_AWS_SECRET_ACCESS_KEY",
        ):
            self.assertIn(env_name, stage)
        for tool_id in TOOL_SUITES:
            for requirement in BUNDLED_TOOLS[tool_id].manifest.config:
                env_name = f"KERN_STAGE_{requirement.key}"
                mapping = f"{env_name}: ${{{{ secrets.{env_name} }}}}"
                if BUNDLED_TOOLS[tool_id].manifest.connection == "oauth":
                    self.assertNotIn(mapping, stage)
                else:
                    self.assertIn(mapping, stage)

    def test_fresh_smoke_workflow_uses_fresh_smoke_script(self) -> None:
        smoke = Path(".github/workflows/kern-smoke.yml").read_text()

        self.assertIn("playwright==1.60.0", smoke)
        self.assertIn("playwright==1.60.0", Path("tests/requirements.txt").read_text())
        self.assertIn('"${RUNNER_TEMP}/kern-smoke-venv/bin/python" tests/smoke/smoke_aws.py', smoke)
        self.assertLess(smoke.index("playwright==1.60.0"), smoke.index("AWS_ACCESS_KEY_ID"))
        self.assertIn("context kern-smoke", smoke)
        self.assertIn("github.event_name == 'workflow_dispatch'", smoke)
        self.assertIn("Fresh AWS smoke is already running; wait for the previous smoke to complete.", smoke)
        self.assertIn("for status in queued in_progress", smoke)
        self.assertIn("group: kern-smoke", smoke)

    def test_superseded_lima_smoke_does_not_overwrite_active_status(self) -> None:
        smoke = Path(".github/workflows/test-lima-host.yml").read_text()

        self.assertIn("needs.smoke.result != 'cancelled'", smoke)
        self.assertNotIn("Fresh Lima smoke was cancelled.", smoke)
