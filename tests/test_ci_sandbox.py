from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
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
record = {"args": args}
if args[0] == "build":
    context = pathlib.Path(args[-1])
    record["files"] = sorted(str(p.relative_to(context)) for p in context.rglob("*") if p.is_file())
with open(os.environ["DOCKER_TEST_LOG"], "a") as log:
    log.write(json.dumps(record) + "\\n")
sys.exit(int(os.environ.get("DOCKER_TEST_" + args[0].upper(), "0")))
''')
        docker.chmod(0o755)
        self.env = {
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "RUNNER_TEMP": str(self.root),
            "DOCKER_TEST_LOG": str(self.log),
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
        self.assertEqual([call["args"][0] for call in calls], ["pull", "tag"])
        self.assertEqual(calls[1]["args"][1], calls[0]["args"][1])
        self.assertEqual(calls[1]["args"][2], "kern-ci:sandbox")

    def test_missing_or_inaccessible_image_builds_only_dependency_inputs(self) -> None:
        self.assertEqual(self.run_helper(pull="1").returncode, 0)
        calls = self.calls()
        self.assertEqual([call["args"][0] for call in calls], ["pull", "build"])
        self.assertEqual(calls[1]["files"], [".github/ci/requirements.txt", "Dockerfile"])
        self.assertFalse(Path(calls[1]["args"][-1]).exists())

    def test_changed_dependencies_select_a_different_image(self) -> None:
        self.assertEqual(self.run_helper().returncode, 0)
        original = self.calls()[0]["args"][1]
        for relative in (".github/ci/sandbox.Dockerfile", ".github/ci/requirements.txt"):
            with self.subTest(path=relative):
                self.log.unlink()
                with (self.repo / relative).open("a") as source:
                    source.write("# changed dependency\n")
                self.assertEqual(self.run_helper().returncode, 0)
                updated = self.calls()[0]["args"][1]
                self.assertNotEqual(updated, original)
                original = updated

    def test_existing_published_image_is_not_overwritten(self) -> None:
        self.assertEqual(self.run_helper("publish").returncode, 0)
        self.assertEqual([call["args"][0] for call in self.calls()], ["manifest"])

    def test_publisher_and_consumer_use_the_same_reference(self) -> None:
        self.assertEqual(self.run_helper().returncode, 0)
        ref = self.calls()[0]["args"][1]
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
