"""One real Browser launch/capture/close cycle, run through smoke SSH as root.

The existing admin-UID readiness request launches Chromium inside the deployed
Browser service. Root only observes its process and private-tmp cleanup.
"""
from __future__ import annotations

from pathlib import Path
import subprocess

PROC = Path('/proc')
CGROUP = Path('/sys/fs/cgroup/kern_workspace.slice/kern-browser.service')


def service() -> dict[str, str]:
    result = subprocess.run(
        ['systemctl', 'show', 'kern-browser.service', '-p', 'MainPID', '-p', 'NRestarts', '-p', 'ActiveState'],
        check=True, capture_output=True, text=True, timeout=5,
    )
    return dict(line.split('=', 1) for line in result.stdout.splitlines())


def probe() -> None:
    before = service()
    assert before['ActiveState'] == 'active' and int(before['MainPID']) > 0, before
    temporary = PROC / before['MainPID'] / 'root/tmp'

    def files() -> set[str]:
        return {str(path) for pattern in ('kern-browser-display-*', 'kern-browser-profile-*')
                for path in temporary.glob(pattern)}

    baseline_files = files()
    baseline_pids = set((CGROUP / 'cgroup.procs').read_text().split())
    client = "from host.runtime.browser.client import request; assert request('/operator/ready').get('ready') is True"
    subprocess.run(
        ['runuser', '-u', 'kern-admin', '--', 'env', 'PYTHONPATH=/opt/kern-host', 'python3', '-c', client],
        check=True, timeout=185,
    )
    assert service() == before, 'Browser service restarted or stopped'
    assert set((CGROUP / 'cgroup.procs').read_text().split()) == baseline_pids, 'Browser child processes leaked'
    assert files() == baseline_files, 'Browser temporary display/profile files leaked'


if __name__ == '__main__':
    probe()
    print('Browser launch, screenshot and cleanup passed')
