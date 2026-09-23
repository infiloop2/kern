"""Swarm reflects runtime failures separately from human attention."""
import base64
import json
from pathlib import Path
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which('node'), 'node is required')
class SwarmPoseTests(unittest.TestCase):
    def test_runtime_state_wins_over_human_assessment(self) -> None:
        source = (Path(__file__).parents[1] / 'host/runtime/admin_api/admin_ui/swarm_pose.js').read_bytes()
        module = 'data:text/javascript;base64,' + base64.b64encode(source).decode()
        script = f'''
            import {{poseForAgent}} from {json.dumps(module)};
            const cases = [
                [{{state:'busy',needs_human:true}}, 'busy'],
                [{{state:'failed',needs_human:true}}, 'failed'],
                [{{state:'failed',needs_human:null}}, 'failed'],
                [{{state:'idle',needs_human:true}}, 'needs-human'],
                [{{state:'idle',needs_human:false}}, 'idle'],
                [{{state:'idle',needs_human:null}}, 'idle'],
                [{{state:'idle',pending_approval_count:9,last_error:'old error'}}, 'idle'],
            ];
            for (const [value, expected] of cases) if (poseForAgent(value) !== expected) process.exit(1);
        '''
        subprocess.run(['node', '--input-type=module', '-e', script], check=True)
