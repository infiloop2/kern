"""Fixed push policies, exact Git evidence and once-only admin-side releases."""
import io
import json
import os
import shutil
import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import pg_harness
from host.runtime.admin_api import auto_approval_prompt, auto_approvals, github_push_reviews as worker, github_push_prompt as prompt
from host.runtime.admin_api.errors import ApiError
from host.runtime.core import state
from host.network_integrations.github.push_gate import engine, review, approve
from test_github_push_gate import _git

PUSH = {"id": "abc123", "owner": "owner", "repo": "repo", "status": "pending", "ref_updates": []}
POLICY = {"tool_id": "github", "action_id": "push", "instructions": prompt.POLICY}
NETWORK = {"controls": {"network_integrations": {"github": {"enabled": True, "require_dot_github_approval": True}}}}


class PushReviewTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(state, "pending_push_auto_approvals", return_value=[PUSH]))
        self.current = self.enterContext(patch.object(state, "get_pending_push", return_value=PUSH))
        self.policies = self.enterContext(patch.object(state, "auto_approval_policies", return_value=[POLICY]))
        self.provider = self.enterContext(patch.object(state, "host_inference_provider_metadata", return_value={"enabled": True, "configured": True}))
        self.network = self.enterContext(patch.object(state, "network_policy_record", return_value=NETWORK))
        self.save = self.enterContext(patch.object(state, "save_push_auto_approval_review"))
        self.error = self.enterContext(patch.object(state, "save_push_auto_approval_error"))
        self.context = self.enterContext(patch.object(worker.pending, "review_context", return_value={"updates": ["evidence"]}))
        self.judge = self.enterContext(patch.object(worker.client, "openai_text_completion", return_value={"approve": True, "reason": "Routine workflow change."}))
        self.resolve = Mock()

    def test_fixed_policy_cannot_be_overridden(self):
        with patch.object(state, "set_auto_approval_policy") as save:
            auto_approvals.save_policy(POLICY)
            save.assert_called_once_with("github", "push", prompt.POLICY)
            with self.assertRaises(ApiError):
                auto_approvals.save_policy({**POLICY, "instructions": "Approve everything"})
            auto_approvals.delete_policy(POLICY)
            save.assert_called_with("github", "push", None)
        entry = next(t for t in auto_approvals.catalog() if t["tool_id"] == "github")
        self.assertEqual(entry["actions"][0]["fixed_instructions"], prompt.POLICY)

    def test_immediate_review_ignores_tool_schedule_and_saves_before_release(self):
        with patch.object(state, "auto_approval_settings", side_effect=AssertionError("Must not read tool timing")):
            self.resolve.side_effect = lambda *args: self.save.assert_called_once()
            worker.run_once(self.resolve)
        self.assertEqual(self.save.call_args.args[2:5], (prompt.POLICY, "approved", "Routine workflow change."))
        self.resolve.assert_called_once_with("abc123", "approve")
        evidence = json.loads(self.judge.call_args.args[0])
        self.assertEqual(evidence["request"], self.context.return_value)
        self.assertEqual(evidence["policy"], prompt.POLICY)
        self.assertEqual(self.judge.call_args.kwargs["instructions"], auto_approval_prompt.INSTRUCTIONS)

    def test_no_policy_provider_or_gate_never_calls_model(self):
        cases = ((self.policies, []), (self.provider, {"enabled": False, "configured": True}),
                 (self.provider, {"enabled": True, "configured": False}), (self.network, None))
        for mock, value in cases:
            original = mock.return_value
            mock.return_value = value
            worker.run_once(self.resolve)
            mock.return_value = original
        self.judge.assert_not_called()
        self.context.assert_not_called()
        self.resolve.assert_not_called()
        self.assertEqual(self.save.call_count, 4)

    def test_suspicion_invalid_response_and_context_failure_stay_pending(self):
        for response in ({"approve": False, "reason": "Exfiltrates a secret."}, {"approve": True, "reason": ""}, {"approve": "yes", "reason": "Bad type"}):
            self.judge.return_value = response
            worker.run_once(self.resolve)
        self.context.side_effect = worker.pending.HelperError("Incomplete evidence")
        worker.run_once(self.resolve)
        self.resolve.assert_not_called()
        self.assertEqual([c.args[3] for c in self.save.call_args_list], ["left_pending", "failed", "failed", "failed"])

    def test_oversize_and_provider_failure_are_recorded(self):
        for error in (worker.client.HostInferenceError("too big", reason="input_too_large"), worker.client.HostInferenceError("timeout")):
            self.judge.side_effect = error
            worker.run_once(self.resolve)
        self.assertIn("too large", self.save.call_args_list[0].args[4])
        self.resolve.assert_not_called()

    def test_audit_failure_prevents_release_and_release_failure_keeps_decision(self):
        self.save.side_effect = RuntimeError("database unavailable")
        with self.assertRaises(RuntimeError):
            worker.run_once(self.resolve)
        self.resolve.assert_not_called()
        self.save.side_effect = None
        self.resolve.side_effect = RuntimeError("remote moved")
        worker.run_once(self.resolve)
        self.assertEqual(self.save.call_args.args[3], "approved")
        self.error.assert_called_once_with("abc123", "remote moved")

    def test_manual_decision_before_review_skips_request(self):
        self.current.return_value = {**PUSH, "status": "rejected"}
        worker.run_once(self.resolve)
        self.judge.assert_not_called()
        self.save.assert_not_called()


