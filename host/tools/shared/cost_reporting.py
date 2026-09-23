"""Tool-side arithmetic for providers that meter units at a known price."""

from decimal import Decimal, DecimalException, ROUND_HALF_UP

from host.tools.host_api import HostAPI


def report_provider_usd(api: HostAPI, amount: object, *, charge_id: str = "") -> None:
    """Record a calculated USD amount when it fits the host ledger."""
    if isinstance(amount, bool) or not isinstance(amount, (Decimal, int, float, str)):
        return
    try:
        value = Decimal(str(amount)).quantize(Decimal("0.000000001"), rounding=ROUND_HALF_UP)
    except (DecimalException, ValueError):
        return
    if value.is_finite() and 0 <= value < Decimal("1000000000"):
        api.costs.record(format(value, "f"), charge_id=charge_id)


def report_priced_units(api: HostAPI, units: object, usd_per_unit: str, *, charge_id: str = "") -> None:
    """Report a charge only when billed units and a published USD rate are known."""
    if isinstance(units, bool) or not isinstance(units, (int, float, str)):
        return
    try:
        count = Decimal(str(units))
        price = Decimal(usd_per_unit)
        amount = (count * price).quantize(Decimal("0.000000001"), rounding=ROUND_HALF_UP)
    except (DecimalException, ValueError):
        return
    if not count.is_finite() or not price.is_finite() or count < 0 or price < 0:
        return
    if amount >= Decimal("1000000000"):
        return
    api.costs.record(format(amount, "f"), charge_id=charge_id)
