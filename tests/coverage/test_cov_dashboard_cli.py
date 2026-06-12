"""Coverage tests for the dashboard server and the CLI entrypoints.

Targets (previously-uncovered lines):

dashboard/server.py
  - trade_detail: unknown intent -> NotFoundError -> HTTP 404 (the except/raise).
  - control: pause / resume / stop + unknown-action 400; the emergency_stop POST.
  - /metrics: Prometheus exposition render.
  - /ws: localhost-skip of the token gate, accept, bus subscription, receive one
    event, send_json, disconnect; plus the non-localhost token-rejection branch.

cli.py
  - run_mcp / run_mcp_http / run_dashboard entrypoints.
  - _scripted_campaign skip branches: no-best-ask `continue` and
    rejected-schema-intent `continue`.
  - run_demo: the --no-serve path and the serve path (monkeypatched _serve).

Production code under src/ is not modified; everything is driven through the
public daemon facade, monkeypatching only test-local seams.
"""

from __future__ import annotations

import argparse
import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from hermes_pm import cli
from hermes_pm.dashboard.server import create_app
from hermes_pm.events import EventType


# --------------------------------------------------------------------------- #
# Dashboard REST endpoints
# --------------------------------------------------------------------------- #
async def _client(daemon):
    app = create_app(daemon)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_trade_detail_unknown_intent_returns_404(daemon):
    """server.py trade_detail: a missing intent raises -> HTTPException(404)."""
    async with await _client(daemon) as c:
        r = await c.get("/api/campaign/no-such-campaign/trade/no-such-intent")
    assert r.status_code == 404
    assert "not found" in r.json()["detail"].lower()


async def test_control_actions_and_emergency_stop(daemon):
    """server.py control: pause/resume/stop, unknown-action 400, and emergency_stop."""
    camp = daemon.start_paper_campaign(
        campaign_name="ctl", duration_hours=24, paper_bankroll_usd=500,
        market_filters={"categories": ["weather", "sports"]},
    )
    cid = camp["campaign_id"]
    async with await _client(daemon) as c:
        assert (await c.post(f"/api/campaign/{cid}/pause")).json()["status"] == "paused"
        assert (await c.post(f"/api/campaign/{cid}/resume")).json()["status"] == "running"
        assert (await c.post(f"/api/campaign/{cid}/stop")).json()["status"] == "stopped"
        bad = await c.post(f"/api/campaign/{cid}/frobnicate")
        assert bad.status_code == 400
        assert "unknown action" in bad.json()["detail"]
        emergency = (await c.post("/api/emergency_stop")).json()
    assert emergency["emergency_stop"] is True
    assert daemon.get_system_status()["emergency_stop"] is True


async def test_metrics_endpoint_renders_prometheus(daemon):
    """server.py /metrics returns the Prometheus exposition text."""
    async with await _client(daemon) as c:
        r = await c.get("/metrics")
    assert r.status_code == 200
    assert b"hpm_" in r.content


# --------------------------------------------------------------------------- #
# Dashboard websocket (/ws)
# --------------------------------------------------------------------------- #
async def test_ws_streams_published_bus_event(daemon):
    """server.py /ws: accept, subscribe to the bus, stream one published event."""
    app = create_app(daemon)
    payload = {"marker": "cov-ws-stream"}
    with TestClient(app) as client:
        base = daemon.bus.subscriber_count
        with client.websocket_connect("/ws") as ws:
            # Wait until the handler has registered its bus subscription.
            for _ in range(300):
                if daemon.bus.subscriber_count > base:
                    break
                await asyncio.sleep(0.01)
            assert daemon.bus.subscriber_count > base, "ws handler never subscribed"

            # Publish on the websocket's own event loop so the asyncio.Queue
            # wakeup is scheduled on the loop that the handler awaits on.
            async def _publish():
                daemon.bus.publish(EventType.SYSTEM_STATUS, payload)

            ws.portal.call(_publish)

            seen = None
            for _ in range(50):
                msg = ws.receive_json()
                assert set(msg) >= {"type", "data", "ts"}
                assert isinstance(msg["ts"], int)
                if (msg["type"] == EventType.SYSTEM_STATUS
                        and msg["data"].get("marker") == payload["marker"]):
                    seen = msg
                    break
    assert seen is not None, "did not receive the published system_status event"


async def test_ws_rejects_when_not_localhost_without_token(daemon):
    """server.py /ws: a non-localhost bind with a bad/missing token closes (1008)."""
    daemon.settings.dashboard_host = "10.0.0.5"
    daemon.settings.dashboard_token = "expected-token"
    app = create_app(daemon)
    with TestClient(app) as client, pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws"):
            pass


