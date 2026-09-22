"""Dedicated egress-capable service for host-owned AI providers."""

from __future__ import annotations

from host.runtime.host_inference import api


def main() -> int:
    api.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
