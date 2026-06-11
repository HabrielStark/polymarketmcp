"""Hard simulations & pipelines (SRS 19 chaos/UAT, exhaustive stress).

Invariants asserted *throughout* every scenario:
  * the double-entry ledger is always balanced (no value created/destroyed),
  * the global audit hash-chain always verifies,
  * portfolio equity stays finite and equals cash + marked position value.
"""

from __future__ import annotations

import asyncio

from hermes_pm.config import load_settings
from hermes_pm.daemon.core import TradingDaemon
from hermes_pm.execution.ledger import Ledger
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


def _approved(ti, size, price):
    return RiskDecision(intent_id=ti.intent_id, campaign_id=ti.campaign_id,
                        result=RiskResult.APPROVE, approved_size_usd=size, approved_limit_price=price)


def _intent(cid, side, price, size, key, token="tok"):
    return TradeIntent(campaign_id=cid, market_id="m", token_id=token, side=side,
                       order_type=OrderType.MARKETABLE_LIMIT, limit_price=price, max_size_usd=size,
                       thesis="t", counter_thesis="c", confidence=0.5,
                       expires_at="2026-12-30T00:00:00Z", idempotency_key=key)


async def test_stress_many_trades_invariants_hold(daemon):
    """Drive a full campaign with many intents across all markets and many book
    cycles; the ledger must stay balanced and the chain valid at every step."""
    camp = daemon.start_paper_campaign(campaign_name="stress", duration_hours=48,
                                       paper_bankroll_usd=100_000,
                                       market_filters={"categories": ["weather", "sports"]})
    cid = camp["campaign_id"]
    for mid in camp["watchlist"]:
        await daemon.gather_evidence(mid)
    placed = 0
    for cycle in range(6):
        await asyncio.sleep(0.05)  # let books advance
        for mid in camp["watchlist"]:
            details = daemon.get_market_details(mid)
            tok = details["token_ids"]["YES"]
            snap = daemon.get_market_snapshot(tok)
            if not snap.get("best_ask"):
                continue
            refs = [e["source_ref"] for e in daemon.get_source_evidence(mid)
                    if e["source_type"] in ("primary", "secondary")][:2]
            side = "BUY" if cycle % 2 == 0 else "SELL"
            px = round(snap["best_ask"] + 0.02, 2) if side == "BUY" else round(snap["best_bid"] - 0.02, 2)
            intent = daemon.propose_trade_intent(
                campaign_id=cid, market_id=mid, outcome="YES", side=side,
                limit_price=max(0.02, min(0.98, px)), max_size_usd=50, thesis="edge",
                counter_thesis="maybe wrong", invalidation_criteria="resolve", evidence_refs=refs,
                confidence=0.6, expires_at="2026-12-30T00:00:00Z")
            if intent.get("status") != "created":
                continue
            rc = daemon.risk_check_trade_intent(intent["trade_intent_id"])
            if rc["decision"] in ("approve", "modify"):
                daemon.paper_place_order(intent["trade_intent_id"], rc["risk_decision_id"])
                placed += 1
            # invariants after every action
            assert Ledger(daemon.db, cid).is_balanced()
            assert daemon.audit.verify_chain()["ok"]
    port = daemon.paper_get_portfolio(cid)
    assert placed > 0
    assert port["ledger_balanced"] is True
    assert isinstance(port["equity"], float)
    # value identity: equity == cash + marked position value
    pv = sum(p["shares"] * (p["mark_price"] or p["avg_price"]) for p in port["open_positions"])
    assert abs(port["equity"] - (port["cash"] + pv)) < 1e-3


