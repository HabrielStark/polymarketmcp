"""Latency benchmarks against NFR-LAT-001..005.

Targets (local, no network on the hot path):
  NFR-LAT-001 cached snapshot read   p95 <= 50 ms
  NFR-LAT-002 risk check (pure)       p95 <= 25 ms
  NFR-LAT-003 paper order acceptance  p95 <= 30 ms
  NFR-LAT-005 market-data processing  p95 <= 10 ms / message
"""

from __future__ import annotations

import statistics
import time

import pytest

from hermes_pm.config import RiskPolicy
from hermes_pm.models import (
    BookLevel,
    Campaign,
    Market,
    Mode,
    OrderBookSnapshot,
    RiskDecision,
    RiskResult,
    Side,
    Signal,
    SourceType,
    TradeIntent,
)
from hermes_pm.risk.engine import RiskContext, RiskEngine

pytestmark = pytest.mark.latency


def _p95(samples: list[float]) -> float:
    samples = sorted(samples)
    return samples[min(len(samples) - 1, int(round(0.95 * (len(samples) - 1))))]


async def test_cached_snapshot_p95_under_50ms(daemon):
    daemon.start_paper_campaign(campaign_name="c", duration_hours=24, paper_bankroll_usd=1000)
    tok = next(iter(daemon.cache.tokens()), None)
    assert tok is not None
    samples = []
    for _ in range(500):
        t0 = time.perf_counter()
        daemon.get_market_snapshot(tok)
        samples.append((time.perf_counter() - t0) * 1000)
    p95 = _p95(samples)
    assert p95 <= 50.0, f"cached snapshot p95={p95:.2f}ms"


def test_risk_check_p95_under_25ms():
    eng = RiskEngine()
    book = OrderBookSnapshot(token_id="tok", bids=[BookLevel(price=0.49, size=500)],
                             asks=[BookLevel(price=0.51, size=500)])
    market = Market(market_id="m", event_id="e", condition_id="c", question="q?",
                    enable_order_book=True, resolution_rules="r", resolution_source="s",
                    token_ids={"YES": "tok"})
    intent = TradeIntent(campaign_id="c", market_id="m", token_id="tok", side=Side.BUY,
                         limit_price=0.51, max_size_usd=10, thesis="t", counter_thesis="c",
                         confidence=0.6, expires_at="2026-12-30T00:00:00Z")
    ev = [Signal(market_id="m", source_type=SourceType.PRIMARY, source_ref="o", text_summary="x",
                 trust_score=0.9)]
    ctx = RiskContext(intent=intent, market=market, campaign=Campaign(name="c", bankroll=1000),
                      policy=RiskPolicy(), book=book, book_is_stale=False, data_age_ms=10, evidence=ev)
    samples = []
    for _ in range(2000):
        t0 = time.perf_counter()
        eng.evaluate(ctx)
        samples.append((time.perf_counter() - t0) * 1000)
    p95 = _p95(samples)
    assert p95 <= 25.0, f"risk check p95={p95:.3f}ms"


async def test_paper_order_acceptance_p95_under_30ms(paper_engine, db):
    camp = Campaign(name="c", mode=Mode.PAPER, bankroll=1_000_000.0)
    db.save_campaign(camp)
    paper_engine.init_campaign(camp)
    paper_engine.cache.update(OrderBookSnapshot(
        token_id="tok", bids=[BookLevel(price=0.49, size=1e9)],
        asks=[BookLevel(price=0.50, size=1e9)]))
    samples = []
    for i in range(200):
        ti = TradeIntent(campaign_id=camp.campaign_id, market_id="m", token_id="tok", side=Side.BUY,
                         order_type="marketable_limit", limit_price=0.50, max_size_usd=5,
                         thesis="t", counter_thesis="c", confidence=0.5,
                         expires_at="2026-12-30T00:00:00Z", idempotency_key=f"k{i}")
        db.save_intent(ti)
        dec = RiskDecision(intent_id=ti.intent_id, campaign_id=camp.campaign_id,
                           result=RiskResult.APPROVE, approved_size_usd=5, approved_limit_price=0.50)
        t0 = time.perf_counter()
        paper_engine.place_order(camp, ti, dec)
        samples.append((time.perf_counter() - t0) * 1000)
    p95 = _p95(samples)
    assert p95 <= 30.0, f"paper order p95={p95:.2f}ms (median={statistics.median(samples):.2f})"


async def test_market_data_processing_p95_under_10ms(paper_engine, db):
    camp = Campaign(name="c", mode=Mode.PAPER, bankroll=1000.0)
    db.save_campaign(camp)
    paper_engine.init_campaign(camp)
    samples = []
    for seq in range(500):
        snap = OrderBookSnapshot(token_id="tok", bids=[BookLevel(price=0.49, size=500)],
                                 asks=[BookLevel(price=0.51, size=500)], sequence=seq)
        t0 = time.perf_counter()
        paper_engine.on_book_update(snap)
        samples.append((time.perf_counter() - t0) * 1000)
    p95 = _p95(samples)
    assert p95 <= 10.0, f"market-data processing p95={p95:.3f}ms"


async def test_dashboard_update_after_local_event_p95_under_250ms():
    """NFR-LAT-004: local event -> subscriber delivery latency."""
    from hermes_pm.events import EventBus
    bus = EventBus()
    samples = []
    with bus.subscription() as q:
        for _ in range(300):
            t0 = time.perf_counter()
            bus.publish("market_data", {"x": 1})
            await q.get()
            samples.append((time.perf_counter() - t0) * 1000)
    p95 = _p95(samples)
    assert p95 <= 250.0, f"dashboard update p95={p95:.3f}ms"


async def test_x_social_processing_p95_under_500ms(settings):
    """NFR-LAT-006: local processing of X signals (offline) — end-to-end X delivery
    latency is upstream and out of scope."""
    from hermes_pm.models import Market
    from hermes_pm.signals.social_x import XSocialAdapter
    adapter = XSocialAdapter(settings)
    market = Market(market_id="m", event_id="e", condition_id="c", question="Will X happen?",
                    enable_order_book=True, resolution_rules="r", resolution_source="s")
    samples = []
    for _ in range(100):
        t0 = time.perf_counter()
        await adapter.fetch(market)
        samples.append((time.perf_counter() - t0) * 1000)
    p95 = _p95(samples)
    assert p95 <= 500.0, f"X social processing p95={p95:.3f}ms"
