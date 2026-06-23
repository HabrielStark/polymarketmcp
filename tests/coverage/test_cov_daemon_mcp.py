"""Coverage stage: DAEMON + MCP.

Real behavioural tests that drive the specific uncovered branches in:
  * ``hermes_pm.daemon.core`` — market-data source selection, lifecycle/consume
    branches, geoblock-on-live, risk-context (re)build edges, search filters,
    not-found tool paths, campaign/intent/paper/live tool branches, postmortem
    no-fill path, purge, lessons, audit export, promotion + lifecycle state errors.
  * ``hermes_pm.mcp.server`` — unknown-tool, bad-request (TypeError/ValueError),
    resource-template listing, unknown-prompt path, and ``run_stdio`` wiring.
  * ``hermes_pm.mcp.http_server`` — token-not-configured fast path, ``_serve``,
    and ``run_http``.

Production code under ``src/`` is treated as frozen; these tests only observe it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.server.lowlevel import Server
from mcp.shared.memory import create_connected_server_and_client_session as connect

from hermes_pm.config import load_settings
from hermes_pm.daemon.core import TradingDaemon, make_source
from hermes_pm.data.polymarket_client import PolymarketSource
from hermes_pm.data.sources import ReplaySource, SyntheticSource
from hermes_pm.errors import EmergencyStopError, NotFoundError, StateError, ValidationError
from hermes_pm.events import EventType
from hermes_pm.mcp import http_server
from hermes_pm.mcp.server import build_server, run_stdio
from hermes_pm.mcp.tools import TOOL_SPECS


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
async def _started_campaign(daemon: TradingDaemon, name: str = "cov") -> tuple[str, str]:
    camp = daemon.start_paper_campaign(
        campaign_name=name, duration_hours=48, paper_bankroll_usd=1000,
        market_filters={"categories": ["weather", "sports"]},
    )
    return camp["campaign_id"], camp["watchlist"][0]


async def _good_intent(daemon: TradingDaemon, cid: str, mid: str, *, size: float = 5.0,
                       thesis: str = "model edge") -> dict:
    await daemon.gather_evidence(mid)
    refs = [e["source_ref"] for e in daemon.get_source_evidence(mid)
            if e["source_type"] in ("primary", "secondary")][:2]
    tok = daemon.get_market_details(mid)["token_ids"]["YES"]
    snap = daemon.get_market_snapshot(tok)
    lp = min(0.98, round((snap.get("best_ask") or 0.5) + 0.02, 2))
    return daemon.propose_trade_intent(
        campaign_id=cid, market_id=mid, outcome="YES", side="BUY", limit_price=lp,
        max_size_usd=size, thesis=thesis, counter_thesis="market may be right",
        invalidation_criteria="resolves before campaign close", evidence_refs=refs,
        confidence=0.62, expires_at="2026-12-30T00:00:00Z",
    )


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def _http_server(tmp_path, token):
    port = _free_port()
    settings = load_settings(
        data_dir=str(tmp_path), db_filename="cov_http.sqlite3", mcp_http_enabled=True,
        mcp_http_host="127.0.0.1", mcp_http_port=port, mcp_http_token=token,
    )
    daemon = TradingDaemon(settings)
    app = http_server.create_http_app(daemon)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    task = asyncio.create_task(server.serve())
    for _ in range(50):
        await asyncio.sleep(0.05)
        if server.started:
            break
    return server, task, port


# --------------------------------------------------------------------------- #
# make_source: market-data source selection (core.py 52, 54-56)
# --------------------------------------------------------------------------- #
async def test_make_source_live_returns_polymarket(tmp_path):
    src = make_source(load_settings(data_dir=str(tmp_path), market_data_source="live"))
    try:
        assert isinstance(src, PolymarketSource)  # core.py line 52
        assert src.name == "live"
    finally:
        await src.close()


def test_make_source_replay_requires_file(tmp_path):
    # replay source with no recording file configured -> config error (core.py 54-55)
    with pytest.raises(ValidationError):
        make_source(load_settings(data_dir=str(tmp_path), market_data_source="replay"))


async def test_make_source_replay_with_file_and_daemon_init(tmp_path):
    syn = SyntheticSource(seed=1, market_count=2)
    markets = await syn.discover_markets()
    toks = [t for m in markets for t in m.token_ids.values()]
    snaps = syn.record(toks, steps=2)
    rec = tmp_path / "rec.json"
    ReplaySource.write_recording(rec, markets, snaps)

    src = make_source(load_settings(
        data_dir=str(tmp_path), market_data_source="replay", replay_file=str(rec)))
    assert isinstance(src, ReplaySource)  # core.py line 56
    assert len(await src.discover_markets()) == len(markets)

    # The daemon constructor also routes through make_source for the replay source.
    daemon = TradingDaemon(load_settings(
        data_dir=str(tmp_path), db_filename="replay.sqlite3",
        market_data_source="replay", replay_file=str(rec)))
    try:
        assert isinstance(daemon.source, ReplaySource)
        assert daemon.get_system_status()["market_data_source"] == "replay"
    finally:
        daemon.db.close()


# --------------------------------------------------------------------------- #
# Lifecycle + market-data consumer branches (core.py 102-104, 109, 128)
# --------------------------------------------------------------------------- #
async def test_daemon_start_stop_idempotent(settings):
    daemon = TradingDaemon(settings)
    try:
        await daemon.start()
        assert daemon._started is True
        await daemon.start()  # early-return branch (already started)
        assert daemon._started is True
        await daemon.stop()
        assert daemon._started is False  # core.py line 128
        await daemon.stop()  # second stop is tolerated
        assert daemon._started is False
    finally:
        daemon.db.close()


async def test_consume_market_data_throttle_and_gap_branches(daemon):
    before = daemon.metrics.api_throttles._value.get()
    daemon.bus.publish(EventType.CONNECTIVITY, {"status": "throttled"})  # core.py 102-103
    daemon.bus.publish(EventType.CONNECTIVITY, {"status": "ok"})  # connectivity continue (104)
    daemon.bus.publish(EventType.MARKET_DATA, {"token_id": None})  # no token -> continue (109)
    daemon.bus.publish(
        EventType.MARKET_DATA, {"token_id": "tok-x", "reconcile_gap": True})  # gap -> continue (109)
    await asyncio.sleep(0.15)

    after = daemon.metrics.api_throttles._value.get()
    assert after >= before + 1  # only the throttled connectivity event bumps the counter
    # the malformed/gap events did not crash the consumer loop
    assert any(not t.done() for t in daemon._bg)
    assert daemon.get_system_status()["mode"] in ("paper", "emergency")


# --------------------------------------------------------------------------- #
# Geoblock check (core.py 132 live branch + 133 synthetic branch)
# --------------------------------------------------------------------------- #
async def test_geoblock_check_live_source(tmp_path, monkeypatch):
    daemon = TradingDaemon(load_settings(data_dir=str(tmp_path), market_data_source="live"))
    try:
        async def fake_geoblock():
            return {"blocked": False, "raw": {"region": "allowed"}}

        monkeypatch.setattr(daemon.source, "geoblock_check", fake_geoblock)
        result = await daemon._geoblock_check()  # isinstance PolymarketSource -> core.py 132
        assert result["blocked"] is False
    finally:
        await daemon.source.close()
        daemon.db.close()


async def test_geoblock_check_synthetic_is_blocked(daemon):
    result = await daemon._geoblock_check()  # no live data source -> fail-closed
    assert result["blocked"] is True


# --------------------------------------------------------------------------- #
# Risk-context build + rebuild edges (core.py 193, 241, 246)
# --------------------------------------------------------------------------- #
async def test_build_risk_context_missing_market_raises(populated):
    daemon, cid = populated
    intents = daemon.db.list_intents(cid)
    assert intents
    campaign = daemon.db.get_campaign(cid)
    bad = intents[0].model_copy(update={"market_id": "no-such-market"})
    with pytest.raises(NotFoundError):
        daemon._build_risk_context(campaign, bad)  # core.py line 193


async def test_rebuild_risk_context_returns_none(daemon):
    assert daemon.rebuild_risk_context("missing-decision") is None  # core.py line 241
    daemon.db.kv_set("risk_ctx:fake", {"intent_id": "x", "market_id": "y", "campaign_id": "z"})
    assert daemon.rebuild_risk_context("fake") is None  # core.py line 246 (refs unresolvable)


# --------------------------------------------------------------------------- #
# search_markets liquidity/spread filters + limit break (core.py 348, 350, 354)
# --------------------------------------------------------------------------- #
async def test_search_markets_filters_and_limit(daemon):
    base = daemon.search_markets({})
    assert len(base) >= 2
    assert len(daemon.search_markets({"min_liquidity_usd": 1e12})) < len(base)  # 348
    assert len(daemon.search_markets({"min_liquidity": 1e12})) < len(base)  # public alias
    assert len(daemon.search_markets({"min_volume": 1e12})) < len(base)  # static Gamma volume
    assert len(daemon.search_markets({"max_spread": -1.0})) < len(base)  # 350
    assert len(daemon.search_markets({}, limit=1)) == 1  # limit break (354)


# --------------------------------------------------------------------------- #
# Not-found / no-book tool paths
# (core.py 367, 398, 422, 437, 474, 534, 617, 628, 658, 781)
# --------------------------------------------------------------------------- #
async def test_tools_not_found_and_missing_book(daemon):
    with pytest.raises(NotFoundError):
        daemon.get_market_details("nope")
    with pytest.raises(NotFoundError):
        daemon.get_resolution_rules("nope")  # 367
    assert daemon.get_order_book("nope")["exists"] is False  # 398
    assert daemon.get_liquidity_summary("nope")["exists"] is False  # 422
    with pytest.raises(NotFoundError):
        await daemon.gather_evidence("nope")  # 437
    with pytest.raises(NotFoundError):
        daemon._require_market("nope")  # 474
    with pytest.raises(NotFoundError):
        daemon.get_trade_detail("any-cid", "nope")  # 534
    with pytest.raises(NotFoundError):
        daemon.simulate_trade_intent("nope")  # 617
    with pytest.raises(NotFoundError):
        daemon.risk_check_trade_intent("nope")  # 628
    with pytest.raises(NotFoundError):
        daemon.explain_risk_rejection("nope")  # 658
    with pytest.raises(NotFoundError):
        daemon.generate_postmortem("any-cid", "nope")  # 781


# --------------------------------------------------------------------------- #
# start_paper_campaign validation rejection (core.py 494-495)
# --------------------------------------------------------------------------- #
async def test_start_paper_campaign_validation_rejected(daemon):
    out = daemon.start_paper_campaign(campaign_name="bad", duration_hours=0, paper_bankroll_usd=1000)
    assert out["status"] == "rejected" and "error" in out  # 494-495
    out2 = daemon.start_paper_campaign(campaign_name="bad2", duration_hours=24, paper_bankroll_usd=0)
    assert out2["status"] == "rejected" and "error" in out2


# --------------------------------------------------------------------------- #
# propose_trade_intent schema rejection from the intent layer (core.py 600)
# --------------------------------------------------------------------------- #
async def test_propose_trade_intent_rejected_schema(populated):
    daemon, cid = populated
    mid = daemon.db.get_campaign(cid).watchlist[0]
    out = daemon.propose_trade_intent(
        campaign_id=cid, market_id=mid, outcome="MAYBE", side="BUY", limit_price=0.5,
        max_size_usd=10, thesis="t", expires_at="2026-12-30T00:00:00Z",
    )
    assert out["status"] == "rejected_schema" and "error" in out  # 600


async def test_simulate_trade_intent_success(populated):
    daemon, cid = populated
    intents = daemon.db.list_intents(cid)
    assert intents
    out = daemon.simulate_trade_intent(intents[0].intent_id)
    assert out["trade_intent_id"] == intents[0].intent_id
    assert "simulated" in out and "break_even_probability" in out


# --------------------------------------------------------------------------- #
# paper_place_order guard branches (core.py 674, 677, 680-681)
# --------------------------------------------------------------------------- #
async def test_paper_place_order_decision_intent_mismatch(daemon):
    cid, mid = await _started_campaign(daemon)
    i1 = await _good_intent(daemon, cid, mid, size=5.0)
    i2 = await _good_intent(daemon, cid, mid, size=8.0, thesis="second thesis")
    assert i1["trade_intent_id"] != i2["trade_intent_id"]
    rc2 = daemon.risk_check_trade_intent(i2["trade_intent_id"])
    with pytest.raises(ValidationError):
        # decision belongs to i2, intent is i1 -> mismatch (core.py 674)
        daemon.paper_place_order(i1["trade_intent_id"], rc2["risk_decision_id"])


async def test_paper_place_order_campaign_not_running(daemon):
    cid, mid = await _started_campaign(daemon)
    intent = await _good_intent(daemon, cid, mid, size=5.0)
    rc = daemon.risk_check_trade_intent(intent["trade_intent_id"])
    daemon.stop_campaign(cid)
    with pytest.raises(StateError):
        daemon.paper_place_order(intent["trade_intent_id"], rc["risk_decision_id"])  # 677


async def test_paper_place_order_reject_decision_rejected(daemon):
    cid, mid = await _started_campaign(daemon)
    tok = daemon.get_market_details(mid)["token_ids"]["YES"]
    snap = daemon.get_market_snapshot(tok)
    lp = min(0.98, round((snap.get("best_ask") or 0.5) + 0.02, 2))
    # no evidence + no counter-thesis -> deterministic REJECT from the risk engine
    bad = daemon.propose_trade_intent(
        campaign_id=cid, market_id=mid, outcome="YES", side="BUY", limit_price=lp,
        max_size_usd=10, thesis="thin", evidence_refs=[], counter_thesis="",
        invalidation_criteria="", confidence=0.5, expires_at="2026-12-30T00:00:00Z",
    )
    rc = daemon.risk_check_trade_intent(bad["trade_intent_id"])
    assert rc["decision"] == "reject"
    out = daemon.paper_place_order(bad["trade_intent_id"], rc["risk_decision_id"])  # 680-681
    assert out["status"] == "rejected" and "error" in out
    assert "hpm_fill_sim_errors_total 1.0" in daemon.metrics.render().decode()


async def test_paper_cancel_order(daemon):
    cid, mid = await _started_campaign(daemon)
    intent = await _good_intent(daemon, cid, mid, size=5.0)
    rc = daemon.risk_check_trade_intent(intent["trade_intent_id"])
    assert rc["decision"] in ("approve", "modify")
    order = daemon.paper_place_order(intent["trade_intent_id"], rc["risk_decision_id"])
    out = daemon.paper_cancel_order(order["paper_order_id"])  # core.py 695-696
    assert out["paper_order_id"] == order["paper_order_id"]
    assert out["status"] in ("cancelled", "filled", "partially_filled", "open", "accepted")


# --------------------------------------------------------------------------- #
# Locked live reference-only methods (core.py 744-745 + cancel/place)
# --------------------------------------------------------------------------- #
async def test_live_methods_reference_only(daemon):
    blocked = await daemon.live_place_order_intent("intent-x", "decision-y")
    assert blocked["status"] == "blocked"
    cancel = await daemon.live_cancel_order("order-ref-1")
    assert cancel["status"] == "cancel_only" and cancel["cancelled"] is True
    assert await daemon.live_get_open_orders() == []  # core.py 744-745


# --------------------------------------------------------------------------- #
# generate_postmortem no-fill/no-position path (core.py 788)
# --------------------------------------------------------------------------- #
async def test_generate_postmortem_no_order(daemon):
    cid, mid = await _started_campaign(daemon)
    intent = await _good_intent(daemon, cid, mid, size=5.0)  # created but never placed
    pm = daemon.generate_postmortem(cid, intent["trade_intent_id"])  # core.py 788
    assert pm["outcome"] == "no_fill_or_position"


# --------------------------------------------------------------------------- #
# purge / lessons / audit-export / promotion-edge / lifecycle state errors
# --------------------------------------------------------------------------- #
async def test_purge_old_signals(populated):
    daemon, _cid = populated
    out = daemon.purge_old_signals(retention_hours=0.0)
    assert "removed" in out and out["retention_hours"] == 0.0
    keep = daemon.purge_old_signals(retention_hours=168.0)
    assert "removed" in keep and keep["retention_hours"] == 168.0
    purged = daemon.get_audit_events(event_type="signals_purged")
    assert purged and all(e["type"] == "signals_purged" for e in purged)


async def test_list_lessons_and_write_lesson_variant(populated):
    daemon, cid = populated
    assert daemon.list_lessons(cid)  # scripted campaign wrote a lesson
    assert isinstance(daemon.list_lessons(), list)  # no-campaign-filter variant
    lesson = daemon.write_lesson(
        cid, trigger="t", observation="o", rule="r", pattern="p", confidence=0.7,
        valid_until="2027-01-01T00:00:00Z", source_refs=["off://1"],
        memory_target="active", supporting_evidence_count=3, human_confirmed=True,
    )
    # human_confirmed=True keeps the ACTIVE target (no FR-LEARN-006 downgrade).
    assert lesson["rule"] == "r" and lesson["memory_target"] == "active"
    assert lesson["supporting_evidence_count"] == 3


async def test_export_campaign_audit_and_event_filter(populated):
    daemon, cid = populated
    exp = daemon.export_campaign_audit(cid)
    assert isinstance(exp, dict)
    events = daemon.get_audit_events(cid, limit=10, event_type="campaign_started")
    assert events and all(e["type"] == "campaign_started" for e in events)


async def test_get_promotion_report_not_found(daemon):
    with pytest.raises(NotFoundError):
        await daemon.get_promotion_report("no-such-campaign")


async def test_campaign_lifecycle_state_errors(daemon):
    cid, _mid = await _started_campaign(daemon)
    with pytest.raises(StateError):  # cannot resume a RUNNING campaign
        daemon.resume_campaign(cid)
    assert daemon.pause_campaign(cid)["status"] == "paused"
    with pytest.raises(StateError):  # cannot pause an already-paused campaign
        daemon.pause_campaign(cid)
    assert daemon.resume_campaign(cid)["status"] == "running"
    assert daemon.stop_campaign(cid)["status"] == "stopped"
    with pytest.raises(StateError):  # cannot stop a stopped campaign
        daemon.stop_campaign(cid)


async def test_emergency_stop_without_campaign_and_reset(daemon):
    await _started_campaign(daemon)
    res = daemon.emergency_stop()  # no campaign_id -> targets all active campaigns
    assert res["emergency_stop"] is True and res["audit_event_id"]
    with pytest.raises(EmergencyStopError):
        daemon.start_paper_campaign(campaign_name="x", duration_hours=24, paper_bankroll_usd=500)
    assert daemon.reset_emergency()["emergency_stop"] is False


# --------------------------------------------------------------------------- #
# MCP server branches (server.py 56, 79, 104, 138-149)
# --------------------------------------------------------------------------- #
async def test_mcp_unknown_tool(daemon):
    server = build_server(daemon)
    async with connect(server) as client:
        await client.initialize()
        out = json.loads((await client.call_tool("definitely_not_a_tool", {})).content[0].text)
        assert out["error"]["code"] == "unknown_tool"  # server.py 56


async def test_mcp_bad_request_on_value_error(daemon, monkeypatch):
    def boom(**_kw):
        raise ValueError("bad numeric value")

    monkeypatch.setattr(daemon, "get_system_status", boom)
    server = build_server(daemon)
    async with connect(server) as client:
        await client.initialize()
        out = json.loads((await client.call_tool("get_system_status", {})).content[0].text)
        assert out["error"]["code"] == "bad_request"  # server.py 79


async def test_mcp_resource_templates_and_prompt_errors(daemon):
    server = build_server(daemon)
    async with connect(server) as client:
        await client.initialize()
        tpl = await client.list_resource_templates()  # server.py 104-107
        assert "system://status" in {t.uriTemplate for t in tpl.resourceTemplates}
        gp = await client.get_prompt("no_such_prompt", {})  # get_prompt else branch
        assert "Unknown prompt" in gp.messages[0].content.text
        bad = json.loads((await client.read_resource("weird://nope")).contents[0].text)
        assert "error" in bad  # resolve_resource unknown-uri branch
        miss = json.loads((await client.read_resource("audit://event/none")).contents[0].text)
        assert miss["error"] == "event not found"


async def test_run_stdio_wires_server_and_stops(settings, monkeypatch):
    state: dict[str, bool] = {}

    @contextlib.asynccontextmanager
    async def fake_stdio_server():
        state["entered"] = True
        yield (object(), object())

    async def fake_run(self, _read, _write, _init_opts):
        state["ran"] = True

    monkeypatch.setattr("mcp.server.stdio.stdio_server", fake_stdio_server)
    monkeypatch.setattr(Server, "run", fake_run)
    await run_stdio(settings)  # server.py 138-149
    assert state.get("entered") and state.get("ran")


# --------------------------------------------------------------------------- #
# HTTP transport branches (http_server.py 52->57, 72-77, 81-84)
# --------------------------------------------------------------------------- #
async def test_http_no_token_skips_auth(tmp_path):
    # No token configured -> the `if token:` guard is skipped (52->57) and the
    # request reaches the session manager; a full handshake must succeed.
    server, task, port = await _http_server(tmp_path, token=None)
    url = f"http://127.0.0.1:{port}/mcp"
    try:
        async with streamablehttp_client(url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert len(tools.tools) == len(TOOL_SPECS)
    finally:
        server.should_exit = True
        await task


async def test_serve_builds_config_and_calls_serve(settings, monkeypatch):
    served: dict[str, bool] = {}

    async def fake_serve(self):
        served["called"] = True

    monkeypatch.setattr(uvicorn.Server, "serve", fake_serve)
    daemon = TradingDaemon(settings)
    try:
        await http_server._serve(daemon)  # http_server.py 72-77
        assert served["called"] is True
    finally:
        daemon.db.close()


def test_run_http_invokes_serve(settings, monkeypatch):
    captured: dict[str, object] = {}

    async def fake_serve(daemon):
        captured["daemon"] = daemon
        daemon.db.close()

    monkeypatch.setattr(http_server, "_serve", fake_serve)
    http_server.run_http(settings)  # http_server.py 81-84
    assert captured.get("daemon") is not None


async def test_http_missing_token_rejected(tmp_path):
    # Complements the no-token path: when a token IS configured, a request without
    # the bearer header is rejected before the MCP machinery (53-56).
    server, task, port = await _http_server(tmp_path, token="COV-TOKEN")
    try:
        async with httpx.AsyncClient(follow_redirects=True) as c:
            resp = await c.post(
                f"http://127.0.0.1:{port}/mcp/",
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                headers={"Origin": f"http://127.0.0.1:{port}"},
            )
            assert resp.status_code == 401
    finally:
        server.should_exit = True
        await task
