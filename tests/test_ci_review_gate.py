"""Check the read-only review gate against GitHub-shaped responses.
Cover exact-head verdicts, conflicting timestamps, stale bases and API failures.
Use temporary repositories to verify cheap preflight parses rather than executes.
Automatic starts are bounded; ordinary retries and permissions remain with GitHub.
"""
from copy import deepcopy
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f".github/ci/{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


policy = load("check_review")
preflight = load("preflight")
REPO = "infiversehq/kern"
HEAD, BASE = "a" * 40, "b" * 40
PR = {"number": 7, "state": "open", "draft": False,
      "head": {"sha": HEAD, "ref": "feature", "repo": {"full_name": REPO}},
      "base": {"sha": BASE, "ref": "main"}}
ROOT_API = f"/repos/{REPO}"
COMMENT = {"id": 12, "user": {"login": policy.CODEX},
           "body": policy.CLEAN + f"\n\n**Reviewed commit:** `{HEAD[:10]}`",
           "created_at": "2026-10-28T01:00:00Z", "updated_at": "2026-10-28T01:00:00Z"}


class ReviewGateTests(unittest.TestCase):
    def verdict(self, comments=None, reviews=None, resolved=HEAD, shadow=False):
        def api(path, method="GET"):
            self.assertEqual(method, "GET")
            if "/comments?" in path:
                return [deepcopy(COMMENT)] if comments is None else comments
            if "/reviews?" in path:
                return reviews or []
            if "/matching-refs/" in path:
                return [{}] if shadow else []
            if "/commits/" in path:
                return {"sha": resolved}
            self.fail(path)
        with patch.object(policy, "request", side_effect=api):
            return policy.clean_review(ROOT_API, 7, HEAD)

    def test_only_explicit_clean_comment_by_codex_on_exact_sha_counts(self):
        self.assertTrue(self.verdict())
        for changes in [{"user": {"login": "other"}}, {"body": "👍"},
                        {"body": "> " + COMMENT["body"]},
                        {"body": COMMENT["body"].replace(HEAD[:10], "c" * 10)}]:
            with self.subTest(changes=changes):
                self.assertFalse(self.verdict(comments=[{**COMMENT, **changes}]))
        self.assertFalse(self.verdict(comments=[]))

    def test_current_receipt_format_from_codex_is_recognized(self) -> None:
        body = "<!-- codex-review-receipt:v1 -->\n\n" + COMMENT["body"]
        self.assertTrue(self.verdict(comments=[{**COMMENT, "body": body}]))
        self.assertFalse(self.verdict(comments=[{**COMMENT, "body": "> " + body}]))
        self.assertFalse(self.verdict(comments=[{**COMMENT, "body": "Unrelated text " + body}]))

    def test_latest_same_sha_finding_revokes_clean_verdict(self):
        review = {"id": 13, "user": {"login": policy.CODEX}, "commit_id": HEAD,
                  "submitted_at": "2026-10-28T01:01:00Z", "state": "COMMENTED", "body": "Found a bug"}
        self.assertFalse(self.verdict(reviews=[review]))
        self.assertTrue(self.verdict(reviews=[{**review, "commit_id": "c" * 40}]))
        self.assertTrue(self.verdict(reviews=[{**review, "submitted_at": "2026-10-28T00:00:00Z"}]))

    def test_conflicting_verdicts_in_same_second_fail_closed(self) -> None:
        for review_id in [1, COMMENT["id"] + 1]:
            review = {"id": review_id, "user": {"login": policy.CODEX}, "commit_id": HEAD,
                      "submitted_at": COMMENT["updated_at"], "state": "COMMENTED", "body": "Found a bug"}
            with self.subTest(review_id=review_id):
                self.assertFalse(self.verdict(reviews=[review]))

    def test_sha_resolution_and_api_errors_fail_closed(self):
        for kwargs in [{"resolved": HEAD[:10] + "c" * 30}, {"shadow": True}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(RuntimeError):
                self.verdict(**kwargs)
        with patch.object(policy, "request", side_effect=RuntimeError("API unavailable")):
            with self.assertRaises(RuntimeError):
                policy.clean_review(ROOT_API, 7, HEAD)

    def test_gate_rechecks_current_head_and_base_after_reading_review(self):
        event = {"number": 7, "pull_request": deepcopy(PR)}
        cases = [{**PR, "state": "closed"}, {**PR, "draft": True},
                 {**PR, "base": {"sha": "c" * 40, "ref": "main"}},
                 {**PR, "head": {**PR["head"], "sha": "c" * 40}},
                 {**PR, "head": {**PR["head"], "repo": {"full_name": "fork/repo"}}}]
        for pr in cases:
            for position in [0, 1]:
                reads = [deepcopy(PR), deepcopy(PR)]
                reads[position] = pr
                with self.subTest(pr=pr, position=position), patch.object(policy, "request", side_effect=reads), patch.object(policy, "clean_review", return_value=True):
                    with self.assertRaises(RuntimeError):
                        policy.gate(event, REPO)

    def test_missing_review_is_deferred_without_exception(self):
        with patch.object(policy, "request", return_value=deepcopy(PR)), patch.object(policy, "clean_review", return_value=False):
            self.assertFalse(policy.gate({"number": 7, "pull_request": PR}, REPO))

    def test_pagination_does_not_miss_late_reviews(self):
        with patch.object(policy, "request", side_effect=[[{}] * 100, [COMMENT]]) as api:
            self.assertEqual(len(policy.pages("/comments")), 101)
            self.assertTrue(api.call_args.args[0].endswith("page=2"))


class PreflightTests(unittest.TestCase):
    def test_parsing_without_execution_and_invalid_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", directory], check=True)
            (root / "safe.py").write_text("raise RuntimeError('must never execute')\n")
            (root / "metadata.json").write_text('{"valid":true}')
            (root / ".github/workflows").mkdir(parents=True)
            action = root / ".github/workflows/test.yml"
            action.write_text("steps:\n  - uses: actions/checkout@" + "a" * 40 + " # pinned\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            preflight.check(root)
            for path, invalid in [(root / "safe.py", "if :"),
                                  (root / "metadata.json", "{invalid}"),
                                  (action, "steps:\n  - uses: actions/checkout@main\n")]:
                original = path.read_text()
                path.write_text(invalid)
                with self.subTest(path=path), self.assertRaises(ValueError):
                    preflight.check(root)
                path.write_text(original)

class AutomaticStartTests(unittest.TestCase):
    def setUp(self) -> None:
        import sys
        with patch.dict(sys.modules, {"check_review": policy}):
            self.worker = load("run_reviewed_ci")
        self.run_record: dict[str, Any] = {
            "id": 99, "run_attempt": 1, "status": "completed", "event": "pull_request",
            "path": ".github/workflows/" + sorted(self.worker.WORKFLOWS)[0],
            "head_sha": HEAD, "pull_requests": [deepcopy(PR)],
        }
        self.jobs: list[dict[str, Any]] = [{"name": "Cheap checks and review gate", "steps": [
            {"name": "Wait for clean Codex review", "conclusion": "failure"}]}]

    def invoke(self, *, clean: bool = True, current: Any = None, moved: bool = False) -> Any:
        def get(path: str) -> Any:
            if "/pulls/" in path:
                return {**PR, "state": "closed"} if moved else deepcopy(PR)
            return deepcopy(self.run_record if current is None else current)

        def pages(path: str, key: str) -> Any:
            return [deepcopy(self.run_record)] if "/workflows/" in path else self.jobs

        with patch.object(policy, "request", side_effect=get), patch.object(policy, "pages", side_effect=pages), patch.object(policy, "clean_review", return_value=clean), patch.object(self.worker.subprocess, "run") as launch:
            self.worker.start(deepcopy(PR), REPO)
            return launch.call_args_list

    def test_clean_review_starts_original_run_once(self) -> None:
        calls = self.invoke()
        self.assertEqual(len(calls), len(self.worker.WORKFLOWS))
        self.assertEqual(calls[0].args[0], ["gh", "api", "--method", "POST", f"{ROOT_API}/actions/runs/99/rerun"])

    def test_no_review_busy_stale_or_already_started_run_does_not_launch(self) -> None:
        self.assertFalse(self.invoke(clean=False))
        self.assertFalse(self.invoke(moved=True))
        for status in ["queued", "in_progress"]:
            with self.subTest(status=status):
                self.assertFalse(self.invoke(current={**self.run_record, "status": status}))
        self.run_record["run_attempt"] = 2
        # Even a forged wait marker cannot create an automatic retry loop.
        self.assertFalse(self.invoke())

    def test_test_failures_and_lookalike_markers_do_not_launch(self) -> None:
        self.jobs[0]["name"] = "Unit tests"
        self.assertFalse(self.invoke())
        self.jobs[0]["name"] = "Cheap checks and review gate"
        self.jobs[0]["steps"][0]["name"] = "Run dependency-free preflight"
        self.assertFalse(self.invoke())
        self.jobs[0]["steps"][0] = {"name": "Wait for clean Codex review", "conclusion": "skipped"}
        self.assertFalse(self.invoke())

    def test_changed_base_and_duplicate_gate_are_rejected(self) -> None:
        self.run_record["pull_requests"] = [{**PR, "base": {"sha": "c" * 40}}]
        self.assertFalse(self.invoke())
        self.run_record["pull_requests"] = [deepcopy(PR)]
        self.jobs *= 2
        self.assertFalse(self.invoke())

    def test_completion_race_accepts_ref_suffix_but_never_second_attempt(self) -> None:
        for suffix in ["", "@main", "@refs/pull/7/merge"]:
            run = {**self.run_record, "path": self.run_record["path"] + suffix}
            with self.subTest(suffix=suffix), patch.object(policy, "request", side_effect=[run, deepcopy(PR)]), patch.object(self.worker, "start") as start:
                self.worker.handle({"workflow_run": {"id": 99}}, "workflow_run", REPO)
                start.assert_called_once_with(PR, REPO)
        with patch.object(policy, "request", return_value={**self.run_record, "run_attempt": 2}), patch.object(self.worker, "start") as start:
            self.worker.handle({"workflow_run": {"id": 99}}, "workflow_run", REPO)
            start.assert_not_called()

    def test_only_clean_codex_comments_are_launch_events(self) -> None:
        event: dict[str, Any] = {"issue": {"number": 7, "pull_request": {"url": "PR"}}, "comment": deepcopy(COMMENT)}
        event["comment"]["body"] = "<!-- codex-review-receipt:v1 -->\n\n" + COMMENT["body"]
        with patch.object(policy, "request", return_value=deepcopy(PR)), patch.object(self.worker, "start") as start:
            self.worker.handle(event, "issue_comment", REPO)
            start.assert_called_once()
            start.reset_mock()
            event["comment"]["user"]["login"] = "outside-commenter"
            self.worker.handle(event, "issue_comment", REPO)
            start.assert_not_called()

    def test_concurrent_acceptance_is_deduplicated_but_denial_is_reported(self) -> None:
        for after in [self.run_record, {**self.run_record, "run_attempt": 2, "status": "queued"}]:
            with self.subTest(after=after), patch.object(self.worker, "WORKFLOWS", {sorted(self.worker.WORKFLOWS)[0]}), patch.object(policy, "clean_review", return_value=True), patch.object(policy, "pages", side_effect=[[self.run_record], self.jobs]), patch.object(policy, "request", side_effect=[deepcopy(PR), self.run_record, after]), patch.object(self.worker.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "gh")):
                if after["run_attempt"] == 1:
                    with self.assertRaises(subprocess.CalledProcessError):
                        self.worker.start(deepcopy(PR), REPO)
                else:
                    self.worker.start(deepcopy(PR), REPO)
