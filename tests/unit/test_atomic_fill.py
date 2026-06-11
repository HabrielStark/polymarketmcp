"""Atomicity of money-state mutations (FR-PAPER-004 / NFR-REL-001).

A paper fill mutates position + cash + the 4 balanced ledger postings + fill +
order in ONE transaction. If anything fails mid-fill, the cash<->ledger<->
position invariant must be left exactly as it was — never half-applied. These
tests inject failures at different points inside the fill and assert a complete
rollback, plus that the happy path keeps cash and the ledger in agreement.
"""

from __future__ import annotations

import pytest

from hermes_pm.audit.store import AuditStore
from hermes_pm.config import RiskPolicy
from hermes_pm.data.cache import OrderBookCache
from hermes_pm.events import EventBus
from hermes_pm.execution import paper_engine as pe
from hermes_pm.execution.ledger import CASH, Ledger
from hermes_pm.execution.paper_engine import PaperEngine
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
from hermes_pm.persistence.db import Database

TOKEN = "tok-atomic"


def _setup():
    db = Database(":memory:")
    cache = OrderBookCache(5000)
    audit = AuditStore(db)
    policy = RiskPolicy(slippage_bps=0.0, fee_bps=0.0)
    camp = Campaign(name="atom", bankroll=10_000.0, mode=Mode.PAPER)
    db.save_campaign(camp)
    paper = PaperEngine(db, cache, EventBus(), audit, policy)
    paper.init_campaign(camp)
    cache.update(
        OrderBookSnapshot(
            token_id=TOKEN, bids=[BookLevel(price=0.40, size=1000.0)],
            asks=[BookLevel(price=0.50, size=1000.0)], sequence=1,
        ),
        5000,
    )
    return db, paper, camp


def _intent_decision(camp):
    intent = TradeIntent(
        campaign_id=camp.campaign_id, market_id="m", token_id=TOKEN, outcome="YES",
        side=Side.BUY, order_type=OrderType.MARKETABLE_LIMIT, limit_price=0.60,
        max_size_usd=100.0, thesis="t", counter_thesis="c", invalidation_criteria="i",
        confidence=0.6, expires_at="2030-01-01T00:00:00Z",
    )
    decision = RiskDecision(
        intent_id=intent.intent_id, campaign_id=camp.campaign_id, result=RiskResult.APPROVE,
        approved_size_usd=100.0, approved_limit_price=0.60,
    )
    return intent, decision


def _assert_unchanged(db, paper, camp, cash_before, bal_before):
    led = Ledger(db, camp.campaign_id)
    assert paper.cash(camp.campaign_id) == cash_before          # cash untouched
    assert led.balances() == bal_before                          # ledger untouched
    assert led.is_balanced()                                     # still balanced
    assert db.get_position(camp.campaign_id, TOKEN) is None       # no position created
    for o in db.list_orders(camp.campaign_id):                    # no orphan fill, order unfilled
        assert db.list_fills(o.order_id) == []
        assert o.filled_size_usd == 0.0


def test_happy_path_keeps_cash_and_ledger_in_agreement():
    db, paper, camp = _setup()
    intent, decision = _intent_decision(camp)
    order = paper.place_order(camp, intent, decision)
    assert order.status.value == "filled"
    pos = db.get_position(camp.campaign_id, TOKEN)
    assert pos is not None and pos.shares == pytest.approx(200.0)  # 100 USD / 0.50
    led = Ledger(db, camp.campaign_id)
    assert led.is_balanced()
    # The authoritative ledger CASH balance must equal the kv cash counter.
    assert paper.cash(camp.campaign_id) == pytest.approx(led.balances()[CASH])
    assert paper.cash(camp.campaign_id) == pytest.approx(9_900.0)


def test_rollback_when_ledger_post_fails(monkeypatch):
    db, paper, camp = _setup()
    intent, decision = _intent_decision(camp)
    cash_before = paper.cash(camp.campaign_id)
    bal_before = Ledger(db, camp.campaign_id).balances()

    def boom(self, postings, tolerance=1e-6):
        raise RuntimeError("simulated crash during ledger.post")

    monkeypatch.setattr(pe.Ledger, "post", boom)
    with pytest.raises(RuntimeError):
        paper.place_order(camp, intent, decision)
    _assert_unchanged(db, paper, camp, cash_before, bal_before)


def test_rollback_when_save_fill_fails(monkeypatch):
    # Failure AFTER cash + ledger writes but before commit must still roll those back.
    db, paper, camp = _setup()
    intent, decision = _intent_decision(camp)
    cash_before = paper.cash(camp.campaign_id)
    bal_before = Ledger(db, camp.campaign_id).balances()

    def boom(_fill):
        raise RuntimeError("simulated crash during save_fill")

    monkeypatch.setattr(db, "save_fill", boom)
    with pytest.raises(RuntimeError):
        paper.place_order(camp, intent, decision)
    _assert_unchanged(db, paper, camp, cash_before, bal_before)


def test_partial_fill_walk_then_failure_leaves_consistent_state(monkeypatch):
    # Two ask levels: the first fill commits atomically; the second crashes. The
    # committed first fill must be fully consistent and the second fully absent.
    db, paper, camp = _setup()
    db_cache = paper.cache
    db_cache.update(
        OrderBookSnapshot(
            token_id=TOKEN,
            bids=[BookLevel(price=0.40, size=1000.0)],
            asks=[BookLevel(price=0.50, size=40.0), BookLevel(price=0.55, size=1000.0)],
            sequence=2,
        ),
        5000,
    )
    intent, decision = _intent_decision(camp)  # size 100 -> 40 at 0.50, then 60 at 0.55

    real_post = pe.Ledger.post
    calls = {"n": 0}

    def flaky(self, postings, tolerance=1e-6):
        calls["n"] += 1
        if calls["n"] == 2:  # let the first fill commit, crash on the second
            raise RuntimeError("crash on 2nd level")
        return real_post(self, postings, tolerance)

    monkeypatch.setattr(pe.Ledger, "post", flaky)
    with pytest.raises(RuntimeError):
        paper.place_order(camp, intent, decision)

    led = Ledger(db, camp.campaign_id)
    pos = db.get_position(camp.campaign_id, TOKEN)
    # Exactly the first level filled: 40 USD / 0.50 = 80 shares, cash 10000-40=9960.
    assert pos is not None and pos.shares == pytest.approx(80.0)
    assert paper.cash(camp.campaign_id) == pytest.approx(9_960.0)
    assert paper.cash(camp.campaign_id) == pytest.approx(led.balances()[CASH])
    assert led.is_balanced()  # invariant holds despite the mid-walk crash


def test_init_campaign_rolls_back_on_opening_posting_failure(monkeypatch):
    db = Database(":memory:")
    paper = PaperEngine(db, OrderBookCache(5000), EventBus(), AuditStore(db), RiskPolicy())
    camp = Campaign(name="c2", bankroll=5_000.0, mode=Mode.PAPER)
    db.save_campaign(camp)

    def boom(self, postings, tolerance=1e-6):
        raise RuntimeError("crash during opening balance")

    monkeypatch.setattr(pe.Ledger, "post", boom)
    with pytest.raises(RuntimeError):
        paper.init_campaign(camp)
    # No cash key, no peak, no ledger rows: campaign simply was not initialised.
    assert db.kv_get(paper._cash_key(camp.campaign_id)) is None
    assert db.list_ledger(camp.campaign_id) == []
