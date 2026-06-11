"""Coverage-closing integration tests: every MCP resource URI family and the
remaining dashboard REST endpoints."""

from __future__ import annotations

import httpx
import pytest

from hermes_pm.dashboard.server import create_app
from hermes_pm.mcp.resources import resolve_resource

pytestmark = pytest.mark.asyncio


async def test_all_resource_uris_resolve(populated):
    daemon, cid = populated
    mid = daemon.db.list_campaigns()[0].watchlist[0]
    token = daemon.get_market_details(mid)["token_ids"]["YES"]
    event_id = daemon.get_audit_events(limit=1)[0]["event_id"]

    assert "mode" in resolve_resource(daemon, "system://status")
    assert "campaign" in resolve_resource(daemon, f"campaign://{cid}/summary")
    assert resolve_resource(daemon, f"market://{mid}")["market_id"] == mid
    assert resolve_resource(daemon, f"orderbook://{token}")  # book or {exists:False}
    assert resolve_resource(daemon, f"portfolio://paper/{cid}")["paper"] is True
    assert "policy" in resolve_resource(daemon, f"risk://limits/{cid}")
    assert "market_id" in resolve_resource(daemon, f"signals://{mid}/social")
    assert "lessons" in resolve_resource(daemon, f"lessons://campaign/{cid}")
    assert resolve_resource(daemon, f"audit://event/{event_id}")["event_id"] == event_id
    # malformed / unknown
    assert "error" in resolve_resource(daemon, "bogus://x/y")
    assert "error" in resolve_resource(daemon, "audit://event/nonexistent")


async def test_dashboard_remaining_endpoints(populated):
    daemon, cid = populated
    app = create_app(daemon)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/api/campaigns")).status_code == 200
        assert (await c.get(f"/api/campaign/{cid}/orders")).status_code == 200
        markets = (await c.get("/api/markets")).json()
        assert isinstance(markets, list) and markets
        mid = markets[0]["market_id"]
        sig = (await c.get(f"/api/market/{mid}/signals")).json()
        assert "summary" in sig and "evidence" in sig
        # lifecycle control endpoints
        assert (await c.post(f"/api/campaign/{cid}/pause")).json()["status"] == "paused"
        assert (await c.post(f"/api/campaign/{cid}/resume")).json()["status"] == "running"
        assert (await c.post(f"/api/campaign/{cid}/stop")).json()["status"] == "stopped"
        # unknown action -> 400
        assert (await c.post(f"/api/campaign/{cid}/frobnicate")).status_code == 400
        # report 404 for missing campaign
        assert (await c.get("/api/campaign/nope/report")).status_code == 404
