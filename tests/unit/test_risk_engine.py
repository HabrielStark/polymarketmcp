"""Unit tests: deterministic risk engine (FR-RISK-001..007, Section 14)."""

from __future__ import annotations

import pytest

from hermes_pm.config import RiskPolicy
from hermes_pm.models import (
    BookLevel,
    Campaign,
    Market,
    Mode,
    OrderBookSnapshot,
    Side,
    Signal,
    SourceType,
    TradeIntent,
)
from hermes_pm.risk.engine import RiskContext, RiskEngine
from hermes_pm.util.timeutil import now_ms

ENGINE = RiskEngine()


def _market(**kw):
    base = dict(market_id="m", event_id="e", condition_id="c", question="q?", category="weather",
                enable_order_book=True, resolution_rules="rules", resolution_source="src",
                token_ids={"YES": "tok"}, end_time="2026-12-31T00:00:00Z")
    base.update(kw)
    return Market(**base)


def _intent(**kw):
    base = dict(campaign_id="c", market_id="m", token_id="tok", outcome="YES", side=Side.BUY,
                limit_price=0.51, max_size_usd=10.0, thesis="t", counter_thesis="ct",
                invalidation_criteria="inv", evidence_refs=["off://1"], confidence=0.6,
                expires_at="2026-12-30T00:00:00Z")
    base.update(kw)
    return TradeIntent(**base)


def _book(bid=0.49, ask=0.51, size=500.0):
    return OrderBookSnapshot(
        token_id="tok",
        bids=[BookLevel(price=bid, size=size), BookLevel(price=round(bid - 0.01, 2), size=size)],
        asks=[BookLevel(price=ask, size=size), BookLevel(price=round(ask + 0.01, 2), size=size)],
    )


def _ctx(intent=None, market=None, policy=None, book=None, **kw):
    defaults = dict(
        intent=intent or _intent(), market=market or _market(), campaign=Campaign(name="c", bankroll=1000),
        policy=policy or RiskPolicy(), book=book or _book(), book_is_stale=False, data_age_ms=100,
        evidence=[Signal(market_id="m", source_type=SourceType.PRIMARY, source_ref="off://1",
                         text_summary="x", trust_score=0.9)],
    )
    defaults.update(kw)
    return RiskContext(**defaults)


def test_approve_clean_intent():
    d = ENGINE.evaluate(_ctx())
    assert d.result.value == "approve"
    assert d.approved_size_usd == 10.0
    assert d.policy_version == RiskPolicy().version


def test_modify_size_capped_to_one_percent():
    d = ENGINE.evaluate(_ctx(intent=_intent(max_size_usd=80.0)))
    assert d.result.value == "modify"
    assert d.approved_size_usd == pytest.approx(10.0)  # 1% of 1000


def test_reject_stale_data():
    d = ENGINE.evaluate(_ctx(book_is_stale=True, data_age_ms=99999))
    assert d.result.value == "reject"
    assert "stale_market_data" in d.violated_rules


def test_reject_ambiguous_resolution():
    d = ENGINE.evaluate(_ctx(market=_market(resolution_rules="")))
    assert "ambiguous_or_missing_resolution_rules" in d.violated_rules


def test_reject_wide_spread():
    d = ENGINE.evaluate(_ctx(book=_book(bid=0.30, ask=0.70)))
    assert "spread_too_wide" in d.violated_rules


def test_reject_thin_depth():
    d = ENGINE.evaluate(_ctx(book=_book(size=10.0)))
    assert "insufficient_orderbook_depth" in d.violated_rules


def test_reject_no_two_sided_market():
    one_sided = OrderBookSnapshot(token_id="tok", bids=[BookLevel(price=0.49, size=500)], asks=[])
    d = ENGINE.evaluate(_ctx(book=one_sided))
    assert "no_two_sided_market" in d.violated_rules


def test_reject_insufficient_evidence_social_only():
    soc = [Signal(market_id="m", source_type=SourceType.SOCIAL, source_ref="x://1",
                  text_summary="viral", trust_score=0.2)]
    d = ENGINE.evaluate(_ctx(evidence=soc))
    assert "insufficient_evidence" in d.violated_rules


def test_two_secondary_sources_satisfy_evidence():
    sec = [Signal(market_id="m", source_type=SourceType.SECONDARY, source_ref=f"n://{i}",
                  text_summary="report", trust_score=0.6) for i in range(2)]
    d = ENGINE.evaluate(_ctx(evidence=sec))
    assert "insufficient_evidence" not in d.violated_rules


def test_reject_tainted_evidence():
    tainted = [Signal(market_id="m", source_type=SourceType.PRIMARY, source_ref="off://1",
                      text_summary="x", trust_score=0.9, suspected_injection=True)]
    d = ENGINE.evaluate(_ctx(evidence=tainted))
    assert "tainted_evidence_suspected_injection" in d.violated_rules


def test_reject_missing_counter_thesis():
    d = ENGINE.evaluate(_ctx(intent=_intent(counter_thesis="")))
    assert "missing_counter_thesis" in d.violated_rules


def test_reject_daily_loss_stop():
    d = ENGINE.evaluate(_ctx(realized_pnl_today=-60.0))  # > 5% of 1000
    assert "daily_loss_stop_hit" in d.violated_rules


def test_reject_campaign_loss_stop():
    d = ENGINE.evaluate(_ctx(realized_pnl_campaign=-150.0))  # > 10% of 1000
    assert "campaign_loss_stop_hit" in d.violated_rules


def test_market_exposure_cap_reduces_size():
    d = ENGINE.evaluate(_ctx(intent=_intent(max_size_usd=10.0), market_exposure_usd=48.0))
    # market cap 5% of 1000 = 50; remaining 2 -> modify down
    assert d.result.value == "modify"
    assert d.approved_size_usd == pytest.approx(2.0)


def test_exposure_exhausted_rejects():
    d = ENGINE.evaluate(_ctx(market_exposure_usd=50.0))
    assert "exposure_capacity_exhausted" in d.violated_rules


def test_no_martingale_after_loss_caps_to_prior_size():
    d = ENGINE.evaluate(_ctx(intent=_intent(max_size_usd=10.0), market_recent_loss=True,
                             last_size_on_market_usd=4.0))
    assert d.approved_size_usd == pytest.approx(4.0)


def test_source_stale_for_horizon():
    # evidence issued long ago relative to near horizon
    near = _market(end_time=ms_to_iso_soon())
    old = [Signal(market_id="m", source_type=SourceType.PRIMARY, source_ref="off://1",
                  text_summary="x", trust_score=0.9, issued_at=now_ms() - 10_000_000)]
    d = ENGINE.evaluate(_ctx(market=near, evidence=old))
    assert "evidence_stale_for_horizon" in d.violated_rules


def test_determinism_same_inputs_same_output():
    ctx = _ctx()
    a, b = ENGINE.evaluate(ctx), ENGINE.evaluate(ctx)
    assert (a.result, a.approved_size_usd, a.violated_rules) == (b.result, b.approved_size_usd, b.violated_rules)


def test_live_mode_requires_confirmations():
    d = ENGINE.evaluate(_ctx(campaign=Campaign(name="c", bankroll=1000, mode=Mode.LIVE_ELIGIBLE)))
    assert "explicit_live_confirmation" in d.required_user_confirmations


def test_policy_version_changes_with_limits():
    assert RiskPolicy().version != RiskPolicy(max_spread=0.2).version


def ms_to_iso_soon():
    from hermes_pm.util.timeutil import ms_to_iso
    return ms_to_iso(now_ms() + 3_600_000)  # 1h horizon
