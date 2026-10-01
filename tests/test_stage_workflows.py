"""Exercise the stage lifecycle shell steps without contacting AWS."""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest

from host.cli.lifecycle_constants import (
    BOOTSTRAP_TIMEOUT_SECONDS,
    SCP_TIMEOUT_SECONDS,
    SSH_WAIT_ATTEMPTS,
    SSH_WAIT_SECONDS,
)

ROOT = Path(__file__).resolve().parents[1]


def step(workflow: str, name: str) -> str:
    source = (ROOT / f'.github/workflows/{workflow}.yml').read_text()
    return source.split(f'      - name: {name}\n', 1)[1].split('\n      - name:', 1)[0]


class StageWorkflowTests(unittest.TestCase):
    def run_step(self, workflow: str, name: str, **overrides: str):
        block = step(workflow, name)
        script = textwrap.dedent(block.split('        run: |\n', 1)[1])
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            fake = f'#!{sys.executable}\n' + '''
import json, os, pathlib, sys
with pathlib.Path('calls').open('a') as log:
    log.write(json.dumps([pathlib.Path(sys.argv[0]).name, *sys.argv[1:]]) + '\\n')
if pathlib.Path(sys.argv[0]).name == 'aws':
    print(os.environ.get('INSTANCE_IDS', 'i-stage'))
else:
    print(os.environ.get('DIAGNOSTIC', ''), file=sys.stderr)
    print('{}')
    sys.exit(int(os.environ.get('CLI_EXIT', '0')))
'''
            for name in ('python3', 'aws'):
                executable = root / name
                executable.write_text(fake)
                executable.chmod(0o755)
            env = {**os.environ, 'PATH': f'{root}:{os.environ["PATH"]}',
                   'GITHUB_OUTPUT': str(root / 'outputs'), **overrides}
            # Upgrade classification reads VERSION from the checked-out release.
            (root / 'VERSION').write_text('1.2.3\n')
            result = subprocess.run(['bash', '-c', script], cwd=root, env=env,
                                    text=True, capture_output=True)
            calls = (root / 'calls').read_text() if (root / 'calls').exists() else ''
            outputs = (root / 'outputs').read_text() if (root / 'outputs').exists() else ''
            return result, calls, outputs

    def test_operational_upgrade_failure_is_not_treated_as_current_release(self):
        result, calls, outputs = self.run_step('kern-stage', 'Upgrade stage',
                                              CLI_EXIT='1', DIAGNOSTIC='bootstrap failed')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('same_version_failure=false', outputs)
        self.assertNotIn('host.cli.recover', calls)
        failure, _, _ = self.run_step('kern-stage', 'Report stage upgrade failure')
        self.assertNotEqual(failure.returncode, 0)
        self.assertIn('kern-stage-recover', failure.stderr)

    def test_current_release_can_start_but_start_failure_is_fatal(self):
        result, _, outputs = self.run_step(
            'kern-stage', 'Upgrade stage', CLI_EXIT='2',
            DIAGNOSTIC='tagged with version 1.2.3; upgrade requires preserved state older than target VERSION 1.2.3')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('same_version_failure=true', outputs)
        result, calls, _ = self.run_step('kern-stage', 'Start existing stage', CLI_EXIT='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('host.cli.start', calls)
        self.assertNotIn('continue-on-error', step('kern-stage', 'Start existing stage'))

    def test_cleanup_stops_tagged_instance_without_a_result_file(self):
        result, calls, _ = self.run_step('kern-stage', 'Stop stage instance')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Name=tag:kern-host-agent-name,Values=kern-stage', calls)
        self.assertIn('host.cli.stop', calls)
        result, calls, _ = self.run_step('kern-stage', 'Stop stage instance', INSTANCE_IDS='')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('host.cli.stop', calls)

    def test_manual_recovery_uses_preserved_state_without_fresh_deploy_fallback(self):
        for exit_code in ('0', '1'):
            with self.subTest(exit_code=exit_code):
                result, calls, _ = self.run_step('kern-stage-recover', 'Recover stage EC2 instance',
                                                CLI_EXIT=exit_code)
                self.assertEqual(result.returncode, int(exit_code), result.stderr)
                self.assertIn('host.cli.recover', calls)
                self.assertIn('--allow-upgrade', calls)
                self.assertNotIn('host.cli.deploy', calls)
                self.assertNotIn('--admin-password-sha256', calls)
        source = (ROOT / '.github/workflows/kern-stage.yml').read_text()
        self.assertNotIn('host.cli.recover', source)
        self.assertNotIn('host.cli.deploy', source)

    def test_recovery_job_allows_cli_deadlines_to_finish(self):
        source = (ROOT / '.github/workflows/kern-stage-recover.yml').read_text()
        match = re.search(r'    timeout-minutes: (\d+)', source)
        self.assertIsNotNone(match)
        assert match is not None
        timeout_seconds = int(match.group(1)) * 60
        cli_deadlines = SSH_WAIT_ATTEMPTS * SSH_WAIT_SECONDS + SCP_TIMEOUT_SECONDS + BOOTSTRAP_TIMEOUT_SECONDS
        # AWS provisioning and the CLI's failure cleanup also need headroom.
        self.assertGreater(timeout_seconds, cli_deadlines)
