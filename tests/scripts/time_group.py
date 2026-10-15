#!/usr/bin/env python3
"""Time a CI command including its subprocess startup and teardown."""
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests/smoke-ui"))
from timing import timed_group

if __name__ == "__main__":
    with timed_group(sys.argv[1]):
        subprocess.run(sys.argv[2:], check=True)
