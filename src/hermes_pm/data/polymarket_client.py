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
from hermes_pm.util.sanitize import sanitize_untrusted
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


def _parse_float(value: object) -> float | None:
    """Parse a Gamma numeric (often delivered as a string) into a float, or None."""
    if value is None or value == "":
        return None
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if out != out or out in (float("inf"), float("-inf")):  # NaN / inf guard
        return None
    return out


# Generic Polymarket tags that do not, on their own, describe a tradable topic
# well enough to serve as a risk-exposure *category*.
_GENERIC_TAGS = frozenset(
    {"all", "trending", "new", "featured", "popular", "recurring", "hide from new", "weekly"}
)


def _tag_labels(obj: dict) -> list[str]:
    """Extract human tag labels from a market/event ``tags`` relation."""
    out: list[str] = []
    for tag in obj.get("tags") or []:
        if isinstance(tag, dict):
            label = tag.get("label") or tag.get("slug")
            if label and str(label).strip():
                out.append(str(label).strip())
        elif isinstance(tag, str) and tag.strip():
            out.append(tag.strip())
    return out


def _derive_category(tags: list[str], events: list) -> str:
    """Real Gamma markets carry no ``category`` field; their topic lives in the
    tag taxonomy (Politics / Crypto / Sports / ...). The risk engine caps exposure
    *per category* (SRS 14.1), so pick the first non-generic tag; else fall back to
    the parent event's slug; else ``uncategorized``."""
    for label in tags:
        if label.strip().lower() not in _GENERIC_TAGS:
            return label.strip().lower()
    if events and isinstance(events[0], dict):
        slug = events[0].get("slug") or events[0].get("ticker")
        if slug and str(slug).strip():
            return str(slug).strip().lower()
    return "uncategorized"


def _resolution_source(raw: dict) -> str:
    """Resolution provenance (FR-MD-003). Prefer an explicit source URL; otherwise
    fall back to the on-chain resolver address (UMA optimistic oracle), which is
    the actual resolution authority for the overwhelming majority of Polymarket
    markets. Returns "" only when neither exists (genuinely ambiguous → FR-MD-004
    will reject it)."""
    explicit = str(raw.get("resolutionSource") or "").strip()
    if explicit:
        return explicit
    resolver = str(raw.get("resolvedBy") or "").strip()
    if resolver:
        return f"uma:{resolver}"
    return ""


