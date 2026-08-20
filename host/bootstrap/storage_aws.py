"""Resolve AWS EBS role inputs to attached guest block devices."""

from __future__ import annotations

import os
from pathlib import Path
import stat
import time
from typing import Any


def resolve(inputs: dict[str, Any]) -> dict[str, str]:
    volume_ids: dict[str, str] = {}
    for role in ("admin", "agent"):
        role_input = inputs.get(role)
        volume_id = role_input.get("volume_id") if isinstance(role_input, dict) else None
        if not isinstance(volume_id, str) or not volume_id:
            raise ValueError(f"AWS storage role {role} has no volume_id")
        volume_ids[role] = volume_id

    devices: dict[str, str] = {}
    for role in ("admin", "agent"):
        volume_id = volume_ids[role]
        normalized = volume_id.replace("-", "")
        candidates = (
            Path(f"/dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_{normalized}"),
            Path(f"/dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_{volume_id}"),
        )
        for _attempt in range(30):
            match = next((candidate for candidate in candidates if candidate.exists()), None)
            if match is not None:
                device = os.path.realpath(match)
                try:
                    mode = os.stat(device).st_mode
                except OSError as exc:
                    raise ValueError(
                        f"EBS volume {volume_id} device {device} is not accessible: {exc}"
                    ) from exc
                if not stat.S_ISBLK(mode):
                    raise ValueError(
                        f"EBS volume {volume_id} device {device} is not a block device"
                    )
                devices[role] = device
                break
            time.sleep(1)
        else:
            raise ValueError(f"could not find attached EBS volume device for {volume_id}")
    return devices
