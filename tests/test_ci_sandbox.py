from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest


class SandboxImageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        (self.repo / ".github/ci").mkdir(parents=True)
        shutil.copy(".github/ci/prepare-sandbox.sh", self.repo / ".github/ci/prepare-sandbox.sh")
        (self.repo / ".github/ci/sandbox.Dockerfile").write_text("FROM ubuntu:22.04\n")
        (self.repo / ".github/ci/requirements.txt").write_text("playwright==1.60.0\n")
        (self.repo / "must-not-enter-image").write_text("private source\n")
        self.log = self.root / "docker.jsonl"
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        docker = bin_dir / "docker"
        docker.write_text(f"#!{sys.executable}\n" + '''
import json, os, pathlib, sys
args = sys.argv[1:]
registry_config = None
if args[:1] == ["--config"]:
    registry_config = pathlib.Path(args[1])
    args = args[2:]
record = {"args": args}
if registry_config is not None:
    record["registry_config"] = str(registry_config)
    record["registry_config_exists"] = registry_config.is_dir()
    if args[0] == "login":
        (registry_config / "config.json").write_text("temporary registry credentials")
if args[0] == "build":
    context = pathlib.Path(args[-1])
    record["files"] = sorted(str(p.relative_to(context)) for p in context.rglob("*") if p.is_file())
with open(os.environ["DOCKER_TEST_LOG"], "a") as log:
    log.write(json.dumps(record) + "\\n")
if args[0] == "build" and os.environ.get("DOCKER_TEST_BUILD_SEQUENCE"):
    builds = sum(json.loads(line)["args"][0] == "build"
                 for line in pathlib.Path(os.environ["DOCKER_TEST_LOG"]).read_text().splitlines())
    sys.exit(int(os.environ["DOCKER_TEST_BUILD_SEQUENCE"].split(",")[builds - 1]))
sys.exit(int(os.environ.get("DOCKER_TEST_" + args[0].upper(), "0")))
''')
        docker.chmod(0o755)
        self.env = {
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "RUNNER_TEMP": str(self.root),
            "DOCKER_TEST_LOG": str(self.log),
            "DOCKER_TEST_IMAGE": "1",  # Exact dependency image is absent by default.
        }

    def run_helper(self, mode: str = "pull", **outcomes: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", ".github/ci/prepare-sandbox.sh", mode, "ghcr.io/example/ci"],
            cwd=self.repo,
            env={**self.env, **{f"DOCKER_TEST_{key.upper()}": value for key, value in outcomes.items()}},
            capture_output=True,
            text=True,
            check=False,
        )

    def calls(self) -> list[dict]:
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_published_image_is_pulled_without_build_or_export(self) -> None:
        self.assertEqual(self.run_helper().returncode, 0)
        calls = self.calls()
        self.assertEqual([call["args"][0] for call in calls], ["image", "pull", "tag"])
        self.assertEqual(calls[2]["args"][1], calls[1]["args"][1])
        self.assertEqual(calls[2]["args"][2], "kern-ci:sandbox")

    def test_locally_pulled_image_is_reused_without_registry_access(self) -> None:
        self.assertEqual(self.run_helper(image="0").returncode, 0)
        calls = self.calls()
        self.assertEqual([call["args"][0] for call in calls], ["image", "tag"])
        self.assertEqual(calls[0]["args"][2], calls[1]["args"][1])
        self.assertEqual(calls[1]["args"][2], "kern-ci:sandbox")

    def test_missing_or_inaccessible_image_builds_only_dependency_inputs(self) -> None:
        self.assertEqual(self.run_helper(pull="1").returncode, 0)
        calls = self.calls()
        self.assertEqual([call["args"][0] for call in calls], ["image", "pull", "build"])
        self.assertEqual(calls[2]["files"], [".github/ci/requirements.txt", "Dockerfile"])
        self.assertFalse(Path(calls[2]["args"][-1]).exists())

    def test_changed_dependencies_select_a_different_image(self) -> None:
        self.assertEqual(self.run_helper().returncode, 0)
        original = self.calls()[1]["args"][1]
        for relative in (".github/ci/sandbox.Dockerfile", ".github/ci/requirements.txt"):
            with self.subTest(path=relative):
                self.log.unlink()
                with (self.repo / relative).open("a") as source:
                    source.write("# changed dependency\n")
                self.assertEqual(self.run_helper().returncode, 0)
                updated = self.calls()[1]["args"][1]
                self.assertNotEqual(updated, original)
                original = updated

    def test_existing_published_image_is_not_overwritten(self) -> None:
        self.assertEqual(self.run_helper("publish").returncode, 0)
        self.assertEqual([call["args"][0] for call in self.calls()], ["manifest"])

    def test_publisher_and_consumer_use_the_same_reference(self) -> None:
        self.assertEqual(self.run_helper().returncode, 0)
        ref = self.calls()[1]["args"][1]
        self.log.unlink()
        self.assertEqual(self.run_helper("publish", manifest="1").returncode, 0)
        calls = self.calls()
        self.assertEqual([call["args"][0] for call in calls], ["manifest", "build", "tag", "push"])
        self.assertEqual(calls[-1]["args"][1], ref)

    def test_build_or_publish_failure_is_not_reported_as_success(self) -> None:
        for mode, outcomes in (
            ("pull", {"pull": "1", "build": "7"}),
            ("publish", {"manifest": "1", "push": "7"}),
        ):
            with self.subTest(mode=mode):
                self.assertEqual(self.run_helper(mode, **outcomes).returncode, 7)
                self.assertEqual(list(self.root.glob("kern-ci-build.*")), [])

    def test_timed_out_build_retries_once_with_the_same_dependency_inputs(self) -> None:
        result = self.run_helper(pull="1", build_sequence="124,0")
        self.assertEqual(result.returncode, 0, result.stderr)
        builds = [call for call in self.calls() if call["args"][0] == "build"]
        self.assertEqual(len(builds), 2)
        self.assertEqual(builds[0], builds[1])
        self.assertEqual(builds[0]["files"], [".github/ci/requirements.txt", "Dockerfile"])
        self.assertEqual(list(self.root.glob("kern-ci-build.*")), [])

    def test_repeated_build_timeouts_fail_without_publishing(self) -> None:
        result = self.run_helper("publish", manifest="1", build_sequence="124,124")
        self.assertEqual(result.returncode, 124, result.stderr)
        self.assertEqual([call["args"][0] for call in self.calls()], ["manifest", "build", "build"])
        self.assertEqual(list(self.root.glob("kern-ci-build.*")), [])

    def test_deterministic_build_failure_is_not_retried(self) -> None:
        result = self.run_helper(pull="1", build_sequence="7")
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual([call["args"][0] for call in self.calls()], ["image", "pull", "build"])

    def test_authenticated_pulls_use_each_owner_and_remove_credentials_even_on_miss(self) -> None:
        workflow = Path('.github/workflows/test-all-host.yml').read_text()
        step = workflow.split('      - name: Pull sandbox image with temporary registry credentials\n', 1)[1]
        script = textwrap.dedent(step.split('        run: |\n', 1)[1].split('\n      - name:', 1)[0])
        for owner in ('infiversehq', 'infiloop2'):
            for pull_status in ('0', '1'):
                with self.subTest(owner=owner, pull_status=pull_status):
                    self.log.unlink(missing_ok=True)
                    result = subprocess.run(
                        ['bash', '-euo', 'pipefail', '-c', script], cwd=self.repo,
                        env={**self.env, 'GHCR_TOKEN': 'test-token', 'GITHUB_ACTOR': 'test-actor',
                             'CI_REGISTRY_IMAGE': f'ghcr.io/{owner}/kern-ci',
                             'DOCKER_TEST_PULL': pull_status},
                        capture_output=True, text=True, check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    calls = self.calls()
                    self.assertEqual([call['args'][0] for call in calls], ['login', 'pull'])
                    self.assertTrue(calls[1]['args'][1].startswith(f'ghcr.io/{owner}/kern-ci:deps-'))
                    self.assertTrue(all(call['registry_config_exists'] for call in calls))
                    self.assertEqual(calls[0]['registry_config'], calls[1]['registry_config'])
                    self.assertFalse(Path(calls[0]['registry_config']).exists())
                    self.assertEqual(list(self.root.glob('kern-ci-registry.*')), [])

    def run_retention(self, versions: list[dict], api_status: str = "0", *,
                      owner: str = "infiversehq", owner_type: str = "Organization") -> subprocess.CompletedProcess[str]:
        workflow = Path('.github/workflows/ci-sandbox-image.yml').read_text()
        cleanup = workflow.split('      - name: Retain recent dependency images\n', 1)[1]
        script = textwrap.dedent(cleanup.split('        run: |\n', 1)[1])
        gh = self.root / 'bin/gh'
        gh.write_text(f'#!{sys.executable}\n' + '''
import json, os, sys
with open(os.environ['GH_TEST_LOG'], 'a') as log:
    log.write(json.dumps(sys.argv[1:]) + '\\n')
if '--paginate' in sys.argv:
    print(os.environ['GH_TEST_PAGES'])
sys.exit(int(os.environ['GH_TEST_STATUS']))
''')
        gh.chmod(0o755)
        return subprocess.run(
            ['bash', '-euo', 'pipefail', '-c', script], cwd=self.repo,
            env={**self.env, 'GH_TEST_LOG': str(self.root / 'gh.jsonl'),
                 'GH_TEST_PAGES': json.dumps([versions[:4], versions[4:]]),
                 'GH_TEST_STATUS': api_status,
                 'PACKAGE_OWNER': owner, 'PACKAGE_OWNER_TYPE': owner_type},
            capture_output=True, text=True, check=False,
        )

    def retention_versions(self) -> list[dict]:
        self.assertEqual(self.run_helper().returncode, 0)
        current_tag = self.calls()[1]['args'][1].rsplit(':', 1)[1]
        return [
            {'id': index, 'created_at': f'2026-10-{index:02d}T00:00:00Z',
             'metadata': {'container': {'tags': [current_tag if index == 1 else f'deps-{index:064x}']}}}
            for index in range(1, 10)
        ]

    def test_retention_keeps_newest_five_current_and_manual_tags(self) -> None:
        versions = self.retention_versions()
        versions[1]['metadata']['container']['tags'].append('manual')
        versions.append({'id': 10, 'created_at': '2026-10-10T00:00:00Z',
                         'metadata': {'container': {'tags': []}}})
        result = self.run_retention(versions)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = [json.loads(line) for line in (self.root / 'gh.jsonl').read_text().splitlines()]
        self.assertIn('--paginate', calls[0])
        self.assertEqual([call[-1].rsplit('/', 1)[1] for call in calls[1:]], ['4', '3'])
        self.assertEqual(calls[1:], [
            ['api', '--method', 'DELETE', f'/orgs/infiversehq/packages/container/kern-ci/versions/{version_id}']
            for version_id in (4, 3)
        ])

    def test_retention_missing_current_image_refuses_deletion(self) -> None:
        result = self.run_retention(self.retention_versions()[1:])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('refusing cleanup', result.stderr)
        self.assertEqual(len((self.root / 'gh.jsonl').read_text().splitlines()), 1)

    def test_retention_uses_the_public_repositories_personal_package_namespace(self) -> None:
        result = self.run_retention(self.retention_versions(), owner='infiloop2', owner_type='User')
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = [json.loads(line) for line in (self.root / 'gh.jsonl').read_text().splitlines()]
        self.assertEqual(calls[0][-1], '/users/infiloop2/packages/container/kern-ci/versions?per_page=100')
        self.assertEqual(calls[1:], [
            ['api', '--method', 'DELETE', f'/users/infiloop2/packages/container/kern-ci/versions/{version_id}']
            for version_id in (4, 3, 2)
        ])

    def test_retention_api_failure_refuses_deletion(self) -> None:
        result = self.run_retention(self.retention_versions(), api_status='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len((self.root / 'gh.jsonl').read_text().splitlines()), 1)
