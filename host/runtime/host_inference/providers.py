"""Concrete provider functions owned by the host-inference service.

Callers name the provider contract they need. There is deliberately no slot
registry or interchangeable-provider dispatch: OpenAI completion and TypeSafe
Jev judgment have different inputs, guarantees, and feature ownership.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import partial
from typing import Any

from host.runtime.core import host_errors, state
from host.runtime.host_inference import openai, provider_http, typesafe, usage


TextAdapter = Callable[..., dict[str, Any]]
JudgmentAdapter = Callable[..., dict[str, Any]]

OPENAI_MAX_TIMEOUT_SECONDS = 60.0
JEV_MAX_TIMEOUT_SECONDS = 2.0
TYPESAFE_JEV_MODEL = "jev-latest"


class ProviderDisabledError(RuntimeError):
    """The owning inference service has no enabled configuration."""


def openai_text_completion(
    prompt: str,
    schema: dict[str, Any],
    schema_name: str,
    *,
    model: str,
    instructions: str,
    reasoning_effort: str,
    max_output_tokens: int,
    timeout_seconds: float,
    adapter: TextAdapter = openai.complete,
) -> dict[str, Any] | None:
    """Run one bounded OpenAI completion using caller-selected settings."""
    try:
        configured = state.enabled_host_inference_provider("openai")
        if configured is None:
            raise ProviderDisabledError("OpenAI is disabled")
        return adapter(
            api_key=configured["api_key"],
            model=model,
            instructions=instructions,
            reasoning_effort=reasoning_effort,
            max_output_tokens=max_output_tokens,
            prompt=prompt,
            schema_name=schema_name,
            schema=schema,
            transport=partial(provider_http.post, timeout=timeout_seconds),
            usage_recorder=usage.record_openai_response,
        )
    except ProviderDisabledError:
        raise
    except Exception as exc:
        host_errors.report_warning(
            "host_inference.openai_text_completion",
            exc,
            context={"provider": "openai"},
            kind="provider_failure",
        )
        if isinstance(exc, TimeoutError):
            raise
        return None


def typesafe_jev_judgment(
    state_value: Any,
    questions: dict[str, dict[str, Any]],
    *,
    timeout_seconds: float = 2.0,
    adapter: JudgmentAdapter = typesafe.judge,
) -> dict[str, Any] | None:
    """Run one bounded TypeSafe Jev judgment."""
    try:
        configured = state.enabled_host_inference_provider("typesafe")
        if configured is None:
            raise ProviderDisabledError("TypeSafe is disabled")
        return adapter(
            api_key=configured["api_key"],
            model=TYPESAFE_JEV_MODEL,
            state=state_value,
            questions=questions,
            transport=partial(provider_http.post, timeout=timeout_seconds),
            usage_recorder=usage.record_typesafe_response,
        )
    except ProviderDisabledError:
        raise
    except Exception as exc:
        host_errors.report_warning(
            "host_inference.typesafe_jev_judgment",
            exc,
            context={"provider": "typesafe"},
            kind="provider_failure",
        )
        if isinstance(exc, TimeoutError):
            raise
        return None
