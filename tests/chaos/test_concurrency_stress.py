"""Concurrency stress: parallel fills + portfolio reads against ONE SQLite
connection (NFR-REL hardening).

The daemon (asyncio thread) and the dashboard (uvicorn worker thread) share a
single DB connection guarded by a lock, and mark-to-market does a position
read-modify-write. Under parallel load the money invariants must still hold
exactly: total shares == sum of fills, cash == the ledger CASH balance, the
ledger stays balanced, and no SQLite/threading error escapes.
"""

from __future__ import annotations

import threading

import pytest

from hermes_pm.audit.store import AuditStore
from hermes_pm.config import RiskPolicy
from hermes_pm.data.cache import OrderBookCache
from hermes_pm.events import EventBus
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

TOKEN = "tok-stress"
PRICE = 0.50
SIZE = 10.0
SHARES_PER_BUY = SIZE / PRICE  # 20.0
N_THREADS = 8
BUYS_PER_THREAD = 25
TOTAL_BUYS = N_THREADS * BUYS_PER_THREAD


def _buy(paper, camp):
    intent = TradeIntent(
        campaign_id=camp.campaign_id, market_id="m", token_id=TOKEN, outcome="YES",
        side=Side.BUY, order_type=OrderType.MARKETABLE_LIMIT, limit_price=PRICE,
        max_size_usd=SIZE, thesis="t", counter_thesis="c", invalidation_criteria="i",
        confidence=0.6, expires_at="2030-01-01T00:00:00Z",
    )
    decision = RiskDecision(
        intent_id=intent.intent_id, campaign_id=camp.campaign_id, result=RiskResult.APPROVE,
        approved_size_usd=SIZE, approved_limit_price=PRICE,
    )
    paper.place_order(camp, intent, decision)


def test_parallel_fills_and_reads_keep_money_invariants():
    db = Database(":memory:")
    cache = OrderBookCache(5_000)
    audit = AuditStore(db)
    policy = RiskPolicy(slippage_bps=0.0, fee_bps=0.0)
    camp = Campaign(name="stress", bankroll=1_000_000.0, mode=Mode.PAPER)
    db.save_campaign(camp)
    paper = PaperEngine(db, cache, EventBus(), audit, policy)
    paper.init_campaign(camp)
    cache.update(
        OrderBookSnapshot(
            token_id=TOKEN, bids=[BookLevel(price=0.49, size=1e9)],
            asks=[BookLevel(price=PRICE, size=1e9)], sequence=1,
        ),
        5_000,
    )

    errors: list[BaseException] = []
    barrier = threading.Barrier(N_THREADS + 2)
    stop = threading.Event()

    def buyer():
        try:
            barrier.wait()
            for _ in range(BUYS_PER_THREAD):
                _buy(paper, camp)
        except BaseException as exc:  # noqa: BLE001 - record for the assertion
            errors.append(exc)

    def reader():
        try:
            barrier.wait()
            while not stop.is_set():
                port = paper.portfolio(camp.campaign_id, camp.bankroll)
                # ledger must be balanced at every observation point
                assert port["ledger_balanced"] is True
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=buyer) for _ in range(N_THREADS)]
    threads += [threading.Thread(target=reader) for _ in range(2)]
    for t in threads:
        t.start()
    buyers = threads[:N_THREADS]
    for t in buyers:
        t.join(timeout=30)
    stop.set()
    for t in threads[N_THREADS:]:
        t.join(timeout=5)

    assert errors == []  # no SQLite/threading error, no failed invariant mid-flight

    pos = db.get_position(camp.campaign_id, TOKEN)
    led = Ledger(db, camp.campaign_id)
    assert pos is not None
    # No lost updates: every buy's shares are present.
    assert pos.shares == pytest.approx(SHARES_PER_BUY * TOTAL_BUYS)
    # Cash counter and the authoritative ledger CASH balance agree exactly.
    assert paper.cash(camp.campaign_id) == pytest.approx(led.balances()[CASH])
    assert paper.cash(camp.campaign_id) == pytest.approx(1_000_000.0 - SIZE * TOTAL_BUYS)
    assert led.is_balanced()
