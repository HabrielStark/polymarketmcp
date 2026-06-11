"""Integration tests: daemon end-to-end flow and MCP server compliance."""

from __future__ import annotations

import json

import pytest
from mcp.shared.memory import create_connected_server_and_client_session as connect

from hermes_pm.mcp.server import build_server
from hermes_pm.mcp.tools import TOOL_SPECS

pytestmark = pytest.mark.asyncio


def _payload(result):
    return json.loads(result.content[0].text)


async def test_daemon_full_paper_flow(daemon):
    camp = daemon.start_paper_campaign(campaign_name="t", duration_hours=48, paper_bankroll_usd=1000,
                                       market_filters={"categories": ["weather", "sports"]})
    cid = camp["campaign_id"]
    assert camp["status"] == "running" and camp["dashboard_url"].startswith("http://127.0.0.1")
    mid = camp["watchlist"][0]
    await daemon.gather_evidence(mid)
    ev = [e["source_ref"] for e in daemon.get_source_evidence(mid)
          if e["source_type"] in ("primary", "secondary")][:2]
    snap = daemon.get_market_snapshot(daemon.get_market_details(mid)["token_ids"]["YES"])
    intent = daemon.propose_trade_intent(
        campaign_id=cid, market_id=mid, outcome="YES", side="BUY",
        limit_price=round(snap["best_ask"] + 0.02, 2), max_size_usd=10, thesis="t",
        counter_thesis="c", invalidation_criteria="i", evidence_refs=ev, confidence=0.62,
        expires_at="2026-12-30T00:00:00Z")
    assert intent["status"] == "created"
    rc = daemon.risk_check_trade_intent(intent["trade_intent_id"])
    assert rc["decision"] in ("approve", "modify")
    order = daemon.paper_place_order(intent["trade_intent_id"], rc["risk_decision_id"])
    assert order["status"] in ("filled", "partially_filled", "open", "accepted")
    port = daemon.paper_get_portfolio(cid)
    assert port["paper"] is True and port["ledger_balanced"] is True


async def test_mcp_lists_all_tools_and_validates(daemon):
    server = build_server(daemon)
    async with connect(server) as client:
        await client.initialize()
        tools = await client.list_tools()
        assert len(tools.tools) == len(TOOL_SPECS)
        # every advertised tool has an object schema with additionalProperties:false
        for t in tools.tools:
            assert t.inputSchema["additionalProperties"] is False
        status = _payload(await client.call_tool("get_system_status", {}))
        assert status["mode"] in ("paper", "emergency")


async def test_mcp_resources_and_prompts(daemon):
    daemon.start_paper_campaign(campaign_name="r", duration_hours=24, paper_bankroll_usd=500)
    server = build_server(daemon)
    async with connect(server) as client:
        await client.initialize()
        res = await client.list_resources()
        assert any(str(r.uri).startswith("system://status") for r in res.resources)
        content = await client.read_resource("system://status")
        assert json.loads(content.contents[0].text)["mode"] in ("paper", "emergency")
        prompts = await client.list_prompts()
        assert {p.name for p in prompts.prompts} >= {"research_market", "promotion_report"}
        gp = await client.get_prompt("trade_intent_reviewer", {"trade_intent_id": "x"})
        assert "adversarially" in gp.messages[0].content.text


async def test_mcp_schema_rejects_unknown_and_missing(daemon):
    server = build_server(daemon)
    async with connect(server) as client:
        await client.initialize()
        bad = _payload(await client.call_tool("get_market_snapshot", {"token_id": "t", "x": 1}))
        assert bad["error"]["code"] == "schema_rejected"
        missing = _payload(await client.call_tool("get_market_details", {}))
        assert missing["error"]["code"] == "schema_rejected"


async def test_mcp_maps_out_of_range_numeric_to_clean_validation_error(daemon):
    # limit_price is schema-typed as a bare number (no min/max), so 5.0 passes the
    # JSON-Schema gate and reaches the model layer, where TradeIntent(le=1.0)
    # raises a *pydantic* ValidationError. That must surface as a clean tool error
    # — not an unhandled exception that crashes the stdio server.
    camp = daemon.start_paper_campaign(
        campaign_name="b", duration_hours=24, paper_bankroll_usd=500,
        market_filters={"categories": ["weather", "sports"]},
    )
    cid, mid = camp["campaign_id"], camp["watchlist"][0]
    server = build_server(daemon)
    async with connect(server) as client:
        await client.initialize()
        out = _payload(await client.call_tool("propose_trade_intent", {
            "campaign_id": cid, "market_id": mid, "outcome": "YES", "side": "BUY",
            "limit_price": 5.0, "max_size_usd": 10, "thesis": "t", "counter_thesis": "c",
            "invalidation_criteria": "i", "evidence_refs": [], "confidence": 0.5,
            "expires_at": "2026-12-30T00:00:00Z",
        }))
        assert "error" in out, out
        assert out["error"]["code"] == "validation_error"
        assert "limit_price" in out["error"]["message"]


async def test_mcp_tool_boundary_never_leaks_unhandled_exception(daemon, monkeypatch):
    # If a daemon method raises something unexpected, the boundary must return a
    # generic internal_error (logged to stderr) — never propagate a raw traceback.
    def boom(**_kw):
        raise RuntimeError("unexpected internal failure with secret=abc123")

    monkeypatch.setattr(daemon, "get_system_status", boom)
    server = build_server(daemon)
    async with connect(server) as client:
        await client.initialize()
        out = _payload(await client.call_tool("get_system_status", {}))
        assert out["error"]["code"] == "internal_error"
        assert "secret=abc123" not in out["error"]["message"]  # no internal/secret leakage
