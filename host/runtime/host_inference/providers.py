"""Concrete provider functions owned by the host-inference service.

Callers name the provider contract they need. There is deliberately no slot
registry or interchangeable-provider dispatch: OpenAI completion and TypeSafe
Jev judgment have different inputs, guarantees, and feature ownership.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from host.runtime.core import host_errors, state
from host.runtime.host_inference import openai, typesafe, usage


TextAdapter = Callable[..., dict[str, Any]]
JudgmentAdapter = Callable[..., dict[str, Any]]

# Models are reviewed host implementation details. A feature names its purpose;
# the operator never has to choose a model or coordinate model changes.
OPENAI_MODEL_BY_PURPOSE = {"swarm_status": "gpt-5.6-luna"}
TYPESAFE_JEV_MODEL = "jev-latest"


def openai_text_completion(
    prompt: str,
    schema: dict[str, Any],
    schema_name: str,
    *,
    purpose: str,
    adapter: TextAdapter = openai.complete,
) -> dict[str, Any] | None:
    """Run one bounded OpenAI completion for a known host feature purpose."""
    try:
        model = OPENAI_MODEL_BY_PURPOSE[purpose]
        configured = state.enabled_host_inference_provider("openai")
        if configured is None:
            return None
        return adapter(
            api_key=configured["api_key"],
            model=model,
            prompt=prompt,
            schema_name=schema_name,
            schema=schema,
            usage_recorder=usage.record_openai_response,
        )
    except Exception as exc:
        host_errors.report_warning(
            "host_inference.openai_text_completion",
            exc,
            context={"provider": "openai", "purpose": purpose},
            kind="provider_failure",
        )
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
            return None
        return adapter(
            api_key=configured["api_key"],
            model=TYPESAFE_JEV_MODEL,
            state=state_value,
            questions=questions,
            timeout_seconds=timeout_seconds,
            usage_recorder=usage.record_typesafe_response,
        )
    except Exception as exc:
        host_errors.report_warning(
            "host_inference.typesafe_jev_judgment",
            exc,
            context={"provider": "typesafe"},
            kind="provider_failure",
        )
        return None
