from __future__ import annotations

import base64
from contextlib import ExitStack, nullcontext
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest

import pg_harness
from unittest.mock import patch

from host.config import (
    ConfigError,
    NetworkControls,
    parse_network_controls,
)
from host.cli.aws_resources import _subnet_has_public_ipv4_route
from host.network_integrations.claude import guard as claude_guard
from host.network_integrations.github import guard as github_guard
from host.network_integrations.openai import guard as openai_guard
from host.network_integrations.registry import managed_domain_owner
from host.network_integrations import runtime as network_integrations
from host.network_integrations.custom.manifest import CustomIntegration, rule_for_host
from host.network_integrations.base import PROXY_DENIAL_REASONS


def _controls(policy: NetworkControls | dict) -> NetworkControls:
    return policy if isinstance(policy, NetworkControls) else parse_network_controls(policy)


def _custom_policy(domains: dict) -> dict:
    """A network policy whose only integration is custom, with ``domains``."""
    return {"network_integrations": {"custom": {"domains": domains}}}


def openai_request_denied(policy, host, headers, body, path="/"):
    return openai_guard.request_denied(
        _controls(policy).integrations["openai"], "POST", host, path, "", headers, body
    )


def _jwt_segment(obj) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


def openai_bearer(account_id: str) -> str:
    """A genuine-shaped ChatGPT OAuth access token: base64url JSON header and
    payload (with the chatgpt_account_id claim) plus a dummy signature."""
    header = _jwt_segment({"alg": "RS256", "typ": "JWT"})
    payload = _jwt_segment({"https://api.openai.com/auth": {"chatgpt_account_id": account_id}})
    return f"Bearer {header}.{payload}.c2ln"


def anthropic_request_denied(policy, method, host, path, headers, body=b"", attest_account=None):
    return claude_guard.request_denied(
        _controls(policy).integrations["claude"], method, host, path, "", headers, body,
        attest_account,
    )


def github_request_denied(policy, method, host, path, query, body):
    return github_guard.request_denied(
        _controls(policy).integrations["github"], method, host, path, query, [], body
    )


def github_push_gate_response(policy, method, host, path, body):
    return github_guard.gate_response(
        _controls(policy).integrations["github"], method, host, path, body
    )


def github_receive_pack(*refs: str, side_band: bool = True) -> bytes:
    old = "0" * 40
    new = "1" * 40
    capabilities = "report-status"
    if side_band:
        capabilities += " side-band-64k"
    lines = []
    for index, ref in enumerate(refs):
        suffix = f"\x00{capabilities}" if index == 0 else ""
        payload = f"{old} {new} {ref}{suffix}".encode()
        lines.append(f"{len(payload) + 4:04x}".encode() + payload)
    return b"".join(lines) + b"0000PACK"


def _gate_capacity(pending: int = 0) -> ExitStack:
    stack = ExitStack()
    stack.enter_context(
        patch("host.network_integrations.github.guard.count_pending_pushes", return_value=pending)
    )
    stack.enter_context(
        patch(
            "host.network_integrations.github.guard.push_gate.quarantine_lock",
            return_value=nullcontext(),
        )
    )
    return stack


def request_allowed(policy, method, host, path, query=""):
    return network_integrations.request_denied(
        _controls(policy), method, host, path, query, [], b""
    ) is None
from host.runtime.network_proxy.service import read_request_head
from host.runtime.core.state import save_proxy_claude_account_id, save_proxy_openai_account_id


