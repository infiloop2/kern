"""Resolve Lima named-disk role inputs to attached guest block devices."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
from typing import Any


_METADATA_PATH = Path("/run/kern-provider/lima-disks.json")


def resolve(inputs: dict[str, Any]) -> dict[str, str]:
    disk_names: dict[str, str] = {}
    for role in ("admin", "agent"):
        role_input = inputs.get(role)
        disk_name = role_input.get("disk_name") if isinstance(role_input, dict) else None
        if not isinstance(disk_name, str) or not disk_name:
            raise ValueError(f"Lima storage role {role} has no disk_name")
        disk_names[role] = disk_name

    if not _METADATA_PATH.exists():
        raise ValueError(
            f"missing {_METADATA_PATH}; the Lima disk-metadata provision script did not run"
        )
    data = json.loads(_METADATA_PATH.read_text())
    entries = data.get("disks") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise ValueError(f"{_METADATA_PATH} does not contain a disk list")
    devices: dict[str, str] = {}
    for role in ("admin", "agent"):
        wanted = disk_names[role]
        matches = [
            entry.get("device")
            for entry in entries
            if isinstance(entry, dict) and entry.get("name") == wanted
        ]
        if len(matches) != 1 or not isinstance(matches[0], str) or not matches[0]:
            raise ValueError(f"expected exactly one Lima disk named {wanted!r}, found {len(matches)}")
        device = os.path.realpath(matches[0])
        try:
            mode = os.stat(device).st_mode
        except OSError as exc:
            raise ValueError(f"Lima disk {wanted!r} device {device} is not accessible: {exc}") from exc
        if not stat.S_ISBLK(mode):
            raise ValueError(f"Lima disk {wanted!r} device {device} is not a block device")
        devices[role] = device
    return devices
