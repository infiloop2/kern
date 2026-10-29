"""Health endpoint cases using the shared admin integration fixture."""

from __future__ import annotations

import urllib.error
from unittest.mock import patch

import test_admin_api
from host.runtime.agent_runtime import orchestrator
from test_admin_api import set_runtime_statuses


class AdminHealthTests(test_admin_api.AdminApiTestCase):
    def test_health_requires_auth_and_reports_state(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("GET", "/v1/health", auth=False)
        self.assertEqual(error.exception.code, 401)

        status, body = self.health()

        self.assertEqual(status, 200)
        self.assertEqual(body["agent_name"], "kern-test")
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["issues"], [])
        self.assertEqual(self.runtime(body)["status"], "active")
        self.assertEqual(self.runtime(body, "claude_code")["status"], "deactivated")
        self.assertEqual(body["network_controls"]["status"], "active")
        self.assertEqual(body["version"], {"status": "ok", "runtime": "0.2.0", "state": "0.2.0"})
        self.assertEqual(body["upgrade"], {"available": True, "latest": "0.3.0"})
        self.assertEqual(
            body["history"],
            {"threads": 0, "messages": 0, "activities": 0},
        )
        self.assertEqual(body["lifetime_tokens"], {
            "input_tokens": 0, "cached_input_tokens": 0, "cache_write_tokens": 0, "output_tokens": 0,
        })


    def test_health_reports_error_when_proxy_is_down(self) -> None:
        _, body = self.health(proxy_alive=False)
        self.assertEqual(body["network_controls"]["status"], "error")
        self.assertEqual(body["status"], "degraded")
        self.assertEqual(body["issues"], [{
            "kind": "network_controls",
            "summary": "Network controls are unavailable",
            "detail": "The network policy is active, but the enforcing proxy is not accepting connections.",
            "next_step": "Stop starting agents and use the operator plane to recover or upgrade the host.",
        }])


    def test_health_is_degraded_when_runtime_and_state_versions_differ(self) -> None:
        _, body = self.health(
            version={"status": "mismatch", "runtime": "1.8.0", "state": "1.7.0"}
        )
        self.assertEqual(body["version"]["status"], "mismatch")
        self.assertEqual(body["status"], "degraded")
        self.assertEqual(body["issues"], [{
            "kind": "version",
            "summary": "Host versions do not match",
            "detail": "Runtime 1.8.0; durable state 1.7.0.",
            "next_step": "Run a Kern upgrade or recovery from the operator plane.",
        }])


    def test_health_identifies_an_unavailable_version_file(self) -> None:
        cases = (
            (
                {"status": "error", "runtime": None, "state": "1.8.0"},
                "Unavailable or invalid: running root version (/opt/kern-host/VERSION).",
            ),
            (
                {"status": "error", "runtime": "1.8.0", "state": None},
                "Unavailable or invalid: durable-state version "
                "(/mnt/kern-admin/admin-state/version.json).",
            ),
        )
        for version, detail in cases:
            with self.subTest(version=version):
                _, body = self.health(version=version)

                self.assertEqual(body["status"], "degraded")
                self.assertEqual(body["issues"], [{
                    "kind": "version",
                    "summary": "Host version information is unavailable",
                    "detail": detail,
                    "next_step": (
                        "Run a Kern recovery from the operator plane to restore the version metadata."
                    ),
                }])


    def test_health_explains_runtime_errors_and_the_next_step(self) -> None:
        set_runtime_statuses(codex="active", claude_code="error")
        orchestrator._RUNTIME_STATUSES["claude_code"]["error_message"] = (
            "Claude authentication probe timed out after 30 seconds"
        )

        _, body = self.health()

        self.assertEqual(body["status"], "degraded")
        self.assertEqual(body["issues"], [{
            "kind": "agent_runtime",
            "summary": "Claude Code is unavailable",
            "detail": "Claude authentication probe timed out after 30 seconds",
            "next_step": (
                "Open Home > Agent runtimes > Claude Code, refresh its status, "
                "and reconnect or revalidate the account if the error continues."
            ),
        }])


    def test_health_is_degraded_when_root_volume_is_nearly_full(self) -> None:
        _, body = self.health(
            host={
                "cpu": {},
                "memory": {},
                "filesystem": {
                    "mounts": {
                        "root": {"used_bytes": 15, "total_bytes": 16},
                    }
                },
                "swap": {},
            }
        )

        self.assertEqual(body["status"], "degraded")
        self.assertEqual(body["issues"], [{
            "kind": "root_filesystem",
            "summary": "Root volume is nearly full",
            "detail": "The root filesystem is 93.8% full.",
            "next_step": (
                "Stop agent work, inspect /tmp and /var/tmp, and remove unneeded temporary "
                "files; redeploy if free space does not recover."
            ),
        }])


    def test_health_never_spawns_codex(self) -> None:
        # The health/status path must read cached state only — a hanging Codex
        # app-server must never be able to block it.
        set_runtime_statuses(codex="loading", claude_code="deactivated")
        with patch(
            "host.runtime.agent_runtime.orchestrator.codex_app_server.account_status",
            side_effect=AssertionError("health must not call Codex"),
        ):
            _, body = self.health()
        self.assertEqual(self.runtime(body)["status"], "loading")

