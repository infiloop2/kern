#!/usr/bin/env python3
"""Real native Chromium lifecycle/adapters, independent of the admin UI mock."""
from pathlib import Path
import sys

# Resolve host imports without relying on the caller's PYTHONPATH or cwd.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from timing import timed_group


@timed_group("native-browser")
def main():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        with timed_group("native-x-and-storage"):
            import browser_adapter_smoke
            browser_adapter_smoke.run(playwright)
        with timed_group("native-linkedin"):
            import linkedin_adapter_smoke
            linkedin_adapter_smoke.run(playwright)


if __name__ == "__main__":
    main()
