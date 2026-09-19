"""Execute the actual admin gate with a fake GitHub permission endpoint."""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]
ACTION = ROOT / '.github/actions/authorize-repo-admin/action.yml'


class WorkflowAuthorizationTests(unittest.TestCase):
    def run_gate(self, original: str, requester: str, *, status: str = '200', permission: str | None = None):
        source = ACTION.read_text()
        actor_context = re.search(r'ACTOR: \$\{\{ github\.(\w+) \}\}', source).group(1)
        actor = {'actor': original, 'triggering_actor': requester}[actor_context]
        script = textwrap.dedent(source.split('      run: |\n', 1)[1])
        script = script.replace('${{ github.api_url }}', 'https://api.github.test').replace('${{ github.repository }}', 'owner/repo')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            # Keep the real shell/URL/permission logic, but isolate its response file.
            script = script.replace('/tmp/permission.json', str(root / 'permission.json'))
            curl = root / 'curl'
            curl.write_text(f'#!{sys.executable}\n' + '''
import json, os, pathlib, sys
args = sys.argv[1:]
pathlib.Path(os.environ['REQUEST_LOG']).write_text(args[-1])
assert args[-1].endswith('/collaborators/' + os.environ['ACTOR'] + '/permission')
permission = os.environ.get('FAKE_PERMISSION', 'admin' if os.environ['ACTOR'] == 'admin-user' else 'write')
pathlib.Path(args[args.index('-o') + 1]).write_text(json.dumps({'permission': permission}))
print(os.environ['FAKE_STATUS'], end='')
''')
            curl.chmod(0o755)
            env = {**os.environ, 'PATH': f'{root}:{os.environ["PATH"]}', 'ACTOR': actor,
                   'GH_TOKEN': 'test-token', 'FAKE_STATUS': status, 'REQUEST_LOG': str(root / 'request')}
            if permission is not None:
                env['FAKE_PERMISSION'] = permission
            result = subprocess.run(['bash', '-c', script], env=env, text=True, capture_output=True)
            return result, (root / 'request').read_text()

    def test_non_admin_rerun_does_not_inherit_original_admin(self):
        result, url = self.run_gate('admin-user', 'writer-user')
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(url.endswith('/writer-user/permission'))
        self.assertIn("Actor 'writer-user'", result.stdout)
        self.assertNotIn("Actor 'admin-user'", result.stdout)

    def test_admin_requester_passes_for_initial_run_and_rerun(self):
        for original in ('admin-user', 'writer-user'):
            with self.subTest(original=original):
                result, url = self.run_gate(original, 'admin-user')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(url.endswith('/admin-user/permission'))

    def test_permission_lookup_and_missing_permission_fail_closed(self):
        for status, permission in (('403', 'admin'), ('404', 'admin'), ('200', '')):
            with self.subTest(status=status, permission=permission):
                result, _ = self.run_gate('admin-user', 'admin-user', status=status, permission=permission)
                self.assertNotEqual(result.returncode, 0)

    def test_execution_jobs_authorize_every_attempt_before_privileged_steps(self):
        for name, job in (('kern-smoke', 'smoke'), ('test-lima-host', 'smoke'),
                          ('kern-stage', 'stage')):
            with self.subTest(workflow=name):
                source = (ROOT / f'.github/workflows/{name}.yml').read_text()
                self.assertNotIn('github.run_attempt', source)
                block = re.split(r'\n  [\w-]+:\n', source.split(f'\n  {job}:\n', 1)[1], 1)[0]
                prefix, remaining = block.split('      - name: Authorize requester', 1)
                self.assertIn('ref: main', prefix)
                self.assertNotIn('secrets.', prefix)
                checkout = prefix.split('      - name: Checkout trusted authorization actions', 1)[1]
                self.assertNotIn('if:', checkout)
                gate, later = remaining.split('\n      - name:', 1)
                self.assertIn('authorize-repo-admin', gate)
                self.assertNotIn('continue-on-error', gate)
                self.assertTrue(later)
                if name in ('kern-smoke', 'test-lima-host'):
                    self.assertIn("if: ${{ github.event_name != 'push' }}", source)
                    condition = "if: ${{ github.event_name != 'push' }}"
                    self.assertIn(condition, gate)
                    self.assertIn("  push:\n    branches:\n      - main\n", source)
                else:
                    self.assertNotIn('if:', gate)
                if name == 'kern-stage':
                    cleanup = block.split('      - name: Stop stage instance', 1)[1].split('\n      - name:', 1)[0]
                    self.assertIn("always() && steps.authorize_requester.outcome == 'success'", cleanup)


    def test_stage_start_and_stop_check_every_attempt_before_aws_access(self):
        for operation in ('start', 'stop'):
            with self.subTest(operation=operation):
                source = (ROOT / f'.github/workflows/kern-stage-{operation}.yml').read_text()
                self.assertNotIn('needs:', source)
                self.assertNotIn('  authorize:', source)
                self.assertNotIn('github.run_attempt', source)
                self.assertIn("if: github.repository == 'infiversehq/kern'", source)
                before, rest = source.split('      - name: Authorize repo admin', 1)
                gate, later = rest.split('      - name: Require main dispatch', 1)
                self.assertIn('ref: main', before)
                self.assertIn('uses: ./.github/actions/authorize-repo-admin', gate)
                self.assertNotIn('if:', gate)
                self.assertNotIn('continue-on-error', source)
                self.assertNotIn('secrets.', before + gate)
                dispatch, execute = later.split(f'      - name: {operation.title()} stage EC2 instance', 1)
                self.assertIn('"${DISPATCH_REF}" != "refs/heads/main"', dispatch)
                self.assertIn('exit 1', dispatch)
                self.assertNotIn('secrets.', dispatch)
                self.assertNotIn('if:', execute)
                self.assertIn('secrets.KERN_STAGE_AWS_ACCESS_KEY_ID', execute)
