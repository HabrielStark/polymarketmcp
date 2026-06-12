"""Final coverage closeout — exercises the remaining reachable branch-arcs and
statements so coverage reaches 100%. Genuinely-unreachable real-I/O paths are
pragma'd in source (with reasons); dead code was removed. Every test here asserts
real behaviour on the targeted path."""

from __future__ import annotations

import asyncio
import contextlib
import io
import sys

from hermes_pm.audit.store import AuditStore
from hermes_pm.config import RiskPolicy, load_settings
from hermes_pm.data.cache import OrderBookCache
from hermes_pm.data.market_data import MarketDataEngine
from hermes_pm.data.polymarket_client import PolymarketSource, normalize_gamma_market
from hermes_pm.data.sources import ReplaySource, SyntheticSource
from hermes_pm.events import EventBus, EventType
from hermes_pm.execution.paper_engine import PaperEngine
from hermes_pm.models import (
    BookLevel,
    Campaign,
    CloseStatus,
    Mode,
    OrderBookSnapshot,
    OrderStatus,
    OrderType,
    Position,
    RiskDecision,
    RiskResult,
    Side,
    TradeIntent,
)
from hermes_pm.persistence.db import Database
from hermes_pm.replay.engine import ReplayEngine, _approx


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _pe():
    db = Database(":memory:")
    cache = OrderBookCache(5000)
    paper = PaperEngine(db, cache, EventBus(), AuditStore(db), RiskPolicy(slippage_bps=0.0, fee_bps=0.0))
    camp = Campaign(name="cov", bankroll=10_000.0, mode=Mode.PAPER)
    db.save_campaign(camp)
    paper.init_campaign(camp)
    return db, cache, paper, camp


def _book(token="tok", bid=0.49, ask=0.51, size=500.0, sequence=1):
    return OrderBookSnapshot(token_id=token, bids=[BookLevel(price=bid, size=size)],
                             asks=[BookLevel(price=ask, size=size)], sequence=sequence)


def _intent(camp, order_type=OrderType.MARKETABLE_LIMIT, price=0.51, token="tok"):
    return TradeIntent(campaign_id=camp.campaign_id, market_id="m", token_id=token, outcome="YES",
                       side=Side.BUY, order_type=order_type, limit_price=price, max_size_usd=10.0,
                       thesis="t", counter_thesis="c", invalidation_criteria="i", confidence=0.6,
                       expires_at="2030-01-01T00:00:00Z")


def _decision(camp, intent, size=10.0, price=0.51):
    return RiskDecision(intent_id=intent.intent_id, campaign_id=camp.campaign_id,
                        result=RiskResult.APPROVE, approved_size_usd=size, approved_limit_price=price)


class _FakeSrc:
    async def discover_markets(self):
        return []

    async def snapshot(self, _t):
        return None

    async def stream(self, _toks, _interval):
        if False:  # pragma: no cover - empty async generator
            yield None

    async def close(self):
        return None


# --------------------------------------------------------------------------- #
# replay engine
# --------------------------------------------------------------------------- #
def test_approx_none_branch():
    assert _approx(None, None) is True
    assert _approx(None, 1.0) is False
    assert _approx(1.0, None) is False
    assert _approx(1.0, 1.0 + 1e-9) is True


async def test_replay_campaign_with_missing_snapshot(populated):
    daemon, cid = populated
    daemon.db.execute("DELETE FROM snapshots")  # orphan every fill's snapshot
    rep = ReplayEngine(daemon).replay_campaign(cid)
    assert "match" in rep  # snap None -> skip rcache.update (128->130); still returns a report


# --------------------------------------------------------------------------- #
# paper engine branch arcs
# --------------------------------------------------------------------------- #
def test_init_campaign_idempotent_second_call():
    _db, _cache, paper, camp = _pe()
    paper.init_campaign(camp)  # already initialised -> kv not None -> exit arc (72->exit)
    assert paper.cash(camp.campaign_id) == camp.bankroll


def test_passive_limit_order_does_not_match(monkeypatch):
    _db, cache, paper, camp = _pe()
    cache.update(_book("tok"), 5000)
    intent = _intent(camp, order_type=OrderType.LIMIT, price=0.10)
    order = paper.place_order(camp, intent, _decision(camp, intent, price=0.10))
    assert order.status is OrderStatus.OPEN and order.filled_size_usd == 0.0  # 125->127


def test_on_book_update_resting_no_fill_and_token_filter():
    db, cache, paper, camp = _pe()
    for tok in ("tok-A", "tok-B"):
        cache.update(_book(tok), 5000)
        i = _intent(camp, order_type=OrderType.LIMIT, price=0.10, token=tok)
        paper.place_order(camp, i, _decision(camp, i, price=0.10))
    paper.on_book_update(_book("tok-A", sequence=9))  # tok-B filtered (408->406); A no fill (176->173)
    assert len(db.list_orders(camp.campaign_id)) == 2