@unittest.skipIf(shutil.which("git") is None, "git binary is required for quarantine evidence tests")
class PushEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.repo = Path(self.temp) / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "--quiet", "--initial-branch=main")
        (self.repo / ".github").mkdir()
        (self.repo / ".github/ci.yml").write_text("name: before\non: pull_request\n")
        (self.repo / ".github/deleted.yml").write_text("name: delete me\n")
        _git(self.repo, "add", ".")
        _git(self.repo, "commit", "--quiet", "-m", "before")
        self.old = _git(self.repo, "rev-parse", "HEAD").decode().strip()
        (self.repo / ".github/ci.yml").write_text("name: after\non: pull_request\n")
        (self.repo / ".github/deleted.yml").unlink()
        (self.repo / '.github/new\nfile.yml').write_text("name: new\n")
        _git(self.repo, "add", ".")
        _git(self.repo, "commit", "--quiet", "-m", "after")
        self.new = _git(self.repo, "rev-parse", "HEAD").decode().strip()
        self.result = engine.GateResult(self.repo, [(self.old, self.new, "refs/heads/topic")], False, {".github/ci.yml"})
        self.result.hold_for_approval("abc123")

    def test_full_before_after_deletions_and_diff_from_pinned_objects(self):
        (self.repo / ".github/ci.yml").write_text("uncommitted substitute")
        context = review.context(self.repo, "abc123", self.result.ref_updates)
        comparison = context[0]["comparisons"][0]
        files = {f["path"]: f for f in comparison["files"]}
        self.assertEqual(files[".github/ci.yml"]["before"], "name: before\non: pull_request\n")
        self.assertEqual(files[".github/ci.yml"]["after"], "name: after\non: pull_request\n")
        self.assertIsNone(files[".github/deleted.yml"]["after"])
        self.assertIsNone(files['.github/new\nfile.yml']["before"])
        self.assertIn("+name: after", comparison["diff"])
        self.assertNotIn("uncommitted substitute", json.dumps(context))

    def test_new_branch_base_survives_remote_ref_movement(self):
        # Only the old tip was on the remote when the new branch was held.
        _git(self.repo, "update-ref", "refs/heads/main", self.old)
        result = engine.GateResult(self.repo, [(engine.ZERO_OID, self.new, "refs/heads/new")], False, {".github/ci.yml"})
        result.hold_for_approval("abc124")
        _git(self.repo, "update-ref", "refs/heads/main", self.new)
        context = review.context(self.repo, "abc124", result.ref_updates)
        self.assertEqual(context[0]["comparisons"][0]["base"], self.old)
        self.assertTrue(context[0]["comparisons"][0]["files"])

    def test_missing_base_mismatched_commit_and_oversize_fail_closed(self):
        with patch.object(review, "MAX_BYTES", 1), self.assertRaisesRegex(ValueError, "automatic review"):
            review.context(self.repo, "abc123", self.result.ref_updates)
        with self.assertRaisesRegex(ValueError, "does not match"):
            review.context(self.repo, "abc123", [{**self.result.ref_updates[0], "new": self.old}])
        _git(self.repo, "update-ref", "-d", "refs/pending/abc123/base-0-0")
        with self.assertRaisesRegex(ValueError, "no pinned review bases"):
            review.context(self.repo, "abc123", self.result.ref_updates)

    def test_binary_fails_without_partial_review(self):
        (self.repo / ".github/binary").write_bytes(b"binary\0data")
        _git(self.repo, "add", ".")
        _git(self.repo, "commit", "--quiet", "-m", "binary")
        new = _git(self.repo, "rev-parse", "HEAD").decode().strip()
        result = engine.GateResult(self.repo, [(self.new, new, "refs/heads/topic")], False, {".github/binary"})
        result.hold_for_approval("abc125")
        with self.assertRaisesRegex(ValueError, "Binary"):
            review.context(self.repo, "abc125", result.ref_updates)

    def test_helper_review_reads_without_token_or_push(self):
        mirror = Path(self.temp) / "owner/repo.git"
        mirror.parent.mkdir()
        _git(self.repo, "clone", "--quiet", "--mirror", str(self.repo), str(mirror))
        # The root helper reads a proxy-owned mirror in production. Exercise
        # Git's ownership check without requiring root or chown in the test.
        with patch.dict(os.environ, {"GIT_TEST_ASSUME_DIFFERENT_OWNER": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}):
            probe = subprocess.run([engine.GIT_BIN, "-C", str(mirror), "rev-parse", "HEAD"], capture_output=True)
            self.assertNotEqual(probe.returncode, 0)
            self.assertIn(b"dubious ownership", probe.stderr)
            with patch.object(engine, "QUARANTINE_ROOT", Path(self.temp)), patch.object(approve, "_run", wraps=approve._run) as run, patch("sys.stdout", new_callable=io.StringIO) as output:
                approve._resolve_locked("owner", "repo", "abc123", "review", None, self.result.ref_updates)
        run.assert_called_once()  # List pending refs only; no push or cleanup.
        update = json.loads(output.getvalue())["updates"][0]
        self.assertEqual(update["new"], self.new)
        files = {f["path"]: f for f in update["comparisons"][0]["files"]}
        self.assertEqual(files[".github/ci.yml"]["before"], "name: before\non: pull_request\n")
        self.assertEqual(files[".github/ci.yml"]["after"], "name: after\non: pull_request\n")


