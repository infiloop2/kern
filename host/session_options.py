"""The operator-selectable model and effort combinations for agent sessions.

The matrix is checked exactly where a session configuration is chosen —
starting a thread, or sending new work to one — and never where a recorded
configuration is read back. A recorded configuration may predate the current
matrix, and history stays readable: use `session_config_error` on the way in
and `recorded_session_config` on the way out.
"""

from __future__ import annotations

from typing import Any, Mapping


SESSION_OPTIONS: dict[str, dict[str, tuple[str, ...]]] = {
    "codex": {
        "gpt-5.6-terra": ("high", "max", "ultra"),
        "gpt-5.6-sol": ("high", "max", "ultra"),
        "gpt-5.6-luna": ("high", "max"),
    },
    # Claude Code also accepts the unversioned aliases (opus, fable, sonnet),
    # but an alias re-points to a new model generation whenever the pinned CLI
    # is upgraded, silently moving existing threads across generations. The
    # catalog names the exact model instead, so the CLI pin never decides which
    # model a thread runs. Threads created under the aliases stay readable but
    # run no further tasks; their rows are preserved, not migrated.
    "claude_code": {
        "claude-opus-5": ("high", "max", "ultracode"),
        "claude-fable-5": ("high", "max", "ultracode"),
        "claude-sonnet-5": ("high", "max", "ultracode"),
    },
    # Hermes's headless CLI has no effort flag.
    "hermes": {
        "deepseek.v3.2": ("high",),
        "qwen.qwen3-coder-next": ("high",),
        "moonshotai.kimi-k2.5": ("high",),
    },
}


def public_session_options() -> dict[str, dict[str, list[str]]]:
    """Return the JSON-facing option matrix as a fresh mutable payload."""
    return {
        runtime: {model: list(efforts) for model, efforts in models.items()}
        for runtime, models in SESSION_OPTIONS.items()
    }


def session_config_error(runtime: str, model: object, effort: object) -> str | None:
    """Reject a configuration that may not start a thread or run new work.

    This is the write gate. Applying it to a configuration read back from
    storage would retire the history of every thread whose model left the
    matrix; use `recorded_session_config` there.
    """
    models = SESSION_OPTIONS.get(runtime)
    if models is None:
        return "agent_runtime must be one of " + ", ".join(f"'{name}'" for name in SESSION_OPTIONS)
    if not isinstance(model, str) or model not in models:
        return f"model must be one of {', '.join(models)} for {runtime}"
    efforts = models[model]
    if not isinstance(effort, str) or effort not in efforts:
        return f"effort must be one of {', '.join(efforts)} for {model}"
    return None


def offered_session_configs() -> list[tuple[str, str, str]]:
    """Every (runtime, model, effort) the matrix currently offers."""
    return [
        (runtime, model, effort)
        for runtime, models in SESSION_OPTIONS.items()
        for model, efforts in models.items()
        for effort in efforts
    ]


def recorded_session_config(payload: Mapping[str, Any]) -> tuple[str, str, str] | None:
    """Read a recorded configuration back, or None when the shape is wrong.

    This is the read path. Only the shape is checked: the runtime, model, and
    effort are whatever was recorded when the thread started, which may predate
    the current matrix. Callers raise their own error for the None case so the
    status code stays theirs.
    """
    values = tuple(payload.get(field) for field in ("agent_runtime", "model", "effort"))
    if not all(isinstance(value, str) and value for value in values):
        return None
    runtime, model, effort = values
    assert isinstance(runtime, str) and isinstance(model, str) and isinstance(effort, str)
    return runtime, model, effort
