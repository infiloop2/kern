"""Discover Workspace mock backends for admin UI smoke tests."""

from __future__ import annotations

from http import HTTPStatus
import importlib.util
from pathlib import Path
import sys
from types import ModuleType
from typing import Any, Callable


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

ApiErrorFactory = Callable[[HTTPStatus, str], Exception]
HostApi = Callable[[str, str, dict[str, list[str]], Any], dict[str, Any]]
WORKSPACE_SMOKE_ROOT = REPO_ROOT / "tests" / "workspaces"
WORKSPACE_ROUTES = {
    "/v1/workspace/chat": ("chat", ""),
    "/v1/workspace/web-apps": ("web_apps", ""),
    "/v1/workspace/memory": ("global", "memory"),
    "/v1/workspace/schedules": ("global", "schedules"),
}
_SMOKE_MODULES: dict[str, ModuleType | None] = {}
_DEMO_MODE = False
_DISMISSED = False


def set_demo_mode(enabled: bool) -> None:
    global _DEMO_MODE
    _DEMO_MODE = enabled


def _set_dismissed() -> None:
    global _DISMISSED
    _DISMISSED = True


def _onboarding_status() -> dict[str, Any]:
    # Demo mode explores the finished checklist; the default empty state is a
    # fresh host, where no step has been reached yet.
    return {
        "provider_ready": _DEMO_MODE,
        "chat_created": _DEMO_MODE,
        "app_created": _DEMO_MODE,
        "schedule_created": _DEMO_MODE,
        "dismissed": _DISMISSED,
    }


def route_workspace_api(
    method: str,
    path: str,
    query: dict[str, list[str]],
    body: Any,
    api_error: ApiErrorFactory,
    host_api: HostApi,
) -> dict[str, Any] | None:
    if method == "GET" and path == "/v1/workspace/getting-started":
        return _onboarding_status()
    if method == "POST" and path == "/v1/workspace/getting-started/dismiss":
        _set_dismissed()
        return _onboarding_status()
    matched = next(
        ((prefix, target) for prefix, target in WORKSPACE_ROUTES.items()
         if path == prefix or path.startswith(prefix + "/")),
        None,
    )
    if matched is None:
        return None
    prefix, (workspace_id, resource) = matched
    suffix = path.removeprefix(prefix).removeprefix("/")
    relative = "/".join(part for part in (resource, suffix) if part)
    module = _load_workspace_smoke(workspace_id)
    handler = None if module is None else getattr(module, "route_workspace_api", None)
    if handler is None:
        raise api_error(
            HTTPStatus.NOT_FOUND, f"mock workspace not found: {workspace_id}"
        )
    return handler(method, relative, query, body, api_error, host_api)


def _load_workspace_smoke(workspace_id: str) -> ModuleType | None:
    if workspace_id in _SMOKE_MODULES:
        return _SMOKE_MODULES[workspace_id]
    smoke_path = WORKSPACE_SMOKE_ROOT / workspace_id / "smoke.py"
    if not smoke_path.is_file():
        _SMOKE_MODULES[workspace_id] = None
        return None
    module_name = f"kern_smoke_{workspace_id}"
    spec = importlib.util.spec_from_file_location(module_name, smoke_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load workspace smoke module: {smoke_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    configure = getattr(module, "configure_mock", None)
    if configure is not None:
        configure(demo_mode=_DEMO_MODE)
    _SMOKE_MODULES[workspace_id] = module
    return module