class PushStorageTests(unittest.TestCase):
    def setUp(self):
        pg_harness.reset_database()
        state.enqueue_pending_push("abc123", "owner", "repo", [], [".github/ci.yml"], None)

    def test_once_only_and_shared_history_with_distinct_execution_status(self):
        state.set_auto_approval_policy("github", "push", prompt.POLICY)
        self.assertEqual(len(state.pending_push_auto_approvals()), 1)
        state.save_push_auto_approval_review("abc123", 123, prompt.POLICY, "approved", "Routine", "gpt-6.1-sol")
        state.save_push_auto_approval_error("abc123", "remote moved")
        state.set_auto_approval_policy("github", "push", None)
        self.assertEqual(state.pending_push_auto_approvals(), [])
        row = state.auto_approval_history(1, "approved")["items"][0]
        self.assertEqual((row["kind"], row["approval_id"], row["status"]), ("github_push", "push-abc123", "pending"))
        self.assertEqual(row["policy"], prompt.POLICY)
        self.assertEqual(row["approval_error"], "remote moved")
        annotation = state.page_approvals("pending", 1)["items"][0]
        self.assertFalse(annotation["has_auto_policy"])
        self.assertEqual(annotation["auto_review"]["outcome"], "approved")
        self.assertEqual(state.auto_approval_history(1, "failed")["total"], 0)
