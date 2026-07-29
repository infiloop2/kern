"""systemd ``ExecStopPost`` entry point for abnormal service exits."""

from __future__ import annotations

import os
import sys

from host.runtime.core.host_errors import report_service_exit


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        raise SystemExit("usage: host_errors_service_exit SERVICE")
    report_service_exit(
        args[0],
        os.environ.get("SERVICE_RESULT", ""),
        os.environ.get("EXIT_CODE", ""),
        os.environ.get("EXIT_STATUS", ""),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
