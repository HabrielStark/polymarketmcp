"""Live Polymarket data client (opt-in; ``market_data_source == "live"``).

Implements the documented public surfaces [S5-S11]:
  * Gamma API for market discovery,
  * CLOB REST ``/book`` for order-book snapshots / reconciliation,
  * CLOB WebSocket *market* channel for live book + price-change events,
  * the geoblock availability check used by the live adapter (COMP-003).

Public market data needs no authentication [S6]. Rate-limit / Cloudflare
throttling is treated as a risk condition: a 429 raises ``RateLimitedError`` and
REST reads use bounded retry with backoff (FR-DATA-006)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import httpx

from hermes_pm.config import Settings
from hermes_pm.errors import RateLimitedError, UpstreamError
from hermes_pm.models import BookLevel, Market, OrderBookSnapshot
from hermes_pm.util.timeutil import now_ms


def _json_array(value: object) -> list:
    """Gamma encodes arrays as JSON strings (e.g. clobTokenIds). Decode safely."""
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def normalize_gamma_market(raw: dict) -> Market | None:
    """Map one Gamma market object to our :class:`Market` (FR-MD-001)."""
    token_ids = _json_array(raw.get("clobTokenIds"))
    outcomes = _json_array(raw.get("outcomes")) or ["Yes", "No"]
    market_id = str(raw.get("id") or raw.get("conditionId") or "")
    if not market_id:
        return None
    token_map: dict[str, str] = {}
    for i, outcome in enumerate(outcomes):
        if i < len(token_ids):
            token_map[str(outcome).upper()] = str(token_ids[i])
    events = raw.get("events") or []
    event_id = str(events[0].get("id")) if events and isinstance(events[0], dict) else market_id
    return Market(
        market_id=market_id,
        event_id=event_id,
        condition_id=str(raw.get("conditionId") or ""),
        question_id=str(raw.get("questionID") or raw.get("questionId") or ""),
        question=str(raw.get("question") or raw.get("title") or ""),
        category=str(raw.get("category") or "uncategorized"),
        outcomes=[str(o) for o in outcomes],
        token_ids=token_map,
        resolution_rules=str(raw.get("description") or raw.get("resolutionSource") or ""),
        resolution_source=str(raw.get("resolutionSource") or ""),
        source_links=[u for u in [raw.get("resolutionSource")] if u],
        end_time=raw.get("endDate"),
        enable_order_book=bool(raw.get("enableOrderBook", False)),
        tags=[str(t.get("label")) for t in (raw.get("tags") or []) if isinstance(t, dict)],
    )


def _book_from_clob(token_id: str, payload: dict, source: str) -> OrderBookSnapshot:
    bids = [
        BookLevel(price=float(b["price"]), size=float(b["size"]))
        for b in payload.get("bids", [])
        if 0.0 <= float(b["price"]) <= 1.0
    ]
    asks = [
        BookLevel(price=float(a["price"]), size=float(a["size"]))
        for a in payload.get("asks", [])
        if 0.0 <= float(a["price"]) <= 1.0
    ]
    bids.sort(key=lambda x: x.price, reverse=True)
    asks.sort(key=lambda x: x.price)
    seq_raw = payload.get("timestamp") or payload.get("seq") or now_ms()
    try:
        seq = int(seq_raw)
    except (TypeError, ValueError):
        seq = now_ms()
    return OrderBookSnapshot(
        token_id=token_id, bids=bids, asks=asks,
        last_trade=float(payload["price"]) if payload.get("price") else None,
        sequence=seq, source=source, received_at=now_ms(),
    )


class PolymarketSource:
    name = "live"

    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(10.0))

    async def _get(self, url: str, params: dict | None = None, retries: int = 3) -> object:
        backoff = 0.5
        last_exc: Exception | None = None
        for _ in range(retries):
            try:
                resp = await self._client.get(url, params=params)
                if resp.status_code == 429:
                    raise RateLimitedError(f"rate limited: {url}", retry_after=resp.headers.get("retry-after"))
                resp.raise_for_status()
                return resp.json()
            except RateLimitedError as exc:
                last_exc = exc
                await asyncio.sleep(backoff)
                backoff *= 2
            except httpx.HTTPError as exc:
                last_exc = exc
                await asyncio.sleep(backoff)
                backoff *= 2
        raise UpstreamError(f"GET failed after {retries} attempts: {url}", cause=str(last_exc))

    async def discover_markets(self) -> list[Market]:
        data = await self._get(f"{self._s.gamma_base_url}/markets", params={"active": "true", "limit": 200})
        rows = data if isinstance(data, list) else data.get("data", []) if isinstance(data, dict) else []
        out: list[Market] = []
        for raw in rows:
            if isinstance(raw, dict):
                m = normalize_gamma_market(raw)
                if m is not None:
                    out.append(m)
        return out

    async def snapshot(self, token_id: str) -> OrderBookSnapshot | None:
        data = await self._get(f"{self._s.clob_base_url}/book", params={"token_id": token_id})
        if not isinstance(data, dict):
            return None
        return _book_from_clob(token_id, data, self.name)

    async def stream(
        self, token_ids: list[str], interval_ms: int
    ) -> AsyncIterator[OrderBookSnapshot]:
        import websockets  # local import: only needed for live mode

        async with websockets.connect(self._s.clob_ws_url, ping_interval=20) as ws:
            await ws.send(json.dumps({"type": "market", "assets_ids": token_ids}))
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                events = msg if isinstance(msg, list) else [msg]
                for ev in events:
                    if not isinstance(ev, dict):
                        continue
                    tid = ev.get("asset_id") or ev.get("token_id")
                    if ev.get("event_type") == "book" and tid:
                        yield _book_from_clob(str(tid), ev, self.name)

    async def geoblock_check(self) -> dict:
        """Return availability for the operator's region (COMP-003, FR-LIVE-003).
        Fail-closed: any error is treated as 'blocked' for live-mode safety."""
        try:
            data = await self._get(self._s.geoblock_url, retries=1)
            if isinstance(data, dict):
                blocked = bool(data.get("blocked", False))
                return {"blocked": blocked, "raw": data}
            return {"blocked": True, "raw": data}
        except Exception as exc:  # noqa: BLE001 - fail-closed by design
            return {"blocked": True, "error": str(exc)}

    async def close(self) -> None:
        await self._client.aclose()