class ConfigTests(unittest.TestCase):
    def test_agent_name_restrictions(self) -> None:
        with self.assertRaises(ConfigError):
            parse_network_controls({"network_integrations": { "openai": {"enabled": True}, "custom": {"domains": {"*": {}}} }})

    def test_managed_providers_are_independently_optional(self) -> None:
        for controls in (
            {},
            {"network_integrations": {}},
            {
                "network_integrations": {"openai": {"enabled": False}, "claude": {"enabled": False}},
            },
            {"network_integrations": {"openai": {"enabled": True}}},
            {"network_integrations": {"claude": {"enabled": True}}},
        ):
            with self.subTest(controls=controls):
                parsed = parse_network_controls(controls)
                self.assertIsInstance(parsed.integrations["openai"].enabled, bool)
                self.assertIsInstance(parsed.integrations["claude"].enabled, bool)

        disabled = parse_network_controls({"network_integrations": {"custom": {"domains": {}}}})
        self.assertEqual(disabled.to_json()["network_integrations"], {})

    def test_claude_web_search_toggle_stays_in_typed_config(self) -> None:
        off = parse_network_controls(
            {"network_integrations": {"claude": {"enabled": True}}}
        )
        self.assertFalse(off.integrations["claude"].web_search)
        on = parse_network_controls(
            {"network_integrations": {"claude": {"enabled": True, "web_search": True}}}
        )
        self.assertTrue(on.integrations["claude"].web_search)

    def test_claude_web_search_requires_enabled(self) -> None:
        with self.assertRaisesRegex(ConfigError, r"web_search requires enabled"):
            parse_network_controls(
                {"network_integrations": {"claude": {"enabled": False, "web_search": True}}})

    def test_xai_carries_enablement_and_nothing_else(self) -> None:
        # The xAI integration has no options: its exact hosted-tool allowlist is
        # fixed, so enablement is the whole of its configuration.
        parsed = parse_network_controls({"network_integrations": {"xai": {"enabled": True}}})
        self.assertEqual(parsed.to_json()["network_integrations"]["xai"], {"enabled": True})

    def test_xai_rejects_a_web_search_option(self) -> None:
        # Grok's web search is not offered, so a policy asking for it is a
        # mistake to surface rather than a setting to ignore.
        with self.assertRaisesRegex(ConfigError, r"web_search"):
            parse_network_controls(
                {"network_integrations": {"xai": {"enabled": True, "web_search": True}}})

    def test_xai_apexes_are_reserved_from_custom_domains(self) -> None:
        # A custom rule must never be broader than the managed guard, including
        # for the metered API host the xAI integration deliberately keeps closed.
        for domain in ("x.ai", "api.x.ai", "grok.com", "cli-chat-proxy.grok.com", "*.grok.com"):
            with self.assertRaisesRegex(ConfigError, r"owned by the xai integration"):
                parse_network_controls(
                    {
                        "network_integrations": {
                            "custom": {"domains": {domain: {"allow_http_methods": ["GET"]}}}
                        }
                    }
                )

    def test_bedrock_policy_contains_only_enablement(self) -> None:
        enabled = parse_network_controls(
            {"network_integrations": {"bedrock": {"enabled": True}}}
        )
        self.assertEqual(
            enabled.to_json()["network_integrations"]["bedrock"],
            {"enabled": True},
        )

    def test_bedrock_region_is_not_network_policy(self) -> None:
        with self.assertRaisesRegex(ConfigError, r"bedrock has unsupported fields: region"):
            parse_network_controls(
                {"network_integrations": {"bedrock": {"enabled": True, "region": "eu-central-1"}}}
            )

    def test_custom_domains_rejects_present_non_object(self) -> None:
        # A present-but-invalid domains value must 400, not silently reset to
        # an empty custom integration (which would erase existing rules on a
        # policy replace). Absent/null defaults to empty.
        for bad in ([], False, 0, "", "x"):
            with self.subTest(domains=bad), self.assertRaisesRegex(
                ConfigError, r"network_integrations\.custom\.domains must be an object"
            ):
                parse_network_controls({"network_integrations": {"custom": {"domains": bad}}})
        for empty in (None, {}):
            controls = parse_network_controls({"network_integrations": {"custom": {"domains": empty}}})
            self.assertFalse(controls.integrations["custom"].enabled)
        controls = parse_network_controls({"network_integrations": {"custom": {}}})
        self.assertFalse(controls.integrations["custom"].enabled)

    def test_custom_rejects_enabled_field(self) -> None:
        # custom has no enabled toggle; enablement is non-empty domains.
        with self.assertRaisesRegex(ConfigError, "unsupported fields: enabled"):
            parse_network_controls({"network_integrations": {"custom": {"enabled": True}}})

    def test_disabled_github_rejects_write_repositories(self) -> None:
        # A disabled integration carries no other state: write repositories (or
        # require_dot_github_approval) require the integration to be enabled. An
        # enabled integration with an empty list stays valid (a read-only agent).
        with self.assertRaisesRegex(
            ConfigError,
            r"network_integrations\.github\.write_repositories, require_dot_github_approval, and block_direct_main_pushes require enabled to be true",
        ):
            parse_network_controls(
                {
                    "network_integrations": {
                        "github": {"enabled": False, "write_repositories": [{"owner": "infiloop2", "repo": "kern"}]}
                    },
                }
            )
        read_only = parse_network_controls(
            {
                "network_integrations": {"github": {"enabled": True, "write_repositories": []}},
            }
        )
        self.assertEqual(read_only.to_json()["network_integrations"], {"github": {"enabled": True}})
        self.assertTrue(read_only.integrations["github"].block_direct_main_pushes)
        # A disabled integration serializes away.
        bare = parse_network_controls(
            {"network_integrations": {"github": {"enabled": False}}}
        )
        self.assertEqual(bare.to_json()["network_integrations"], {})

    def test_runtime_network_controls_reject_ssh_port_field(self) -> None:
        with self.assertRaisesRegex(ConfigError, "network_controls has unsupported fields: ssh_port_opened"):
            parse_network_controls(
                {"ssh_port_opened": True, "network_integrations": {}}
            )

    def test_parse_preserves_public_policy_without_generated_rules(self) -> None:
        controls = parse_network_controls(
            {
                "network_integrations": {"openai": {"enabled": True}},
            }
        )
        user_policy = controls.to_json()
        self.assertEqual(user_policy["network_integrations"], {"openai": {"enabled": True}})

        self.assertTrue(network_integrations.host_allowed(controls, "api.openai.com"))
        self.assertTrue(network_integrations.host_allowed(controls, "auth.openai.com"))
        self.assertTrue(network_integrations.host_allowed(controls, "chatgpt.com"))
        self.assertFalse(network_integrations.host_allowed(controls, "other.openai.com"))

    def test_openai_domains_are_reserved_for_managed_integration(self) -> None:
        for domain in ("api.openai.com", "auth.openai.com", "chatgpt.com", "*.chatgpt.com"):
            with self.subTest(domain=domain), self.assertRaisesRegex(ConfigError, "network_integrations.openai"):
                parse_network_controls(
                    {
                        "network_integrations": { "openai": {"enabled": True}, "custom": {"domains": {domain: {"allow_http_methods": ["GET"]}}} },
                    }
                )

        for field in ("openai_external_url_request_guard", "openai_account_guard"):
            with self.subTest(field=field), self.assertRaisesRegex(ConfigError, "unsupported fields"):
                parse_network_controls(
                    {
                        "network_integrations": { "openai": {"enabled": True}, "custom": {"domains": {"api.example.com": {"allow_http_methods": ["GET"], field: True}}} },
                    }
                )

    def test_claude_provider_owns_routes_and_rejects_custom_overlap(self) -> None:
        controls = parse_network_controls(
            {
                "network_integrations": {"claude": {"enabled": True}},
            }
        )
        self.assertEqual(controls.to_json()["network_integrations"], {"claude": {"enabled": True}})
        self.assertTrue(network_integrations.host_allowed(controls, "api.anthropic.com"))
        self.assertTrue(network_integrations.host_allowed(controls, "platform.claude.com"))
        self.assertFalse(network_integrations.host_allowed(controls, "claude.ai"))
        self.assertTrue(request_allowed(controls, "GET", "platform.claude.com", "/v1/oauth/token"))
        self.assertFalse(request_allowed(controls, "GET", "platform.claude.com", "/api/account"))

        for domain in ("api.anthropic.com", "claude.ai", "platform.claude.com", "*.anthropic.com"):
            with self.subTest(domain=domain), self.assertRaisesRegex(
                ConfigError, "network_integrations.claude"
            ):
                parse_network_controls(
                    {
                        "network_integrations": { "claude": {"enabled": True}, "custom": {"domains": {domain: {"allow_http_methods": ["GET"]}}} },
                    }
                )

    def test_network_integrations_own_routes_and_reserve_domains(self) -> None:
        controls = parse_network_controls(
            {
                "network_integrations": {
                    "openai": {"enabled": True},
                    "github": {
                        "enabled": True,
                        "write_repositories": [
                            {"owner": "InfiverseHQ", "repo": "Kern"},
                            {"owner": "infiversehq", "repo": "kern-tools"},
                        ],
                    },
                    "python_packages": {"enabled": True},
                    "npm_packages": {"enabled": True},
                },
            }
        )

        self.assertEqual(
            controls.to_json()["network_integrations"]["github"]["write_repositories"],
            [
                {"owner": "infiversehq", "repo": "kern"},
                {"owner": "infiversehq", "repo": "kern-tools"},
            ],
        )
        self.assertTrue(network_integrations.host_allowed(controls, "api.github.com"))
        self.assertTrue(network_integrations.host_allowed(controls, "uploads.github.com"))
        for signed_domain in (
            "objects.githubusercontent.com",
            "github-cloud.githubusercontent.com",
            "release-assets.githubusercontent.com",
            "results-receiver.actions.githubusercontent.com",
        ):
            self.assertTrue(request_allowed(controls, "GET", signed_domain, "/asset"))
            self.assertFalse(request_allowed(controls, "POST", signed_domain, "/asset"))
        actions_blob = "productionresultssa17.blob.core.windows.net"
        signed_query = (
            "sv=2025-07-05"
            "&sig=HhC%2FUPa%2FtitCP1DLVLa0ZnGPCw0RT338fxdeQ04ZoPw%3D"
        )
        self.assertTrue(
            request_allowed(
                controls,
                "GET",
                actions_blob,
                "/actions-results/file",
                signed_query,
            )
        )
        self.assertTrue(
            request_allowed(
                controls,
                "HEAD",
                actions_blob,
                "/actions-results/file",
                signed_query,
            )
        )
        self.assertFalse(
            request_allowed(
                controls,
                "POST",
                actions_blob,
                "/actions-results/file",
                signed_query,
            )
        )
        self.assertTrue(request_allowed(controls, "GET", "pypi.org", "/simple/pkg"))
        self.assertTrue(request_allowed(controls, "GET", "registry.npmjs.org", "/pkg"))

        for domain, owner in (
            ("github.com", "github"),
            ("uploads.github.com", "github"),
            ("raw.githubusercontent.com", "github"),
            ("pypi.org", "python_packages"),
            ("registry.npmjs.org", "npm_packages"),
        ):
            with self.subTest(domain=domain), self.assertRaisesRegex(ConfigError, f"network_integrations.{owner}"):
                parse_network_controls(
                    {
                        "network_integrations": { "custom": {"domains": {domain: {"allow_http_methods": ["GET"]}}} },
                    }
                )

        with self.assertRaisesRegex(ConfigError, "duplicate repository"):
            parse_network_controls(
                {
                    "network_integrations": {
                        "github": {
                            "enabled": True,
                            "write_repositories": [
                                {"owner": "infiversehq", "repo": "kern"},
                                {"owner": "InfiverseHQ", "repo": "Kern"},
                            ],
                        }
                    },
                }
            )

    def test_broad_wildcards_covering_managed_domains_are_rejected(self) -> None:
        # Two layers: TLD-wide wildcards like *.com never parse (the wildcard
        # shape needs a concrete multi-label suffix), and the reservation
        # check independently owns any wildcard that would cover a managed
        # apex, so neither layer's future loosening exposes managed domains.
        for domain in ("*.com", "*.ai", "*.org"):
            with self.subTest(domain=domain):
                with self.assertRaises(ConfigError):
                    parse_network_controls(
                        {
                            "network_integrations": { "custom": {"domains": {domain: {"allow_http_methods": ["GET"]}}} },
                        }
                    )
                self.assertIsNotNone(managed_domain_owner(domain))
        with self.assertRaisesRegex(ConfigError, "network_integrations"):
            parse_network_controls(
                {
                    "network_integrations": { "custom": {"domains": {"*.githubusercontent.com": {"allow_http_methods": ["GET"]}}} },
                }
            )
        for domain in (
            "blob.core.windows.net",
            "productionresultssa17.blob.core.windows.net",
            "*.blob.core.windows.net",
            "*.core.windows.net",
            "*.windows.net",
        ):
            with self.subTest(domain=domain), self.assertRaisesRegex(
                ConfigError, "network_integrations"
            ):
                parse_network_controls(
                    {
                        "network_integrations": {
                            "custom": {
                                "domains": {
                                    domain: {"allow_http_methods": ["GET"]}
                                }
                            }
                        },
                    }
                )
        # An unrelated wildcard still works.
        controls = parse_network_controls(
            {
                "network_integrations": { "custom": {"domains": {"*.example.com": {"allow_http_methods": ["GET"]}}} },
            }
        )
        custom = controls.integrations["custom"]
        self.assertIsInstance(custom, CustomIntegration)
        self.assertIn("*.example.com", custom.domains)
        self.assertIsNone(managed_domain_owner("*.example.com"))

    def test_github_repository_git_suffix_is_normalized_away(self) -> None:
        # The commonly pasted "repo.git" form must match requests, whose repo
        # segment is .git-stripped before lookup.
        controls = parse_network_controls(
            {
                "network_integrations": {
                    "github": {
                        "enabled": True,
                        "write_repositories": [{"owner": "infiloop2", "repo": "Kern.git"}],
                    }
                },
            }
        )
        self.assertEqual(
            controls.to_json()["network_integrations"]["github"]["write_repositories"],
            [{"owner": "infiloop2", "repo": "kern"}],
        )
        policy = (controls)
        # The write repo matches a push whose repo segment is .git-stripped.
        self.assertIsNone(
            github_request_denied(
                policy, "POST", "github.com", "/infiloop2/kern.git/git-receive-pack", "", b""
            )
        )

        with self.assertRaisesRegex(ConfigError, "duplicate repository"):
            parse_network_controls(
                {
                    "network_integrations": {
                        "github": {
                            "enabled": True,
                            "write_repositories": [
                                {"owner": "infiloop2", "repo": "kern"},
                                {"owner": "infiloop2", "repo": "kern.git"},
                            ],
                        }
                    },
                }
            )

    def test_legacy_managed_ai_provider_field_is_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigError, "unsupported fields: managed_ai_provider_network_access"):
            parse_network_controls(
                {"managed_ai_provider_network_access": {"openai": True}}
            )

    def test_github_credential_field_is_rejected_in_policy(self) -> None:
        with self.assertRaisesRegex(ConfigError, "unsupported fields: credential"):
            parse_network_controls(
                {
                    "network_integrations": {
                        "github": {
                            "enabled": True,
                            "write_repositories": [{"owner": "infiloop2", "repo": "demo"}],
                            "credential": {"mode": "token"},
                        }
                    },
                }
            )

    def test_overlapping_wildcard_domain_rules_are_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigError, "wildcard domains must not overlap"):
            parse_network_controls(
                {
                    "network_integrations": { "openai": {"enabled": True}, "custom": {"domains": {"*.example.com": {"allow_http_methods": ["GET"]},
                        "*.api.example.com": {"allow_http_methods": ["POST"]},}} },
                }
            )

    def test_non_overlapping_wildcard_domain_rules_are_allowed(self) -> None:
        controls = parse_network_controls(
            {
                "network_integrations": { "openai": {"enabled": True}, "custom": {"domains": {"*.api.example.com": {"allow_http_methods": ["GET"]},
                    "*.static.example.com": {"allow_http_methods": ["POST"]},}} },
            }
        )

        custom = controls.integrations["custom"]
        self.assertIsInstance(custom, CustomIntegration)
        self.assertEqual(custom.domains["*.api.example.com"].allow_http_methods, ("GET",))
        self.assertEqual(custom.domains["*.static.example.com"].allow_http_methods, ("POST",))

    def test_exact_domain_override_under_wildcard_is_allowed(self) -> None:
        controls = parse_network_controls(
            {
                "network_integrations": { "openai": {"enabled": True}, "custom": {"domains": {"*.example.com": {"allow_http_methods": ["GET"]},
                    "api.example.com": {"allow_http_methods": ["POST"]},}} },
            }
        )

        custom = controls.integrations["custom"]
        self.assertIsInstance(custom, CustomIntegration)
        self.assertEqual(custom.domains["api.example.com"].allow_http_methods, ("POST",))

    def test_domain_keys_are_normalized_and_case_duplicates_rejected(self) -> None:
        controls = parse_network_controls(
            {
                "network_integrations": { "openai": {"enabled": True}, "custom": {"domains": {"API.Example.COM": {"allow_http_methods": ["GET"]},}} },
            }
        )
        custom = controls.integrations["custom"]
        self.assertIsInstance(custom, CustomIntegration)
        self.assertIn("api.example.com", custom.domains)
        self.assertNotIn("API.Example.COM", custom.domains)

        with self.assertRaisesRegex(ConfigError, "duplicate domain rules"):
            parse_network_controls(
                {
                    "network_integrations": { "openai": {"enabled": True}, "custom": {"domains": {"api.example.com": {"allow_http_methods": ["GET"]},
                        "API.EXAMPLE.COM": {"allow_http_methods": ["POST"]},}} },
                }
            )


