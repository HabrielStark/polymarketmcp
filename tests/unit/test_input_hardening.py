"""Numeric boundary hardening (models + risk policy).

Happy-path validation is decoration; these feed nan / inf / out-of-range /
non-positive values into every externally-influenced numeric field and assert
the construction is REJECTED, while genuinely-safe extreme values (ultra-strict
policy) are still accepted.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from hermes_pm.config import RiskPolicy
from hermes_pm.models import BookLevel, Campaign, OrderBookSnapshot, Token

INF = float("inf")
NAN = float("nan")


# --------------------------------------------------------------------------- #
# Campaign — bankroll/duration drive sizing and end_ms (int() would crash on
# nan/inf), so they must be finite and strictly positive.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", [0.0, -1.0, NAN, INF, -INF])
def test_campaign_rejects_bad_bankroll(bad):
    with pytest.raises(ValidationError):
        Campaign(name="x", bankroll=bad)


@pytest.mark.parametrize("bad", [0.0, -5.0, NAN, INF, -INF])
def test_campaign_rejects_bad_duration(bad):
    with pytest.raises(ValidationError):
        Campaign(name="x", duration_hours=bad)


def test_campaign_valid_end_ms_is_safe():
    c = Campaign(name="x", bankroll=1000.0, duration_hours=48.0)
    # Previously int(float('nan')/'inf') in end_ms could raise; valid input is safe.
    assert c.end_ms > c.start_ms


# --------------------------------------------------------------------------- #
# Order book — prices are probabilities in [0,1], sizes finite & non-negative.
# A 0-price level is allowed (non-economic, the matcher skips it) but inf is not.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "price,size",
    [(NAN, 1.0), (INF, 1.0), (-0.1, 1.0), (1.1, 1.0), (0.5, INF), (0.5, NAN), (0.5, -1.0)],
)
def test_book_level_rejects_bad_numbers(price, size):
    with pytest.raises(ValidationError):
        BookLevel(price=price, size=size)


def test_book_level_accepts_valid_including_zero_price():
    assert BookLevel(price=0.0, size=10.0).size == 10.0
    assert BookLevel(price=0.5, size=100.0).price == 0.5


@pytest.mark.parametrize("bad", [NAN, INF, -0.1, 1.5])
def test_snapshot_last_trade_rejects_bad(bad):
    with pytest.raises(ValidationError):
        OrderBookSnapshot(token_id="t", last_trade=bad)


@pytest.mark.parametrize("field", ["best_bid", "best_ask", "last_trade_price", "spread"])
@pytest.mark.parametrize("bad", [NAN, INF, -0.1, 1.5])
def test_token_price_fields_reject_bad(field, bad):
    with pytest.raises(ValidationError):
        Token(token_id="t", market_id="m", outcome="YES", **{field: bad})


# --------------------------------------------------------------------------- #
# RiskPolicy — the deterministic risk guarantee rests on a sane policy.
# Negative/non-finite values are rejected; ultra-strict values are allowed.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("field", ["fee_bps", "slippage_bps"])
def test_risk_policy_rejects_negative_costs(field):
    # A negative cost would make EV *optimistic* — the core risk-integrity hole.
    with pytest.raises(ValidationError):
        RiskPolicy(**{field: -1.0})


@pytest.mark.parametrize(
    "field",
    ["max_single_trade_risk_pct", "max_market_exposure_pct", "daily_loss_stop_pct",
     "campaign_loss_stop_pct", "max_spread", "min_confidence", "min_orderbook_depth_usd"],
)
def test_risk_policy_rejects_negative(field):
    with pytest.raises(ValidationError):
        RiskPolicy(**{field: -0.01})


@pytest.mark.parametrize("field", ["max_single_trade_risk_pct", "max_spread", "daily_loss_stop_pct"])
def test_risk_policy_rejects_fraction_above_one(field):
    with pytest.raises(ValidationError):
        RiskPolicy(**{field: 1.5})


@pytest.mark.parametrize("bad", [NAN, INF])
def test_risk_policy_rejects_non_finite(bad):
    with pytest.raises(ValidationError):
        RiskPolicy(slippage_bps=bad)


def test_risk_policy_allows_ultra_strict_values():
    # Ultra-pessimistic / ultra-strict is SAFE and must be permitted (tightening).
    p = RiskPolicy(slippage_bps=10_000.0, min_confidence=2.0, min_orderbook_depth_usd=1_000_000.0)
    assert p.slippage_bps == 10_000.0 and p.min_confidence == 2.0


def test_risk_policy_version_stable_and_deterministic():
    assert RiskPolicy().version == RiskPolicy().version
    assert RiskPolicy(slippage_bps=10.0).version != RiskPolicy(slippage_bps=20.0).version
