"""Account-bound training preferences, refreshed by the existing runtime worker."""
from __future__ import annotations

from datetime import datetime
import time
from typing import Any

from host.runtime.agent_runtime import claude_code
from host.runtime.core import state

UNKNOWN_RECHECK_SECONDS = 10 * 60
KNOWN_RECHECK_SECONDS = 24 * 60 * 60


def check_due(previous: dict[str, Any], now: float) -> bool:
    attempted = previous.get("attempted_at")
    if not attempted:
        return True
    successful = (isinstance(previous.get("allowed"), bool)
                  and previous.get("checked_at") == attempted)
    interval = KNOWN_RECHECK_SECONDS if successful else UNKNOWN_RECHECK_SECONDS
    return now - datetime.fromisoformat(attempted).timestamp() >= interval


def probe(runtime_type: str, account: dict[str, Any]) -> bool | None:
    if runtime_type == "claude_code":
        token_hash = account.get("access_token_sha256")
        return claude_code.read_training_allowed(token_hash) if token_hash else None
    # This is fresh auth/info metadata from this refresh's account probe, not
    # the persisted display cache. The two providers use opposite polarities.
    opted_out = account.get("coding_data_retention_opt_out")
    return not opted_out if isinstance(opted_out, bool) else None


def refresh(runtime_type: str, account: dict[str, Any] | None, *, force: bool = False) -> None:
    # Imported here to keep the orchestrator's dependency one-way at import time.
    from host.runtime.agent_runtime.orchestrator import runtime_network_enabled

    if runtime_type not in state.TRAINING_PROVIDERS or account is None:
        return
    now = time.time()
    if not force and not check_due(state.read_training_status(runtime_type), now):
        return
    allowed = probe(runtime_type, account)
    with state.mutation() as cur:
        if runtime_network_enabled(runtime_type):
            state.save_training_status(cur, runtime_type, account, allowed, now)