class PolicyTests(unittest.TestCase):
    def test_custom_websocket_requires_boolean_opt_in_and_round_trips(self) -> None:
        policy = _custom_policy(
            {
                "socket.example.com": {
                    "allow_http_methods": ["GET"],
                    "allow_websocket": True,
                },
                "http.example.com": {"allow_http_methods": ["GET"]},
            }
        )
        controls = _controls(policy)
        self.assertTrue(network_integrations.websocket_allowed(controls, "socket.example.com"))
        self.assertFalse(network_integrations.websocket_allowed(controls, "http.example.com"))
        self.assertEqual(controls.to_json(), policy)

        for bad in (None, 0, "true", []):
            with self.subTest(value=bad), self.assertRaisesRegex(
                ConfigError, "allow_websocket must be a boolean"
            ):
                _controls(
                    _custom_policy(
                        {
                            "socket.example.com": {
                                "allow_http_methods": ["GET"],
                                "allow_websocket": bad,
                            }
                        }
                    )
                )

    def test_policy_matches_domain_method_and_path(self) -> None:
        policy = _custom_policy({
            "*.example.com": {
                "allow_http_methods": ["GET"],
                "path_guards": ["^/dist(?:/.*)?$"],
            }
        })

        self.assertTrue(request_allowed(policy, "GET", "cdn.example.com", "/dist/app.js", ""))
        self.assertFalse(request_allowed(policy, "POST", "cdn.example.com", "/dist/app.js", ""))
        self.assertFalse(request_allowed(policy, "GET", "cdn.example.com", "/admin", ""))

    def test_path_guard_resists_traversal_and_encoding(self) -> None:
        policy = _custom_policy({
            "api.example.com": {"allow_http_methods": ["GET"], "path_guards": ["^/v1/threads(?:/.*)?$"]}
        })
        # A legitimate guarded path is allowed.
        self.assertTrue(request_allowed(policy, "GET", "api.example.com", "/v1/threads/abc", ""))
        # ../ traversal that the upstream would resolve to /admin is denied,
        # both raw and percent-encoded.
        self.assertFalse(
            request_allowed(policy, "GET", "api.example.com", "/v1/threads/../../admin", "")
        )
        self.assertFalse(
            request_allowed(policy, "GET", "api.example.com", "/v1/threads/%2e%2e/%2e%2e/admin", "")
        )

    def test_exact_domain_rule_wins_over_wildcard(self) -> None:
        policy = _custom_policy({
            "*.example.com": {"allow_http_methods": ["GET", "POST"]},
            "api.example.com": {"allow_http_methods": ["GET"]},
        })

        custom = _controls(policy).integrations["custom"]
        self.assertIsInstance(custom, CustomIntegration)
        self.assertIs(rule_for_host(custom, "api.example.com"), custom.domains["api.example.com"])
        self.assertFalse(request_allowed(policy, "POST", "api.example.com", "/", ""))
        self.assertTrue(request_allowed(policy, "POST", "other.example.com", "/", ""))

    def test_host_allowed_requires_listed_domain_with_methods(self) -> None:
        policy = _custom_policy({
            "allowed.example.com": {"allow_http_methods": ["GET"]},
            "closed.example.com": {"allow_http_methods": []},
        })

        self.assertTrue(network_integrations.host_allowed(_controls(policy), "allowed.example.com"))
        self.assertFalse(network_integrations.host_allowed(_controls(policy), "closed.example.com"))
        self.assertFalse(network_integrations.host_allowed(_controls(policy), "unlisted.example.com"))

    def test_github_guard_allows_all_reads_and_scopes_writes(self) -> None:
        policy = (
            parse_network_controls(
                {
                    "network_integrations": {
                        "github": {
                            "enabled": True,
                            "write_repositories": [
                                {"owner": "infiversehq", "repo": "kern-tools"},
                            ],
                        }
                    },
                }
            )
        )

        # Every read is allowed, whether or not the repo is a write target —
        # web pages, git fetch, raw blobs, archives, and any API GET/HEAD,
        # including unlisted repos and non-repo endpoints like search.
        for host, path in (
            ("github.com", "/infiversehq/kern-tools"),
            ("github.com", "/other/private-repo"),
            ("api.github.com", "/repos/other/private-repo"),
            ("api.github.com", "/repos/other/private-repo/contents/secret.env"),
            ("api.github.com", "/search/code"),
            ("api.github.com", "/user"),
            ("api.github.com", "/orgs/other-org/members"),
            ("raw.githubusercontent.com", "/other/private-repo/main/README.md"),
            ("codeload.github.com", "/other/private-repo/tar.gz/main"),
            ("objects.githubusercontent.com", "/github-production-release-asset"),
            ("github-cloud.githubusercontent.com", "/github-production-repository-file-5c1aeb"),
        ):
            with self.subTest(read=f"{host}{path}"):
                self.assertIsNone(github_request_denied(policy, "GET", host, path, "", b""))
        # Git fetch (upload-pack) on any repo is a read; compare views with
        # cross-repo fork-network refs are just reads too now.
        self.assertIsNone(
            github_request_denied(policy, "POST", "github.com", "/other/private-repo/git-upload-pack", "", b"")
        )
        for basehead in ("main...dev", "main...attacker:leak", "main...attacker/other-repo:leak"):
            with self.subTest(compare=basehead):
                self.assertIsNone(
                    github_request_denied(policy, "GET", "github.com", f"/other/repo/compare/{basehead}", "", b"")
                )

        # Push (git-receive-pack) is gated on a configured write repository.
        self.assertIsNone(
            github_request_denied(
                policy, "POST", "github.com", "/infiversehq/kern-tools.git/git-receive-pack", "", b""
            )
        )
        self.assertEqual(
            github_request_denied(policy, "POST", "github.com", "/other/repo.git/git-receive-pack", "", b""),
            "github_write_repo_required",
        )
        self.assertEqual(
            github_request_denied(
                policy, "GET", "github.com", "/other/repo.git/info/refs", "service=git-receive-pack", b""
            ),
            "github_write_repo_required",
        )

        # Workflow dispatch is an api.github.com REST route; the same path on
        # GitHub's release-upload host does not inherit the exception.
        self.assertEqual(
            github_request_denied(
                policy,
                "POST",
                "uploads.github.com",
                "/repos/infiversehq/kern-tools/actions/workflows/deploy.yml/dispatches",
                "",
                b'{"ref":"main"}',
            ),
            "github_repo_admin_write_denied",
        )

        # API writes: a repo-scoped mutation on a write repo passes; the same on
        # an unlisted repo needs a write repo.
        self.assertIsNone(
            github_request_denied(policy, "PATCH", "api.github.com", "/repos/infiversehq/kern-tools/issues/1", "", b"")
        )
        self.assertEqual(
            github_request_denied(policy, "PATCH", "api.github.com", "/repos/other/repo/issues/1", "", b""),
            "github_write_repo_required",
        )
        # Release-asset uploads are repo-scoped writes.
        self.assertIsNone(
            github_request_denied(
                policy, "POST", "uploads.github.com", "/repos/infiversehq/kern-tools/releases/1/assets", "", b""
            )
        )
        self.assertEqual(
            github_request_denied(
                policy, "POST", "uploads.github.com", "/repos/other/repo/releases/1/assets", "", b""
            ),
            "github_write_repo_required",
        )
        # A mutation that targets no repository (create a repo, create a gist)
        # is never a configured write repo.
        for non_repo_write in ("/user/repos", "/gists", "/orgs/other-org/repos"):
            with self.subTest(path=non_repo_write):
                self.assertEqual(
                    github_request_denied(policy, "POST", "api.github.com", non_repo_write, "", b""),
                    "github_write_repo_required",
                )

        # Repository administration is denied even on a write repo, under one
        # unified reason; reads of all of it stay plain repo reads. This covers
        # the repo root (settings/visibility, delete), boundary-escaping
        # mutations (fork/generate/transfer), and the admin sub-resources.
        for admin_method in ("PATCH", "DELETE", "PUT"):
            self.assertEqual(
                github_request_denied(
                    policy, admin_method, "api.github.com", "/repos/infiversehq/kern-tools", "", b""
                ),
                "github_repo_admin_write_denied",
            )
        for admin_path in (
            "forks",
            "generate",
            "transfer",
            "collaborators/attacker",
            "invitations/1",
            "keys",
            "hooks",
            "pages",
            "environments/prod",
            "codespaces",
            "dependabot/secrets/TOKEN",
            "rulesets",
            "rulesets/1",
            "properties/values",
            "interaction-limits",
            "releases",
            "immutable-releases",
            "autolinks",
            "topics",
            "vulnerability-alerts",
            "automated-security-fixes",
            "private-vulnerability-reporting",
            "security-advisories",
            "bypass-requests",
            "actions/secrets/TOKEN",
            "actions/variables/NAME",
            "actions/runners/registration-token",
            "actions/permissions",
            "actions/oidc/customization/sub",
            "actions/cache/usage",
            "actions/caches",
            "actions/workflows/12345/disable",
            "actions/workflows/ci.yml/enable",
            "actions/workflows/ci.yml/dispatches",
            "dispatches",
            "statuses/abc123",
            "check-runs",
            "check-suites/7/rerequest",
            "deployments",
            "attestations",
            "actions/runs/1/cancel",
            "actions/runs/1/force-cancel",
            "actions/runs/1/approve",
            "code-scanning/alerts/1",
            "secret-scanning/alerts/1",
            "actions/runs/1/pending_deployments",
            "actions/runs/1/deployment_protection_rule",
            "actions/artifacts/1",
            "branches/main/protection",
            "branches/main/protection/required_status_checks",
            "tags/protection",
            "lfs",
            "pulls/7/update-branch",
        ):
            with self.subTest(path=admin_path):
                self.assertEqual(
                    github_request_denied(
                        policy, "PUT", "api.github.com", f"/repos/infiversehq/kern-tools/{admin_path}", "", b""
                    ),
                    "github_repo_admin_write_denied",
                )
        # Reading those same admin sub-resources stays a plain repo read.
        for read_path in ("forks", "collaborators", "hooks"):
            with self.subTest(read=read_path):
                self.assertIsNone(
                    github_request_denied(
                        policy, "GET", "api.github.com", f"/repos/infiversehq/kern-tools/{read_path}", "", b""
                    )
                )
        # Deleting a run or its logs erases the automation record.
        for delete_path in ("actions/runs/1", "actions/runs/1/logs"):
            with self.subTest(path=delete_path):
                self.assertEqual(
                    github_request_denied(
                        policy, "DELETE", "api.github.com", f"/repos/infiversehq/kern-tools/{delete_path}", "", b""
                    ),
                    "github_repo_admin_write_denied",
                )
        # Normal repo-scoped writes (issues, contents, workflow dispatches and
        # re-runs, and non-protection branch operations) on the write repo
        # still pass.
        for write_path in (
            "issues",
            "contents/docs/README.md",
            "actions/workflows/deploy.yml/dispatches",
            "actions/runs/1/rerun",
            "branches/main/rename",
        ):
            with self.subTest(path=write_path):
                self.assertIsNone(
                    github_request_denied(
                        policy, "POST", "api.github.com", f"/repos/infiversehq/kern-tools/{write_path}", "", b""
                    )
                )

        # A workflow dispatch is still a scoped write, so the same exact route
        # cannot target a repository the operator did not list.
        self.assertEqual(
            github_request_denied(
                policy,
                "POST",
                "api.github.com",
                "/repos/other/repo/actions/workflows/deploy.yml/dispatches",
                "",
                b'{"ref":"main"}',
            ),
            "github_write_repo_required",
        )

    def test_require_dot_github_approval_rides_into_guard(self) -> None:
        controls = parse_network_controls(
            {
                "network_integrations": {
                    "github": {
                        "enabled": True,
                        "block_direct_main_pushes": False,
                        "require_dot_github_approval": True,
                        "write_repositories": [{"owner": "infiversehq", "repo": "kern-tools"}],
                    }
                },
            }
        )
        self.assertEqual(controls.to_json()["network_integrations"]["github"]["require_dot_github_approval"], True)
        policy = controls
        self.assertTrue(policy.integrations["github"].require_dot_github_approval)
        # The gate triggers only on receive-pack POSTs (it runs after the
        # write guard allowed the push, so the repo is a write repo by
        # construction); a read probe never reaches inspect().
        clean = type("R", (), {"touches_github": False})()
        with (
            _gate_capacity(),
            patch("host.network_integrations.github.guard.read_proxy_github_token", return_value="ghs_test"),
            patch("host.network_integrations.github.guard.push_gate.inspect", return_value=clean) as inspect_fn,
        ):
            response, reason = github_push_gate_response(
                policy, "POST", "github.com", "/infiversehq/kern-tools.git/git-receive-pack", b"body"
            )
            self.assertEqual((response, reason), (None, None))
            inspect_fn.assert_called_once()
            self.assertEqual(inspect_fn.call_args.args[:2], ("infiversehq", "kern-tools"))
        # The inspected owner/repo come from the same normalized path the
        # trigger (and the write guard before it) matched: dot segments and
        # percent-encoding cannot smuggle a different identity into the gate.
        with (
            _gate_capacity(),
            patch("host.network_integrations.github.guard.read_proxy_github_token", return_value="ghs_test"),
            patch("host.network_integrations.github.guard.push_gate.inspect", return_value=clean) as inspect_fn,
        ):
            response, reason = github_push_gate_response(
                policy, "POST", "github.com", "/x/../infiversehq/kern-tools.git/git-receive-pack", b"body"
            )
            self.assertEqual((response, reason), (None, None))
            inspect_fn.assert_called_once()
            self.assertEqual(inspect_fn.call_args.args[:2], ("infiversehq", "kern-tools"))
        with patch("host.network_integrations.github.guard.push_gate.inspect") as inspect_fn:
            self.assertEqual(
                github_push_gate_response(
                    policy, "GET", "github.com", "/infiversehq/kern-tools.git/info/refs", b""
                ),
                (None, None),
            )
            inspect_fn.assert_not_called()
        # Off by default: no require_dot_github_approval means no gating.
        off = (
            parse_network_controls(
                {
                    "network_integrations": {
                        "github": {
                            "enabled": True,
                            "block_direct_main_pushes": False,
                            "write_repositories": [{"owner": "infiversehq", "repo": "kern-tools"}],
                        }
                    },
                }
            )
        )
        self.assertFalse(off.integrations["github"].require_dot_github_approval)
        with patch("host.network_integrations.github.guard.push_gate.inspect") as inspect_fn:
            self.assertEqual(
                github_push_gate_response(
                    off, "POST", "github.com", "/infiversehq/kern-tools.git/git-receive-pack", b"body"
                ),
                (None, None),
            )
            inspect_fn.assert_not_called()

        # Approval mode also closes REST content-write bypasses that can create
        # .github-changing commits without entering git-receive-pack.
        for blocked in (
            "/repos/infiversehq/kern-tools/contents/.github/workflows/ci.yml",
            "/repos/infiversehq/kern-tools/git/refs/heads/main",
            "/repos/infiversehq/kern-tools/git/trees",
            "/repos/infiversehq/kern-tools/git/commits",
            "/repos/infiversehq/kern-tools/merges",
            "/repos/infiversehq/kern-tools/merge-upstream",
        ):
            with self.subTest(blocked=blocked):
                self.assertEqual(
                    github_request_denied(policy, "PUT", "api.github.com", blocked, "", b""),
                    "github_dot_github_rest_write_denied",
                )
        # A PR merge is still scoped to an operator-configured write repo.
        # The one REST route supports both regular and squash merge methods.
        for merge_method in ("merge", "squash"):
            body = json.dumps({"merge_method": merge_method}).encode()
            with self.subTest(merge_method=merge_method):
                self.assertIsNone(
                    github_request_denied(
                        policy,
                        "PUT",
                        "api.github.com",
                        "/repos/infiversehq/kern-tools/pulls/12/merge",
                        "",
                        body,
                    )
                )
                self.assertEqual(
                    github_request_denied(
                        policy,
                        "PUT",
                        "api.github.com",
                        "/repos/other/repo/pulls/12/merge",
                        "",
                        body,
                    ),
                    "github_write_repo_required",
                )
        self.assertIsNone(
            github_request_denied(
                policy, "PUT", "api.github.com",
                "/repos/infiversehq/kern-tools/contents/docs/README.md", "", b"",
            )
        )

    def test_direct_main_pushes_are_blocked_by_default_without_queueing(self) -> None:
        policy = parse_network_controls(
            {
                "network_integrations": {
                    "github": {
                        "enabled": True,
                        "write_repositories": [{"owner": "infiversehq", "repo": "kern-tools"}],
                    }
                }
            }
        )
        github = policy.integrations["github"]
        self.assertTrue(github.block_direct_main_pushes)
        # The safe default stays sparse in serialized policy. Only the
        # operator's explicit opt-out needs durable representation.
        self.assertNotIn("block_direct_main_pushes", policy.to_json()["network_integrations"]["github"])
        self.assertEqual(
            github_push_gate_response(
                policy,
                "POST",
                "github.com",
                "/infiversehq/kern-tools.git/git-receive-pack",
                github_receive_pack("refs/heads/feature/x"),
            ),
            (None, None),
        )
        body = github_receive_pack("refs/heads/feature/x", "refs/heads/main")
        with (
            patch("host.network_integrations.github.guard.push_gate.inspect") as inspect_fn,
            patch("host.network_integrations.github.guard.enqueue_pending_push") as enqueue_fn,
        ):
            response, reason = github_push_gate_response(
                policy, "POST", "github.com", "/infiversehq/kern-tools.git/git-receive-pack", body
            )
        self.assertEqual(reason, "github_main_push_denied")
        assert response is not None
        self.assertTrue(response.startswith(b"HTTP/1.1 200 OK"))
        self.assertIn(b"ng refs/heads/main direct pushes to main are blocked", response)
        # A receive-pack transaction is atomic at the proxy boundary: no ref
        # in a multi-ref request is forwarded when main is present.
        self.assertIn(b"ng refs/heads/feature/x direct pushes to main are blocked", response)
        inspect_fn.assert_not_called()
        enqueue_fn.assert_not_called()

    def test_main_push_protection_can_be_disabled(self) -> None:
        policy = parse_network_controls(
            {
                "network_integrations": {
                    "github": {
                        "enabled": True,
                        "block_direct_main_pushes": False,
                        "write_repositories": [{"owner": "infiversehq", "repo": "kern-tools"}],
                    }
                }
            }
        )
        self.assertFalse(policy.integrations["github"].block_direct_main_pushes)
        self.assertFalse(
            policy.to_json()["network_integrations"]["github"]["block_direct_main_pushes"]
        )
        self.assertEqual(
            github_push_gate_response(
                policy,
                "POST",
                "github.com",
                "/infiversehq/kern-tools.git/git-receive-pack",
                github_receive_pack("refs/heads/main"),
            ),
            (None, None),
        )

    def test_main_push_guard_fails_closed_on_malformed_receive_pack(self) -> None:
        policy = parse_network_controls(
            {
                "network_integrations": {
                    "github": {
                        "enabled": True,
                        "write_repositories": [{"owner": "infiversehq", "repo": "kern-tools"}],
                    }
                }
            }
        )
        self.assertEqual(
            github_push_gate_response(
                policy, "POST", "github.com", "/infiversehq/kern-tools.git/git-receive-pack", b"not-pkt-line"
            ),
            (None, "github_main_push_guard_unavailable"),
        )

    def test_github_push_gate_cleans_pending_refs_when_enqueue_fails(self) -> None:
        class FakeResult:
            touches_github = True
            ref_updates = [{"old": "0" * 40, "new": "1" * 40, "ref": "refs/heads/main"}]
            paths = {".github/workflows/ci.yml"}

            def __init__(self) -> None:
                self.cleaned: list[str] = []

            def hold_for_approval(self, push_id: str) -> bytes:
                return b"HTTP/1.1 200 OK\r\n\r\n"

            def cleanup_pending(self, push_id: str) -> None:
                self.cleaned.append(push_id)

        result = FakeResult()
        policy = parse_network_controls({
            "network_integrations": {
                "github": {
                    "enabled": True,
                    "block_direct_main_pushes": False,
                    "require_dot_github_approval": True,
                    "write_repositories": [{"owner": "infiversehq", "repo": "kern-tools"}],
                }
            },
        })
        with (
            _gate_capacity(),
            patch("host.network_integrations.github.guard.read_proxy_github_token", return_value=None),
            patch("host.network_integrations.github.guard.push_gate.inspect", return_value=result),
            patch("host.network_integrations.github.guard.push_gate.new_push_id", return_value="abc123"),
            patch("host.network_integrations.github.guard.enqueue_pending_push", side_effect=RuntimeError("db down")),
        ):
            response, reason = github_push_gate_response(
                policy, "POST", "github.com", "/infiversehq/kern-tools.git/git-receive-pack", b"body"
            )

        self.assertIsNone(response)
        self.assertEqual(reason, "github_push_gate_unavailable")
        self.assertEqual(result.cleaned, ["abc123"])

    def test_github_push_gate_cleans_pending_refs_when_hold_fails(self) -> None:
        class FakeResult:
            touches_github = True
            ref_updates = [{"old": "0" * 40, "new": "1" * 40, "ref": "refs/heads/main"}]
            paths = {".github/workflows/ci.yml"}

            def __init__(self) -> None:
                self.cleaned: list[str] = []

            def hold_for_approval(self, push_id: str) -> bytes:
                raise RuntimeError("stale lock")

            def cleanup_pending(self, push_id: str) -> None:
                self.cleaned.append(push_id)

        result = FakeResult()
        policy = parse_network_controls({
            "network_integrations": {
                "github": {
                    "enabled": True,
                    "block_direct_main_pushes": False,
                    "require_dot_github_approval": True,
                    "write_repositories": [{"owner": "infiversehq", "repo": "kern-tools"}],
                }
            },
        })
        with (
            _gate_capacity(),
            patch("host.network_integrations.github.guard.read_proxy_github_token", return_value=None),
            patch("host.network_integrations.github.guard.push_gate.inspect", return_value=result),
            patch("host.network_integrations.github.guard.push_gate.new_push_id", return_value="abc123"),
        ):
            response, reason = github_push_gate_response(
                policy, "POST", "github.com", "/infiversehq/kern-tools.git/git-receive-pack", b"body"
            )

        self.assertIsNone(response)
        self.assertEqual(reason, "github_push_gate_unavailable")
        self.assertEqual(result.cleaned, ["abc123"])

    def test_github_push_queue_cap_is_checked_before_indexing(self) -> None:
        policy = parse_network_controls({
            "network_integrations": {
                "github": {
                    "enabled": True,
                    "block_direct_main_pushes": False,
                    "require_dot_github_approval": True,
                    "write_repositories": [{"owner": "infiversehq", "repo": "kern-tools"}],
                }
            },
        })
        path = "/infiversehq/kern-tools.git/git-receive-pack"
        with (
            _gate_capacity(pending=10),
            patch("host.network_integrations.github.guard.push_gate.inspect") as inspect_fn,
        ):
            self.assertEqual(
                github_push_gate_response(policy, "POST", "github.com", path, b"body"),
                (None, "github_push_queue_full"),
            )
            inspect_fn.assert_not_called()

    def test_github_lfs_batch_allows_download_denies_upload(self) -> None:
        policy = (
            parse_network_controls(
                {
                    "network_integrations": {
                        "github": {
                            "enabled": True,
                            "write_repositories": [
                                {"owner": "infiloop2", "repo": "infibot"},
                            ],
                        }
                    },
                }
            )
        )
        download = json.dumps({"operation": "download", "objects": []}).encode()
        upload = json.dumps({"operation": "upload", "objects": []}).encode()
        batch = "/other/repo.git/info/lfs/objects/batch"
        write_batch = "/infiloop2/infibot.git/info/lfs/objects/batch"

        # Download (clone/fetch) is a read and passes for any repo.
        self.assertIsNone(github_request_denied(policy, "POST", "github.com", batch, "", download))
        # Uploads are denied even for write repos: the follow-up object PUTs go
        # to signed URLs whose opaque paths cannot be repo-checked, so the batch
        # fails closed with a crisp reason.
        for path in (batch, write_batch):
            with self.subTest(path=path):
                self.assertEqual(
                    github_request_denied(policy, "POST", "github.com", path, "", upload),
                    "github_lfs_push_unsupported",
                )
        for garbage in (b"", b"not json", b'{"operation": "mystery"}'):
            with self.subTest(body=garbage):
                self.assertEqual(
                    github_request_denied(policy, "POST", "github.com", batch, "", garbage),
                    "github_lfs_operation_unresolved",
                )

    def test_github_graphql_requests_fail_closed(self) -> None:
        policy = (
            parse_network_controls(
                {
                    "network_integrations": {
                        "github": {
                            "enabled": True,
                            "write_repositories": [{"owner": "infiversehq", "repo": "kern"}],
                        }
                    },
                }
            )
        )

        # GraphQL repository scope cannot be verified without a real parser
        # (argument order, aliased variables, and fragments all evade regex
        # extraction), so every GraphQL request is denied — including ones that
        # only reference allowed repositories.
        scoped_query = json.dumps(
            {
                "query": "query($owner:String!, $name:String!) { repository(owner:$owner, name:$name) { id } }",
                "variables": {"owner": "infiversehq", "name": "kern"},
            }
        ).encode()
        mutation = b'{"query":"mutation { createIssue(input:{}) { clientMutationId } }"}'

        for body in (scoped_query, mutation, b"", b"not json"):
            with self.subTest(body=body):
                self.assertEqual(
                    github_request_denied(policy, "POST", "api.github.com", "/graphql", "", body),
                    "github_graphql_denied",
                )

    def test_openai_guard_pins_account_and_blocks_external_url_requests(self) -> None:
        pg_harness.reset_database()
        policy = parse_network_controls({
            "network_integrations": {"openai": {"enabled": True}},
        })
        host = "chatgpt.com"
        # Subsequent web-search checks carry the valid account header and a
        # matching bearer so they isolate the web-search logic from the
        # account pin and the credential binding.
        auth = ("Authorization", openai_bearer("acct_good"))
        json_header = [("Content-Type", "application/json"), ("ChatGPT-Account-Id", "acct_good"), auth]

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"KERN_STATE_DIR": tmp}):
            # Without a stored account id, OpenAI data-plane requests fail
            # closed even if the presented header would otherwise match.
            self.assertIsNotNone(openai_request_denied(policy, host, [("ChatGPT-Account-Id", "acct_good"), auth], b"{}"))
            save_proxy_openai_account_id("acct_good")
            # Account pinning on the ChatGPT-Account-Id header.
            self.assertIsNone(openai_request_denied(policy, host, [("ChatGPT-Account-Id", "acct_good"), auth], b"{}"))
            self.assertIsNotNone(openai_request_denied(policy, host, [("ChatGPT-Account-Id", "acct_evil"), auth], b"{}"))
            # A missing account header is denied, not allowed — otherwise the pin is
            # bypassable by omission.
            self.assertIsNotNone(openai_request_denied(policy, host, [("Content-Type", "application/json")], b"{}"))
            self.assertIsNone(openai_request_denied(policy, "auth.openai.com", [], b"{}"))

            # API WebSockets are a narrow GET exception for Responses only;
            # ordinary API GETs and upgrades on other paths remain denied.
            websocket_headers = [
                ("ChatGPT-Account-Id", "acct_good"),
                ("Upgrade", "websocket"),
                auth,
            ]
            api_config = policy.integrations["openai"]
            self.assertIsNone(
                openai_guard.request_denied(
                    api_config,
                    "GET",
                    "api.openai.com",
                    "/v1/responses",
                    "",
                    websocket_headers,
                    b"",
                )
            )
            for path, headers in (
                ("/v1/responses", [("ChatGPT-Account-Id", "acct_good"), auth]),
                ("/v1/realtime", websocket_headers),
            ):
                with self.subTest(path=path, headers=headers):
                    self.assertEqual(
                        openai_guard.request_denied(
                            api_config,
                            "GET",
                            "api.openai.com",
                            path,
                            "",
                            headers,
                            b"",
                        ),
                        "network_policy_denied",
                    )

            # Live web search (external access on, or unset) is denied; cached is allowed.
            live = b'{"tools": [{"type": "web_search", "external_web_access": true}]}'
            unset = b'{"tools": [{"type": "web_search"}]}'
            cached = b'{"tools": [{"type": "web_search", "external_web_access": false}]}'
            self.assertIsNotNone(openai_request_denied(policy, host, json_header, live))
            self.assertIsNotNone(openai_request_denied(policy, host, json_header, unset))
            self.assertIsNone(openai_request_denied(policy, host, json_header, cached))

            # The legacy preview tool and dated variants are always denied: they
            # ignore external_web_access and always browse live.
            self.assertIsNotNone(openai_request_denied(policy, host, json_header, b'{"tools": [{"type": "web_search_preview"}]}'))
            self.assertIsNotNone(
                openai_request_denied(policy, host, json_header, b'{"tools": [{"type": "web_search_preview_2025_03_11"}]}')
            )
            self.assertIsNotNone(
                openai_request_denied(
                    policy, host, json_header, b'{"tools": [{"type": "web_search_2025_08_26", "external_web_access": false}]}'
                )
            )
            # Prompt text mentioning a search tool carries no capability: the
            # upstream only enables search from a parsed tool object.
            self.assertIsNone(openai_request_denied(policy, host, json_header, b'{"note": "web_search"}'))
            self.assertIsNone(
                openai_request_denied(policy, host, json_header, b'{"instructions": "never use web_search_preview"}')
            )
            # A web_search_call history item replays an earlier cached search and
            # is not a tool declaration.
            self.assertIsNone(
                openai_request_denied(
                    policy,
                    host,
                    json_header,
                    b'{"input": [{"type": "web_search_call", "action": {"type": "search", "query": "x"}}],'
                    b' "tools": [{"type": "web_search", "external_web_access": false}]}',
                )
            )

            # Remote MCP tools make the upstream call an external server with
            # request data: denied by tool type (server_url or hosted
            # connector_id form) and by a server_url key anywhere.
            self.assertIsNotNone(
                openai_request_denied(
                    policy, host, json_header,
                    b'{"tools": [{"type": "mcp", "server_label": "evil", "server_url": "https://evil.example/mcp"}]}',
                )
            )
            self.assertIsNotNone(
                openai_request_denied(
                    policy, host, json_header, b'{"tools": [{"type": "mcp", "connector_id": "connector_gmail"}]}'
                )
            )
            self.assertIsNotNone(
                openai_request_denied(
                    policy, host, json_header, b'{"input": "hi", "extra": {"server_url": "https://evil.example"}}'
                )
            )
            # Text mentioning MCP carries no capability.
            self.assertIsNone(
                openai_request_denied(
                    policy, host, json_header, b'{"input": "declare type mcp with a server_url to call out"}'
                )
            )

            # Chat Completions search has no cached form: both the options field
            # and search models are denied outright.
            self.assertIsNotNone(
                openai_request_denied(policy, host, json_header, b'{"model": "gpt-5.5", "web_search_options": {}}')
            )
            self.assertIsNotNone(
                openai_request_denied(policy, host, json_header, b'{"model": "gpt-4o-search-preview", "messages": []}')
            )

            # Standalone search endpoints must opt into cached retrieval; the
            # server default is live, so a missing setting fails closed.
            search_path = "/backend-api/codex/alpha/search"
            self.assertIsNone(
                openai_request_denied(
                    policy,
                    host,
                    json_header,
                    b'{"commands": {"search_query": [{"q": "x"}]}, "settings": {"external_web_access": false}}',
                    path=search_path,
                )
            )
            for search_body in (
                b'{"commands": {"search_query": [{"q": "x"}]}}',
                b'{"commands": {"search_query": [{"q": "x"}]}, "settings": {"external_web_access": true}}',
                b'{"commands": {"search_query": [{"q": "x"}]}, "settings": {"external_web_access": "indexed"}}',
            ):
                self.assertIsNotNone(openai_request_denied(policy, host, json_header, search_body, path=search_path))
            # The endpoint match survives percent-encoding and dot-segment
            # disguises, which the upstream would serve as the search route.
            for disguised_path in (
                "/backend-api/codex/alpha/%73earch",
                "/backend-api/codex/extra/../alpha/search",
                "/backend-api/codex/alpha/search/",
            ):
                self.assertIsNotNone(
                    openai_request_denied(
                        policy, host, json_header,
                        b'{"commands": {"search_query": [{"q": "x"}]}}',
                        path=disguised_path,
                    )
                )

            # A body the upstream cannot parse as JSON cannot declare tools, so a
            # mislabeled body with a junk prefix is not a search vector; a
            # JSON-looking body that fails to parse still fails closed.
            text_header = [("Content-Type", "text/plain"), ("ChatGPT-Account-Id", "acct_good"), auth]
            self.assertIsNone(openai_request_denied(policy, host, text_header, b'x' + live))
            self.assertIsNotNone(openai_request_denied(policy, host, json_header, b'{"tools": [{"type": "web_search"'))
            self.assertIsNotNone(
                openai_request_denied(policy, host, text_header, b'{"tools": [{"type": "web_search_preview"}]}')
            )

            # A request with no web search is fine.
            self.assertIsNone(openai_request_denied(policy, host, json_header, b'{"input": "hello"}'))

            # gzip-encoded live request is decoded and still denied (no evasion).
            import gzip
            gz_headers = [
                ("Content-Type", "application/json"),
                ("Content-Encoding", "gzip"),
                ("ChatGPT-Account-Id", "acct_good"),
                auth,
            ]
            self.assertIsNotNone(openai_request_denied(policy, host, gz_headers, gzip.compress(live)))

    def test_openai_guard_binds_bearer_credential_to_pinned_account(self) -> None:
        # NET-006: the chatgpt-account-id header names an account without
        # authenticating as it. The Authorization bearer must be a ChatGPT
        # OAuth JWT whose payload claims the pinned account.
        pg_harness.reset_database()
        policy = parse_network_controls({
            "network_integrations": {"openai": {"enabled": True}},
        })
        account_header = ("ChatGPT-Account-Id", "acct_good")
        good = ("Authorization", openai_bearer("acct_good"))
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"KERN_STATE_DIR": tmp}):
            save_proxy_openai_account_id("acct_good")
            # A genuine-shaped token claiming the pinned account is allowed.
            self.assertIsNone(openai_request_denied(policy, "chatgpt.com", [account_header, good], b"{}"))
            # A valid JWT for a different account echoes the pinned header but
            # authenticates as someone else: the credential decides, not the
            # header.
            for host in ("chatgpt.com", "api.openai.com"):
                with self.subTest(host=host):
                    self.assertEqual(
                        openai_request_denied(
                            policy, host,
                            [account_header, ("Authorization", openai_bearer("acct_evil"))],
                            b"{}",
                        ),
                        "openai_token_account_mismatch",
                    )
            # Non-JWT bearers (sk- platform keys), missing Authorization, a
            # non-Bearer scheme, and duplicated Authorization all fail closed.
            for headers in (
                [account_header, ("Authorization", "Bearer sk-proj-1234567890")],
                [account_header],
                [account_header, ("Authorization", "Basic dXNlcjpwYXNz")],
                [account_header, good, good],
            ):
                with self.subTest(headers=headers):
                    self.assertEqual(
                        openai_request_denied(policy, "chatgpt.com", headers, b"{}"),
                        "openai_token_account_mismatch",
                    )
            # The Responses WebSocket handshake runs through the same request
            # guard, so the binding covers it too.
            upgrade = ("Upgrade", "websocket")
            api_config = policy.integrations["openai"]
            self.assertEqual(
                openai_guard.request_denied(
                    api_config, "GET", "api.openai.com", "/v1/responses", "",
                    [account_header, upgrade, ("Authorization", openai_bearer("acct_evil"))],
                    b"",
                ),
                "openai_token_account_mismatch",
            )
            self.assertIsNone(
                openai_guard.request_denied(
                    api_config, "GET", "api.openai.com", "/v1/responses", "",
                    [account_header, upgrade, good],
                    b"",
                )
            )
            # auth.openai.com (token refresh) is unguarded and needs no bearer.
            self.assertIsNone(openai_request_denied(policy, "auth.openai.com", [], b"{}"))

    def test_external_url_guard_caps_gzip_and_deflate_decoded_size(self) -> None:
        pg_harness.reset_database()
        import gzip
        import zlib

        from host.runtime.core import network_policy

        policy = parse_network_controls({
            "network_integrations": {"openai": {"enabled": True}},
        })
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"KERN_STATE_DIR": tmp}),
            patch.object(network_policy, "MAX_DECODED_BODY_BYTES", 32),
        ):
            save_proxy_openai_account_id("acct_good")
            for encoding, compressed in (
                ("gzip", gzip.compress(b'{"input":"' + b"x" * 40 + b'"}')),
                ("deflate", zlib.compress(b'{"input":"' + b"x" * 40 + b'"}')),
            ):
                headers = [
                    ("Content-Type", "application/json"),
                    ("Content-Encoding", encoding),
                    ("ChatGPT-Account-Id", "acct_good"),
                    ("Authorization", openai_bearer("acct_good")),
                ]
                with self.subTest(encoding=encoding):
                    self.assertIsNotNone(openai_request_denied(policy, "chatgpt.com", headers, compressed))

    def test_external_url_guard_denies_stdlib_undecodable_encodings(self) -> None:
        pg_harness.reset_database()
        # Only stdlib-decodable encodings are inspected; zstd and brotli fail
        # closed (clients essentially never compress request bodies, and a
        # live denial is the signal to add an encoding, not a fallback path).
        policy = parse_network_controls({
            "network_integrations": {"openai": {"enabled": True}},
        })
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"KERN_STATE_DIR": tmp}):
            save_proxy_openai_account_id("acct_good")
            for encoding in ("zstd", "br"):
                with self.subTest(encoding=encoding):
                    headers = [
                        ("Content-Type", "application/json"),
                        ("Content-Encoding", encoding),
                        ("ChatGPT-Account-Id", "acct_good"),
                        ("Authorization", openai_bearer("acct_good")),
                    ]
                    self.assertIsNotNone(
                        openai_request_denied(policy, "chatgpt.com", headers, b'{"input": "hello"}')
                    )

    def test_unsupported_content_encoding_fails_closed(self) -> None:
        pg_harness.reset_database()
        policy = parse_network_controls({
            "network_integrations": {"openai": {"enabled": True}},
        })
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"KERN_STATE_DIR": tmp}):
            save_proxy_openai_account_id("acct_good")
            headers = [
                ("Content-Type", "application/json"),
                ("Content-Encoding", "lzma"),
                ("ChatGPT-Account-Id", "acct_good"),
                ("Authorization", openai_bearer("acct_good")),
            ]
            self.assertIsNotNone(openai_request_denied(policy, "chatgpt.com", headers, b'{"input": "hello"}'))

    def test_anthropic_guard_requires_approved_account_identity(self) -> None:
        pg_harness.reset_database()
        policy = parse_network_controls({
            "network_integrations": {"claude": {"enabled": True}},
        })
        headers = [("Authorization", "Bearer token-good")]
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"KERN_STATE_DIR": tmp}):
            self.assertEqual(
                anthropic_request_denied(policy, "POST", "api.anthropic.com", "/v1/messages", headers),
                "anthropic_account_unavailable",
            )
            for path in (
                "/api/oauth/profile",
                "/api/oauth/claude_cli/roles",
                "/api/organization/claude_code_first_token_date",
                "/api/claude_code/policy_limits",
                "/api/claude_code/settings",
            ):
                with self.subTest(pre_pin_bootstrap_path=path):
                    self.assertIsNone(anthropic_request_denied(policy, "GET", "api.anthropic.com", path, headers))
            self.assertIsNotNone(anthropic_request_denied(policy, "GET", "api.anthropic.com", "/api/oauth/profile", []))
            self.assertIsNotNone(
                anthropic_request_denied(
                    policy, "POST", "api.anthropic.com", "/api/event_logging/v2/batch", headers
                )
            )
            save_proxy_claude_account_id("acct")
            self.assertEqual(
                anthropic_request_denied(
                    policy, "POST", "api.anthropic.com", "/v1/messages", headers
                ),
                "anthropic_token_mismatch",
            )
            self.assertIsNotNone(
                anthropic_request_denied(
                    policy, "POST", "api.anthropic.com", "/v1/messages", [("Authorization", "Bearer wrong")]
                )
            )
            self.assertIsNotNone(anthropic_request_denied(policy, "POST", "api.anthropic.com", "/v1/messages", []))
            self.assertIsNone(anthropic_request_denied(policy, "GET", "api.anthropic.com", "/api/hello", []))
            self.assertIsNotNone(
                anthropic_request_denied(policy, "GET", "api.anthropic.com", "/api/oauth/profile", [])
            )
            self.assertIsNotNone(
                anthropic_request_denied(
                    policy, "GET", "api.anthropic.com", "/api/oauth/profile", [("Authorization", "Bearer wrong")]
                )
            )

    def test_anthropic_guard_always_attests_uuid_once_per_token_hash(self) -> None:
        pg_harness.reset_database()
        controls = parse_network_controls({
            "network_integrations": {"claude": {"enabled": True}},
        })
        save_proxy_claude_account_id("acct-approved")
        claude_guard.clear_token_attestation_cache()
        self.addCleanup(claude_guard.clear_token_attestation_cache)
        calls: list[str] = []

        def attest(token: str) -> str:
            calls.append(token)
            return "acct-approved"

        headers = [("Authorization", "Bearer older-token")]
        self.assertIsNone(
            anthropic_request_denied(
                controls, "POST", "api.anthropic.com", "/v1/messages", headers,
                attest_account=attest,
            )
        )
        self.assertIsNone(
            anthropic_request_denied(
                controls, "POST", "api.anthropic.com", "/v1/messages", headers,
                attest_account=lambda _token: self.fail("cached token was attested twice"),
            )
        )
        self.assertEqual(calls, ["older-token"])

    def test_anthropic_guard_rejects_token_attested_to_another_uuid(self) -> None:
        pg_harness.reset_database()
        controls = parse_network_controls({
            "network_integrations": {"claude": {"enabled": True}},
        })
        save_proxy_claude_account_id("acct-approved")
        claude_guard.clear_token_attestation_cache()
        self.addCleanup(claude_guard.clear_token_attestation_cache)
        headers = [("Authorization", "Bearer other-account-token")]

        self.assertEqual(
            anthropic_request_denied(
                controls, "POST", "api.anthropic.com", "/v1/messages", headers,
                attest_account=lambda _token: "acct-other",
            ),
            "anthropic_token_mismatch",
        )
        self.assertEqual(
            anthropic_request_denied(
                controls, "POST", "api.anthropic.com", "/v1/messages",
                headers + [("Authorization", "Bearer duplicate")],
                attest_account=lambda _token: "acct-approved",
            ),
            "anthropic_token_mismatch",
        )

    def test_anthropic_parallel_cache_misses_attest_once(self) -> None:
        pg_harness.reset_database()
        controls = parse_network_controls({
            "network_integrations": {"claude": {"enabled": True}},
        })
        save_proxy_claude_account_id("acct-approved")
        claude_guard.clear_token_attestation_cache()
        self.addCleanup(claude_guard.clear_token_attestation_cache)
        headers = [("Authorization", "Bearer one-new-parallel-token")]
        entered = threading.Event()
        release = threading.Event()
        calls: list[str] = []
        results: list[str | None] = []

        def attest(token: str) -> str:
            calls.append(token)
            entered.set()
            if not release.wait(timeout=5):
                raise AssertionError("test did not release attestation")
            return "acct-approved"

        def authorize() -> None:
            results.append(
                anthropic_request_denied(
                    controls, "POST", "api.anthropic.com", "/v1/messages", headers,
                    attest_account=attest,
                )
            )

        threads = [threading.Thread(target=authorize) for _ in range(4)]
        threads[0].start()
        self.assertTrue(entered.wait(timeout=5))
        for thread in threads[1:]:
            thread.start()
        release.set()
        for thread in threads:
            thread.join(timeout=5)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(results, [None] * 4)
        self.assertEqual(calls, ["one-new-parallel-token"])

    def test_anthropic_guard_rechecks_account_pin_after_attestation(self) -> None:
        pg_harness.reset_database()
        controls = parse_network_controls({
            "network_integrations": {"claude": {"enabled": True}},
        })
        save_proxy_claude_account_id("acct-approved")
        claude_guard.clear_token_attestation_cache()
        self.addCleanup(claude_guard.clear_token_attestation_cache)
        headers = [("Authorization", "Bearer rotating-token")]

        def attest_after_reset(_token: str) -> str:
            save_proxy_claude_account_id(None)
            return "acct-approved"

        self.assertEqual(
            anthropic_request_denied(
                controls,
                "POST",
                "api.anthropic.com",
                "/v1/messages",
                headers,
                attest_account=attest_after_reset,
            ),
            "anthropic_token_mismatch",
        )

    def test_parse_https_request_head(self) -> None:
        method, target, headers = read_request_head(
            io.BytesIO(b"GET /v1/health?check=1 HTTP/1.1\r\nHost: api.example.com\r\nUpgrade: websocket\r\n\r\n")
        )

        self.assertEqual(method, "GET")
        self.assertEqual(target, "/v1/health?check=1")
        self.assertEqual(headers, [("Host", "api.example.com"), ("Upgrade", "websocket")])


