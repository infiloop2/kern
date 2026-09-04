"""The operator-selectable model and effort combinations for agent sessions.

The matrix is checked exactly where a session configuration is chosen —
starting a thread, or sending new work to one — and never where a recorded
configuration is read back. A recorded configuration may predate the current
matrix, and history stays readable: use `session_config_error` on the way in
and `recorded_session_config` on the way out.

The script runtime is part of the same matrix but is offered only where a
schedule is configured: it runs a static bash script instead of a model turn,
so it has nothing to say in a conversation and cannot build a Web App. Its
model and effort are the single fixed pair below, which keeps one shape —
runtime, model, effort — across schedules and threads rather
than making every reader special-case a runtime with no model.
"""

from __future__ import annotations

from typing import Any, Mapping


SCRIPT_RUNTIME = "script"
SCRIPT_MODEL = "bash"
SCRIPT_EFFORT = "fixed"

INTERACTIVE_SESSION_OPTIONS: dict[str, dict[str, tuple[str, ...]]] = {
    "codex": {
        "gpt-5.6-terra": ("high", "max", "ultra"),
        "gpt-5.6-sol": ("high", "max", "ultra"),
        "gpt-5.6-luna": ("high", "max"),
        "gpt-6-astra": ("high", "max", "ultra"),
    },
    # Claude Code also accepts the unversioned aliases (opus, fable, sonnet),
    # but an alias re-points to a new model generation whenever the pinned CLI
    # is upgraded, silently moving existing threads across generations. The
    # catalog names the exact model instead, so the CLI pin never decides which
    # model a thread runs. Threads created under the aliases stay readable but
    # run no further tasks; their rows are preserved, not migrated.
    "claude_code": {
        "claude-opus-5": ("high", "max", "ultracode"),
        "claude-fable-5-1": ("high", "max", "ultracode"),
        "claude-sonnet-5": ("high", "max", "ultracode"),
    },
    # Grok Build's subscription runtime exposes one pinned model in the
    # vendored CLI. These are its wire-level reasoning effort values.
    "grok": {
        "grok-4.6": ("xhigh", "high"),
    },
    # Hermes's headless CLI has no effort flag.
    "hermes": {
        "deepseek.v3.2": ("high",),
        "qwen.qwen3-coder-next": ("high",),
        "moonshotai.kimi-k2.5": ("high",),
    },
}

# Defaults are named deliberately rather than inferred from catalog order.
# Model ordering is presentation, not a capability ranking.
DEFAULT_INTERACTIVE_MODELS: dict[str, str] = {
    "codex": "gpt-5.6-sol",
    "claude_code": "claude-opus-5",
    "grok": "grok-4.6",
    "hermes": "moonshotai.kimi-k2.5",
}

SCRIPT_SESSION_OPTIONS: dict[str, dict[str, tuple[str, ...]]] = {
    SCRIPT_RUNTIME: {SCRIPT_MODEL: (SCRIPT_EFFORT,)},
}

# Every configuration the host can execute. The surfaces below decide which
# part of it they offer.
SESSION_OPTIONS: dict[str, dict[str, tuple[str, ...]]] = {
    **INTERACTIVE_SESSION_OPTIONS,
    **SCRIPT_SESSION_OPTIONS,
}


def public_session_options() -> dict[str, dict[str, list[str]]]:
    """The conversational option matrix, as a fresh mutable payload."""
    return _public(INTERACTIVE_SESSION_OPTIONS)


def schedule_session_options() -> dict[str, dict[str, list[str]]]:
    """The option matrix a schedule may choose from: conversation or script."""
    return _public(SESSION_OPTIONS)


def _public(matrix: Mapping[str, dict[str, tuple[str, ...]]]) -> dict[str, dict[str, list[str]]]:
    return {
        runtime: {model: list(efforts) for model, efforts in models.items()}
        for runtime, models in matrix.items()
    }


def session_config_error(
    runtime: str, model: object, effort: object, *, allow_script: bool = False
) -> str | None:
    """Reject a configuration that may not start a thread or run new work.

    This is the write gate. Applying it to a configuration read back from
    storage would retire the history of every thread whose model left the
    matrix; use `recorded_session_config` there.

    ``allow_script`` is the opt-in for the two callers that own script work —
    the schedule that configures it and the admin API that runs it. A caller
    that leaves it off states that its surface is conversational, so the
    script runtime is rejected there by name rather than by omission.
    """
    offered = SESSION_OPTIONS if allow_script else INTERACTIVE_SESSION_OPTIONS
    models = offered.get(runtime)
    if models is None:
        return "agent_runtime must be one of " + ", ".join(f"'{name}'" for name in offered)
    if not isinstance(model, str) or model not in models:
        return f"model must be one of {', '.join(models)} for {runtime}"
    efforts = models[model]
    if not isinstance(effort, str) or effort not in efforts:
        return f"effort must be one of {', '.join(efforts)} for {model}"
    return None


def recorded_session_config(payload: Mapping[str, Any]) -> tuple[str, str, str] | None:
    """Read a recorded configuration back, or None when the shape is wrong.

    This is the read path. Only the shape is checked: the runtime, model, and
    effort are whatever was recorded when the thread started, which may predate
    the current matrix. Callers raise their own error for the None case so the
    status code stays theirs.
    """
    runtime = payload.get("agent_runtime")
    model = payload.get("model")
    effort = payload.get("effort")
    if not (
        isinstance(runtime, str)
        and runtime
        and isinstance(model, str)
        and model
        and isinstance(effort, str)
        and effort
    ):
        return None
    return runtime, model, effort
