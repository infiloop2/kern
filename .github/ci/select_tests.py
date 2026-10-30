#!/usr/bin/env python3
"""Select browser suites from the complete PR diff, or all suites on public main."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess

ADMIN = "admin-ui"
WORKSPACE = "workspace-ui"
NATIVE = "native-browser"
ALL = frozenset({ADMIN, WORKSPACE, NATIVE})
UI = frozenset({ADMIN, WORKSPACE})

# These CLI modules are deployment clients, never imported by the UI mock or
# native browser fixtures. Full Python/compile/type checks still cover them.
# External API tool packages supply Home manifests/guides, but do not run inside
# the workspace or native browser. Shared tool contracts are deliberately absent.
API_TOOLS = frozenset({
    "aeo_checker", "ahrefs_domain_rating", "apify", "apify_developer", "brave_search",
    "cloudwatch_logs", "elevenlabs", "fal_ai", "gmail", "google_ads",
    "google_calendar", "google_search", "google_search_console", "ibkr",
    "indexnow", "instagram", "instagram_ads", "instagram_discovery", "linkedin",
    "openai_images", "openrouter", "pagespeed_insights", "polymarket", "prolific",
    "reddit", "reddit_scrapecreators", "runway", "sec_edgar", "seo_metrics_api", "twitter",
    "twitterapi_io", "upwork", "vercel_analytics", "web_fetch", "whatsapp",
    "x_ads", "zoho_mail",
})
WORKSPACE_SMOKES = frozenset({
    "workspace_smokes.py", "app_chat_review_smokes.py",
    "context_notices_smokes.py", "dictation_smokes.py", "navigation_order_smokes.py",
})
NATIVE_SMOKES = frozenset({
    "native_browser_smoke.py", "browser_adapter_smoke.py", "linkedin_adapter_smoke.py",
})
ADMIN_SMOKES = frozenset({
    "overload_smokes.py", "training_privacy_smokes.py", "runtime_account_smokes.py",
    "oauth_poll_smokes.py", "google_ads_smokes.py", "swarm_smokes.py",
    "x_ads_smokes.py", "instagram_ads_smokes.py", "file_preview_smokes.py",
    "integration_config_smokes.py", "seo_tools_smokes.py", "analytics_smokes.py",
    "approval_smokes.py", "auto_approval_smokes.py", "host_inference_usage_smokes.py",
    "tool_cost_usage_smokes.py",
})
UI_RUNNERS = frozenset({
    "admin_ui_smoke.py", "run_admin_ui_mock.py", "workspace_mocks.py", "browser_smokes.py",
})
# These shell/navigation assets host workspaces as well as the admin panels.
SHARED_UI = frozenset({
    "app.js", "api.js", "helpers.js", "index.html", "admin_ui.css",
    "service-worker.js", "install.js", "manifest.webmanifest", "passkeys.js",
    "workspace_last_seen.js", "workspace_reorder.js",
})


def suites_for_path(path: str) -> frozenset[str]:
    parts = Path(path).parts
    if path == "VERSION" or path.startswith("docs/") or path in {"README.md", "LICENSE"}:
        return frozenset()
    if path.startswith("host/cli/") or path in {"tests/test_deploy.py", "tests/test_deploy_lima.py", "tests/test_bootstrap_download.py"}:
        return frozenset()
    if len(parts) >= 4 and parts[:2] == ("host", "tools") and parts[2] in API_TOOLS:
        return frozenset({ADMIN})
    if path.startswith("host/tools/browser/") or path == "host/runtime/browser_network/config.py":
        # Both Chromium admin core and the WebKit browser UI canary consume
        # browser manifests/guides and the network location contract.
        return ALL
    if path.startswith(("host/runtime/browser/", "host/runtime/browser_network/")):
        return frozenset({NATIVE})
    if path == "host/runtime/admin_api/browser.py":
        return ALL
    if path.startswith("host/runtime/admin_api/admin_ui/"):
        return UI if parts[4] in SHARED_UI or parts[4].startswith("browser") else frozenset({ADMIN})
    if path.startswith("host/runtime/workspace/"):
        # Navigation/global resources are also rendered by admin core's Swarm
        # and route-restoration journeys. Chat and generated Apps are isolated.
        return frozenset({WORKSPACE}) if parts[3] in {"chat", "web_apps"} else UI
    if path in {"host/runtime/admin_api/workspace_api.py", "host/runtime/admin_api/workspace_proxy.py"}:
        return UI
    if path.startswith("host/runtime/admin_api/"):
        # Auth, routing, request contracts, config and approvals can affect all
        # families, including native storage/account authentication.
        return ALL
    if path.startswith("tests/smoke-ui/"):
        name = parts[-1]
        if name in NATIVE_SMOKES:
            return frozenset({NATIVE})
        if name in WORKSPACE_SMOKES:
            return frozenset({WORKSPACE})
        if name in ADMIN_SMOKES:
            return frozenset({ADMIN})
        if name in UI_RUNNERS:
            return UI
        # Shared timing and unfamiliar fixtures stay broad until reviewed.
        return ALL
    if path.startswith("tests/workspaces/") or path.startswith(("tests/test_workspace", "tests/test_personal_web_app")):
        return UI
    if path.startswith("tests/test_browser") or path == "tests/browser_fakes.py":
        return frozenset({ADMIN, NATIVE})
    if len(parts) == 2 and parts[0] == "tests" and parts[1].startswith("test_") and parts[1].endswith(".py"):
        # Unit modules aren't browser fixtures. Python always runs the entire
        # unit suite; product-code changes in the same diff select independently.
        return frozenset()
    # CI/runners, bootstrap/dependencies, core/auth/config, proxy/storage and
    # every unclassified input stay broad. No blanket host/** optimization.
    return ALL


def select(paths: list[str]) -> dict[str, list[str]]:
    selected: set[str] = set()
    for path in paths:
        selected.update(suites_for_path(path))
    if not paths:
        selected.update(ALL)
    return {"suite": ["python", *(suite for suite in (ADMIN, WORKSPACE, NATIVE) if suite in selected)]}


def changed_paths(base: str, head: str) -> list[str]:
    for sha in (base, head):
        if not re.fullmatch(r"[0-9a-fA-F]{40}", sha):
            raise ValueError("base and head must be complete commit SHAs")
    merge_base = subprocess.check_output(["git", "merge-base", base, head], text=True).strip()
    # --no-renames reports BOTH paths as deletion/addition; -z preserves spaces,
    # newlines and non-ASCII names. Deleted files still select their old owner.
    diff = subprocess.check_output(["git", "diff", "--name-only", "--no-renames", "-z", merge_base, head, "--"])
    return [os.fsdecode(path) for path in diff.split(b"\0") if path]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base")
    parser.add_argument("--head")
    parser.add_argument("--all", action="store_true", help="Run every suite without file selection")
    args = parser.parse_args(argv)
    if args.all:
        if args.base or args.head:
            parser.error("--all cannot be combined with --base or --head")
        paths = []
        print("All host suites requested; file selection disabled")
    else:
        if not args.base or not args.head:
            parser.error("--base and --head are required without --all")
        paths = changed_paths(args.base, args.head)  # failure must fail the selector job
        print(f"Complete PR diff ({len(paths)} paths): {paths!r}")
    matrix = select(paths)
    print(f"Selected suite matrix: {matrix}")
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
        output.write(f"matrix={json.dumps(matrix)}\n")


if __name__ == "__main__":
    main()
