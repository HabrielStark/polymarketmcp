"""Unit tests: paper trading engine (FR-PAPER-001..007)."""

from __future__ import annotations

import pytest

from hermes_pm.execution.ledger import Ledger
from hermes_pm.models import (
    BookLevel,
    Campaign,
    Mode,
    OrderBookSnapshot,
    OrderStatus,
    OrderType,
    RiskDecision,
    RiskResult,
    Side,
    TradeIntent,
)


def _setup(paper_engine, db, bankroll=1000.0):
    camp = Campaign(name="c", mode=Mode.PAPER, bankroll=bankroll)
    db.save_campaign(camp)
    paper_engine.init_campaign(camp)
    return camp


def _intent(cid, side=Side.BUY, price=0.51, size=60.0, ot=OrderType.MARKETABLE_LIMIT, token="tok"):
    ti = TradeIntent(campaign_id=cid, market_id="m", token_id=token, outcome="YES", side=side,
                     order_type=ot, limit_price=price, max_size_usd=size, thesis="t",
                     counter_thesis="c", confidence=0.6, expires_at="2026-12-30T00:00:00Z")
    return ti


def _decision(ti, size=None, price=None):
    return RiskDecision(intent_id=ti.intent_id, campaign_id=ti.campaign_id, result=RiskResult.APPROVE,
                        approved_size_usd=size if size is not None else ti.max_size_usd,
                        approved_limit_price=price if price is not None else ti.limit_price)


def test_marketable_buy_walks_book(paper_engine, db):
    camp = _setup(paper_engine, db)
    paper_engine.cache.update(OrderBookSnapshot(
        token_id="tok", bids=[BookLevel(price=0.49, size=200)],
        asks=[BookLevel(price=0.50, size=30), BookLevel(price=0.51, size=50)]))
    ti = _intent(camp.campaign_id, size=60.0)
    db.save_intent(ti)
    order = paper_engine.place_order(camp, ti, _decision(ti))
    assert order.status is OrderStatus.FILLED
    assert len(order.fills) == 2  # two levels
    pos = db.get_position(camp.campaign_id, "tok")
    assert pos.shares == pytest.approx(30 / 0.50 + 30 / 0.51, rel=1e-6)
    assert Ledger(db, camp.campaign_id).is_balanced()


def test_partial_fill_when_insufficient_depth(paper_engine, db):
    camp = _setup(paper_engine, db)
    paper_engine.cache.update(OrderBookSnapshot(
        token_id="tok", bids=[BookLevel(price=0.49, size=200)],
        asks=[BookLevel(price=0.50, size=20)]))
    ti = _intent(camp.campaign_id, size=60.0)
    db.save_intent(ti)
    order = paper_engine.place_order(camp, ti, _decision(ti))
    assert order.status in (OrderStatus.PARTIALLY_FILLED, OrderStatus.OPEN)
    assert order.filled_size_usd == pytest.approx(20.0)


def test_marketable_buy_does_not_cross_above_limit(paper_engine, db):
    camp = _setup(paper_engine, db)
    paper_engine.cache.update(OrderBookSnapshot(
        token_id="tok", bids=[BookLevel(price=0.49, size=200)],
        asks=[BookLevel(price=0.60, size=200)]))
    ti = _intent(camp.campaign_id, price=0.51, size=60.0)
    db.save_intent(ti)
    order = paper_engine.place_order(camp, ti, _decision(ti))
    assert order.filled_size_usd == 0.0  # best ask 0.60 > limit 0.51


def test_sell_to_close_realizes_pnl(paper_engine, db):
    camp = _setup(paper_engine, db)
    paper_engine.cache.update(OrderBookSnapshot(
        token_id="tok", bids=[BookLevel(price=0.49, size=500)],
        asks=[BookLevel(price=0.50, size=500)]))
    ti = _intent(camp.campaign_id, size=50.0)
    db.save_intent(ti)
    paper_engine.place_order(camp, ti, _decision(ti))
    # price rises; sell to close
    paper_engine.cache.update(OrderBookSnapshot(
        token_id="tok", bids=[BookLevel(price=0.60, size=500)],
        asks=[BookLevel(price=0.61, size=500)]))
    pos = db.get_position(camp.campaign_id, "tok")
    sell = _intent(camp.campaign_id, side=Side.SELL, price=0.60, size=round(pos.shares * 0.60, 2))
    db.save_intent(sell)
    paper_engine.place_order(camp, sell, _decision(sell))
    pos2 = db.get_position(camp.campaign_id, "tok")
    assert pos2.realized_pnl > 0
    assert Ledger(db, camp.campaign_id).is_balanced()


def test_rejected_decision_cannot_place(paper_engine, db):
    camp = _setup(paper_engine, db)
    ti = _intent(camp.campaign_id)
    db.save_intent(ti)
    rej = RiskDecision(intent_id=ti.intent_id, campaign_id=camp.campaign_id, result=RiskResult.REJECT)
    with pytest.raises(Exception):
        paper_engine.place_order(camp, ti, rej)


def test_idempotent_order_placement(paper_engine, db):
    camp = _setup(paper_engine, db)
    paper_engine.cache.update(OrderBookSnapshot(
        token_id="tok", bids=[BookLevel(price=0.49, size=500)],
        asks=[BookLevel(price=0.50, size=500)]))
    ti = _intent(camp.campaign_id, size=10.0)
    db.save_intent(ti)
    dec = _decision(ti)
    o1 = paper_engine.place_order(camp, ti, dec)
    o2 = paper_engine.place_order(camp, ti, dec)
    assert o1.order_id == o2.order_id  # idempotent
    assert len(db.list_orders(camp.campaign_id)) == 1


def test_mark_to_market_unrealized(paper_engine, db):
    camp = _setup(paper_engine, db)
    paper_engine.cache.update(OrderBookSnapshot(
        token_id="tok", bids=[BookLevel(price=0.49, size=500)],
        asks=[BookLevel(price=0.50, size=500)]))
    ti = _intent(camp.campaign_id, size=50.0)
    db.save_intent(ti)
    paper_engine.place_order(camp, ti, _decision(ti))
    paper_engine.cache.update(OrderBookSnapshot(
        token_id="tok", bids=[BookLevel(price=0.55, size=500)],
        asks=[BookLevel(price=0.57, size=500)]))
    port = paper_engine.portfolio(camp.campaign_id, camp.bankroll)
    assert port["paper"] is True
    assert port["unrealized_pnl"] > 0
    assert port["ledger_balanced"] is True


def test_cancel_order(paper_engine, db):
    camp = _setup(paper_engine, db)
    paper_engine.cache.update(OrderBookSnapshot(
        token_id="tok", bids=[BookLevel(price=0.49, size=500)], asks=[BookLevel(price=0.60, size=500)]))
    ti = _intent(camp.campaign_id, price=0.50, size=10.0)  # won't cross -> rests
    db.save_intent(ti)
    order = paper_engine.place_order(camp, ti, _decision(ti))
    cancelled = paper_engine.cancel_order(order.order_id)
    assert cancelled.status is OrderStatus.CANCELLED
