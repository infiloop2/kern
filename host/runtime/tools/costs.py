"""Call-scoped implementation of the tool cost API; no provider pricing here."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import re
from uuid import uuid4

from host.runtime.core import host_errors, state

_CHARGE_ID = re.compile(r"^[A-Za-z0-9_.:/-]{1,256}$")


class HostCosts:
    def __init__(self, tool_id: str, connection_id: str, action_id: str,
                 reports_cost: bool, origin_thread_id: str | None,
                 approval_id: str | None = None):
        self.tool_id = tool_id
        self.connection_id = connection_id
        self.action_id = action_id
        self.reports_cost = reports_cost
        self.origin_thread_id = origin_thread_id
        self.approval_id = approval_id
        self.execution_id = f"approval:{approval_id}" if approval_id else str(uuid4())

    def _check(self, charge_id: str) -> None:
        if not self.action_id or not self.reports_cost:
            raise ValueError("Tool cost reporting is unavailable for this call.")
        if not isinstance(charge_id, str) or not _CHARGE_ID.fullmatch(charge_id):
            raise ValueError("Invalid tool charge id.")

    def record(self, amount_usd: str, *, charge_id: str = "") -> None:
        charge_id = charge_id or f"call:{self.execution_id}"
        self._check(charge_id)
        try:
            if not isinstance(amount_usd, str) or len(amount_usd) > 64:
                raise ValueError("USD amount must be a decimal string.")
            amount = Decimal(amount_usd)
            if not amount.is_finite() or not 0 <= amount < Decimal("1000000000"):
                raise ValueError("Invalid USD amount.")
            if amount != amount.quantize(Decimal("0.000000001")):
                raise ValueError("USD amount exceeds nine decimal places.")
        except InvalidOperation as exc:
            raise ValueError("Invalid USD amount.") from exc
        try:
            state.record_tool_cost(
                tool_id=self.tool_id, connection_id=self.connection_id, charge_id=charge_id,
                execution_id=self.execution_id, action_id=self.action_id,
                origin_thread_id=self.origin_thread_id, approval_id=self.approval_id,
                amount_nano_usd=int(amount * 1_000_000_000),
            )
        except Exception as exc:
            # A completed paid side effect must not become a retryable failure
            # just because accounting failed. Surface the gap in diagnostics.
            host_errors.report_warning("tools.costs", exc,
                context={"tool_id": self.tool_id, "action_id": self.action_id})
