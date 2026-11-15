"""Reconcile Claude's step inputs with each stream-json result's usage.

Assistant output counts are placeholders. Result.usage is the main loop's
per-user-turn total; modelUsage is session-cumulative (including resumed work
and subagents) and must not be added to it. See Claude's cost-tracking docs:
https://code.claude.com/docs/en/agent-sdk/cost-tracking
"""
from __future__ import annotations

from typing import Any
from uuid import uuid4

from host.runtime.agent_runtime import token_usage


class ClaudeUsage:
    def __init__(self) -> None:
        self.prefix = f"claude-query:{uuid4()}"
        self.sequence = 0
        self.steps = token_usage.TurnUsage()
        self.partial: dict[str, int | None] | None = None
        self.results: set[str] = set()

    def add(self, message: dict[str, Any]) -> dict[str, Any] | None:
        kind = message.get("type")
        if kind == "assistant":
            response = message.get("message")
            if not isinstance(response, dict) or not isinstance(response.get("usage"), dict):
                return None
            sample = token_usage.record(response.get("id"), {
                **response["usage"], "output_tokens": None,
            }, "claude")
            if sample is None:
                return None
            # The main-loop result excludes children. Keep their measured
            # input separately; their placeholder output remains unknown.
            if message.get("parent_tool_use_id"):
                sample["source_id"] = f"{self.prefix}:child:{sample['source_id']}"
                return sample
            totals = self.steps.add(sample)
            if totals is None:
                return None
            self.partial = totals
            return self._record(totals)
        if kind != "result":
            return None
        result_id = message.get("uuid")
        if isinstance(result_id, str) and result_id:
            if result_id in self.results:
                return None
            self.results.add(result_id)
        sample = token_usage.record("result", message.get("usage"), "claude")
        totals = dict(self.partial or dict.fromkeys(token_usage.FIELDS))
        if sample is not None:
            final = sample["usage"]
            # A crash can zero the result. Do not erase measured step inputs
            # or turn an unavailable output measurement into a measured zero.
            if not (all(value == 0 for value in final.values()) and any(totals.values())):
                totals.update({key: value for key, value in final.items() if value is not None})
        measured = self._record(totals) if any(value is not None for value in totals.values()) else None
        # Steering can produce multiple result frames in one Kern turn. Each
        # gets a separate replacement slot, rather than replacing prior work.
        self.sequence += 1
        self.steps = token_usage.TurnUsage()
        self.partial = None
        return measured

    def _record(self, totals: dict[str, int | None]) -> dict[str, Any]:
        return {"type": "token_usage", "source_id": f"{self.prefix}:{self.sequence}", "usage": totals}