def test_round_trip_both_directions(paper_engine, db):
    camp = Campaign(name="rt", mode=Mode.PAPER, bankroll=100_000.0)
    db.save_campaign(camp)
    paper_engine.init_campaign(camp)
    book = OrderBookSnapshot(token_id="tok", bids=[BookLevel(price=0.50, size=1e6)],
                             asks=[BookLevel(price=0.50, size=1e6)])
    paper_engine.cache.update(book)

    # LONG: buy then sell higher -> positive realized
    ti = _intent(camp.campaign_id, Side.BUY, 0.50, 100, "b1")
    db.save_intent(ti)
    paper_engine.place_order(camp, ti, _approved(ti, 100, 0.50))
    paper_engine.cache.update(OrderBookSnapshot(token_id="tok", bids=[BookLevel(price=0.60, size=1e6)],
                                                asks=[BookLevel(price=0.60, size=1e6)]))
    pos = db.get_position(camp.campaign_id, "tok")
    ti2 = _intent(camp.campaign_id, Side.SELL, 0.60, round(pos.shares * 0.60, 2), "s1")
    db.save_intent(ti2)
    paper_engine.place_order(camp, ti2, _approved(ti2, ti2.max_size_usd, 0.60))
    assert db.get_position(camp.campaign_id, "tok").realized_pnl > 0
    assert Ledger(db, camp.campaign_id).is_balanced()

    # SHORT: sell (open short) then buy back lower -> positive realized
    paper_engine.cache.update(OrderBookSnapshot(token_id="t2", bids=[BookLevel(price=0.50, size=1e6)],
                                                asks=[BookLevel(price=0.50, size=1e6)]))
    s = _intent(camp.campaign_id, Side.SELL, 0.50, 100, "sh1", token="t2")
    db.save_intent(s)
    paper_engine.place_order(camp, s, _approved(s, 100, 0.50))
    assert db.get_position(camp.campaign_id, "t2").shares < 0  # short
    paper_engine.cache.update(OrderBookSnapshot(token_id="t2", bids=[BookLevel(price=0.40, size=1e6)],
                                                asks=[BookLevel(price=0.40, size=1e6)]))
    poss = db.get_position(camp.campaign_id, "t2")
    cover = _intent(camp.campaign_id, Side.BUY, 0.40, round(abs(poss.shares) * 0.40, 2), "cv1", token="t2")
    db.save_intent(cover)
    paper_engine.place_order(camp, cover, _approved(cover, cover.max_size_usd, 0.40))
    assert db.get_position(camp.campaign_id, "t2").realized_pnl > 0  # bought back cheaper
    assert Ledger(db, camp.campaign_id).is_balanced()


async def test_restart_mid_flight(tmp_path):
    s = load_settings(data_dir=str(tmp_path), db_filename="mf.sqlite3",
                      ws_reconnect_stale_ms=60000, reconcile_interval_ms=60000)
    d1 = TradingDaemon(s)
    await d1.start()
    await asyncio.sleep(0.2)
    camp = d1.start_paper_campaign(campaign_name="mf", duration_hours=48, paper_bankroll_usd=10_000,
                                   market_filters={"categories": ["weather", "sports"]})
    cid = camp["campaign_id"]
    mid = camp["watchlist"][0]
    await d1.gather_evidence(mid)
    refs = [e["source_ref"] for e in d1.get_source_evidence(mid)
            if e["source_type"] in ("primary", "secondary")][:2]
    tok = d1.get_market_details(mid)["token_ids"]["YES"]
    snap = d1.get_market_snapshot(tok)
    it = d1.propose_trade_intent(campaign_id=cid, market_id=mid, outcome="YES", side="BUY",
                                 limit_price=round(snap["best_ask"] + 0.02, 2), max_size_usd=20,
                                 thesis="t", counter_thesis="c", invalidation_criteria="i",
                                 evidence_refs=refs, confidence=0.6, expires_at="2026-12-30T00:00:00Z")
    rc = d1.risk_check_trade_intent(it["trade_intent_id"])
    d1.paper_place_order(it["trade_intent_id"], rc["risk_decision_id"])
    cash_before = d1.paper.cash(cid)
    await d1.stop()  # crash mid-campaign

    d2 = TradingDaemon(s)
    await d2.start()
    try:
        assert d2.paper.cash(cid) == cash_before
        assert d2.paper_get_portfolio(cid)["ledger_balanced"] is True
        assert d2.audit.verify_chain()["ok"] is True
        # campaign can resume trading after restart
        await d2.gather_evidence(mid)
        snap2 = d2.get_market_snapshot(tok)
        it2 = d2.propose_trade_intent(campaign_id=cid, market_id=mid, outcome="YES", side="BUY",
                                      limit_price=round((snap2["best_ask"] or 0.5) + 0.02, 2),
                                      max_size_usd=20, thesis="t", counter_thesis="c",
                                      invalidation_criteria="i", evidence_refs=refs, confidence=0.6,
                                      expires_at="2026-12-30T00:00:00Z")
        rc2 = d2.risk_check_trade_intent(it2["trade_intent_id"])
        assert rc2["decision"] in ("approve", "modify", "reject")
        assert d2.audit.verify_chain()["ok"] is True
    finally:
        await d2.stop()


