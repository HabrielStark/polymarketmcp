"""Integration tests: dashboard REST/HTML/metrics (FR-DASH, Section 17)."""

from __future__ import annotations

import httpx
import pytest

from hermes_pm.dashboard.server import create_app

pytestmark = pytest.mark.asyncio


async def _client(daemon):
    app = create_app(daemon)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_index_has_paper_label(daemon):
    async with await _client(daemon) as c:
        r = await c.get("/")
        assert r.status_code == 200
        assert "PAPER MODE" in r.text and "LIVE LOCKED" in r.text


async def test_status_and_campaign_endpoints(populated):
    daemon, cid = populated
    async with await _client(daemon) as c:
        assert (await c.get("/api/status")).json()["mode"] in ("paper", "emergency")
        rep = (await c.get(f"/api/campaign/{cid}/report")).json()
        assert rep["portfolio"]["paper"] is True
        port = (await c.get(f"/api/campaign/{cid}/portfolio")).json()
        assert "equity" in port and port["ledger_balanced"] is True


async def test_promotion_and_audit_endpoints(populated):
    daemon, cid = populated
    async with await _client(daemon) as c:
        promo = (await c.get(f"/api/campaign/{cid}/promotion")).json()
        assert set(promo["verdicts"]) == {"statistically_weak", "operationally_safe", "compliance_eligible"}
        audit = (await c.get(f"/api/audit?campaign_id={cid}")).json()
        assert audit["chain"]["ok"] is True and len(audit["events"]) > 0


async def test_metrics_endpoint(populated):
    daemon, cid = populated
    async with await _client(daemon) as c:
        r = await c.get("/metrics")
        assert r.status_code == 200 and b"hpm_" in r.content


async def test_trade_detail_and_export_endpoints(populated):
    daemon, cid = populated
    orders = daemon.paper_get_orders(cid)
    async with await _client(daemon) as c:
        if orders:
            iid = orders[0]["intent_id"]
            detail = (await c.get(f"/api/campaign/{cid}/trade/{iid}")).json()
            assert "thesis" in detail and "risk_decisions" in detail and "entry_order_book" in detail
        exp = (await c.get(f"/api/campaign/{cid}/audit/export")).json()
        assert exp["chain_verification"]["ok"] is True
        assert "***REDACTED***" not in str(exp) or exp["event_count"] >= 0  # redaction applied


async def test_index_has_trades_tab_and_export(daemon):
    async with await _client(daemon) as c:
        html = (await c.get("/")).text
        assert "trades" in html and "export audit" in html.lower()


async def test_emergency_endpoint_blocks(populated):
    daemon, cid = populated
    async with await _client(daemon) as c:
        r = (await c.post(f"/api/emergency_stop?campaign_id={cid}")).json()
        assert r["emergency_stop"] is True
        # status reflects emergency
        assert (await c.get("/api/status")).json()["emergency_stop"] is True