async def test_ws_handler_error_path_closes_socket(daemon, monkeypatch):
    """server.py /ws: an error while handling an event hits the except/suppress/close."""
    class _BoomHist:
        def observe(self, *args, **kwargs):
            raise RuntimeError("metric observe failed")

    monkeypatch.setattr(daemon.metrics, "dashboard_push_latency_ms", _BoomHist())
    app = create_app(daemon)
    with TestClient(app) as client:
        base = daemon.bus.subscriber_count
        with client.websocket_connect("/ws") as ws:
            for _ in range(300):
                if daemon.bus.subscriber_count > base:
                    break
                await asyncio.sleep(0.01)
            assert daemon.bus.subscriber_count > base

            async def _publish():
                daemon.bus.publish(EventType.SYSTEM_STATUS, {"marker": "boom"})

            ws.portal.call(_publish)
            # The handler raises inside the loop -> except Exception -> close().
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()


async def test_ws_accepts_when_not_localhost_with_valid_token(daemon):
    """server.py /ws: a non-localhost bind with a valid token proceeds to accept + stream."""
    daemon.settings.dashboard_host = "10.0.0.5"
    daemon.settings.dashboard_token = "expected-token"
    app = create_app(daemon)
    with TestClient(app) as client:
        base = daemon.bus.subscriber_count
        with client.websocket_connect("/ws?token=expected-token") as ws:
            for _ in range(300):
                if daemon.bus.subscriber_count > base:
                    break
                await asyncio.sleep(0.01)
            assert daemon.bus.subscriber_count > base

            async def _publish():
                daemon.bus.publish(EventType.SYSTEM_STATUS, {"marker": "tok-ok"})

            ws.portal.call(_publish)
            seen = None
            for _ in range(50):
                msg = ws.receive_json()
                if (msg["type"] == EventType.SYSTEM_STATUS
                        and msg["data"].get("marker") == "tok-ok"):
                    seen = msg
                    break
    assert seen is not None


# --------------------------------------------------------------------------- #
# Dashboard server entrypoints (_serve / run_dashboard)
# --------------------------------------------------------------------------- #
async def test_serve_builds_uvicorn_config(daemon, monkeypatch):
    """server.py _serve binds uvicorn to the configured dashboard host/port."""
    import uvicorn

    from hermes_pm.dashboard.server import _serve

    served = {}

    async def fake_serve(self, *args, **kwargs):
        served["host"] = self.config.host
        served["port"] = self.config.port

    monkeypatch.setattr(uvicorn.Server, "serve", fake_serve)
    await _serve(daemon)
    assert served["host"] == daemon.settings.dashboard_host
    assert served["port"] == daemon.settings.dashboard_port


def test_run_dashboard_server_entrypoint(monkeypatch, tmp_path):
    """server.py run_dashboard: load settings, start a daemon, serve, then stop."""
    from hermes_pm.config import load_settings as _real
    from hermes_pm.dashboard import server as srv

    served = {}

    async def fake_serve(daemon):
        served["daemon"] = daemon

    monkeypatch.setattr(srv, "_serve", fake_serve)
    monkeypatch.setattr(srv, "load_settings", lambda **kw: _real(data_dir=str(tmp_path), **kw))
    srv.run_dashboard()  # settings=None -> exercises `settings or load_settings()`
    assert served["daemon"] is not None
    assert str(served["daemon"].settings.data_dir) == str(tmp_path)


# --------------------------------------------------------------------------- #
# CLI entrypoints
# --------------------------------------------------------------------------- #
def _redirect_settings(monkeypatch, tmp_path):
    """Patch cli.load_settings so entrypoints don't create dirs in the CWD."""
    from hermes_pm.config import load_settings as _real

    def _fake(**overrides):
        overrides["data_dir"] = str(tmp_path)
        return _real(**overrides)

    monkeypatch.setattr(cli, "load_settings", _fake)


def test_run_mcp_entrypoint(monkeypatch, tmp_path):
    """cli.run_mcp imports run_stdio and drives it through asyncio.run."""
    _redirect_settings(monkeypatch, tmp_path)
    captured = {}

    def fake_run_stdio(settings):
        captured["settings"] = settings
        return "STDIO_COROUTINE"

    def fake_asyncio_run(arg):
        captured["ran"] = arg

    monkeypatch.setattr("hermes_pm.mcp.server.run_stdio", fake_run_stdio)
    monkeypatch.setattr(cli.asyncio, "run", fake_asyncio_run)
    cli.run_mcp()
    assert captured["ran"] == "STDIO_COROUTINE"
    assert captured["settings"] is not None


