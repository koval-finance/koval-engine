"""Pure arithmetic for paper_ohlcv_fixed_v1 fills. No state, no I/O."""

from __future__ import annotations


def adjusted_price(
    reference: float, *, side: str, fraction: float, limit: float | None = None
) -> float:
    sign = 1.0 if side == "buy" else -1.0
    adjusted = float(reference) * (1.0 + sign * float(fraction))
    if limit is None:
        return adjusted
    return min(adjusted, float(limit)) if side == "buy" else max(adjusted, float(limit))


def cost_split(
    reference: float,
    fill: float,
    quantity: float,
    *,
    spread_bps: float,
    slippage_bps: float,
) -> tuple[float, float]:
    total = abs(float(quantity)) * abs(float(fill) - float(reference))
    weight = float(spread_bps) / 2 + float(slippage_bps)
    if total == 0.0 or weight == 0.0:
        return 0.0, 0.0
    spread = total * (float(spread_bps) / 2 / weight)
    return spread, total - spread


def commission(quantity: float, fill: float, *, commission_bps: float) -> float:
    return abs(float(quantity)) * float(fill) * float(commission_bps) / 10_000


def required_margin(quantity: float, reference: float, *, leverage: float) -> float:
    return abs(float(quantity)) * float(reference) / float(leverage)


def max_quantity_for_stop_risk(
    *,
    risk_budget: float,
    entry_fill: float,
    stop_reference: float,
    position_side: str,
    adjustment_fraction: float,
    commission_bps: float | None = None,
    entry_commission_bps: float | None = None,
    exit_commission_bps: float | None = None,
) -> float:
    """Largest quantity whose modeled stop outcome stays within ``risk_budget``."""
    closing_side = "sell" if position_side == "buy" else "buy"
    stop_fill = adjusted_price(
        stop_reference,
        side=closing_side,
        fraction=adjustment_fraction,
    )
    price_loss = abs(float(entry_fill) - stop_fill)
    if commission_bps is not None:
        if entry_commission_bps is not None or exit_commission_bps is not None:
            raise ValueError("use commission_bps or role-specific commission rates, not both")
        entry_commission_bps = exit_commission_bps = float(commission_bps)
    if entry_commission_bps is None or exit_commission_bps is None:
        raise ValueError("entry and exit commission rates are required")
    fee_per_unit = (
        float(entry_fill) * float(entry_commission_bps) + stop_fill * float(exit_commission_bps)
    ) / 10_000
    risk_per_unit = price_loss + fee_per_unit
    if risk_per_unit <= 0:
        return 0.0
    return max(0.0, float(risk_budget)) / risk_per_unit
