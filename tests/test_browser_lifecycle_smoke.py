"""Failure controls for the AWS/Lima Browser lifecycle probe."""
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tests.smoke import browser_lifecycle as probe


class BrowserLifecycleSmokeTests(unittest.TestCase):
    def run_probe(self, failure=None):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cgroup, proc = root / 'cgroup', root / 'proc'
            cgroup.mkdir()
            private = proc / '10/root/tmp'
            private.mkdir(parents=True)
            (cgroup / 'cgroup.procs').write_text('10')
            state = {'MainPID': '10', 'NRestarts': '0', 'ActiveState': 'active'}
            after = {**state, 'NRestarts': '1'} if failure == 'restart' else state

            def readiness(*args, **kwargs):
                if failure == 'readiness':
                    raise subprocess.CalledProcessError(1, args[0])
                if failure == 'process_leak':
                    (cgroup / 'cgroup.procs').write_text('10\n20')
                if failure == 'file_leak':
                    (private / 'kern-browser-display-fixture').mkdir()
                if failure == 'profile_leak':
                    (private / 'kern-browser-profile-fixture').mkdir()

            with (patch.object(probe, 'CGROUP', cgroup), patch.object(probe, 'PROC', proc),
                  patch.object(probe, 'service', side_effect=[state, after]),
                  patch.object(probe.subprocess, 'run', side_effect=readiness)):
                probe.probe()

    def test_successful_readiness_and_cleanup_pass(self):
        self.run_probe()

    def test_failures_cannot_be_reported_as_success(self):
        for failure in ('restart', 'process_leak', 'file_leak', 'profile_leak'):
            with self.subTest(failure=failure), self.assertRaises(AssertionError):
                self.run_probe(failure)
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_probe('readiness')
