"""Swarm reflects runtime failures separately from pending approvals."""
import base64
import json
from pathlib import Path
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which('node'), 'node is required')
class SwarmPoseTests(unittest.TestCase):
    def test_runtime_state_wins_over_pending_approvals(self) -> None:
        source = (Path(__file__).parents[1] / 'host/runtime/admin_api/admin_ui/swarm_pose.js').read_bytes()
        module = 'data:text/javascript;base64,' + base64.b64encode(source).decode()
        script = f'''
            import {{poseForAgent}} from {json.dumps(module)};
            const cases = [
                [{{state:'busy',pending_approval_count:1}}, 'busy'],
                [{{state:'failed',pending_approval_count:1}}, 'failed'],
                [{{state:'failed',pending_approval_count:0}}, 'failed'],
                [{{state:'idle',pending_approval_count:2}}, 'needs-human'],
                [{{state:'idle',pending_approval_count:0}}, 'idle'],
                [{{state:'idle'}}, 'idle'],
            ];
            for (const [value, expected] of cases) if (poseForAgent(value) !== expected) process.exit(1);
        '''
        subprocess.run(['node', '--input-type=module', '-e', script], check=True)
