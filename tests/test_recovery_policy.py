"""Usage evidence, model scope and cross-account recovery decisions."""
from datetime import datetime, timezone
import unittest
from unittest.mock import patch

from host.runtime.agent_runtime import recovery_policy as policy


class RecoveryPolicyTests(unittest.TestCase):
    def snapshot(self, runtime, usage, model="gpt-6.2-astra", since=1000):
        key = "codex_usage" if runtime.startswith("codex") else "grok_usage" if runtime.startswith("grok") else "claude_usage"
        return policy.usage_snapshot(runtime, model, {key: {
            "last_checked_at": datetime.fromtimestamp(1000, timezone.utc).isoformat(), **usage,
        }}, since)

    def test_loose_case_insensitive_routing_with_limit_precedence(self):
        for runtime in ("codex", "codex-2", "codex-3", "claude_code", "grok", "grok-2"):
            self.assertEqual(policy.classify(runtime, "New wording: LIMIT REACHED, timed out"), "limit")
        for text in ("TIMED out", "connection timeout", "model at CAPACITY"):
            self.assertEqual(policy.classify("hermes", text), "transient")
        for runtime, text in (("hermes", "limit reached"), ("script", "timeout"),
                              ("codex", "authentication failed"), ("grok", "invalid model")):
            self.assertIsNone(policy.classify(runtime, text))

    def test_codex_requires_all_windows_and_accepts_99(self):
        limits = {name: {"used_percent": 99} for name in ("primary", "secondary")}
        self.assertFalse(self.snapshot("codex", {"rate_limits": limits}).exhausted)
        limits["secondary"] = {"used_percent": 100, "resets_at": 2000}
        self.assertEqual(self.snapshot("codex", {"rate_limits": limits}).reset(1000), 2060)
        del limits["primary"]
        self.assertIsNone(self.snapshot("codex", {"rate_limits": limits}))

    def test_fable_window_only_applies_to_fable_model(self):
        usage = {"current_session_used_percent": 99, "weekly_used_percent": 20,
                 "fable_weekly_used_percent": 100, "fable_weekly_resets_at": 5000}
        self.assertFalse(self.snapshot("claude_code", usage, "claude-opus-5-5").exhausted)
        self.assertEqual(self.snapshot("claude_code", usage, "claude-fable-5-2").reset(1000), 5060)
        del usage["fable_weekly_used_percent"]
        self.assertIsNone(self.snapshot("claude_code", usage, "claude-fable-5-2"))
        self.assertIsNotNone(self.snapshot("claude_code", usage, "claude-opus-5-5"))

    def test_wait_until_every_exhausted_window_resets(self):
        usage = {"current_session_used_percent": 100, "current_session_resets_at": 1200,
                 "weekly_used_percent": 100, "weekly_resets_at": 5000}
        self.assertEqual(self.snapshot("claude_code", usage).reset(1000), 5060)
        usage["current_session_resets_at"] = None
        self.assertIsNone(self.snapshot("claude_code", usage).reset(1000))

    def test_missing_stale_and_invalid_usage_is_unknown(self):
        for percent in (None, "99", True, float("nan"), float("inf"), -1):
            self.assertIsNone(self.snapshot("grok", {"usage_percent": percent}))
        self.assertIsNone(self.snapshot("grok", {"usage_percent": 99}, since=1002))
        self.assertFalse(self.snapshot("grok", {"usage_percent": 99}).exhausted)
        self.assertIsNone(self.snapshot("grok", {"usage_percent": 100, "resets_at": 900}).reset(1000))

    def test_switches_codex_and_grok_only_to_enabled_compatible_capacity(self):
        for runtime, model, target in (("codex", "gpt-6.2-astra", "codex-2"),
                                       ("grok", "grok-4.7", "grok-2")):
            with self.subTest(runtime=runtime), \
                    patch.object(policy, "fresh_usage", side_effect=[policy.Usage(((100, 5000),)), policy.Usage(((99, None),))]), \
                    patch.object(policy.state, "runtime_account_enabled", return_value=True), \
                    patch.object(policy.time, "time", return_value=1000):
                self.assertEqual(policy.limit_recovery(runtime, model, "high"), (target, 1000))

    def test_exhausted_alternative_does_not_change_current_subscription(self):
        with patch.object(policy, "fresh_usage", side_effect=[policy.Usage(((100, 5000),)), policy.Usage(((100, 2000),))]), \
                patch.object(policy.state, "runtime_account_enabled", return_value=True), \
                patch.object(policy.time, "time", return_value=1000):
            self.assertEqual(policy.limit_recovery("grok", "grok-4.7", "high"), ("grok", 5060))

    def test_uncertain_current_or_below_100_never_switches(self):
        for usage in (None, policy.Usage(((99, 5000),))):
            with patch.object(policy, "fresh_usage", return_value=usage) as probe:
                self.assertIsNone(policy.limit_recovery("codex", "gpt-6.2-astra", "high"))
                probe.assert_called_once()

    def test_disabled_accounts_are_never_probed(self):
        with patch.object(policy, "fresh_usage", return_value=policy.Usage(((100, 5000),))) as probe, \
                patch.object(policy.state, "runtime_account_enabled", return_value=False), \
                patch.object(policy.time, "time", return_value=1000):
            self.assertEqual(policy.limit_recovery("codex", "gpt-6.2-astra", "high"), ("codex", 5060))
            probe.assert_called_once()
