"""Registry of runtime harness implementations and their capabilities."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from host.runtime.agent_runtime import (
    claude_code,
    codex_app_server,
    grok_agent,
    hermes_agent,
    script_runner,
)
from host.runtime.agent_runtime.harness import FinishTurn, HarnessAdapter, MessageHandler


StatusProbe = Callable[[bool], tuple[str, str | None, dict[str, Any] | None]]
SessionFactory = Callable[[str, Callable[[], bool], Callable[[str], None]], Any]
TurnRunner = Callable[
    [Any, str, str | None, str, str, MessageHandler, FinishTurn | None],
    tuple[str, str],
]


@dataclass(frozen=True)
class ModuleHarnessAdapter:
    runtime_type: str
    label: str
    managed_provider: str | None
    oauth_key: str | None
    steerable: bool
    refresh_before_turn: bool
    collect_login_before_probe: bool
    transport_errors: tuple[type[Exception], ...]
    module: Any
    _status_probe: StatusProbe
    _session_factory: SessionFactory
    _turn_runner: TurnRunner
    _collect_login_completion: Callable[[], None] = lambda: None

    def account_status(
        self, *, force_provider_probe: bool = False
    ) -> tuple[str, str | None, dict[str, Any] | None]:
        return self._status_probe(force_provider_probe)

    def collect_login_completion(self) -> None:
        self._collect_login_completion()

    def new_session(
        self,
        thread_id: str,
        on_ready: Callable[[], bool],
        on_session_id: Callable[[str], None],
    ) -> Any:
        return self._session_factory(thread_id, on_ready, on_session_id)

    def run_turn(
        self,
        server: Any,
        input_message: str,
        provider_session_id: str | None,
        model: str,
        effort: str,
        on_message: MessageHandler,
        finish_turn: FinishTurn | None = None,
    ) -> tuple[str, str]:
        return self._turn_runner(
            server,
            input_message,
            provider_session_id,
            model,
            effort,
            on_message,
            finish_turn,
        )


HARNESSES: dict[str, HarnessAdapter] = {
    "codex": ModuleHarnessAdapter(
        "codex", "Codex", "openai", "codex", True, False, False,
        (codex_app_server.CodexAppServerError,),
        codex_app_server,
        lambda force: (
            codex_app_server.account_status(force_provider_probe=True)
            if force
            else codex_app_server.account_status()
        ),
        lambda thread_id, on_ready, on_session_id: codex_app_server.CodexAppServer(
            thread_id=thread_id, on_ready=on_ready, on_session_id=on_session_id
        ),
        lambda server, message, session_id, model, effort, on_message, finish: codex_app_server.run_turn(
            server, message, session_id, model, effort, on_message
        ),
    ),
    "claude_code": ModuleHarnessAdapter(
        "claude_code", "Claude Code", "claude", "claude", True, True, False,
        (claude_code.ClaudeCodeError,),
        claude_code,
        lambda force: claude_code.account_status(),
        lambda thread_id, on_ready, on_session_id: claude_code.ClaudeCodeSession(
            thread_id=thread_id, on_ready=on_ready, on_session_id=on_session_id
        ),
        lambda server, message, session_id, model, effort, on_message, finish: claude_code.run_turn(
            server, message, session_id, model, effort, on_message, finish
        ),
    ),
    "grok": ModuleHarnessAdapter(
        "grok", "Grok", "xai", "grok", True, False, True,
        (grok_agent.GrokAgentError,),
        grok_agent,
        lambda force: (
            grok_agent.account_status(force_provider_probe=True)
            if force
            else grok_agent.account_status()
        ),
        lambda thread_id, on_ready, on_session_id: grok_agent.GrokAcpServer(
            thread_id=thread_id, on_ready=on_ready, on_session_id=on_session_id
        ),
        lambda server, message, session_id, model, effort, on_message, finish: grok_agent.run_turn(
            server, message, session_id, model, effort, on_message
        ),
        lambda: grok_agent.collect_login_completion(),
    ),
    "hermes": ModuleHarnessAdapter(
        "hermes", "Hermes", "bedrock", None, False, False, False,
        (hermes_agent.HermesAgentError,),
        hermes_agent,
        lambda force: hermes_agent.account_status(),
        lambda thread_id, on_ready, on_session_id: hermes_agent.HermesSession(
            thread_id=thread_id, on_ready=on_ready, on_session_id=on_session_id
        ),
        lambda server, message, session_id, model, effort, on_message, finish: hermes_agent.run_turn(
            server, message, session_id, model, effort, on_message
        ),
    ),
    "script": ModuleHarnessAdapter(
        "script", "Script", None, None, False, False, False,
        (script_runner.ScriptRunError,),
        script_runner,
        lambda force: script_runner.account_status(),
        lambda thread_id, on_ready, on_session_id: script_runner.ScriptSession(
            thread_id=thread_id, on_ready=on_ready, on_session_id=on_session_id
        ),
        lambda server, message, session_id, model, effort, on_message, finish: script_runner.run_turn(
            server, message, session_id, model, effort, on_message
        ),
    ),
}


def harness_adapter(runtime_type: str) -> HarnessAdapter:
    try:
        return HARNESSES[runtime_type]
    except KeyError as exc:
        raise ValueError(f"unknown agent runtime: {runtime_type}") from exc