def normalize_gamma_market(raw: dict) -> Market | None:
    """Map one Gamma market object to our :class:`Market` (FR-MD-001).

    Normalizes events, markets, outcomes, *outcome prices*, token IDs, condition
    IDs, question IDs, *tags*, *liquidity/volume/spread*, and resolution rules —
    every field FR-MD-001 and FR-MD-005 require — from the real payload shape.
    """
    token_ids = _json_array(raw.get("clobTokenIds"))
    outcomes = _json_array(raw.get("outcomes")) or ["Yes", "No"]
    market_id = str(raw.get("id") or raw.get("conditionId") or "")
    if not market_id:
        return None

    token_map: dict[str, str] = {}
    for i, outcome in enumerate(outcomes):
        if i < len(token_ids):
            token_map[str(outcome).upper()] = str(token_ids[i])

    # Outcome prices (FR-MD-001): Gamma encodes them as a JSON-string array
    # positionally aligned with ``outcomes``.
    price_values = _json_array(raw.get("outcomePrices"))
    outcome_prices: dict[str, float] = {}
    for i, outcome in enumerate(outcomes):
        if i < len(price_values):
            price = _parse_float(price_values[i])
            if price is not None:
                outcome_prices[str(outcome).upper()] = max(0.0, min(1.0, price))

    events = raw.get("events") or []
    event_id = str(events[0].get("id")) if events and isinstance(events[0], dict) else market_id
    tags = [sanitize_untrusted(t, max_len=256).text for t in _tag_labels(raw)]
    if not tags and events and isinstance(events[0], dict):
        tags = [sanitize_untrusted(t, max_len=256).text for t in _tag_labels(events[0])]

    spread = _parse_float(raw.get("spread"))
    if spread is not None:
        spread = max(0.0, min(1.0, spread))
    liquidity = _parse_float(raw.get("liquidityNum"))
    if liquidity is None:
        liquidity = _parse_float(raw.get("liquidity"))
    volume = _parse_float(raw.get("volumeNum"))
    if volume is None:
        volume = _parse_float(raw.get("volume"))
    volume_24hr = _parse_float(raw.get("volume24hr"))

    question_clean = sanitize_untrusted(str(raw.get("question") or raw.get("title") or ""))
    description_clean = sanitize_untrusted(str(raw.get("description") or ""))
    source_clean = sanitize_untrusted(_resolution_source(raw), max_len=1024)
    market_flags = sorted(
        set(question_clean.injection_flags)
        | set(description_clean.injection_flags)
        | set(source_clean.injection_flags)
    )
    explicit_source = raw.get("resolutionSource")
    source_links = (
        [sanitize_untrusted(explicit_source, max_len=1024).text]
        if isinstance(explicit_source, str) and explicit_source.strip() else []
    )

    return Market(
        market_id=market_id,
        event_id=event_id,
        condition_id=str(raw.get("conditionId") or ""),
        question_id=str(raw.get("questionID") or raw.get("questionId") or ""),
        question=question_clean.text,
        category=_derive_category(tags, events),
        outcomes=[str(o) for o in outcomes],
        token_ids=token_map,
        resolution_rules=description_clean.text,
        resolution_source=source_clean.text,
        source_links=source_links,
        end_time=raw.get("endDate") or raw.get("endDateIso"),
        enable_order_book=bool(raw.get("enableOrderBook", False)),
        tags=tags,
        is_untrusted=True,
        suspected_injection=bool(market_flags),
        injection_flags=market_flags,
        outcome_prices=outcome_prices,
        liquidity_usd=liquidity,
        volume_usd=volume,
        volume_24hr_usd=volume_24hr,
        spread=spread,
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
    last_trade_raw = payload.get("last_trade_price")
    if last_trade_raw in (None, ""):
        last_trade_raw = payload.get("price")
    seq_raw = payload.get("timestamp") or payload.get("seq") or now_ms()
    try:
        seq = int(seq_raw)
    except (TypeError, ValueError):
        seq = now_ms()
    return OrderBookSnapshot(
        token_id=token_id, bids=bids, asks=asks,
        last_trade=_parse_float(last_trade_raw),
        sequence=seq, source=source, received_at=now_ms(),
    )


def _event_sequence(payload: dict) -> int:
    seq_raw = payload.get("timestamp") or payload.get("seq") or now_ms()
    try:
        return int(seq_raw)
    except (TypeError, ValueError):
        return now_ms()


def _copy_levels(levels: list[BookLevel]) -> list[BookLevel]:
    return [BookLevel(price=level.price, size=level.size) for level in levels]


def _replace_level(levels: list[BookLevel], price: float, size: float) -> list[BookLevel]:
    out = [level for level in levels if level.price != price]
    if size > 0:
        out.append(BookLevel(price=price, size=size))
    return out


def _snapshot_from_parts(
    token_id: str,
    bids: list[BookLevel],
    asks: list[BookLevel],
    last_trade: float | None,
    sequence: int,
    source: str,
) -> OrderBookSnapshot:
    bids.sort(key=lambda x: x.price, reverse=True)
    asks.sort(key=lambda x: x.price)
    return OrderBookSnapshot(
        token_id=token_id, bids=bids, asks=asks,
        last_trade=last_trade, sequence=sequence, source=source, received_at=now_ms(),
    )


def _snapshots_from_clob_event(
    event: dict, source: str, books: dict[str, OrderBookSnapshot]
) -> list[OrderBookSnapshot]:
    """Translate documented CLOB market-channel event types into hot-cache
    snapshots. Lifecycle-only events are accepted but do not emit snapshots."""
    event_type = event.get("event_type")
    if event_type == "book":
        tid = event.get("asset_id") or event.get("token_id")
        if not tid:
            return []
        snap = _book_from_clob(str(tid), event, source)
        books[snap.token_id] = snap
        return [snap]

    if event_type == "price_change":
        out: list[OrderBookSnapshot] = []
        for change in event.get("price_changes") or []:
            if not isinstance(change, dict):
                continue
            tid = change.get("asset_id") or change.get("token_id")
            price, size = _parse_float(change.get("price")), _parse_float(change.get("size"))
            if not tid or price is None or size is None:
                continue
            prev = books.get(str(tid))
            bids = _copy_levels(prev.bids) if prev else []
            asks = _copy_levels(prev.asks) if prev else []
            side = str(change.get("side") or "").upper()
            if side == "BUY":
                bids = _replace_level(bids, price, size)
            elif side == "SELL":
                asks = _replace_level(asks, price, size)
            else:
                continue
            snap = _snapshot_from_parts(
                str(tid), bids, asks, prev.last_trade if prev else None,
                _event_sequence(event), source,
            )
            books[snap.token_id] = snap
            out.append(snap)
        return out

    if event_type == "last_trade_price":
        tid = event.get("asset_id") or event.get("token_id")
        if not tid:
            return []
        prev = books.get(str(tid))
        snap = _snapshot_from_parts(
            str(tid),
            _copy_levels(prev.bids) if prev else [],
            _copy_levels(prev.asks) if prev else [],
            _parse_float(event.get("price")),
            _event_sequence(event),
            source,
        )
        books[snap.token_id] = snap
        return [snap]

    if event_type == "best_bid_ask":
        tid = event.get("asset_id") or event.get("token_id")
        if not tid:
            return []
        prev = books.get(str(tid))
        bids = _copy_levels(prev.bids) if prev else []
        asks = _copy_levels(prev.asks) if prev else []
        best_bid, best_ask = _parse_float(event.get("best_bid")), _parse_float(event.get("best_ask"))
        if best_bid is not None and not bids:
            bids.append(BookLevel(price=best_bid, size=0.0))
        if best_ask is not None and not asks:
            asks.append(BookLevel(price=best_ask, size=0.0))
        snap = _snapshot_from_parts(
            str(tid), bids, asks, prev.last_trade if prev else None,
            _event_sequence(event), source,
        )
        books[snap.token_id] = snap
        return [snap]

    return []


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
        # ``include_tag=true`` returns the tag taxonomy inline (the real category
        # signal — Politics/Crypto/Sports/...), avoiding an N+1 per-market fetch.
        data = await self._get(
            f"{self._s.gamma_base_url}/markets",
            params={"active": "true", "closed": "false", "limit": 200, "include_tag": "true"},
        )
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

        books: dict[str, OrderBookSnapshot] = {}
        async with websockets.connect(self._s.clob_ws_url, ping_interval=20) as ws:
            await ws.send(json.dumps({
                "type": "market", "assets_ids": token_ids, "custom_feature_enabled": True
            }))
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                events = msg if isinstance(msg, list) else [msg]
                for ev in events:
                    if not isinstance(ev, dict):
                        continue
                    for snap in _snapshots_from_clob_event(ev, self.name, books):
                        yield snap

    async def geoblock_check(self) -> dict:
        """Return availability for the operator's region (COMP-003, FR-LIVE-003).
        Fail-closed: any error is treated as 'blocked' for live-mode safety."""
        try:
            data = await self._get(self._s.geoblock_url, retries=1)
            if isinstance(data, dict):
                if isinstance(data.get("blocked"), bool):
                    blocked = bool(data["blocked"])
                elif isinstance(data.get("allowed"), bool):
                    blocked = not bool(data["allowed"])
                else:
                    blocked = True
                return {"blocked": blocked, "raw": data}
            return {"blocked": True, "raw": data}
        except Exception as exc:  # noqa: BLE001 - fail-closed by design
            return {"blocked": True, "error": str(exc)}

    async def close(self) -> None:
        await self._client.aclose()
