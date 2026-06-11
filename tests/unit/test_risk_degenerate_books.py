"""Risk engine on DEGENERATE order books (FR-RISK-002/004 hardening).

A corrupt or stale feed can produce books that aren't the happy two-sided case:
crossed (best_bid > best_ask), locked (bid == ask), one-sided, empty, or padded
with non-economic zero-price levels. The engine must never crash on any of them
and must stay conservative (reject the anomalous ones), and the depth gate must
reflect only liquidity the matcher could actually take.
"""

from __future__ import annotations

import pytest

from hermes_pm.config import RiskPolicy
from hermes_pm.models import (
    BookLevel,
    Campaign,
    Market,
    OrderBookSnapshot,
    RiskResult,
    Side,
    Signal,
    SourceType,
    TradeIntent,
)
from hermes_pm.risk.engine import RiskContext, RiskEngine

ENGINE = RiskEngine()


def _market():
    return Market(
        market_id="m", event_id="e", condition_id="c", question="q?", category="weather",
        enable_order_book=True, resolution_rules="rules", resolution_source="src",
        token_ids={"YES": "tok"}, end_time="2026-12-31T00:00:00Z",
    )


def _intent():
    return TradeIntent(
        campaign_id="c", market_id="m", token_id="tok", outcome="YES", side=Side.BUY,
        limit_price=0.51, max_size_usd=10.0, thesis="t", counter_thesis="ct",
        invalidation_criteria="inv", evidence_refs=["off://1"], confidence=0.6,
        expires_at="2026-12-30T00:00:00Z",
    )


def _ctx(book):
    return RiskContext(
        intent=_intent(), market=_market(), campaign=Campaign(name="c", bankroll=1000.0),
        policy=RiskPolicy(), book=book, book_is_stale=False, data_age_ms=100,
        evidence=[Signal(market_id="m", source_type=SourceType.PRIMARY, source_ref="off://1",
                         text_summary="x", trust_score=0.9)],
    )


def test_crossed_book_is_rejected():
    # best_bid 0.60 > best_ask 0.50 -> spread -0.10. Must NOT slip through the
    # spread gate as "tight"; it is a corrupt/stale-feed anomaly.
    crossed = OrderBookSnapshot(
        token_id="tok", bids=[BookLevel(price=0.60, size=500.0)],
        asks=[BookLevel(price=0.50, size=500.0)],
    )
    d = ENGINE.evaluate(_ctx(crossed))
    assert d.result is RiskResult.REJECT
    assert "crossed_book" in d.violated_rules
    assert "spread_too_wide" not in d.violated_rules  # not misclassified


def test_locked_book_is_not_flagged_crossed_or_wide():
    # bid == ask (spread 0) is the tightest legitimate book, not an anomaly.
    locked = OrderBookSnapshot(
        token_id="tok",
        bids=[BookLevel(price=0.50, size=500.0)], asks=[BookLevel(price=0.50, size=500.0)],
    )
    d = ENGINE.evaluate(_ctx(locked))
    assert "crossed_book" not in d.violated_rules
    assert "spread_too_wide" not in d.violated_rules


def test_zero_price_levels_do_not_count_as_depth():
    # A non-economic 0-price level (matcher never fills it) must not satisfy the
    # depth gate. Real best ask 0.51 with tiny size; a fat 0-price level padded in.
    padded = OrderBookSnapshot(
        token_id="tok",
        bids=[BookLevel(price=0.49, size=500.0)],
        asks=[BookLevel(price=0.51, size=5.0), BookLevel(price=0.0, size=10_000.0)],
    )
    assert padded.depth_usd(Side.BUY) == pytest.approx(5.0)  # the 10k at price 0 is excluded
    d = ENGINE.evaluate(_ctx(padded))
    assert d.result is RiskResult.REJECT
    assert "insufficient_orderbook_depth" in d.violated_rules


@pytest.mark.parametrize(
    "book",
    [
        None,
        OrderBookSnapshot(token_id="tok", bids=[], asks=[]),                                # empty
        OrderBookSnapshot(token_id="tok", bids=[BookLevel(price=0.49, size=500.0)], asks=[]),  # bid only
        OrderBookSnapshot(token_id="tok", bids=[], asks=[BookLevel(price=0.51, size=500.0)]),  # ask only
        OrderBookSnapshot(token_id="tok", bids=[BookLevel(price=0.60, size=500.0)],
                          asks=[BookLevel(price=0.50, size=500.0)]),                         # crossed
        OrderBookSnapshot(token_id="tok", bids=[BookLevel(price=0.0, size=900.0)],
                          asks=[BookLevel(price=0.0, size=900.0)]),                          # all zero price
        OrderBookSnapshot(token_id="tok", bids=[BookLevel(price=0.01, size=1.0)],
                          asks=[BookLevel(price=0.99, size=1.0)]),                           # huge spread + thin
    ],
)
def test_degenerate_books_never_crash_and_reject(book):
    d = ENGINE.evaluate(_ctx(book))
    # Must always return a decision (no exception) and never APPROVE a degenerate book.
    assert d.result in (RiskResult.REJECT, RiskResult.MODIFY)
    assert d.result is RiskResult.REJECT
    assert d.violated_rules  # at least one machine-readable reason