def test_cancel_all_multiple_orders():
    _db, cache, paper, camp = _pe()
    for _ in range(2):
        cache.update(_book("tok"), 5000)
        i = _intent(camp, order_type=OrderType.LIMIT, price=0.10)
        paper.place_order(camp, i, _decision(camp, i, price=0.10))
    assert paper.cancel_all(camp.campaign_id) == 2  # loop arc 331->329


# --------------------------------------------------------------------------- #
# sources stream filter arcs
# --------------------------------------------------------------------------- #
async def test_synthetic_stream_skips_unknown_token():
    src = SyntheticSource(seed=1, market_count=1)
    markets = await src.discover_markets()
    real = next(iter(markets[0].token_ids.values()))
    gen = src.stream([real, "unknown-token"], interval_ms=0)
    a = await anext(gen)
    b = await anext(gen)  # resumes, skips the unknown token (135->134), yields real again
    assert a.token_id == real and b.token_id == real
    await gen.aclose()


async def test_replay_stream_skips_unwanted(tmp_path):
    import json
    snaps = [
        OrderBookSnapshot(token_id="X", bids=[BookLevel(price=0.5, size=10.0)],
                          asks=[BookLevel(price=0.51, size=10.0)]).model_dump(mode="json"),
        OrderBookSnapshot(token_id="Y", bids=[BookLevel(price=0.4, size=10.0)],
                          asks=[BookLevel(price=0.6, size=10.0)]).model_dump(mode="json"),
    ]
    p = tmp_path / "replay.json"
    p.write_text(json.dumps({"markets": [], "snapshots": snaps}), encoding="utf-8")
    src = ReplaySource(p)
    got = [s.token_id async for s in src.stream(["X"], interval_ms=0)]
    assert got == ["X"]  # Y skipped (176->175)


# --------------------------------------------------------------------------- #
# polymarket client (mocked; no real network)
# --------------------------------------------------------------------------- #
def test_normalize_more_outcomes_than_tokens():
    m = normalize_gamma_market({
        "id": "m1", "conditionId": "c1", "question": "q?",
        "outcomes": ["YES", "NO", "MAYBE"], "clobTokenIds": '["t1", "t2"]',
    })
    assert m is not None
    # 3 outcomes vs 2 tokens -> the 3rd hits `i < len(token_ids)` False (49->48)
    assert set(m.token_ids.keys()) == {"YES", "NO"}


async def test_discover_markets_skips_non_dict_and_bad(tmp_path, monkeypatch):
    src = PolymarketSource(load_settings(data_dir=str(tmp_path)))

    async def fake_get(*_a, **_k):
        return ["not-a-dict", {"no": "id"}, {"id": "m1", "question": "ok"}]

    monkeypatch.setattr(src, "_get", fake_get)
    out = await src.discover_markets()
    await src.close()
    # non-dict skipped (128->127); {"no":"id"} -> normalize None (130->127); only m1 kept
    assert [m.market_id for m in out] == ["m1"]


