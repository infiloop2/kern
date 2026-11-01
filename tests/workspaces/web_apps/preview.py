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


def lint_ui(ui: dict[str, Any]) -> list[dict[str, str]]:
    """Conservative source checks, not a replacement for the runtime sanitizer."""
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

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            for key, value in attrs:
                if key in {"data-action", "data-enter-action", "data-drop-action"} and value:
                    self.names.add(value)

    parser = Actions()
    parser.names = set()
    parser.feed(ui.get("html", ""))
    # Include generated HTML literals as well as the saved compact shell.
    parser.feed(javascript)
    handlers = set(re.findall(r'''\bapp\s*\.\s*on\s*\(\s*["']([A-Za-z][A-Za-z0-9_-]*)["']''', javascript))
    for name in sorted(parser.names - handlers):
        finding("missing-handler", f"Action {name!r} needs app.on({name!r}, handler)", name)
    for name in sorted(handlers - parser.names):
        finding("unused-handler", f"Handler {name!r} has no HTML action", name)
    node = shutil.which("node")
    if node:
        result = subprocess.run([node, "--check"], input=javascript, text=True, capture_output=True, timeout=10)
        if result.returncode:
            finding("javascript-syntax", "node --check rejected JavaScript", result.stderr.strip())
    return findings


def create_server(ui: dict[str, Any], data: dict[str, Any], collections: dict[str, Any], port: int) -> ThreadingHTTPServer:
    if not 8000 <= port <= 8015:
        raise ValueError("port must be from 8000 to 8015")
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
    return ThreadingHTTPServer(("127.0.0.1", port), mock.Handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ui", type=Path, required=True)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--collection", action="append", default=[], metavar="NAME=ROWS.JSON")
    parser.add_argument("--port", type=int, choices=range(8000, 8016), default=8000)
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
                print(json.dumps(finding, ensure_ascii=False), flush=True)
            if findings:
                return 1
        server = create_server(ui, data, collections, args.port)
    except (ValueError, KeyError, OSError, WorkspaceError) as exc:
        parser.error(str(exc))
    print(f"App preview: http://127.0.0.1:{args.port}/ (mock password: dev; open App preview)", flush=True)
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
