"""End-to-end acceptance tests — one test per MVP acceptance criterion
(SRS 19.2, AC-001..AC-008)."""

from __future__ import annotations

import json

import pytest
from mcp.shared.memory import create_connected_server_and_client_session as connect

from hermes_pm.mcp.server import build_server
from hermes_pm.replay.engine import ReplayEngine

pytestmark = pytest.mark.asyncio


def _p(result):
    return json.loads(result.content[0].text)


async def _drive(daemon):
    """Start a campaign and execute one full intent->risk->paper flow; return ids."""
    camp = daemon.start_paper_campaign(campaign_name="ac", duration_hours=48, paper_bankroll_usd=1000,
                                       market_filters={"categories": ["weather", "sports"]})
    cid = camp["campaign_id"]
    mid = camp["watchlist"][0]
    await daemon.gather_evidence(mid)
    refs = [e["source_ref"] for e in daemon.get_source_evidence(mid)
            if e["source_type"] in ("primary", "secondary")][:2]
    tok = daemon.get_market_details(mid)["token_ids"]["YES"]
    snap = daemon.get_market_snapshot(tok)
    intent = daemon.propose_trade_intent(campaign_id=cid, market_id=mid, outcome="YES", side="BUY",
                                         limit_price=round(snap["best_ask"] + 0.02, 2), max_size_usd=10,
                                         thesis="model edge", counter_thesis="market may be right",
                                         invalidation_criteria="resolve before close",
                                         evidence_refs=refs, confidence=0.62,
                                         expires_at="2026-12-30T00:00:00Z")
    rc = daemon.risk_check_trade_intent(intent["trade_intent_id"])
    order = daemon.paper_place_order(intent["trade_intent_id"], rc["risk_decision_id"])
    return cid, mid, intent, rc, order


async def test_ac001_start_campaign_returns_dashboard_url(daemon):
    server = build_server(daemon)
    async with connect(server) as client:
        await client.initialize()
        out = _p(await client.call_tool("start_paper_campaign",
                 {"campaign_name": "ac1", "duration_hours": 48, "paper_bankroll_usd": 1000}))
        assert out["status"] == "running"
        assert out["dashboard_url"].startswith("http://127.0.0.1")


async def test_ac002_discover_orderbook_markets_with_live_prices(daemon):
    markets = daemon.search_markets({"require_order_book": True})
    tradable = [m for m in markets if m["tradable"]]
    assert tradable
    tok = tradable[0]["token_ids"]["YES"]
    snap = daemon.get_market_snapshot(tok)
    assert snap["exists"] and snap["best_ask"] is not None and not snap["stale"]


async def test_ac003_intents_must_pass_risk_before_paper(daemon):
    cid, mid, intent, rc, order = await _drive(daemon)
    assert intent["status"] == "created"
    assert rc["decision"] in ("approve", "modify")
    assert order["status"] in ("filled", "partially_filled", "open", "accepted")
    # An intent without a risk approval cannot be paper-placed.
    with pytest.raises(Exception):
        daemon.paper_place_order(intent["trade_intent_id"], "nonexistent-decision")


async def test_ac004_fills_replayable_from_snapshots(daemon):
    cid, mid, intent, rc, order = await _drive(daemon)
    engine = ReplayEngine(daemon)
    if order["simulated_fills"]:
        assert engine.replay_order(order["paper_order_id"])["match"] is True
    dr = engine.replay_decision(rc["risk_decision_id"])
    assert dr["result_matches"] and dr["idempotency_key_matches"]
    assert engine.replay_campaign(cid)["equity_match"] is True


async def test_ac005_dashboard_shows_paper_pnl_and_evidence(daemon):
    cid, mid, intent, rc, order = await _drive(daemon)
    report = daemon.get_campaign_report(cid)
    assert report["portfolio"]["paper"] is True
    assert "equity" in report["portfolio"]
    assert daemon.get_source_evidence(mid)  # evidence present
    assert daemon.get_audit_events(cid)  # rationale/audit trail present


async def test_ac006_live_disabled_and_not_triggerable_by_raw_args(daemon):
    server = build_server(daemon)
    async with connect(server) as client:
        await client.initialize()
        tool = next(t for t in (await client.list_tools()).tools if t.name == "live_place_order_intent")
        props = set(tool.inputSchema["properties"])
        # reference-only: no raw market/side/size/price accepted
        assert props == {"trade_intent_id", "risk_decision_id", "user_confirmation_token"}
        out = _p(await client.call_tool("live_place_order_intent",
                 {"trade_intent_id": "x", "risk_decision_id": "y"}))
        assert out["status"] == "blocked"
        # raw order params are rejected by the strict schema
        rej = _p(await client.call_tool("live_place_order_intent",
                 {"trade_intent_id": "x", "risk_decision_id": "y", "market_id": "m",
                  "side": "BUY", "size": 1000}))
        assert rej["error"]["code"] == "schema_rejected"


async def test_ac007_emergency_stop_freezes_and_audits(daemon):
    cid, *_ = await _drive(daemon)
    res = daemon.emergency_stop(cid)
    assert res["emergency_stop"] is True and res["audit_event_id"]
    with pytest.raises(Exception):
        daemon.start_paper_campaign(campaign_name="x", duration_hours=24, paper_bankroll_usd=500)
    assert any(e["type"] == "emergency_stop" for e in daemon.get_audit_events(limit=50))


async def test_ac008_promotion_report_states_three_verdicts(daemon):
    cid, *_ = await _drive(daemon)
    report = await daemon.get_promotion_report(cid)
    v = report["verdicts"]
    assert set(v) == {"statistically_weak", "operationally_safe", "compliance_eligible"}
    assert v["compliance_eligible"] is False  # live locked by default
    assert v["statistically_weak"] is True  # short campaign
    assert isinstance(report["8_recommendation"], str) and report["8_recommendation"]


async def test_full_audit_chain_integrity_after_e2e(daemon):
    await _drive(daemon)
    assert daemon.audit.verify_chain()["ok"] is True
