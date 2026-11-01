#!/usr/bin/env python3
"""Preview saved App state with the real builder and the in-memory smoke backend."""
from __future__ import annotations

import argparse
from html.parser import HTMLParser
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from host.runtime.workspace.host_api import WorkspaceError


BLOCKING_LINT_CODES = frozenset({"forbidden-global", "missing-onload", "javascript-syntax"})
PREVIEW_PORTS = range(8000, 8016)
_STATIC_ACTION = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")


def lint_ui(ui: dict[str, Any]) -> list[dict[str, str]]:
    """Conservative source checks, not a replacement for the runtime sanitizer.

    Codes in BLOCKING_LINT_CODES stop the preview. Handler cross-reference codes
    (missing-handler, unused-handler) are warnings: generated action names and
    loop registrations cannot be resolved from source, so they are skipped when
    the code builds action names dynamically.
    """
    javascript = ui.get("javascript", "")
    findings = []

    def finding(code: str, message: str, snippet: str) -> None:
        findings.append({"code": code, "message": message, "snippet": snippet[:200]})

    forbidden = r"\b(?:fetch|XMLHttpRequest|WebSocket|setTimeout|setInterval|importScripts|localStorage|indexedDB)\b|\b(?:document|window|navigator)\s*\."
    for match in re.finditer(forbidden, javascript):
        finding("forbidden-global", f"Unavailable worker global: {match.group()}", javascript[max(0, match.start()-30):match.end()+50])
    if not re.search(r"\bapp\s*\.\s*onLoad\s*\(", javascript):
        finding("missing-onload", "Register app.onLoad(handler), including in targeted mode", javascript[:100])

    class Actions(HTMLParser):
        names: set[str] = set()
        dynamic = False

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            for key, value in attrs:
                if key in {"data-action", "data-enter-action", "data-drop-action"} and value:
                    if _STATIC_ACTION.fullmatch(value):
                        self.names.add(value)
                    else:
                        self.dynamic = True  # template expression or concatenation

    parser = Actions()
    parser.names = set()
    parser.feed(ui.get("html", ""))
    # Include generated HTML literals as well as the saved compact shell.
    parser.feed(javascript)
    # Count registrations through app.on and through a local alias such as on(name, fn).
    registration = r"""(?<![A-Za-z0-9_$.])(?:app\s*\.\s*)?on\s*\(\s*"""
    handlers = set(re.findall(registration + r"""["']([A-Za-z][A-Za-z0-9_-]*)["']""", javascript))
    dynamic_handlers = any(not re.match(r"""["']""", javascript[match.end():match.end() + 1]) for match in re.finditer(registration, javascript))
    if not dynamic_handlers:
        for name in sorted(parser.names - handlers):
            finding("missing-handler", f"Action {name!r} needs app.on({name!r}, handler)", name)
    if not parser.dynamic:
        for name in sorted(handlers - parser.names):
            finding("unused-handler", f"Handler {name!r} has no HTML action", name)
    node = shutil.which("node")
    if node:
        result = subprocess.run([node, "--check"], input=javascript, text=True, capture_output=True, timeout=10)
        if result.returncode:
            finding("javascript-syntax", "node --check rejected JavaScript", result.stderr.strip())
    return findings


def create_server(ui: dict[str, Any], data: dict[str, Any], collections: dict[str, Any], port: int) -> ThreadingHTTPServer:
    """Serve the saved App on a loopback preview port; port 0 takes the first free one."""
    if port != 0 and port not in PREVIEW_PORTS:
        raise ValueError("port must be 0 (first free) or from 8000 to 8015")
    sys.path.insert(0, str(REPO_ROOT / "tests/smoke-ui"))
    import run_admin_ui_mock as mock
    from tests.workspaces.web_apps import smoke

    mock.seed_state()
    mock.seed_workspace_smoke_state()
    mock.workspace_mocks._SMOKE_MODULES["web_apps"] = smoke
    smoke.reset_mock_state()
    smoke._create_app()
    workspace = smoke.WORKSPACES["app-1"]
    workspace["name"] = "App preview"
    if ui["revision"] != data["revision"]:
        raise ValueError("UI and data revisions differ; fetch a consistent saved App")
    workspace["app"].update(ui)
    workspace["app"]["data"] = data["data"]
    workspace["collections"] = collections
    for candidate in [port] if port else PREVIEW_PORTS:
        try:
            return ThreadingHTTPServer(("127.0.0.1", candidate), mock.Handler)
        except OSError:
            if port:
                raise
    raise OSError("every preview port from 8000 to 8015 is in use")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ui", type=Path, required=True)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--collection", action="append", default=[], metavar="NAME=ROWS.JSON")
    parser.add_argument("--port", type=int, choices=[0, *PREVIEW_PORTS], default=0, help="0 (default) picks the first free port from 8000 to 8015")
    parser.add_argument("--lint", action="store_true")
    args = parser.parse_args(argv)
    try:
        ui_body = json.loads(args.ui.read_text())
        data_body = json.loads(args.data.read_text())
        for label, body in (("UI", ui_body), ("data", data_body)):
            if not isinstance(body, dict) or not isinstance(body.get("app"), dict):
                raise ValueError(f"{label} file must contain an object with an app object")
            revision = body["app"].get("revision")
            if type(revision) is not int or revision < 0:
                raise ValueError(f"{label} app.revision must be a nonnegative integer")
        ui = ui_body["app"]
        data = data_body["app"]
        for field in ("html", "css", "javascript"):
            if not isinstance(ui.get(field), str):
                raise ValueError(f"UI app.{field} must be a string")
        if "data" not in data:
            raise ValueError("data file must contain app.data")
        collections = {}
        for item in args.collection:
            name, path = item.split("=", 1)
            from host.runtime.workspace.web_apps.collections import _validated_collection_name
            _validated_collection_name(name)
            rows = json.loads(Path(path).read_text())
            if not isinstance(rows, list) or any(not isinstance(row, dict) or not isinstance(row.get("value"), dict) or not isinstance(row.get("id"), str) for row in rows):
                raise ValueError("collection files must contain [{id, value}, ...]")
            collections[name] = rows
        if args.lint:
            findings = lint_ui(ui)
            for finding in findings:
                severity = "error" if finding["code"] in BLOCKING_LINT_CODES else "warning"
                print(json.dumps({"severity": severity, **finding}, ensure_ascii=False), flush=True)
            if any(finding["code"] in BLOCKING_LINT_CODES for finding in findings):
                return 1
        server = create_server(ui, data, collections, args.port)
    except (ValueError, KeyError, OSError, WorkspaceError) as exc:
        parser.error(str(exc))
    port = server.server_address[1]
    print(f"App preview: http://127.0.0.1:{port}/ (mock password: dev; open App preview)", flush=True)
    print("Writes affect this in-memory mock only. Stop with Ctrl-C.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
