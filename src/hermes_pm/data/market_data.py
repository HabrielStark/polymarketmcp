"""Market Data Engine — drives a :class:`MarketDataSource` into the hot cache,
persists every snapshot for replay (FR-DATA-005), publishes events, reconciles
WS state against REST snapshots to detect gaps (FR-DATA-003), and sweeps for
staleness / connectivity loss (FR-DATA-004, NFR-REL-003)."""

from __future__ import annotations

import asyncio
import contextlib
import sys

from hermes_pm.config import Settings
from hermes_pm.data.cache import OrderBookCache
from hermes_pm.data.discovery import DiscoveryEngine
from hermes_pm.data.sources import MarketDataSource
from hermes_pm.events import EventBus, EventType
from hermes_pm.models import Market
from hermes_pm.persistence.db import Database
from hermes_pm.util.timeutil import now_ms


class MarketDataEngine:
    def __init__(
        self,
        settings: Settings,
        source: MarketDataSource,
        cache: OrderBookCache,
        db: Database,
        bus: EventBus,
        stream_interval_ms: int = 100,
    ) -> None:
        self._s = settings
        self._source = source
        self._cache = cache
        self._db = db
        self._bus = bus
        self._interval = stream_interval_ms
        self._subscribed: set[str] = set()
        self._markets: dict[str, Market] = {}
        self._tasks: list[asyncio.Task] = []
        self._stream_task: asyncio.Task | None = None
        self._running = False
        self.reconnects = 0
        self.gaps_detected = 0
        #: transient per-token reconcile errors (observable, not silently swallowed)
        self.reconcile_errors = 0
        #: times a supervised background loop crashed and had to be restarted
        self.loop_failures = 0
        self._loop_backoff = 0.5

    @property
    def markets(self) -> list[Market]:
        return list(self._markets.values())

    def get_market(self, market_id: str) -> Market | None:
        return self._markets.get(market_id)

    async def discover(self) -> list[Market]:
        markets = await self._source.discover_markets()
        for m in markets:
            self._markets[m.market_id] = m
            self._db.save_market(m)
            self._bus.publish(
                EventType.MARKET_DISCOVERED,
                {"market_id": m.market_id, "tradable": DiscoveryEngine.is_tradable(m)[0]},
            )
        return markets

    async def start(self, auto_subscribe_tradable: bool = True) -> None:
        self._running = True
        await self.discover()
        if auto_subscribe_tradable:
            tokens = [
                tid
                for m in self._markets.values()
                if DiscoveryEngine.is_tradable(m)[0]
                for tid in m.token_ids.values()
            ]
            await self.subscribe(tokens)
        self._tasks = [
            asyncio.create_task(
                self._supervised(self._reconcile_loop, "reconcile"), name="md-reconcile"
            ),
            asyncio.create_task(
                self._supervised(self._staleness_loop, "staleness"), name="md-staleness"
            ),
        ]

    async def subscribe(self, token_ids: list[str]) -> None:
        new = set(token_ids) - self._subscribed
        if not new:
            return
        self._subscribed |= set(token_ids)
        # Seed cache immediately with a REST-style snapshot so reads work at once.
        for tid in new:
            snap = await self._source.snapshot(tid)
            if snap is not None:
                self._ingest(snap)
        self._restart_stream()

    def _restart_stream(self) -> None:
        if self._stream_task is not None:
            self._stream_task.cancel()
        self._stream_task = asyncio.create_task(self._stream_loop(), name="md-stream")

    def _ingest(self, snapshot) -> None:
        self._cache.update(snapshot, self._s.ws_reconnect_stale_ms)
        self._db.save_snapshot(snapshot)
        self._bus.publish(
            EventType.MARKET_DATA,
            {
                "token_id": snapshot.token_id,
                "best_bid": snapshot.best_bid,
                "best_ask": snapshot.best_ask,
                "spread": snapshot.spread,
                "mid": snapshot.mid,
                "sequence": snapshot.sequence,
                "received_at": snapshot.received_at,
            },
        )

    async def _stream_loop(self) -> None:
        tokens = list(self._subscribed)
        backoff = 0.5
        while self._running:
            try:
                async for snapshot in self._source.stream(tokens, self._interval):
                    if not self._running:
                        break
                    self._ingest(snapshot)
                    backoff = 0.5
                # Generator finished (e.g. replay exhausted): stop cleanly.
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect on any stream error
                self.reconnects += 1
                self._cache.set_connectivity_lost(True)
                from hermes_pm.errors import RateLimitedError
                self._bus.publish(
                    EventType.CONNECTIVITY,
                    {"status": "throttled" if isinstance(exc, RateLimitedError) else "disconnected",
                     "error": str(exc)},
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 10.0)

    async def _supervised(self, loop_factory, name: str) -> None:
        """Run a background loop so an unexpected exception can never silently
        kill the subsystem. On crash: count it, log to stderr, back off, restart.

        This matters most for the staleness loop — if it died silently the cache
        would stop being flagged stale and the risk engine could approve trades on
        dead data (FR-DATA-004 / NFR-REL-003)."""
        backoff = self._loop_backoff
        while self._running:
            try:
                await loop_factory()
                return  # clean, intentional completion (e.g. _running went False)
            except asyncio.CancelledError:
                raise  # cooperative shutdown — propagate, do not restart
            except Exception as exc:  # noqa: BLE001 - a safety loop must not die silently
                self.loop_failures += 1
                print(
                    f"[market_data] supervised loop {name!r} crashed: {exc!r} — "
                    f"restarting in {backoff:.2f}s (failures={self.loop_failures})",
                    file=sys.stderr,
                )
                try:
                    await asyncio.sleep(backoff)
                except asyncio.CancelledError:
                    raise
                backoff = min(backoff * 2, 10.0)

    async def _reconcile_loop(self) -> None:
        interval = max(0.5, self._s.reconcile_interval_ms / 1000)
        while self._running:
            await asyncio.sleep(interval)
            await self._reconcile_once()

    async def _reconcile_once(self) -> None:
        """One reconciliation sweep. A transient per-token source error is counted
        and skipped (observable via ``reconcile_errors``) — never silently dropped,
        and never allowed to abort the whole sweep."""
        for tid in list(self._subscribed):
            try:
                fresh = await self._source.snapshot(tid)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - transient source/network error
                self.reconcile_errors += 1
                continue
            if fresh is None:
                continue
            cached = self._cache.get(tid)
            if cached is None or fresh.sequence > cached.sequence:
                if cached is not None and fresh.sequence > cached.sequence + 1:
                    self.gaps_detected += 1
                    self._bus.publish(
                        EventType.MARKET_DATA,
                        {"token_id": tid, "reconcile_gap": True,
                         "from_seq": cached.sequence, "to_seq": fresh.sequence},
                    )
                self._ingest(fresh)

    async def _staleness_loop(self) -> None:
        while self._running:
            await asyncio.sleep(0.5)
            age = now_ms() - self._cache._last_message_ms  # noqa: SLF001 (same package)
            if self._subscribed and age > self._s.ws_reconnect_stale_ms:
                if not self._cache.connectivity_lost:
                    self._cache.set_connectivity_lost(True)
                    self._bus.publish(
                        EventType.BOOK_STALE,
                        {"reason": "connectivity_or_data_stale", "age_ms": age,
                         "stale_tokens": self._cache.sweep_stale()},
                    )

    async def stop(self) -> None:
        self._running = False
        if self._stream_task:
            self._stream_task.cancel()
        for t in self._tasks:
            t.cancel()
        for t in [*self._tasks, self._stream_task]:
            if t:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t
        with contextlib.suppress(Exception):
            await self._source.close()