class DenialReasonCatalogTests(unittest.TestCase):
    def test_every_emitted_denial_code_has_catalog_guidance(self) -> None:
        # Anti-drift: a denial code added to a guard (or the proxy core)
        # without a catalog entry would surface to the agent with no guidance.
        # Codes are the only snake_case string literals in these modules apart
        # from request/config vocabulary, which is excluded by name.
        import inspect
        import re as re_module

        from host.network_integrations import registry
        from host.network_integrations.custom import guard as custom_guard
        from host.network_integrations.npm_packages import guard as npm_guard
        from host.network_integrations.python_packages import guard as python_guard
        from host.runtime.network_proxy import service as network_proxy

        catalog = registry.denial_reason_catalog()
        emitted: set[str] = set()
        for module in (
            openai_guard, claude_guard, github_guard, custom_guard,
            npm_guard, python_guard, network_proxy,
        ):
            source = inspect.getsource(module)
            emitted |= set(re_module.findall(r'"([a-z][a-z0-9]*(?:_[a-z0-9]+)+)"', source))
        # Non-code snake_case vocabulary these modules legitimately mention.
        emitted -= {
            "write_repositories", "require_dot_github_approval", "external_web_access",
            "indexed_web_access", "server_url", "web_search_options", "web_search",
            "web_search_call", "web_fetch", "code_execution", "mcp_servers",
            "computer_use", "computer_use_preview", "code_interpreter",
            "allow_http_methods", "path_guards", "network_integrations",
            "account_id", "access_token_sha256", "chatgpt_account_id",
            "pending_deployments", "deployment_protection_rule",
        }
        self.assertGreater(len(emitted), 15)
        self.assertEqual(emitted - set(catalog), set())
        for code, reason in catalog.items():
            with self.subTest(code=code):
                self.assertTrue(reason.guidance.strip())

    def test_registry_rejects_duplicate_integration_ids(self) -> None:
        # A duplicate id must fail loudly at registry construction: a silent
        # dict overwrite would pair the surviving manifest's apex claims with
        # the wrong guard and drop the original claim entirely.
        from host.network_integrations import registry
        from host.network_integrations.openai import manifest as openai_manifest

        with self.assertRaisesRegex(ValueError, "duplicate integration id 'openai'"):
            registry._build_registry((openai_manifest, openai_manifest))

    def test_registry_packages_validation_and_guard_pairing(self) -> None:
        from host.network_integrations import runtime as integrations_runtime
        from host.network_integrations import registry

        package_root = Path(registry.__file__).parent
        package_ids = {
            path.parent.name
            for path in package_root.glob("*/manifest.py")
            if (path.parent / "__init__.py").exists()
        }
        self.assertEqual(package_ids, set(registry.NETWORK_INTEGRATIONS))
        self.assertEqual(set(integrations_runtime.GUARDS), set(registry.NETWORK_INTEGRATIONS))

        apex_owners: dict[str, str] = {}
        codes = [reason.code for reason in PROXY_DENIAL_REASONS]
        for integration_id, registered in registry.NETWORK_INTEGRATIONS.items():
            for apex in registered.manifest.owned_apexes:
                self.assertFalse(
                    any(apex.endswith(f".{seen}") or seen.endswith(f".{apex}") for seen in apex_owners),
                    f"{integration_id} apex {apex} overlaps {apex_owners}",
                )
                apex_owners[apex] = integration_id
            codes.extend(reason.code for reason in registered.manifest.denial_reasons)
        self.assertEqual(len(codes), len(set(codes)))
        self.assertIsNone(managed_domain_owner("example.com"))


