"""Common contract implemented by every agent runtime harness."""

from __future__ import annotations

from typing import Any, Callable, Protocol


AgentMessage = str | dict[str, Any]
MessageHandler = Callable[[AgentMessage], None]
FinishTurn = Callable[[str, str], int]


class ProviderSessionLost(RuntimeError):
    """A durable host thread points at a provider session that no longer exists."""


class ProviderTurnFinishing(RuntimeError):
    """The provider completed while a steer was crossing the transport."""


class HarnessAdapter(Protocol):
    """Uniform runtime surface consumed by turn and account orchestration."""

    @property
    def runtime_type(self) -> str: ...

    @property
    def label(self) -> str: ...

    @property
    def managed_provider(self) -> str | None: ...

    @property
    def oauth_key(self) -> str | None: ...

    @property
    def steerable(self) -> bool: ...

    @property
    def refresh_before_turn(self) -> bool: ...

    @property
    def collect_login_before_probe(self) -> bool: ...

    @property
    def transport_errors(self) -> tuple[type[Exception], ...]: ...

    @property
    def module(self) -> Any: ...

    def account_status(
        self, *, force_provider_probe: bool = False
    ) -> tuple[str, str | None, dict[str, Any] | None]: ...

    def collect_login_completion(self) -> None: ...

    def new_session(
        self,
        thread_id: str,
        on_ready: Callable[[], bool],
        on_session_id: Callable[[str], None],
    ) -> Any: ...

    def run_turn(
        self,
        server: Any,
        input_message: str,
        provider_session_id: str | None,
        model: str,
        effort: str,
        on_message: MessageHandler,
        finish_turn: FinishTurn | None = None,
    ) -> tuple[str, str]: ...


def subprocess_cwd(
    command: list[str], default_command: list[str], agent_cwd: str
) -> str | None:
    """Use the helper's cwd in production and agent cwd for test commands."""
    return None if command == default_command else agent_cwd
