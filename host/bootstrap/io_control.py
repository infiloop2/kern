"""Enable proportional disk I/O control on Kern's three mounted volumes.

Run as root at deployment and every boot. IOWeight alone does nothing with
the default NVMe scheduler unless the kernel's I/O-cost controller is enabled.
Use its automatic device model, without guessing a device's IOPS or bandwidth.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import sys


MOUNTS = (Path("/"), Path("/mnt/kern-admin"), Path("/mnt/kern-agent"))


def backing_devices(
    mounts: tuple[Path, ...] = MOUNTS,
    sys_dev_block: Path = Path("/sys/dev/block"),
) -> list[str]:
    """Resolve mounted filesystems to whole disks (NVMe or virtio on Lima).

    Device numbers/names can change across boots. Partitions must be resolved
    to their parent disk: I/O-cost operates on the disk's request queue.
    Kern provisions plain block volumes; reject unsupported stacked devices
    instead of claiming a weight is enforced on their underlying disks.
    """
    devices: set[str] = set()
    for mount in mounts:
        dev = mount.stat().st_dev
        node = (sys_dev_block / f"{os.major(dev)}:{os.minor(dev)}").resolve(strict=True)
        if (node / "partition").exists():
            node = node.parent
        if any((node / "slaves").iterdir()):
            raise RuntimeError(f"unsupported stacked block device for {mount}")
        number = (node / "dev").read_text().strip()
        if not re.fullmatch(r"[0-9]+:[0-9]+", number):
            raise RuntimeError(f"invalid block device number for {mount}: {number!r}")
        devices.add(number)
    return sorted(devices)


def enable(cgroup_root: Path = Path("/sys/fs/cgroup")) -> None:
    qos = cgroup_root / "io.cost.qos"
    model = cgroup_root / "io.cost.model"
    if not qos.is_file() or not model.is_file():
        raise RuntimeError("Kern disk protection requires cgroup v2 CONFIG_BLK_CGROUP_IOCOST")
    # Resolve every mount before making changes. Repeating these writes after
    # reboot/reconfigure is safe and also resets obsolete manual model tuning.
    for device in backing_devices():
        model.write_text(f"{device} ctrl=auto\n")
        qos.write_text(f"{device} enable=1 ctrl=auto\n")
        print(f"Kern I/O-cost protection enabled on {device}", flush=True)


def main() -> int:
    try:
        enable()
    except (OSError, RuntimeError) as exc:
        print(f"Kern disk protection failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
