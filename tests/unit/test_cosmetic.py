"""Tests for correctness refinements: ledger-authoritative realized P&L,
provenance graph (FR-SOC-006), and agent prompt_version traceability (NFR-OBS-002)."""

from __future__ import annotations

from hermes_pm.execution.ledger import REALIZED, Ledger
from hermes_pm.models import (
    BookLevel,
    Campaign,
    Mode,
    OrderBookSnapshot,
    OrderType,
    RiskDecision,
    RiskResult,
    Side,
    TradeIntent,
)


def test_portfolio_realized_matches_ledger(paper_engine, db):
    camp = Campaign(name="c", mode=Mode.PAPER, bankroll=1000.0)
    db.save_campaign(camp)
    paper_engine.init_campaign(camp)
    paper_engine.cache.update(OrderBookSnapshot(
        token_id="tok", bids=[BookLevel(price=0.50, size=1000)],
        asks=[BookLevel(price=0.50, size=1000)]))

    def trade(side, price, size, key):
        ti = TradeIntent(campaign_id=camp.campaign_id, market_id="m", token_id="tok", side=side,
                         order_type=OrderType.MARKETABLE_LIMIT, limit_price=price, max_size_usd=size,
                         thesis="t", counter_thesis="c", confidence=0.5,
                         expires_at="2026-12-30T00:00:00Z", idempotency_key=key)
        db.save_intent(ti)
        paper_engine.place_order(camp, ti, RiskDecision(
            intent_id=ti.intent_id, campaign_id=camp.campaign_id, result=RiskResult.APPROVE,
            approved_size_usd=size, approved_limit_price=price))

    trade(Side.BUY, 0.50, 50, "k1")
    # price rises, sell to close at 0.60
    paper_engine.cache.update(OrderBookSnapshot(
        token_id="tok", bids=[BookLevel(price=0.60, size=1000)],
        asks=[BookLevel(price=0.60, size=1000)]))
    pos = db.get_position(camp.campaign_id, "tok")
    trade(Side.SELL, 0.60, round(pos.shares * 0.60, 2), "k2")

    port = paper_engine.portfolio(camp.campaign_id, camp.bankroll)
    ledger_realized = round(-Ledger(db, camp.campaign_id).balances().get(REALIZED, 0.0), 6)
    assert port["realized_pnl"] == ledger_realized
    assert port["realized_pnl"] > 0  # profitable close
    assert port["ledger_balanced"] is True


async def test_provenance_graph_structure(daemon):
    camp = daemon.start_paper_campaign(campaign_name="c", duration_hours=24, paper_bankroll_usd=500,
                                       market_filters={"categories": ["weather", "sports"]})
    mid = camp["watchlist"][0]
    await daemon.gather_evidence(mid)
    summary = daemon.get_social_signal_summary(mid)
    graph = summary["provenance_graph"]
    assert any(n["type"] == "market" for n in graph["nodes"])
    assert any(n["type"] == "signal" for n in graph["nodes"])
    assert any(n["type"] == "source" for n in graph["nodes"])
    assert any(e["rel"] == "has_signal" for e in graph["edges"])
    assert any(e["rel"] == "derived_from" for e in graph["edges"])


async def test_prompt_version_traced(daemon):
    camp = daemon.start_paper_campaign(campaign_name="c", duration_hours=24, paper_bankroll_usd=500,
                                       market_filters={"categories": ["weather", "sports"]})
    cid = camp["campaign_id"]
    mid = camp["watchlist"][0]
    out = daemon.propose_trade_intent(
        campaign_id=cid, market_id=mid, outcome="YES", side="BUY", limit_price=0.55,
        max_size_usd=10, thesis="t", counter_thesis="c", invalidation_criteria="i",
        evidence_refs=[], confidence=0.6, expires_at="2026-12-30T00:00:00Z",
        prompt_version="research_market@v2")
    intent = daemon.db.get_intent(out["trade_intent_id"])
    assert intent.prompt_version == "research_market@v2"