def test_run_mcp_http_entrypoint(monkeypatch, tmp_path):
    """cli.run_mcp_http loads settings with HTTP enabled and calls run_http."""
    _redirect_settings(monkeypatch, tmp_path)
    captured = {}

    def fake_run_http(settings):
        captured["settings"] = settings

    monkeypatch.setattr("hermes_pm.mcp.http_server.run_http", fake_run_http)
    cli.run_mcp_http()
    assert captured["settings"].mcp_http_enabled is True


def test_run_dashboard_entrypoint(monkeypatch, tmp_path):
    """cli.run_dashboard delegates to the dashboard server's run_dashboard."""
    _redirect_settings(monkeypatch, tmp_path)
    captured = {}

    def fake_run(settings=None):
        captured["settings"] = settings

    monkeypatch.setattr("hermes_pm.dashboard.server.run_dashboard", fake_run)
    cli.run_dashboard()
    assert captured["settings"] is not None


# --------------------------------------------------------------------------- #
# cli._scripted_campaign skip branches
# --------------------------------------------------------------------------- #
async def test_scripted_campaign_skips_when_no_best_ask(daemon, monkeypatch):
    """cli._scripted_campaign: candidates without a best ask hit the `continue`."""
    monkeypatch.setattr(daemon, "get_market_snapshot", lambda token_id: {})
    cid = await cli._scripted_campaign(daemon)
    assert isinstance(cid, str) and cid
    # Every candidate was skipped at the no-ask guard, so nothing was ordered.
    assert daemon.paper_get_orders(cid) == []
    # The post-loop lesson is still written.
    assert daemon.list_lessons(cid)


async def test_scripted_campaign_skips_rejected_intents(daemon, monkeypatch):
    """cli._scripted_campaign: schema-rejected intents hit the `continue`."""
    monkeypatch.setattr(
        daemon, "propose_trade_intent",
        lambda **kwargs: {"status": "rejected_schema", "error": {"code": "validation_error"}},
    )
    cid = await cli._scripted_campaign(daemon)
    assert isinstance(cid, str) and cid
    # Intents were rejected before any order could be placed.
    assert daemon.paper_get_orders(cid) == []


# --------------------------------------------------------------------------- #
# cli.run_demo
# --------------------------------------------------------------------------- #
class _FakeDaemon:
    """Stand-in for TradingDaemon so run_demo exercises without real services."""

    def __init__(self, settings):
        self.settings = settings
        self.stopped = False

    def get_dashboard_url(self, campaign_id=None):
        return f"http://fake/?campaign={campaign_id}"

    async def get_promotion_report(self, campaign_id):
        return {"verdicts": {"statistically_weak": True}}

    async def stop(self):
        self.stopped = True


def _fake_args(**kw):
    defaults = {"port": None, "no_serve": False}
    defaults.update(kw)
    return argparse.Namespace(**defaults)


def test_run_demo_no_serve(monkeypatch, tmp_path, capsys):
    """cli.run_demo --no-serve: run the campaign, print verdicts, stop, return."""
    _redirect_settings(monkeypatch, tmp_path)
    monkeypatch.setattr("argparse.ArgumentParser.parse_args",
                        lambda self: _fake_args(port=8799, no_serve=True))
    monkeypatch.setattr(cli, "TradingDaemon", _FakeDaemon)

    async def fake_scripted(daemon):
        assert isinstance(daemon, _FakeDaemon)
        return "demo-campaign"

    monkeypatch.setattr(cli, "_scripted_campaign", fake_scripted)
    cli.run_demo()
    out = capsys.readouterr().out
    assert "demo-campaign" in out
    assert "verdicts" in out


def test_run_demo_serves_dashboard(monkeypatch, tmp_path, capsys):
    """cli.run_demo (default): start the campaign then serve the dashboard."""
    _redirect_settings(monkeypatch, tmp_path)
    monkeypatch.setattr("argparse.ArgumentParser.parse_args",
                        lambda self: _fake_args(port=None, no_serve=False))
    monkeypatch.setattr(cli, "TradingDaemon", _FakeDaemon)
    served = {}

    async def fake_scripted(daemon):
        return "demo-serve"

    async def fake_serve(daemon):
        served["daemon"] = daemon

    monkeypatch.setattr(cli, "_scripted_campaign", fake_scripted)
    monkeypatch.setattr("hermes_pm.dashboard.server._serve", fake_serve)
    cli.run_demo()
    assert isinstance(served["daemon"], _FakeDaemon)
    assert served["daemon"].stopped is True
    assert "Dashboard:" in capsys.readouterr().out
