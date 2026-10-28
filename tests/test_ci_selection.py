"""Behavioral checks for PR merge-base diff selection."""
from __future__ import annotations

import contextlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]


def load_script(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


selector = load_script("select_tests", ".github/ci/select_tests.py")


class SelectionTests(unittest.TestCase):
    def test_suites_survive_skipped_review_but_require_successful_selection(self):
        source = (ROOT / ".github/workflows/test-all-host.yml").read_text()
        suites = source.split("\n  suites:\n", 1)[1].split("\n  test:\n", 1)[0]
        condition = re.search(r"    if: >-\n((?:      .+\n)+)", suites).group(1)
        # GitHub applies implicit success() across the needs chain unless a
        # status function overrides it. Public review is intentionally skipped.
        self.assertIn("always()", condition)
        self.assertIn("!cancelled()", condition)
        self.assertIn("needs.select.result == 'success'", condition)
        self.assertIn("github.repository != 'infiversehq/kern'", condition)
        self.assertIn("github.event_name == 'pull_request' || github.event.repository.private == false", condition)

    def test_aggregate_rejects_missing_or_failed_selected_suites(self):
        source = (ROOT / ".github/workflows/test-all-host.yml").read_text()
        aggregate = source.split("\n  test:\n", 1)[1]
        script = textwrap.dedent(aggregate.split("        run: |\n", 1)[1].split("      - name:", 1)[0])
        for select in ("success", "failure", "cancelled", "skipped"):
            for suites in ("success", "failure", "cancelled", "skipped"):
                with self.subTest(select=select, suites=suites):
                    result = subprocess.run(["bash", "-e", "-c", script], capture_output=True,
                                            env={**os.environ, "REPOSITORY": "infiloop2/kern",
                                                 "REVIEW_RESULT": "skipped", "SELECT_RESULT": select,
                                                 "SUITES_RESULT": suites})
                    self.assertEqual(result.returncode == 0, select == suites == "success")

    def selected(self, *paths):
        matrix = selector.select(list(paths))
        self.assertIn("python", matrix["suite"])
        self.assertEqual(len(matrix["suite"]), len(set(matrix["suite"])))
        return set(matrix["suite"]) - {"python"}

    def test_isolated_backend_and_version_do_not_force_browsers(self):
        self.assertEqual(self.selected("host/cli/lifecycle_aws.py", "VERSION"), set())
        self.assertEqual(self.selected("host/tools/brave_search/__init__.py"), {selector.ADMIN})

    def test_each_browser_family(self):
        cases = [
            ("host/runtime/admin_api/admin_ui/network.js", {selector.ADMIN}),
            ("host/runtime/workspace/chat/ui/agent_chat.js", {selector.WORKSPACE}),
            ("host/runtime/browser/actions/linkedin_dm.py", {selector.NATIVE}),
            ("host/runtime/browser_network/relay.py", {selector.NATIVE}),
            ("host/tools/browser/__init__.py", selector.ALL),
            ("host/runtime/browser_network/config.py", selector.ALL),
            ("tests/smoke-ui/linkedin_adapter_smoke.py", {selector.NATIVE}),
            ("tests/smoke-ui/workspace_smokes.py", {selector.WORKSPACE}),
            ("tests/smoke-ui/approval_smokes.py", {selector.ADMIN}),
            ("tests/smoke-ui/tool_cost_usage_smokes.py", {selector.ADMIN}),
            ("tests/smoke-ui/browser_smokes.py", selector.UI),
            ("tests/smoke-ui/admin_ui_smoke.py", selector.UI),
            ("tests/smoke-ui/run_admin_ui_mock.py", selector.UI),
            ("tests/smoke-ui/workspace_mocks.py", selector.UI),
        ]
        for path, expected in cases:
            with self.subTest(path=path):
                self.assertEqual(self.selected(path), expected)

    def test_shared_inputs_and_indirect_native_dependencies(self):
        for path in ("host/config.py", "host/runtime/core/state.py",
                     "host/runtime/network_proxy/service.py", "host/bootstrap/bootstrap.sh",
                     "host/runtime/admin_api/admin_auth.py", "host/tools/shared/web.py",
                     "host/npm/package-lock.json", ".github/ci/select_tests.py",
                     "tests/scripts/time_group.py", ".github/workflows/test-all-host.yml",
                     "tests/smoke-ui/timing.py", "tests/conftest.py"):
            with self.subTest(path=path):
                self.assertEqual(self.selected(path), selector.ALL)
        self.assertEqual(self.selected("host/runtime/admin_api/admin_ui/app.js"), selector.UI)
        self.assertEqual(self.selected("host/runtime/admin_api/workspace_api.py"), selector.UI)
        self.assertEqual(self.selected("host/runtime/admin_api/browser.py"), selector.ALL)
        self.assertEqual(self.selected("host/runtime/admin_api/admin_ui/browser_popup.js"), selector.UI)

    def test_unknown_and_empty_diff_are_broad(self):
        self.assertEqual(self.selected("new-subsystem/unknown.py"), selector.ALL)
        self.assertEqual(self.selected("tests/smoke-ui/new_fixture.py"), selector.ALL)
        self.assertEqual(self.selected(), selector.ALL)

    def test_unit_modules_do_not_expand_product_scope(self):
        unit = "tests/test_tools_openrouter.py"
        self.assertEqual(self.selected(unit, "tests/test_param_guard_coverage.py"), set())
        self.assertEqual(self.selected(unit, "host/tools/openrouter/__init__.py"), {selector.ADMIN})
        self.assertEqual(self.selected(unit, "host/runtime/core/state/accounts.py"), selector.ALL)
        # Existing browser/workspace-specific unit and fixture mappings remain.
        self.assertEqual(self.selected("tests/test_browser_actions.py"), {selector.ADMIN, selector.NATIVE})
        self.assertEqual(self.selected("tests/test_workspace_api.py"), selector.UI)

    def test_all_suites_without_a_git_diff(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory, "output")
            result = subprocess.run([sys.executable, str(ROOT / ".github/ci/select_tests.py"), "--all"],
                                    cwd=directory, env={**os.environ, "GITHUB_OUTPUT": str(output)},
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            matrix = json.loads(output.read_text().removeprefix("matrix="))
            self.assertEqual(matrix["suite"], ["python", "admin-ui", "workspace-ui", "native-browser"])

    def test_incomplete_or_ambiguous_arguments_fail_without_outputs(self):
        for args in ([], ["--base", "a" * 40], ["--all", "--head", "b" * 40]):
            with self.subTest(args=args), tempfile.TemporaryDirectory() as directory:
                output = Path(directory, "output")
                result = subprocess.run([sys.executable, str(ROOT / ".github/ci/select_tests.py"), *args],
                                        env={**os.environ, "GITHUB_OUTPUT": str(output)}, capture_output=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(output.exists())

    def test_mixed_diff_unions_every_path(self):
        self.assertEqual(self.selected("host/cli/deploy.py", "host/runtime/browser/storage.py",
                                       "host/runtime/workspace/chat/backend.py"),
                         {selector.NATIVE, selector.WORKSPACE})

    def test_complete_merge_base_diff_and_rename_deletion(self):
        with tempfile.TemporaryDirectory() as directory:
            def git(*args):
                return subprocess.check_output(["git", *args], cwd=directory, text=True).strip()

            def write(path, body):
                target = Path(directory, path)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(body)

            def commit():
                git("add", "-A")
                git("-c", "user.name=CI Test", "-c", "user.email=ci@example.test",
                    "commit", "-qm", "fixture")
                return git("rev-parse", "HEAD")

            git("init", "-q")
            old = "host/runtime/admin_api/admin_ui/network.js"
            native = "host/runtime/browser/fixture to delete.py"
            new = "host/runtime/workspace/chat/ui/renamed\nasset.js"
            write(old, "rename fixture\n")
            write(native, "deleted fixture\n")
            ancestor = commit()
            write("base-only/unknown.py", "must not select from the base branch\n")
            base = commit()
            git("checkout", "-q", "--detach", ancestor)
            write("host/cli/deploy.py", "first PR commit\n")
            first = commit()
            Path(directory, old).rename(Path(directory, "temp"))
            Path(directory, new).parent.mkdir(parents=True, exist_ok=True)
            Path(directory, "temp").rename(Path(directory, new))
            Path(directory, native).unlink()
            head = commit()
            with contextlib.chdir(directory):
                paths = selector.changed_paths(base, head)
                self.assertEqual(set(paths), {old, new, native, "host/cli/deploy.py"})
                self.assertEqual(self.selected(*paths), selector.ALL)
                self.assertEqual(selector.changed_paths(base, first), ["host/cli/deploy.py"])

    def test_git_failure_fails_without_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory, "output")
            result = subprocess.run([sys.executable, str(ROOT / ".github/ci/select_tests.py"),
                                     "--base", "a" * 40, "--head", "b" * 40],
                                    cwd=directory, env={**os.environ, "GITHUB_OUTPUT": str(output)},
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(output.exists())