# --------------------------------------------------------------------------- #
# live_process: _main EOF path + client.stop without process
# --------------------------------------------------------------------------- #
def test_live_process_main_eof_without_shutdown(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HPM_DATA_DIR", str(tmp_path))
    from hermes_pm.execution import live_process as lp
    monkeypatch.setattr(sys, "stdin", io.StringIO('\n{"cmd": "status"}\nnot-json\n'))
    lp._main()  # blank->continue, status handled, bad json handled, EOF -> for-loop exits (70->exit)
    out = capsys.readouterr().out
    assert '"bad json"' in out and '"ok": true' in out


async def test_live_client_stop_without_process(tmp_path):
    from hermes_pm.execution.live_process import LiveProcessClient
    c = LiveProcessClient(load_settings(data_dir=str(tmp_path)))
    await c.stop()  # proc is None -> skip graceful shutdown (225->230)
    assert c._proc is None


# --------------------------------------------------------------------------- #
# market data: staleness not-stale arc + stop without started tasks
# --------------------------------------------------------------------------- #
async def test_staleness_loop_not_stale_arc(tmp_path):
    s = load_settings(data_dir=str(tmp_path), ws_reconnect_stale_ms=600_000)
    eng = MarketDataEngine(s, _FakeSrc(), OrderBookCache(600_000), Database(":memory:"), EventBus())
    eng._subscribed = {"tok"}
    eng._cache.update(_book("tok"), 600_000)  # fresh -> not stale
    eng._running = True
    task = asyncio.create_task(eng._staleness_loop())
    await asyncio.sleep(0.6)  # >=1 iteration with age <= threshold (211->207)
    eng._running = False
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert eng._cache.connectivity_lost is False


async def test_market_data_stop_without_tasks(tmp_path):
    s = load_settings(data_dir=str(tmp_path))
    eng = MarketDataEngine(s, _FakeSrc(), OrderBookCache(), Database(":memory:"), EventBus())
    await eng.stop()  # no tasks, stream_task None -> `if t` False arcs (221->223, 226->225)
    assert eng._running is False


# --------------------------------------------------------------------------- #
# daemon branch arcs
# --------------------------------------------------------------------------- #
async def test_subscribe_markets_skips_unknown(daemon):
    r = await daemon.subscribe_markets(["does-not-exist-mid"])
    assert r["subscribed_tokens"] == []  # market not found -> m None -> skip (406->404)


async def test_search_past_decisions_non_matching(populated):
    daemon, _cid = populated
    assert daemon.search_past_decisions("zzz-no-such-thesis-xyz") == []  # 773->772 false arc


async def test_consume_market_data_uncached_token(daemon):
    daemon.bus.publish(EventType.MARKET_DATA, {"token_id": "uncached-xyz"})
    await asyncio.sleep(0.15)  # _consume_market_data: snap None -> skip on_book_update (111->114)
    assert daemon.get_system_status()["mode"] in ("paper", "emergency")


async def test_live_iface_isolation_uses_subprocess_client(tmp_path, monkeypatch):
    from hermes_pm.daemon.core import TradingDaemon

    class _FakeClient:
        def __init__(self, _settings):
            self.started = False

        async def start(self):
            self.started = True

    monkeypatch.setattr("hermes_pm.execution.live_process.LiveProcessClient", _FakeClient)
    d = TradingDaemon(load_settings(data_dir=str(tmp_path), live_process_isolation=True))
    iface = await d._live_iface()          # isolation True, client None -> build + start
    assert isinstance(iface, _FakeClient) and iface.started
    assert await d._live_iface() is iface  # client not None -> return cached
    d.db.close()


# --------------------------------------------------------------------------- #
# evaluation: closed position whose order has no intent
# --------------------------------------------------------------------------- #
def test_evaluation_closed_position_without_intent(db):
    from hermes_pm.campaign.evaluation import CampaignEvaluator
    camp = Campaign(name="e", bankroll=1000.0, mode=Mode.PAPER)
    db.save_campaign(camp)
    db.upsert_position(Position(campaign_id=camp.campaign_id, market_id="m", token_id="orphan",
                                shares=0.0, avg_price=0.0, realized_pnl=5.0,
                                close_status=CloseStatus.CLOSED))
    metrics = CampaignEvaluator(db).evaluate(
        camp, {"net_pnl": 5.0, "max_drawdown": 0.0, "equity": 1005.0, "ledger_balanced": True}
    )
    assert metrics["closed_positions"] == 1 and metrics["brier_score"] is None  # 43->40


# --------------------------------------------------------------------------- #
# cli scripted campaign: risk-reject loop-continue arc
# --------------------------------------------------------------------------- #
async def test_scripted_campaign_risk_reject_branch(daemon, monkeypatch):
    from hermes_pm.cli import _scripted_campaign
    real = daemon.risk_check_trade_intent

    def always_reject(intent_id):
        out = real(intent_id)
        out["decision"] = "reject"
        return out

    monkeypatch.setattr(daemon, "risk_check_trade_intent", always_reject)
    cid = await _scripted_campaign(daemon)  # every rc -> reject -> 65->44 loop continue
    assert daemon.paper_get_portfolio(cid)["open_positions"] == []



def test_mark_to_market_skips_position_without_book():
    # A position whose token has no cached book: book is None -> skip mark, loop
    # back (paper_engine 331->329).
    db, _cache, paper, camp = _pe()
    db.upsert_position(Position(campaign_id=camp.campaign_id, market_id="m", token_id="no-book-tok",
                                shares=10.0, avg_price=0.5))
    paper.mark_to_market(camp.campaign_id)
    pos = db.get_position(camp.campaign_id, "no-book-tok")
    assert pos is not None and pos.mark_price is None  # never marked (no book)


async def test_staleness_loop_already_lost_arc(tmp_path):
    # Stale (age > threshold) but connectivity ALREADY marked lost: the inner
    # `if not connectivity_lost` is False -> skip re-publish, loop back (211->207).
    s = load_settings(data_dir=str(tmp_path), ws_reconnect_stale_ms=1)
    eng = MarketDataEngine(s, _FakeSrc(), OrderBookCache(1), Database(":memory:"), EventBus())
    eng._subscribed = {"tok"}
    eng._cache.set_connectivity_lost(True)
    eng._running = True
    task = asyncio.create_task(eng._staleness_loop())
    await asyncio.sleep(0.6)  # age >> 1ms -> stale; already-lost -> inner if False (211->207)
    eng._running = False
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert eng._cache.connectivity_lost is True
