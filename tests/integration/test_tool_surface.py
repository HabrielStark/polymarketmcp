"""Tool-surface smoke: call every remaining daemon tool once and validate the
shape of its output. Complements the per-feature tests by exercising the full
MCP tool surface end-to-end (and the MCP error path)."""

from __future__ import annotations

import json

import pytest
from mcp.shared.memory import create_connected_server_and_client_session as connect

from hermes_pm.mcp.server import build_server

pytestmark = pytest.mark.asyncio


async def test_system_and_config_tools(daemon):
    assert daemon.get_dashboard_url().startswith("http://127.0.0.1")
    cfg = daemon.get_config()
    assert "default_risk_policy" in cfg and "***REDACTED***" not in json.dumps(cfg.get("data_dir", ""))
    applied = daemon.update_config({"reconcile_interval_ms": 1234, "live_enabled": True})
    assert applied["applied"]["reconcile_interval_ms"] == 1234
    assert "live_enabled" in applied["ignored"]  # cannot enable live via update_config
    assert daemon.settings.live_enabled is False
    assert daemon.reset_emergency()["emergency_stop"] is False


async def test_market_and_signal_tools(daemon):
    mid = daemon.build_watchlist({"categories": ["weather", "sports"]})[0]
    details = daemon.get_market_details(mid)
    tok = details["token_ids"]["YES"]
    assert daemon.get_order_book(tok)["token_id"] == tok
    assert isinstance(daemon.get_price_history(tok, limit=10), list)
    assert daemon.get_liquidity_summary(tok)["token_id"] == tok
    await daemon.subscribe_markets([mid])
    wx = next((m for m in daemon.build_watchlist({}) if daemon.get_market_details(m)["category"] == "weather"), mid)
    assert "signals" in await daemon.get_weather_signal_summary(wx)
    sp = next((m for m in daemon.build_watchlist({}) if daemon.get_market_details(m)["category"] == "sports"), mid)
    assert "signals" in await daemon.get_sports_signal_summary(sp)


async def test_intent_paper_learning_audit_tools(daemon):
    camp = daemon.start_paper_campaign(campaign_name="surf", duration_hours=48, paper_bankroll_usd=1000,
                                       market_filters={"categories": ["weather", "sports"]})
    cid = camp["campaign_id"]
    mid = camp["watchlist"][0]
    await daemon.gather_evidence(mid)
    refs = [e["source_ref"] for e in daemon.get_source_evidence(mid)
            if e["source_type"] in ("primary", "secondary")][:2]
    tok = daemon.get_market_details(mid)["token_ids"]["YES"]
    snap = daemon.get_market_snapshot(tok)
    it = daemon.propose_trade_intent(campaign_id=cid, market_id=mid, outcome="YES", side="BUY",
                                     limit_price=round(snap["best_ask"] + 0.02, 2), max_size_usd=10,
                                     thesis="t", counter_thesis="c", invalidation_criteria="i",
                                     evidence_refs=refs, confidence=0.62,
                                     expires_at="2026-12-30T00:00:00Z")
    # simulate (read-only) then risk then place
    assert "simulated" in daemon.simulate_trade_intent(it["trade_intent_id"])
    rc = daemon.risk_check_trade_intent(it["trade_intent_id"])
    daemon.explain_risk_rejection(rc["risk_decision_id"])  # exercises explain path
    order = daemon.paper_place_order(it["trade_intent_id"], rc["risk_decision_id"])
    assert daemon.paper_get_orders(cid)
    daemon.paper_mark_to_market(cid)
    # cancel path (place a non-crossing passive order then cancel)
    if order["status"] not in ("filled",):
        daemon.paper_cancel_order(order["paper_order_id"])
    # learning tools
    daemon.write_lesson(cid, trigger="t", observation="o", rule="r")
    assert isinstance(daemon.list_lessons(cid), list)
    assert isinstance(daemon.search_past_decisions("t", cid), list)
    sk = daemon.create_skill_candidate("counter_check", "desc", ["step1", "step2"], ["lesson://1"])
    assert "path" in sk
    mem = daemon.export_active_memory(cid)
    assert "path" in mem
    daemon.purge_old_signals(retention_hours=0.0)  # retention path
    # audit tools
    assert daemon.get_audit_events(cid, limit=10, event_type="risk_decision") is not None
    exp = daemon.export_campaign_audit(cid)
    assert exp["chain_verification"]["ok"] is True


async def test_mcp_error_path_returns_structured_error(daemon):
    server = build_server(daemon)
    async with connect(server) as client:
        await client.initialize()
        res = await client.call_tool("get_market_details", {"market_id": "no-such-market"})
        payload = json.loads(res.content[0].text)
        assert payload["error"]["code"] == "not_found"
