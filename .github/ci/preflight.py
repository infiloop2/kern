"""Catch syntax and metadata errors without installing project dependencies.
Compile tracked Python without importing it or creating bytecode files.
Parse JSON and scan external action pins before the expensive CI environment.
Full typing, YAML policy, dependency audits and tests remain in complete CI.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess


def check(root: Path) -> None:
    paths = subprocess.check_output(
        ["git", "ls-files", "-z"], cwd=root,
    ).decode().split("\0")
    errors = []
    for name in filter(None, paths):
        path = root / name
        if path.suffix not in {".py", ".json", ".yml", ".yaml"}:
            continue
        if not path.resolve().is_relative_to(root.resolve()):
            errors.append(f"{name}: source symlink escapes the checkout")
            continue
        try:
            if path.suffix == ".py":
                compile(path.read_bytes(), name, "exec")
            elif path.suffix == ".json":
                json.loads(path.read_text())
            elif name.startswith(".github/"):
                # A cheap lexical check, not a replacement for the full YAML
                # policy parser. No dependency installation on iteration runs.
                for line in path.read_text().splitlines():
                    match = re.match(r"\s*(?:-\s*)?uses:\s*([^#]+)", line)
                    if not match:
                        continue
                    action = match[1].strip().strip("\"'")
                    if not action.startswith("./") and not re.fullmatch(r"[^@\s]+@[0-9a-f]{40}", action):
                        errors.append(f"{name}: external action must use a full commit SHA: {action}")
        except (SyntaxError, ValueError, OSError) as error:
            errors.append(f"{name}: {error}")
    if errors:
        raise ValueError("\n".join(errors))
    print("Python syntax, JSON and external action pin preflight passed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    check(parser.parse_args().root)
