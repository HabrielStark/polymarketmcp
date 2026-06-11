"""Deterministic trade economics (FR-TI-004).

Prices are probabilities in [0, 1] (USD per share that pays 1 on a YES
resolution). Costs are applied pessimistically (FR-PAPER-006):
  * a BUY effectively pays *more* than the quoted price,
  * a SELL effectively receives *less*.

  break-even probability = the outcome probability at which the position's
    expected value is zero, given execution price + costs.
  normalized EV = the agent's edge in probability units: how far the agent's
    probability estimate sits beyond the break-even point (positive = +EV)."""

from __future__ import annotations

from hermes_pm.models import Side


def cost_multiplier(fee_bps: float, slippage_bps: float) -> float:
    return (fee_bps + slippage_bps) / 10_000.0


def effective_price(side: Side, price: float, fee_bps: float, slippage_bps: float) -> float:
    """Execution price after pessimistic costs, clamped to [0, 1]."""
    adj = price * cost_multiplier(fee_bps, slippage_bps)
    eff = price + adj if side is Side.BUY else price - adj
    return max(0.0, min(1.0, round(eff, 6)))


def break_even_probability(
    side: Side, price: float, fee_bps: float, slippage_bps: float
) -> float:
    """Outcome probability needed to break even (= effective cost of a BUY, or
    effective proceeds of a SELL)."""
    return effective_price(side, price, fee_bps, slippage_bps)


def normalized_ev(
    side: Side,
    price: float,
    model_probability: float,
    fee_bps: float,
    slippage_bps: float,
) -> float:
    """Signed edge in probability units. For a BUY: model_prob - break_even.
    For a SELL: break_even - model_prob."""
    be = break_even_probability(side, price, fee_bps, slippage_bps)
    edge = (model_probability - be) if side is Side.BUY else (be - model_probability)
    return round(edge, 6)
