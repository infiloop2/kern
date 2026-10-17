"""Training checks never turn missing/failed provider data into an opt-out."""
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import pg_harness
from host.runtime.agent_runtime import claude_code, orchestrator, training_privacy
from host.runtime.admin_api import runtime_accounts
from host.runtime.core import pgclient, state


def stamp(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


class TrainingPrivacyTests(unittest.TestCase):
    def test_daily_after_success_ten_minutes_after_unknown_or_failed_recheck(self):
        self.assertTrue(training_privacy.check_due({}, 1000))
        for allowed in (True, False):
            previous = {"allowed": allowed, "checked_at": stamp(1000), "attempted_at": stamp(1000)}
            self.assertFalse(training_privacy.check_due(previous, 87399))
            self.assertTrue(training_privacy.check_due(previous, 87400))
            previous["attempted_at"] = stamp(2000)
            self.assertFalse(training_privacy.check_due(previous, 2599))
            self.assertTrue(training_privacy.check_due(previous, 2600))
        self.assertFalse(training_privacy.check_due({"allowed": None, "attempted_at": stamp(1000)}, 1599))
        self.assertTrue(training_privacy.check_due({"allowed": None, "attempted_at": stamp(1000)}, 1600))

    def test_grok_polarity_and_strict_missing_values(self):
        for runtime in ("grok", "grok-2"):
            for value, expected in ((True, False), (False, True), (None, None), (1, None), ("false", None)):
                self.assertIs(training_privacy.probe(runtime, {"coding_data_retention_opt_out": value}), expected)
            self.assertIsNone(training_privacy.probe(runtime, {}))

    def test_claude_helper_result_is_bound_to_token_and_strict_boolean(self):
        for value, token, expected in ((True, "hash", True), (False, "hash", False),
                                       (None, "hash", None), ("false", "hash", None),
                                       (False, "other", None)):
            proc = subprocess.CompletedProcess([], 0, json.dumps({"training_allowed": value, "access_token_sha256": token}), "")
            with patch.object(claude_code.subprocess, "run", return_value=proc) as run:
                self.assertIs(claude_code.read_training_allowed("hash"), expected)
                self.assertEqual(run.call_args.args[0][-3:], ["--training-settings", "--expected-token-sha256", "hash"])
        for proc in (subprocess.CompletedProcess([], 1, "", "failed"), subprocess.CompletedProcess([], 0, "bad json", "")):
            with patch.object(claude_code.subprocess, "run", return_value=proc):
                self.assertIsNone(claude_code.read_training_allowed("hash"))
        with patch.object(claude_code.subprocess, "run", side_effect=subprocess.TimeoutExpired("helper", 10)):
            self.assertIsNone(claude_code.read_training_allowed("hash"))

    def test_helper_reads_account_preference_not_feature_flag(self):
        helper = (Path(__file__).parents[1] / "host/bootstrap/helpers/read-claude-account.sh").read_text()
        source = helper.split("<<'PY'\n", 1)[1].split("\nPY", 1)[0]
        token = "synthetic-test-token"
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / ".claude").mkdir()
            (home / ".claude/.credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": token}}))
            source = source.replace('AGENT_HOME = "/mnt/kern-agent/agent-home"', f'AGENT_HOME = {directory!r}')
            for settings, expected in (({"grove_enabled": False}, False), ({"grove_enabled": True}, True), ({}, None), ({"grove_enabled": "false"}, None)):
                output = io.StringIO()
                response = MagicMock()
                response.__enter__.return_value = io.StringIO(json.dumps(settings))
                with (patch.dict(os.environ, {"ACCOUNT_READ_MODE": "training-settings", "EXPECTED_TOKEN_SHA256": token_hash}),
                      patch("urllib.request.urlopen", return_value=response) as request,
                      redirect_stdout(output), self.assertRaises(SystemExit) as stopped):
                    exec(compile(source, "claude-account-helper", "exec"), {})
                self.assertEqual(stopped.exception.code, 0)
                self.assertIs(json.loads(output.getvalue())["training_allowed"], expected)
                self.assertEqual(request.call_args.args[0].full_url, "https://api.anthropic.com/api/oauth/account/settings")
                self.assertNotIn(token, output.getvalue())
            with (patch.dict(os.environ, {"ACCOUNT_READ_MODE": "training-settings", "EXPECTED_TOKEN_SHA256": "other"}),
                  patch("urllib.request.urlopen") as request, redirect_stderr(io.StringIO()),
                  self.assertRaises(SystemExit)):
                exec(compile(source, "claude-account-helper", "exec"), {})
            request.assert_not_called()

    def test_refresh_due_force_and_disable_during_probe(self):
        account = {"account_id": "one", "access_token_sha256": "hash"}
        previous = {"allowed": False, "attempted_at": stamp(1000), "checked_at": stamp(1000)}
        with (patch.object(state, "read_training_status", return_value=previous),
              patch.object(training_privacy.time, "time", return_value=1001),
              patch.object(training_privacy, "probe", return_value=True) as probe,
              patch.object(state, "mutation") as mutation,
              patch.object(state, "save_training_status") as save,
              patch.object(orchestrator, "runtime_network_enabled", return_value=True) as enabled):
            training_privacy.refresh("grok", account)
            probe.assert_not_called()
            training_privacy.refresh("grok", account, force=True)
            save.assert_called_once_with(mutation.return_value.__enter__.return_value, "grok", account, True, 1001)
            save.reset_mock()
            enabled.return_value = False
            training_privacy.refresh("grok", account, force=True)
            save.assert_not_called()
            probe.reset_mock()
            training_privacy.refresh("codex", None, force=True)
            training_privacy.refresh("grok", None, force=True)
            probe.assert_not_called()

    def test_storage_uses_the_host_database_clients_timestamp_contract(self):
        cur = MagicMock()
        state.save_training_status(cur, "claude_code", {"account_id": "one", "access_token_sha256": "hash"}, False, 1000)
        for parameter in cur.execute.call_args.args[1]:
            pgclient._encode_parameter(parameter)
        # Read numeric epochs through the actual float8 decoder. PostgreSQL
        # timestamp text uses offsets/fractions that Python 3.10 rejects.
        for seconds in (None, 0, 1000, 1792230840.12345):
            with self.subTest(seconds=seconds):
                wire = str(seconds).encode() if seconds is not None else None
                decoded = pgclient._decode_value(701, wire)
                cur.fetchone.return_value = (False, decoded, decoded)
                expected = stamp(seconds) if seconds is not None else None
                self.assertEqual(state.read_training_status("claude_code", cur),
                                 {"allowed": False, "checked_at": expected, "attempted_at": expected})
                query = cur.execute.call_args.args[0]
                self.assertIn("EXTRACT(EPOCH FROM training_checked_at)::double precision", query)
                self.assertIn("EXTRACT(EPOCH FROM training_attempted_at)::double precision", query)

    def test_api_omits_training_for_disabled_untrusted_and_codex_accounts(self):
        approved = {"account_id": "one", "operator_approval": orchestrator.XAI_OPERATOR_APPROVAL}
        with (patch.object(runtime_accounts, "read_xai_account", return_value=approved) as read,
              patch.object(runtime_accounts, "read_openai_account", return_value={}),
              patch.object(state, "runtime_account_enabled", return_value=True) as enabled,
              patch.object(state, "read_training_status", return_value={"allowed": True}) as privacy):
            result = runtime_accounts._current_agent_account({"grok-2": {"status": "error"}}, "grok-2")
            self.assertEqual(result["data_training"], {"allowed": True})
            enabled.return_value = False
            self.assertNotIn("data_training", runtime_accounts._current_agent_account({}, "grok-2"))
            enabled.return_value = True
            read.return_value = {"account_id": "untrusted"}
            self.assertNotIn("data_training", runtime_accounts._current_agent_account({}, "grok-2"))
            self.assertNotIn("data_training", runtime_accounts._current_agent_account({}, "codex"))
            privacy.assert_called_once()


