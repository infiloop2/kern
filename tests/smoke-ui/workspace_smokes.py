"""Discover and run workspace Playwright smoke tests."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import ModuleType
from typing import Any, Iterator


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

WORKSPACE_SMOKE_ROOT = REPO_ROOT / "tests" / "workspaces"
WORKSPACES = ("chat", "web_apps", "global")
_SMOKE_MODULES: dict[str, ModuleType] = {}


def desktop_smoke(page: Any) -> None:
    _run_workspace_smokes("desktop_smoke", page)


def mobile_smoke(page: Any) -> None:
    _run_workspace_smokes("mobile_smoke", page)


def web_app_stylesheet_fallback_smoke(page: Any) -> None:
    smoke = getattr(_load_workspace_smoke("web_apps"), "stylesheet_fallback_smoke", None)
    if smoke is None:
        raise AssertionError("web_apps smoke.py is missing stylesheet_fallback_smoke()")
    smoke(page)


def web_app_worker_startup_smoke(page: Any) -> None:
    smoke = getattr(_load_workspace_smoke("web_apps"), "worker_startup_smoke", None)
    if smoke is None:
        raise AssertionError("web_apps smoke.py is missing worker_startup_smoke()")
    smoke(page)


def _run_workspace_smokes(function_name: str, page: Any) -> None:
    for workspace_id, module in _iter_workspace_smokes():
        smoke = getattr(module, function_name, None)
        if smoke is None:
            raise AssertionError(f"{workspace_id} smoke.py is missing {function_name}()")
        smoke(page)


def _iter_workspace_smokes() -> Iterator[tuple[str, ModuleType]]:
    for workspace_id in WORKSPACES:
        yield workspace_id, _load_workspace_smoke(workspace_id)


def _load_workspace_smoke(workspace_id: str) -> ModuleType:
    if workspace_id in _SMOKE_MODULES:
        return _SMOKE_MODULES[workspace_id]
    smoke_path = WORKSPACE_SMOKE_ROOT / workspace_id / "smoke.py"
    if not smoke_path.is_file():
        raise AssertionError(f"{workspace_id} is missing workspace smoke module {smoke_path}")
    module_name = f"kern_smoke_{workspace_id}"
    spec = importlib.util.spec_from_file_location(module_name, smoke_path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load workspace smoke module: {smoke_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _SMOKE_MODULES[workspace_id] = module
    return module
