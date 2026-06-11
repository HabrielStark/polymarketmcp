"""Property-based / fuzz tests with Hypothesis (SRS 19.1; mutation-resistant
invariants)."""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from hermes_pm.audit.store import AuditStore
from hermes_pm.config import RiskPolicy
from hermes_pm.data.cache import OrderBookCache
from hermes_pm.events import EventBus
from hermes_pm.execution.economics import break_even_probability, effective_price, normalized_ev
from hermes_pm.execution.ledger import Ledger
from hermes_pm.execution.paper_engine import PaperEngine
from hermes_pm.models import (
    BookLevel,
    Campaign,
    Mode,
    OrderBookSnapshot,
    RiskDecision,
    RiskResult,
    Side,
    TradeIntent,
)
from hermes_pm.persistence.db import Database
from hermes_pm.util.sanitize import sanitize_untrusted

price_st = st.floats(min_value=0.02, max_value=0.98, allow_nan=False, allow_infinity=False)
bps_st = st.floats(min_value=0.0, max_value=500.0, allow_nan=False)
prob_st = st.floats(min_value=0.0, max_value=1.0, allow_nan=False)


@given(text=st.text(max_size=5000))
def test_sanitize_never_crashes_and_strips(text):
    out = sanitize_untrusted(text)
    assert isinstance(out.text, str)
    for bad in "\u202e\u200b\x00\x07":
        assert bad not in out.text
    assert len(out.text) <= 5000 + 20


@given(price=price_st, fee=bps_st, slip=bps_st)
def test_economics_pessimism_invariant(price, fee, slip):
    # effective_price rounds to 6 decimals, so allow 1e-6 rounding tolerance.
    assert effective_price(Side.BUY, price, fee, slip) >= price - 1e-6
    assert effective_price(Side.SELL, price, fee, slip) <= price + 1e-6
    be = break_even_probability(Side.BUY, price, fee, slip)
    assert 0.0 <= be <= 1.0


@given(price=price_st, model=prob_st)
def test_normalized_ev_monotonic_in_model_prob(price, model):
    lo = normalized_ev(Side.BUY, price, model, 0, 0)
    hi = normalized_ev(Side.BUY, price, min(1.0, model + 0.1), 0, 0)
    assert hi >= lo - 1e-9


@given(
    bid=st.floats(min_value=0.02, max_value=0.95, allow_nan=False),
    delta=st.floats(min_value=0.01, max_value=0.04, allow_nan=False),
    size=st.floats(min_value=1.0, max_value=1000.0, allow_nan=False),
)
def test_orderbook_spread_nonnegative(bid, delta, size):
    ask = min(0.99, bid + delta)
    ob = OrderBookSnapshot(token_id="t", bids=[BookLevel(price=bid, size=size)],
                           asks=[BookLevel(price=ask, size=size)])
    assert ob.spread is None or ob.spread >= -1e-9


@settings(max_examples=40, suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None)
@given(
    trades=st.lists(
        st.tuples(st.sampled_from([Side.BUY, Side.SELL]),
                  st.floats(min_value=0.1, max_value=0.9, allow_nan=False),
                  st.floats(min_value=1.0, max_value=30.0, allow_nan=False)),
        min_size=1, max_size=12,
    )
)
def test_ledger_always_balances_under_random_trades(trades):
    db = Database(":memory:")
    cache = OrderBookCache()
    eng = PaperEngine(db, cache, EventBus(), AuditStore(db), RiskPolicy(fee_bps=0, slippage_bps=0))
    camp = Campaign(name="c", mode=Mode.PAPER, bankroll=100_000.0)
    db.save_campaign(camp)
    eng.init_campaign(camp)
    for i, (side, price, size) in enumerate(trades):
        # deep two-sided book straddling the price so the order is marketable
        cache.update(OrderBookSnapshot(
            token_id="tok",
            bids=[BookLevel(price=round(max(0.01, price - 0.01), 2), size=1e6)],
            asks=[BookLevel(price=round(min(0.99, price + 0.01), 2), size=1e6)]))
        limit = round(price + 0.02, 2) if side is Side.BUY else round(price - 0.02, 2)
        ti = TradeIntent(campaign_id=camp.campaign_id, market_id="m", token_id="tok", side=side,
                         order_type="marketable_limit", limit_price=max(0.01, min(0.99, limit)),
                         max_size_usd=round(size, 2), thesis="t", counter_thesis="c",
                         confidence=0.5, expires_at="2026-12-30T00:00:00Z",
                         idempotency_key=f"k{i}")
        db.save_intent(ti)
        dec = RiskDecision(intent_id=ti.intent_id, campaign_id=camp.campaign_id,
                           result=RiskResult.APPROVE, approved_size_usd=ti.max_size_usd,
                           approved_limit_price=ti.limit_price)
        eng.place_order(camp, ti, dec)
        assert Ledger(db, camp.campaign_id).is_balanced(), "ledger must always balance"
    db.close()


@settings(max_examples=50)
@given(
    spread=st.floats(min_value=0.0, max_value=0.2, allow_nan=False),
    depth=st.floats(min_value=0.0, max_value=1000.0, allow_nan=False),
    size=st.floats(min_value=1.0, max_value=200.0, allow_nan=False),
    stale=st.booleans(),
    conf=prob_st,
)
def test_risk_engine_is_deterministic(spread, depth, size, stale, conf):
    from hermes_pm.models import Market, Signal, SourceType
    from hermes_pm.risk.engine import RiskContext, RiskEngine
    mid = 0.5
    book = OrderBookSnapshot(
        token_id="tok",
        bids=[BookLevel(price=round(max(0.01, mid - spread / 2), 2), size=depth)],
        asks=[BookLevel(price=round(min(0.99, mid + spread / 2), 2), size=depth)])
    intent = TradeIntent(campaign_id="c", market_id="m", token_id="tok", side=Side.BUY,
                         limit_price=0.55, max_size_usd=round(size, 2), thesis="t",
                         counter_thesis="c", confidence=conf, expires_at="2026-12-30T00:00:00Z")
    market = Market(market_id="m", event_id="e", condition_id="c", question="q?",
                    enable_order_book=True, resolution_rules="r", resolution_source="s",
                    token_ids={"YES": "tok"})
    ev = [Signal(market_id="m", source_type=SourceType.PRIMARY, source_ref="o", text_summary="x",
                 trust_score=0.9)]
    ctx = RiskContext(intent=intent, market=market, campaign=Campaign(name="c", bankroll=1000),
                      policy=RiskPolicy(), book=book, book_is_stale=stale,
                      data_age_ms=99999 if stale else 10, evidence=ev)
    eng = RiskEngine()
    a, b = eng.evaluate(ctx), eng.evaluate(ctx)
    assert a.result == b.result
    assert a.approved_size_usd == b.approved_size_usd
    assert a.violated_rules == b.violated_rules