class TrainingPrivacyPersistenceTests(unittest.TestCase):
    def setUp(self):
        pg_harness.reset_database()
        self.account = {"account_id": "one", "access_token_sha256": "hash"}
        state.save_xai_account(self.account)

    def save(self, allowed, attempted=1000, account=None):
        with state.mutation() as cur:
            state.save_training_status(cur, "grok", account or self.account, allowed, attempted)

    def test_success_survives_metadata_refresh_and_failed_recheck(self):
        self.save(True)
        state.save_xai_account(self.account | {"email": "new@example.com"})
        self.save(None, 2000)
        self.assertEqual(state.read_training_status("grok"), {"allowed": True, "checked_at": stamp(1000), "attempted_at": stamp(2000)})
        self.save(False, 3000)
        self.assertEqual(state.read_training_status("grok"), {"allowed": False, "checked_at": stamp(3000), "attempted_at": stamp(3000)})

    def test_reset_or_replacement_clears_status_and_rejects_old_probe(self):
        for replacement in (None, {"account_id": "two", "access_token_sha256": "new"}):
            state.save_xai_account(self.account)
            self.save(True)
            state.save_xai_account(replacement)
            self.save(True, 2000)
            self.assertEqual(state.read_training_status("grok"), {"allowed": None, "checked_at": None, "attempted_at": None})

    def test_rotated_token_rejects_inflight_result(self):
        self.save(True)
        state.save_xai_account(self.account | {"access_token_sha256": "new"})
        self.save(False, 2000)
        self.assertTrue(state.read_training_status("grok")["allowed"])
        self.assertEqual(state.read_training_status("grok")["attempted_at"], stamp(1000))

    def test_runtime_slots_are_isolated(self):
        state.save_xai_account(self.account, runtime_type="grok-2")
        self.save(True)
        self.assertIsNone(state.read_training_status("grok-2")["allowed"])

    def test_runtime_activation_and_manual_refresh_persist_provider_preference(self):
        state.save_xai_account(self.account | {"operator_approval": orchestrator.XAI_OPERATOR_APPROVAL})
        provider = MagicMock(collect_login_before_probe=False)
        provider.account_status.return_value = ("active", None, self.account | {"coding_data_retention_opt_out": False})
        with (patch.object(orchestrator, "runtime_network_enabled", return_value=True),
              patch.object(orchestrator, "harness_adapter", return_value=provider),
              patch.object(orchestrator, "_capture_completed_token_login"),
              patch.object(orchestrator, "_sync_runtime_proxy_pin_in"),
              patch.dict(orchestrator._RUNTIME_STATUSES, {}, clear=True),
              patch.object(training_privacy.time, "time", return_value=1000) as now):
            self.assertEqual(orchestrator.refresh_runtime_status("grok"), "active")
            self.assertTrue(state.read_training_status("grok")["allowed"])
            now.return_value = 1300
            provider.account_status.return_value = ("active", None, self.account | {"coding_data_retention_opt_out": True})
            orchestrator.refresh_runtime_status("grok")
            self.assertTrue(state.read_training_status("grok")["allowed"])
            orchestrator.refresh_runtime_status("grok", force_provider_probe=True)
            self.assertFalse(state.read_training_status("grok")["allowed"])
            response = runtime_accounts.current_agent_accounts()
            grok = next(row for row in response["accounts"] if row.get("agent_runtime") == "grok")
            self.assertEqual(grok["data_training"], {"allowed": False, "checked_at": stamp(1300), "attempted_at": stamp(1300)})