def test_concurrent_fills_are_thread_safe(paper_engine, db):
    """Many threads place orders concurrently; the engine lock + atomic cash must
    keep the ledger exactly balanced (no lost updates)."""
    import threading
    camp = Campaign(name="cc", mode=Mode.PAPER, bankroll=1_000_000.0)
    db.save_campaign(camp)
    paper_engine.init_campaign(camp)
    paper_engine.cache.update(OrderBookSnapshot(token_id="tok", bids=[BookLevel(price=0.50, size=1e9)],
                                                asks=[BookLevel(price=0.50, size=1e9)]))
    intents = []
    for i in range(40):
        ti = _intent(camp.campaign_id, Side.BUY if i % 2 == 0 else Side.SELL, 0.50, 10, f"k{i}")
        db.save_intent(ti)
        intents.append(ti)

    def worker(ti):
        paper_engine.place_order(camp, ti, _approved(ti, 10, 0.50))

    threads = [threading.Thread(target=worker, args=(ti,)) for ti in intents]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert Ledger(db, camp.campaign_id).is_balanced()
    assert len(db.list_orders(camp.campaign_id)) == 40


async def test_full_pipeline_stage_by_stage(daemon):
    """Each pipeline stage's output must feed the next (16.1 agent loop)."""
    # Stage 1: campaign
    camp = daemon.start_paper_campaign(campaign_name="pipe", duration_hours=48,
                                       paper_bankroll_usd=1000,
                                       market_filters={"categories": ["weather", "sports"]})
    cid = camp["campaign_id"]
    assert camp["watchlist"]
    # Stage 2: discovery -> tradable
    mid = camp["watchlist"][0]
    assert daemon.get_market_details(mid)["tradable"] is True
    # Stage 3: resolution rules clear
    assert daemon.get_resolution_rules(mid)["has_clear_resolution"] is True
    # Stage 4: evidence + counter
    await daemon.gather_evidence(mid)
    cnt = await daemon.gather_evidence(mid, counter=True)
    assert cnt["count"] > 0
    refs = [e["source_ref"] for e in daemon.get_source_evidence(mid)
            if e["source_type"] in ("primary", "secondary")][:2]
    # Stage 5: intent
    snap = daemon.get_market_snapshot(daemon.get_market_details(mid)["token_ids"]["YES"])
    it = daemon.propose_trade_intent(campaign_id=cid, market_id=mid, outcome="YES", side="BUY",
                                     limit_price=round(snap["best_ask"] + 0.02, 2), max_size_usd=10,
                                     thesis="t", counter_thesis="c", invalidation_criteria="i",
                                     evidence_refs=refs, confidence=0.62,
                                     expires_at="2026-12-30T00:00:00Z")
    assert it["status"] == "created"
    # Stage 6: risk -> Stage 7: paper order
    rc = daemon.risk_check_trade_intent(it["trade_intent_id"])
    assert rc["decision"] in ("approve", "modify")
    order = daemon.paper_place_order(it["trade_intent_id"], rc["risk_decision_id"])
    assert order["paper_order_id"]
    # Stage 8: postmortem -> Stage 9: lesson -> Stage 10: promotion
    pm = daemon.generate_postmortem(cid, it["trade_intent_id"])
    assert pm["outcome"] in ("win", "loss", "flat", "no_fill_or_position")
    daemon.write_lesson(cid, trigger="t", observation="o", rule="r")
    promo = await daemon.get_promotion_report(cid)
    assert set(promo["verdicts"]) == {"statistically_weak", "operationally_safe", "compliance_eligible"}
    # Stage 11: replay closes the loop deterministically
    assert daemon.replay_decision(rc["risk_decision_id"])["result_matches"] is True
    assert daemon.audit.verify_chain()["ok"] is True
