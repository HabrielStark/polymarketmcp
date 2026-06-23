"""Dashboard server (FR-DASH-001..006, Section 17).

Serves a local single-page UI and streams daemon events over a WebSocket so the
operator sees campaigns, P&L, drawdown, exposures, orders, fills, intents, risk
decisions, evidence, lessons, and the promotion report in real time. Every money
view is labelled PAPER (17.2) and stale/locked states are surfaced. Binds to
localhost by default; a token is required if bound elsewhere (NFR-SEC-006)."""

from __future__ import annotations

import contextlib
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, PlainTextResponse

from hermes_pm import __version__
from hermes_pm.config import Settings, load_settings
from hermes_pm.daemon.core import TradingDaemon
from hermes_pm.dashboard.ui import INDEX_HTML
from hermes_pm.util.security import tokens_match
from hermes_pm.util.timeutil import now_ms


def _bearer_token(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


def _check_token(settings: Settings, request: Request, query_token: str | None = None) -> None:
    # Local-only by default; require a token only when exposed beyond localhost.
    # Constant-time compare so a remote attacker can't recover the token by timing.
    if query_token is not None:
        raise HTTPException(status_code=400, detail="use Authorization: Bearer, not query tokens")
    if settings.dashboard_host not in ("127.0.0.1", "localhost", "::1"):
        if not tokens_match(settings.dashboard_token, _bearer_token(request)):
            raise HTTPException(status_code=401, detail="dashboard access token required")


def _check_origin(settings: Settings, request: Request) -> None:
    origin = request.headers.get("origin")
    if not origin:
        return
    host = urlparse(origin).hostname
    allowed = {settings.dashboard_host, "127.0.0.1", "localhost", "::1"}
    if host not in allowed:
        raise HTTPException(status_code=403, detail="invalid dashboard origin")


def create_app(daemon: TradingDaemon) -> FastAPI:
    app = FastAPI(title="Hermes-PM Dashboard", version=__version__)
    s = daemon.settings

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return INDEX_HTML

    @app.get("/api/status")
    async def status(request: Request, token: str | None = Query(None)):
        _check_token(s, request, token)
        return daemon.get_system_status()

    @app.get("/api/campaigns")
    async def campaigns(request: Request, token: str | None = Query(None)):
        _check_token(s, request, token)
        return [c.model_dump(mode="json") for c in daemon.db.list_campaigns()]

    @app.get("/api/campaign/{cid}/report")
    async def report(cid: str, request: Request, token: str | None = Query(None)):
        _check_token(s, request, token)
        try:
            return daemon.get_campaign_report(cid)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(404, str(exc)) from exc

    @app.get("/api/campaign/{cid}/portfolio")
    async def portfolio(cid: str, request: Request, token: str | None = Query(None)):
        _check_token(s, request, token)
        return daemon.paper_get_portfolio(cid)

    @app.get("/api/campaign/{cid}/orders")
    async def orders(cid: str, request: Request, token: str | None = Query(None)):
        _check_token(s, request, token)
        return daemon.paper_get_orders(cid)

    @app.get("/api/campaign/{cid}/trade/{intent_id}")
    async def trade_detail(
        cid: str, intent_id: str, request: Request, token: str | None = Query(None)
    ):
        _check_token(s, request, token)
        try:
            return daemon.get_trade_detail(cid, intent_id)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(404, str(exc)) from exc

    @app.get("/api/campaign/{cid}/audit/export")
    async def audit_export(cid: str, request: Request, token: str | None = Query(None)):
        _check_token(s, request, token)
        return daemon.export_campaign_audit(cid)

    @app.get("/api/campaign/{cid}/promotion")
    async def promotion(cid: str, request: Request, token: str | None = Query(None)):
        _check_token(s, request, token)
        return await daemon.get_promotion_report(cid)

    @app.get("/api/markets")
    async def markets(request: Request, token: str | None = Query(None)):
        _check_token(s, request, token)
        return daemon.search_markets({"require_order_book": False}, limit=100)

    @app.get("/api/market/{mid}/signals")
    async def signals(mid: str, request: Request, token: str | None = Query(None)):
        _check_token(s, request, token)
        return {"summary": daemon.get_social_signal_summary(mid),
                "evidence": daemon.get_source_evidence(mid)}

    @app.get("/api/audit")
    async def audit(
        request: Request,
        campaign_id: str | None = Query(None),
        limit: int = Query(100),
        token: str | None = Query(None),
    ):
        _check_token(s, request, token)
        return {"chain": daemon.audit.verify_chain(campaign_id),
                "events": daemon.get_audit_events(campaign_id, limit)}

    @app.post("/api/campaign/{cid}/{action}")
    async def control(
        cid: str, action: str, request: Request, token: str | None = Query(None)
    ):
        _check_token(s, request, token)
        _check_origin(s, request)
        if action == "pause":
            return daemon.pause_campaign(cid)
        if action == "resume":
            return daemon.resume_campaign(cid)
        if action == "stop":
            return daemon.stop_campaign(cid)
        raise HTTPException(400, f"unknown action: {action}")

    @app.post("/api/emergency_stop")
    async def emergency(
        request: Request, campaign_id: str | None = Query(None), token: str | None = Query(None)
    ):
        _check_token(s, request, token)
        _check_origin(s, request)
        return daemon.emergency_stop(campaign_id)

    @app.get("/metrics", response_class=PlainTextResponse)
    async def metrics(request: Request, token: str | None = Query(None)) -> bytes:
        _check_token(s, request, token)
        return daemon.metrics.render()

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        # Enforce the same token policy as REST when not bound to localhost.
        if s.dashboard_host not in ("127.0.0.1", "localhost", "::1"):
            if websocket.query_params.get("token") is not None:
                await websocket.close(code=1008)
                return
            protocols = [
                p.strip() for p in websocket.headers.get("sec-websocket-protocol", "").split(",")
            ]
            token = next((p.removeprefix("hpm-token-") for p in protocols
                          if p.startswith("hpm-token-")), None)
            if not tokens_match(s.dashboard_token, token):
                await websocket.close(code=1008)
                return
        await websocket.accept()
        try:
            with daemon.bus.subscription() as q:
                while True:
                    event = await q.get()
                    daemon.metrics.dashboard_push_latency_ms.observe(max(0, now_ms() - event.ts))
                    await websocket.send_json({"type": event.type, "data": event.data, "ts": event.ts})
        except WebSocketDisconnect:  # pragma: no cover - only a live ASGI client disconnect mid-stream
            return
        except Exception:  # noqa: BLE001
            with contextlib.suppress(Exception):
                await websocket.close()

    return app


async def _serve(daemon: TradingDaemon) -> None:
    import uvicorn

    s = daemon.settings
    config = uvicorn.Config(create_app(daemon), host=s.dashboard_host, port=s.dashboard_port,
                            log_level="warning")
    await uvicorn.Server(config).serve()


def run_dashboard(settings: Settings | None = None) -> None:
    import asyncio

    settings = settings or load_settings()

    async def main() -> None:
        daemon = TradingDaemon(settings)
        await daemon.start()
        try:
            await _serve(daemon)
        finally:
            await daemon.stop()

    asyncio.run(main())
