"""Resolve provider storage into the shared bootstrap device contract.

This is the only guest-side provider selection point. Each fixed adapter
returns absolute block-device paths for the ``admin`` and ``agent`` roles;
``bootstrap.sh`` consumes those two paths without knowing the provider.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any, Callable

from host.bootstrap import storage_aws, storage_lima


Resolver = Callable[[dict[str, Any]], dict[str, str]]
_RESOLVERS: dict[str, Resolver] = {
    "aws": storage_aws.resolve,
    "lima": storage_lima.resolve,
}


def resolve_payload(payload: object) -> dict[str, str]:
    storage = payload.get("storage") if isinstance(payload, dict) else None
    if not isinstance(storage, dict):
        raise ValueError("bootstrap payload has no storage object")
    name = storage.get("resolver")
    inputs = storage.get("resolver_input")
    if not isinstance(name, str) or name not in _RESOLVERS:
        raise ValueError(f"unsupported storage resolver: {name}")
    if not isinstance(inputs, dict):
        raise ValueError("bootstrap payload has no storage resolver_input object")
    devices = _RESOLVERS[name](inputs)
    if set(devices) != {"admin", "agent"}:
        raise ValueError("storage resolver did not return exactly the admin and agent roles")
    if not all(
        isinstance(device, str) and device.startswith("/dev/")
        for device in devices.values()
    ):
        raise ValueError("storage resolver returned an invalid device path")
    if devices["admin"] == devices["agent"]:
        raise ValueError("storage resolver did not return two distinct role devices")
    return devices


def main() -> int:
    try:
        payload = json.loads(Path("/tmp/kern_payload.json").read_text())
        devices = resolve_payload(payload)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(devices["admin"])
    print(devices["agent"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