class DisabledIntegrationDispatchTests(unittest.TestCase):
    def test_disabled_integrations_are_denied_at_dispatch(self) -> None:
        # The enabled gate lives in the dispatch layer, not in the guards
        # (their contract is that hooks run only for enabled configs). A
        # disabled integration must fail closed for every hook, pre-DNS
        # included, for each host it owns.
        controls = parse_network_controls({"network_integrations": {}})
        for host in (
            "api.openai.com", "chatgpt.com", "auth.openai.com",
            "api.anthropic.com", "platform.claude.com",
            "github.com", "api.github.com", "raw.githubusercontent.com",
            "results-receiver.actions.githubusercontent.com",
            "productionresultssa17.blob.core.windows.net",
            "pypi.org", "registry.npmjs.org",
            "custom.example.com",
        ):
            with self.subTest(host=host):
                self.assertFalse(network_integrations.host_allowed(controls, host))
                self.assertEqual(
                    network_integrations.request_denied(controls, "GET", host, "/", "", [], b""),
                    "network_policy_denied",
                )
                self.assertEqual(
                    network_integrations.gate_response(controls, "POST", host, "/x/y.git/git-receive-pack", b""),
                    (None, None),
                )
                self.assertFalse(network_integrations.websocket_allowed(controls, host))
                # The frame path always receives a callable content decision;
                # the disabled integration's decision is the default no-op,
                # even though its upgrade gate can never reach that path.
                self.assertIsNone(
                    network_integrations.ws_message_guard(controls, host)(b"message")
                )

    def test_enabled_integrations_allow_their_hosts_at_dispatch(self) -> None:
        controls = parse_network_controls(
            {
                "network_integrations": {
                    "openai": {"enabled": True},
                    "claude": {"enabled": True},
                    "github": {"enabled": True},
                    "python_packages": {"enabled": True},
                    "npm_packages": {"enabled": True},
                "custom": {"domains": {"custom.example.com": {"allow_http_methods": ["GET"]}}},},
            }
        )
        for host in (
            "api.openai.com", "api.anthropic.com", "github.com",
            "results-receiver.actions.githubusercontent.com",
            "productionresultssa17.blob.core.windows.net",
            "pypi.org", "registry.npmjs.org", "custom.example.com",
        ):
            with self.subTest(host=host):
                self.assertTrue(network_integrations.host_allowed(controls, host))


class DeployNetworkTests(unittest.TestCase):
    def test_subnet_requires_active_internet_gateway_default_route(self) -> None:
        responses = [
            {
                "RouteTables": [
                    {
                        "Routes": [
                            {
                                "DestinationCidrBlock": "0.0.0.0/0",
                                "GatewayId": "igw-123",
                                "State": "active",
                            }
                        ]
                    }
                ]
            }
        ]

        with patch("host.cli.aws_resources._aws", side_effect=responses):
            self.assertTrue(_subnet_has_public_ipv4_route({}, "vpc-1", "subnet-1"))

    def test_subnet_rejects_nat_default_route(self) -> None:
        responses = [
            {
                "RouteTables": [
                    {
                        "Routes": [
                            {
                                "DestinationCidrBlock": "0.0.0.0/0",
                                "NatGatewayId": "nat-123",
                                "State": "active",
                            }
                        ]
                    }
                ]
            }
        ]

        with patch("host.cli.aws_resources._aws", side_effect=responses):
            self.assertFalse(_subnet_has_public_ipv4_route({}, "vpc-1", "subnet-1"))


if __name__ == "__main__":
    unittest.main()
