"""Integration tests: market data engine + cache + persistence (SRS 19.1)."""

from __future__ import annotations

import asyncio

import pytest

from hermes_pm.data.cache import OrderBookCache
from hermes_pm.data.discovery import DiscoveryEngine
from hermes_pm.data.market_data import MarketDataEngine
from hermes_pm.data.sources import ReplaySource, SyntheticSource
from hermes_pm.events import EventBus, EventType
from hermes_pm.persistence.db import Database

pytestmark = pytest.mark.asyncio


async def _engine(settings, interval=20):
    db = Database(":memory:")
    cache = OrderBookCache(settings.ws_reconnect_stale_ms)
    bus = EventBus()
    src = SyntheticSource(seed=42, market_count=6)
    eng = MarketDataEngine(settings, src, cache, db, bus, stream_interval_ms=interval)
    return eng, cache, db, bus


async def test_discovery_excludes_untradable(settings):
    eng, cache, db, bus = await _engine(settings)
    markets = await eng.discover()
    tradable = [m for m in markets if DiscoveryEngine.is_tradable(m)[0]]
    assert len(markets) == 6
    assert len(tradable) == 4  # one non-orderbook, one ambiguous excluded
    assert len(db.list_markets()) == 6


async def test_stream_updates_cache_and_persists(settings):
    eng, cache, db, bus = await _engine(settings)
    await eng.start()
    await asyncio.sleep(0.25)
    tradable = [m for m in eng.markets if DiscoveryEngine.is_tradable(m)[0]]
    tok = tradable[0].token_ids["YES"]
    book = cache.get(tok)
    assert book is not None and not cache.is_stale(tok)
    seq1 = book.sequence
    await asyncio.sleep(0.2)
    assert cache.get(tok).sequence > seq1  # advancing
    assert len(db.list_snapshots(tok)) > 0  # persisted for replay
    await eng.stop()


async def test_market_data_events_published(settings):
    eng, cache, db, bus = await _engine(settings)
    received = []
    bus.add_listener(lambda e: received.append(e.type) if e.type == EventType.MARKET_DATA else None)
    await eng.start()
    await asyncio.sleep(0.3)
    await eng.stop()
    assert EventType.MARKET_DATA in received


async def test_connectivity_loss_marks_stale(settings):
    eng, cache, db, bus = await _engine(settings)
    await eng.start()
    await asyncio.sleep(0.2)
    tok = next(m for m in eng.markets if DiscoveryEngine.is_tradable(m)[0]).token_ids["YES"]
    await eng.stop()
    cache.set_connectivity_lost(True)
    assert cache.is_stale(tok)


async def test_replay_source_roundtrip(settings, tmp_path):
    src = SyntheticSource(seed=7, market_count=4)
    markets = await src.discover_markets()
    toks = [m.token_ids["YES"] for m in markets if m.enable_order_book]
    snaps = src.record(toks, steps=5)
    path = tmp_path / "rec.json"
    ReplaySource.write_recording(path, markets, snaps)
    rs = ReplaySource(path)
    assert len(await rs.discover_markets()) == 4
    got = []
    async for s in rs.stream(toks, 0):
        got.append(s)
    assert len(got) > 0
