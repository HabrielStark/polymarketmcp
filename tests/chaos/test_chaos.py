"""Chaos tests (SRS 19.1 Chaos row; NFR-REL-002/003, FR-DATA-006)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from hermes_pm.cli import _scripted_campaign
from hermes_pm.config import load_settings
from hermes_pm.daemon.core import TradingDaemon
from hermes_pm.data.polymarket_client import PolymarketSource
from hermes_pm.errors import RateLimitedError, UpstreamError

pytestmark = pytest.mark.asyncio


async def test_restart_recovers_ledger_and_positions(tmp_path):
    s = load_settings(data_dir=str(tmp_path), db_filename="chaos.sqlite3",
                      ws_reconnect_stale_ms=60_000, reconcile_interval_ms=60_000)
    d1 = TradingDaemon(s)
    cid = await _scripted_campaign(d1)
    cash_before = d1.paper.cash(cid)
    positions_before = {p.token_id: p.shares for p in d1.db.list_positions(cid)}
    realized_before = round(sum(p.realized_pnl for p in d1.db.list_positions(cid)), 6)
    await d1.stop()

    # New process / daemon over the same database.
    d2 = TradingDaemon(s)
    await d2.start()
    try:
        assert d2.db.get_campaign(cid) is not None
        assert d2.paper.cash(cid) == cash_before  # cash persisted exactly
        positions_after = {p.token_id: p.shares for p in d2.db.list_positions(cid)}
        assert positions_after == positions_before
        assert round(sum(p.realized_pnl for p in d2.db.list_positions(cid)), 6) == realized_before
        # audit chain continues from persisted head and stays valid
        assert d2.audit.verify_chain()["ok"]
        assert d2.paper_get_portfolio(cid)["ledger_balanced"] is True
    finally:
        await d2.stop()


async def test_connectivity_loss_forces_staleness_and_risk_reject(daemon):
    camp = daemon.start_paper_campaign(campaign_name="c", duration_hours=24, paper_bankroll_usd=1000,
                                       market_filters={"categories": ["weather", "sports"]})
    cid = camp["campaign_id"]
    mid = camp["watchlist"][0]
    await daemon.gather_evidence(mid)
    ev = [e["source_ref"] for e in daemon.get_source_evidence(mid)
          if e["source_type"] in ("primary", "secondary")][:2]
    tok = daemon.get_market_details(mid)["token_ids"]["YES"]
    snap = daemon.get_market_snapshot(tok)
    intent = daemon.propose_trade_intent(campaign_id=cid, market_id=mid, outcome="YES", side="BUY",
                                         limit_price=round(snap["best_ask"] + 0.02, 2), max_size_usd=10,
                                         thesis="t", counter_thesis="c", invalidation_criteria="i",
                                         evidence_refs=ev, confidence=0.62,
                                         expires_at="2026-12-30T00:00:00Z")
    daemon.cache.set_connectivity_lost(True)  # simulate WS desync / outage
    rc = daemon.risk_check_trade_intent(intent["trade_intent_id"])
    assert rc["decision"] == "reject"
    assert "stale_market_data" in rc["violated_rules"]


async def test_replay_handles_missing_snapshot(daemon):
    from hermes_pm.replay.engine import ReplayEngine
    out = ReplayEngine(daemon).replay_order("does-not-exist")
    assert "error" in out


async def test_rate_limit_raises(monkeypatch):
    s = load_settings()
    src = PolymarketSource(s)
    resp = MagicMock()
    resp.status_code = 429
    resp.headers = {"retry-after": "1"}
    src._client.get = AsyncMock(return_value=resp)
    with pytest.raises((RateLimitedError, UpstreamError)):
        await src._get("http://x", retries=2)
    await src.close()


async def test_upstream_error_after_retries(monkeypatch):
    s = load_settings()
    src = PolymarketSource(s)
    src._client.get = AsyncMock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(UpstreamError):
        await src._get("http://x", retries=2)
    await src.close()


async def test_corrupted_snapshot_checksum_detectable():
    from hermes_pm.models import BookLevel, OrderBookSnapshot
    snap = OrderBookSnapshot(token_id="t", bids=[BookLevel(price=0.4, size=10)],
                             asks=[BookLevel(price=0.5, size=10)])
    good = snap.checksum
    corrupted = snap.model_copy(update={"bids": [BookLevel(price=0.9, size=99)]})
    assert corrupted.compute_checksum() != good
