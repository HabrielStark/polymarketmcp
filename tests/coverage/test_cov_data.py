"""Targeted coverage for the data lane (``hermes_pm.data.*``).

Each test drives a specific previously-uncovered branch with a meaningful
assertion — no imports-only or no-op tests. Loops in the market-data engine are
driven directly (calling ``_reconcile_once`` / ``_staleness_loop`` /
``_stream_loop`` and forcing ``_running`` / ``_last_message_ms``) and external
I/O is faked (a fake ``MarketDataSource``, ``httpx.MockTransport`` and a fake
websocket context manager) so the suite stays fast and offline.

Targets:
  sources.py        : 99, 117, 169
  cache.py          : 51, 58, 63-66
  discovery.py      : 35, 42-46, 60
  market_data.py    : 56, 103, 129, 133, 136-146, 171-172, 179, 189, 193-204, 211-213
  polymarket_client : 30, 35-36, 87-88, 137, 143-158, 168
"""

from __future__ import annotations

import asyncio
import contextlib
import json

import httpx
import pytest
import websockets

from hermes_pm.config import load_settings
from hermes_pm.data import market_data as md
from hermes_pm.data.cache import OrderBookCache
from hermes_pm.data.discovery import DiscoveryEngine
from hermes_pm.data.market_data import MarketDataEngine
from hermes_pm.data.polymarket_client import (
    PolymarketSource,
    _book_from_clob,
    _json_array,
    _snapshots_from_clob_event,
)
from hermes_pm.data.sources import ReplaySource, SyntheticSource
from hermes_pm.errors import RateLimitedError
from hermes_pm.events import EventBus, EventType
from hermes_pm.models import BookLevel, Market, OrderBookSnapshot, Side
from hermes_pm.persistence.db import Database
from hermes_pm.util.timeutil import now_ms


# --------------------------------------------------------------------------- #
# Helpers / fakes
# --------------------------------------------------------------------------- #
def _snap(token: str, seq: int) -> OrderBookSnapshot:
    return OrderBookSnapshot(
        token_id=token,
        bids=[BookLevel(price=0.4, size=100.0)],
        asks=[BookLevel(price=0.5, size=100.0)],
        sequence=seq,
    )


def _market(
    market_id: str = "m",
    *,
    enable_order_book: bool = True,
    rules: str = "Resolves YES if the official outcome is confirmed.",
    source: str = "official-authority",
    tags: list[str] | None = None,
    end_time: str | None = None,
    category: str = "weather",
    liquidity_usd: float | None = None,
    volume_usd: float | None = None,
    spread: float | None = None,
) -> Market:
    return Market(
        market_id=market_id,
        event_id="e",
        condition_id="c",
        question="Will it happen?",
        category=category,
        enable_order_book=enable_order_book,
        resolution_rules=rules,
        resolution_source=source,
        tags=list(tags or []),
        end_time=end_time,
        liquidity_usd=liquidity_usd,
        volume_usd=volume_usd,
        spread=spread,
    )


def _engine(tmp_path, source, interval: int = 20):
    settings = load_settings(
        data_dir=str(tmp_path),
        db_filename="cov.sqlite3",
        ws_reconnect_stale_ms=60_000,
        reconcile_interval_ms=60_000,
    )
    cache = OrderBookCache(60_000)
    db = Database(":memory:")
    bus = EventBus()
    eng = MarketDataEngine(settings, source, cache, db, bus, stream_interval_ms=interval)
    return eng, cache, db, bus


class _FakeSource:
    """Configurable MarketDataSource stand-in for driving engine loops."""

    name = "fake"

    def __init__(self) -> None:
        self.markets: list[Market] = []
        self.snapshot_queue: dict[str, list] = {}
        self.snapshot_exc: Exception | None = None

    async def discover_markets(self):
        return list(self.markets)

    async def snapshot(self, token_id: str):
        if self.snapshot_exc is not None:
            raise self.snapshot_exc
        q = self.snapshot_queue.get(token_id)
        if q:
            return q.pop(0)
        return None

    async def stream(self, token_ids, interval_ms):
        if False:  # pragma: no cover - empty async generator
            yield None

    async def close(self):
        return None


class _BreakSource:
    """Yields one snapshot, flips the engine's ``_running`` to False, then yields
    a second snapshot the loop must skip (exercises the mid-stream break)."""

    name = "break"

    def __init__(self, engine: MarketDataEngine, snaps) -> None:
        self._engine = engine
        self._snaps = snaps

    async def discover_markets(self):
        return []

    async def snapshot(self, token_id: str):
        return None

    async def stream(self, token_ids, interval_ms):
        yield self._snaps[0]
        self._engine._running = False
        yield self._snaps[1]

    async def close(self):
        return None


class _ErrSource:
    """``stream`` raises immediately to exercise the reconnect-on-error path."""

    name = "err"

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def discover_markets(self):
        return []

    async def snapshot(self, token_id: str):
        return None

    async def stream(self, token_ids, interval_ms):
        if self._exc is not None:
            raise self._exc
        if False:  # pragma: no cover - never reached; only defines async generator
            yield None

    async def close(self):
        return None


class _FakeWS:
    def __init__(self, messages) -> None:
        self._messages = list(messages)
        self.sent: list[str] = []

    async def send(self, data: str) -> None:
        self.sent.append(data)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)


class _FakeConnect:
    def __init__(self, ws: _FakeWS) -> None:
        self._ws = ws

    async def __aenter__(self):
        return self._ws

    async def __aexit__(self, *exc):
        return False


# --------------------------------------------------------------------------- #
# sources.py — 99, 117, 169
# --------------------------------------------------------------------------- #
async def test_synthetic_snapshot_unknown_token_returns_none():
    # sources.py:117 — token not in the synthetic universe -> None
    src = SyntheticSource(seed=11, market_count=3)
    assert await src.snapshot("does-not-exist") is None


async def test_synthetic_book_crossed_quote_is_corrected():
    # sources.py:99 — a degenerate probability makes ask <= bid, so the book
    # builder nudges the ask up by a tick to keep a sane two-sided book.
    src = SyntheticSource(seed=12, market_count=2)
    src._p["crosstok"] = 0.0
    book = await src.snapshot("crosstok")
    assert book is not None
    assert book.best_bid == 0.01 and book.best_ask == 0.02  # corrected to bb + tick
    assert book.best_ask > book.best_bid


async def test_synthetic_book_filters_out_of_range_levels():
    # sources.py comprehension filters — extreme-low prob truncates bids;
    # extreme-high prob truncates asks (the comprehension skip branch).
    src = SyntheticSource(seed=12, market_count=2)
    src._p["lowtok"] = 0.02
    low = await src.snapshot("lowtok")
    assert low is not None
    assert len(low.bids) == 1  # only the 0.01 level survives the > 0 filter
    assert all(b.price > 0.0 for b in low.bids)

    src._p["hightok"] = 0.98
    high = await src.snapshot("hightok")
    assert high is not None
    assert len(high.asks) == 1  # only the 0.99 level survives the < 1 filter
    assert all(a.price < 1.0 for a in high.asks)


async def test_replay_source_snapshot_returns_latest(tmp_path):
    # sources.py:169 — ReplaySource.snapshot returns the latest recorded book.
    src = SyntheticSource(seed=7, market_count=4)
    markets = await src.discover_markets()
    toks = [m.token_ids["YES"] for m in markets if m.enable_order_book]
    snaps = src.record(toks, steps=3)
    path = tmp_path / "rec.json"
    ReplaySource.write_recording(path, markets, snaps)

    rs = ReplaySource(path)
    got = await rs.snapshot(toks[0])
    assert got is not None and got.token_id == toks[0]
    assert await rs.snapshot("unknown-token") is None


# --------------------------------------------------------------------------- #
# cache.py — 51, 58, 63-66
# --------------------------------------------------------------------------- #
def test_cache_is_stale_for_unknown_token_when_connected():
    # cache.py:51 — not connectivity-lost, but no book for the token -> stale.
    cache = OrderBookCache()
    assert cache.connectivity_lost is False
    assert cache.is_stale("missing") is True


def test_cache_age_ms_unknown_token_is_max(book_factory):
    # cache.py:58 — no book -> sentinel huge age.
    cache = OrderBookCache()
    assert cache.age_ms("missing") == 2**31
    # cache.py:59-60 — present book -> real, non-negative age.
    book = book_factory(token_id="t", bid=0.49, ask=0.51)
    cache.update(book)
    age = cache.age_ms("t", now=book.received_at + 1234)
    assert age == 1234
    assert cache.age_ms("t") >= 0


def test_cache_best_paths(book_factory):
    # cache.py:63-66 — best() missing -> None; present -> ask for BUY, bid for SELL.
    cache = OrderBookCache()
    assert cache.best("missing", Side.BUY) is None  # lines 63-65

    book = book_factory(token_id="t", bid=0.49, ask=0.51, size=400.0)
    cache.update(book)
    assert cache.best("t", Side.BUY) == 0.51   # line 66, BUY -> best_ask
    assert cache.best("t", Side.SELL) == 0.49  # line 66, SELL -> best_bid


def test_cache_sweep_stale_and_connectivity(book_factory):
    cache = OrderBookCache()
    cache.update(book_factory(token_id="t", bid=0.49, ask=0.51))
    assert cache.sweep_stale() == []  # fresh book -> nothing stale
    cache.set_connectivity_lost(True)
    assert cache.connectivity_lost is True
    assert cache.sweep_stale() == ["t"]  # connectivity loss forces all stale


# --------------------------------------------------------------------------- #
# discovery.py — 35, 42-46, 60
# --------------------------------------------------------------------------- #
def test_passes_filters_rejects_on_tags_any():
    # discovery.py:35 — tags_any set but no overlap with market tags.
    m = _market(category="weather", tags=["Weather"])
    assert DiscoveryEngine.passes_filters(m, {"tags_any": ["sports"]}) is False
    # control: overlapping tag passes the tags filter
    assert DiscoveryEngine.passes_filters(m, {"tags_any": ["weather"]}) is True
    assert DiscoveryEngine.passes_filters(m, {"categories": ["Weather"]}) is True
    assert DiscoveryEngine.passes_filters(m, {"exclude_categories": ["Weather"]}) is False


def test_passes_filters_rejects_end_time_after_max():
    # discovery.py:42-44 — end_time later than the max_end_time filter.
    m = _market(end_time="2027-06-01T00:00:00Z")
    assert (
        DiscoveryEngine.passes_filters(m, {"max_end_time": "2026-01-01T00:00:00Z"}) is False
    )
    # control: an earlier end time is allowed
    m2 = _market(end_time="2025-06-01T00:00:00Z")
    assert (
        DiscoveryEngine.passes_filters(m2, {"max_end_time": "2026-01-01T00:00:00Z"}) is True
    )


def test_passes_filters_rejects_unparseable_end_time():
    # discovery.py:45-46 — iso_to_ms raises ValueError -> fail closed.
    m = _market(end_time="not-a-real-date")
    assert (
        DiscoveryEngine.passes_filters(m, {"max_end_time": "2026-01-01T00:00:00Z"}) is False
    )


def test_passes_filters_other_rejections():
    # discovery.py:29/32/37/39 — categories, exclude, order-book, resolution gates.
    weather = _market(category="weather")
    assert DiscoveryEngine.passes_filters(weather, {"categories": ["sports"]}) is False  # 29
    assert DiscoveryEngine.passes_filters(weather, {"exclude_categories": ["weather"]}) is False  # 32
    no_ob = _market(enable_order_book=False)
    assert DiscoveryEngine.passes_filters(no_ob, {}) is False  # 37 (require_order_book default)
    ambiguous = _market(rules="", source="")
    assert DiscoveryEngine.passes_filters(ambiguous, {}) is False  # 39 (require_clear_resolution)
    assert DiscoveryEngine.passes_filters(weather, {}) is True  # control: all gates pass


def test_passes_filters_min_liquidity_volume_spread():
    # discovery.py FR-MD-005 — liquidity / volume / spread microstructure filters.
    liquid = _market(liquidity_usd=10_000.0, volume_usd=100_000.0, spread=0.02)
    assert DiscoveryEngine.passes_filters(liquid, {"min_liquidity": 5_000.0}) is True
    assert DiscoveryEngine.passes_filters(liquid, {"min_liquidity": 50_000.0}) is False
    # unknown liquidity is treated strictly as 0 (excluded when a floor is set).
    assert (
        DiscoveryEngine.passes_filters(_market(liquidity_usd=None), {"min_liquidity": 1.0}) is False
    )
    assert DiscoveryEngine.passes_filters(liquid, {"min_volume": 50_000.0}) is True
    assert DiscoveryEngine.passes_filters(liquid, {"min_volume": 200_000.0}) is False
    assert DiscoveryEngine.passes_filters(liquid, {"max_spread": 0.05}) is True
    assert DiscoveryEngine.passes_filters(liquid, {"max_spread": 0.01}) is False
    # unknown spread is lenient: only the live order-book gate applies later.
    assert DiscoveryEngine.passes_filters(_market(spread=None), {"max_spread": 0.01}) is True


def test_build_watchlist_filters_and_keeps():
    # discovery.py:57 — a market failing the filters is skipped before tradability.
    sports = _market(market_id="s", category="sports")
    weather = _market(market_id="w", category="weather")
    kept = DiscoveryEngine.build_watchlist([sports, weather], {"categories": ["weather"]})
    assert [m.market_id for m in kept] == ["w"]


def test_build_watchlist_skips_untradable_when_required():
    # discovery.py:60 — passes filters but is not tradable + require_tradable.
    untradable = _market(market_id="x", enable_order_book=False)
    relaxed = {"require_order_book": False, "require_clear_resolution": False}

    skipped = DiscoveryEngine.build_watchlist([untradable], relaxed)
    assert skipped == []  # require_tradable defaults True -> continue (line 60)

    kept = DiscoveryEngine.build_watchlist([untradable], {**relaxed, "require_tradable": False})
    assert [m.market_id for m in kept] == ["x"]  # included when not required


def test_is_tradable_reasons():
    assert DiscoveryEngine.is_tradable(_market())[0] is True
    ok, reasons = DiscoveryEngine.is_tradable(_market(enable_order_book=False))
    assert ok is False and "order_book_disabled" in reasons
    ok2, reasons2 = DiscoveryEngine.is_tradable(_market(rules="", source=""))
    assert ok2 is False and "ambiguous_or_missing_resolution_rules" in reasons2


# --------------------------------------------------------------------------- #
# market_data.py — 56, 103, 129/133, 136-146, 171-172, 179, 189, 193-204, 211-213
# --------------------------------------------------------------------------- #
async def test_get_market_known_and_unknown(tmp_path):
    # market_data.py:56 — get_market lookup.
    src = SyntheticSource(seed=3, market_count=3)
    eng, cache, db, bus = _engine(tmp_path, src)
    markets = await eng.discover()
    assert eng.get_market(markets[0].market_id) == markets[0]
    assert eng.get_market("nope") is None


async def test_restart_stream_cancels_existing_task(tmp_path):
    # market_data.py:103 — second _restart_stream cancels the prior stream task.
    eng, cache, db, bus = _engine(tmp_path, _FakeSource())
    eng._restart_stream()
    first = eng._stream_task
    assert first is not None
    eng._restart_stream()  # _stream_task is not None -> cancel (line 103)
    second = eng._stream_task
    assert second is not None and second is not first
    for task in (first, second):
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


async def test_stream_loop_returns_when_stream_exhausts(tmp_path):
    # market_data.py:127->133 — generator finishes naturally -> clean return.
    eng, cache, db, bus = _engine(tmp_path, _FakeSource())  # empty stream
    eng._subscribed = {"t"}
    eng._running = True
    await asyncio.wait_for(eng._stream_loop(), timeout=1)
    assert cache.get("t") is None  # nothing was streamed


async def test_stream_loop_propagates_cancellation(tmp_path):
    # market_data.py:134-135 — cooperative cancellation is re-raised, not counted.
    class _HangSource(_FakeSource):
        async def stream(self, token_ids, interval_ms):
            yield _snap("t", 1)
            await asyncio.sleep(100)  # block so the task can be cancelled mid-stream

    eng, cache, db, bus = _engine(tmp_path, _HangSource())
    eng._subscribed = {"t"}
    eng._running = True
    task = asyncio.create_task(eng._stream_loop())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert eng.reconnects == 0  # cancellation is not a reconnect


async def test_stream_loop_breaks_when_running_cleared(tmp_path):
    # market_data.py:128-129/132-133 — _running flips mid-stream -> break + return.
    eng, cache, db, bus = _engine(tmp_path, _FakeSource())
    eng._source = _BreakSource(eng, [_snap("t", 1), _snap("t", 2)])
    eng._subscribed = {"t"}
    eng._running = True
    await eng._stream_loop()
    book = cache.get("t")
    assert book is not None and book.sequence == 1  # second snapshot was skipped


async def test_stream_loop_reconnects_on_rate_limit(tmp_path, monkeypatch):
    # market_data.py:136-146 — stream raises RateLimitedError -> "throttled".
    eng, cache, db, bus = _engine(tmp_path, _ErrSource(RateLimitedError("slow down")))
    eng._subscribed = {"t"}
    eng._running = True
    events: list = []
    bus.add_listener(events.append)

    async def fast_sleep(_):
        eng._running = False

    monkeypatch.setattr(md.asyncio, "sleep", fast_sleep)
    await eng._stream_loop()

    assert eng.reconnects == 1
    assert cache.connectivity_lost is True
    conn = [e for e in events if e.type == EventType.CONNECTIVITY]
    assert conn and conn[-1].data["status"] == "throttled"


async def test_stream_loop_reconnects_on_generic_error(tmp_path, monkeypatch):
    # market_data.py:136-146 — non-rate-limit error -> "disconnected".
    eng, cache, db, bus = _engine(tmp_path, _ErrSource(RuntimeError("socket died")))
    eng._subscribed = {"t"}
    eng._running = True
    events: list = []
    bus.add_listener(events.append)

    async def fast_sleep(_):
        eng._running = False

    monkeypatch.setattr(md.asyncio, "sleep", fast_sleep)
    await eng._stream_loop()

    assert eng.reconnects == 1
    conn = [e for e in events if e.type == EventType.CONNECTIVITY]
    assert conn and conn[-1].data["status"] == "disconnected"
    assert "socket died" in conn[-1].data["error"]


async def test_supervised_cancel_during_backoff_sleep(tmp_path):
    # market_data.py:171-172 — cancellation while sleeping in the restart backoff.
    eng, cache, db, bus = _engine(tmp_path, _FakeSource())
    eng._running = True
    eng._loop_backoff = 0.2

    async def crashy():
        raise RuntimeError("boom")

    task = asyncio.create_task(eng._supervised(crashy, "staleness"))
    await asyncio.sleep(0.05)  # let it crash and enter the backoff sleep
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert eng.loop_failures == 1  # the crash was counted before the cancel


async def test_reconcile_loop_invokes_reconcile_once(tmp_path, monkeypatch):
    # market_data.py:179 — _reconcile_loop awaits _reconcile_once once.
    src = _FakeSource()
    src.snapshot_queue["t"] = [_snap("t", 3)]
    eng, cache, db, bus = _engine(tmp_path, src)
    eng._subscribed = {"t"}
    eng._running = True

    async def fast_sleep(_):
        eng._running = False

    monkeypatch.setattr(md.asyncio, "sleep", fast_sleep)
    await eng._reconcile_loop()

    book = cache.get("t")
    assert book is not None and book.sequence == 3  # reconcile ingested the snapshot


async def test_reconcile_once_propagates_cancellation(tmp_path):
    # market_data.py:188-189 — CancelledError is re-raised, not counted.
    src = _FakeSource()
    src.snapshot_exc = asyncio.CancelledError()
    eng, cache, db, bus = _engine(tmp_path, src)
    eng._subscribed = {"t"}
    with pytest.raises(asyncio.CancelledError):
        await eng._reconcile_once()
    assert eng.reconcile_errors == 0


async def test_reconcile_once_counts_transient_errors(tmp_path):
    # market_data.py:190-192 — transient per-token errors are counted, not fatal.
    src = _FakeSource()
    src.snapshot_exc = RuntimeError("source down")
    eng, cache, db, bus = _engine(tmp_path, src)
    eng._subscribed = {"t1", "t2"}
    await eng._reconcile_once()  # must not raise
    assert eng.reconcile_errors == 2


async def test_reconcile_once_detects_gaps_and_ingests(tmp_path):
    # market_data.py:193-204 — None skip, first ingest, gap detection, no-gap, stale skip.
    src = _FakeSource()
    src.snapshot_queue["t"] = [
        _snap("t", 5),   # cached None -> ingest
        _snap("t", 10),  # 10 > 5 + 1 -> gap detected + ingest
        _snap("t", 11),  # 11 == 10 + 1 -> ingest, no gap
        _snap("t", 11),  # not newer -> skipped
        None,            # fresh is None -> continue
    ]
    eng, cache, db, bus = _engine(tmp_path, src)
    eng._subscribed = {"t"}
    events: list = []
    bus.add_listener(events.append)

    for _ in range(5):
        await eng._reconcile_once()

    book = cache.get("t")
    assert book is not None and book.sequence == 11
    assert eng.gaps_detected == 1
    assert eng.reconcile_errors == 0
    gaps = [e for e in events if e.type == EventType.MARKET_DATA and e.data.get("reconcile_gap")]
    assert gaps and gaps[0].data["from_seq"] == 5 and gaps[0].data["to_seq"] == 10


async def test_staleness_loop_marks_connectivity_lost(tmp_path, monkeypatch):
    # market_data.py:209-217 (incl. 211-213) — stale age forces connectivity loss.
    src = _FakeSource()
    eng, cache, db, bus = _engine(tmp_path, src)
    eng._subscribed = {"t"}
    eng._cache._last_message_ms = now_ms() - 10_000_000  # far past staleness budget
    eng._running = True
    events: list = []
    bus.add_listener(events.append)

    async def fast_sleep(_):
        eng._running = False

    monkeypatch.setattr(md.asyncio, "sleep", fast_sleep)
    await eng._staleness_loop()

    assert eng._cache.connectivity_lost is True
    assert any(e.type == EventType.BOOK_STALE for e in events)


async def test_stop_cancels_tasks_and_closes_source(tmp_path):
    # market_data.py:219-230 — stop() flips running, cancels tasks, closes source.
    closed = {"v": False}

    class _ClosingSource(_FakeSource):
        async def close(self):
            closed["v"] = True

    eng, cache, db, bus = _engine(tmp_path, _ClosingSource())
    await eng.start(auto_subscribe_tradable=False)
    await eng.subscribe(["t"])  # creates a live _stream_task -> exercises line 222
    assert eng._running is True
    assert eng._stream_task is not None
    await eng.stop()
    assert eng._running is False
    assert closed["v"] is True


# --------------------------------------------------------------------------- #
# polymarket_client.py — 30, 35-36, 87-88, 137, 143-158, 168
# --------------------------------------------------------------------------- #
def test_json_array_handles_all_shapes():
    assert _json_array([1, 2]) == [1, 2]            # line 30: list passthrough
    assert _json_array('["a", "b"]') == ["a", "b"]  # str-encoded JSON list
    assert _json_array("{not valid json") == []     # lines 35-36: JSONDecodeError
    assert _json_array('{"a": 1}') == []            # decoded value is not a list
    assert _json_array("") == []                    # empty/blank string
    assert _json_array(123) == []                   # neither list nor str


def test_book_from_clob_bad_timestamp_falls_back_to_now():
    # polymarket_client.py:87-88 — non-numeric timestamp -> seq = now_ms().
    payload = {
        "bids": [{"price": "0.4", "size": "100"}],
        "asks": [{"price": "0.5", "size": "100"}],
        "timestamp": "not-a-number",
        "price": "0.45",
    }
    book = _book_from_clob("t", payload, "live")
    assert book.token_id == "t"
    assert book.sequence > 10**12  # fell back to an epoch-ms timestamp
    assert book.last_trade == 0.45


async def test_polymarket_snapshot_returns_none_for_non_dict():
    # polymarket_client.py:137 — _get returns a non-dict payload.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[1, 2, 3])

    src = PolymarketSource(load_settings())
    src._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        assert await src.snapshot("111") is None
    finally:
        await src.close()


async def test_polymarket_geoblock_non_dict_blocks():
    # polymarket_client.py:168 — non-dict geoblock payload -> blocked (fail closed).
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["unexpected"])

    src = PolymarketSource(load_settings())
    src._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await src.geoblock_check()
    finally:
        await src.close()
    assert result["blocked"] is True
    assert result["raw"] == ["unexpected"]


async def test_polymarket_geoblock_unknown_schema_blocks():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    src = PolymarketSource(load_settings())
    src._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await src.geoblock_check()
    finally:
        await src.close()
    assert result["blocked"] is True
    assert result["raw"] == {}


async def test_polymarket_geoblock_allowed_false_blocks():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"allowed": False})

    src = PolymarketSource(load_settings())
    src._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await src.geoblock_check()
    finally:
        await src.close()
    assert result["blocked"] is True


async def test_polymarket_stream_parses_ws_messages(monkeypatch):
    # polymarket_client.py:143-158 — WS subscribe + parse book events.
    book_msg = json.dumps(
        {
            "event_type": "book",
            "asset_id": "111",
            "bids": [{"price": "0.4", "size": "100"}],
            "asks": [{"price": "0.5", "size": "100"}],
            "price": "0.45",
            "timestamp": "100",
        }
    )
    price_change_msg = json.dumps(
        {
            "event_type": "price_change",
            "price_changes": [
                {"asset_id": "111", "price": "0.41", "size": "50", "side": "BUY"}
            ],
            "timestamp": "101",
        }
    )
    last_trade_msg = json.dumps(
        {"event_type": "last_trade_price", "asset_id": "111", "price": "0.46", "timestamp": "102"}
    )
    best_bid_ask_msg = json.dumps(
        {
            "event_type": "best_bid_ask",
            "asset_id": "333",
            "best_bid": "0.2",
            "best_ask": "0.3",
            "timestamp": "103",
        }
    )
    list_msg = json.dumps(
        [
            42,  # non-dict element -> skipped (lines 154-155)
            {"event_type": "book", "token_id": "222", "bids": [], "asks": [], "timestamp": "x"},
        ]
    )
    nonbook_msg = json.dumps([{"event_type": "price_change", "asset_id": "999"}])
    ws = _FakeWS([
        "this-is-not-json", book_msg, price_change_msg, last_trade_msg,
        best_bid_ask_msg, list_msg, nonbook_msg,
    ])
    monkeypatch.setattr(websockets, "connect", lambda *a, **k: _FakeConnect(ws))

    src = PolymarketSource(load_settings())
    try:
        books = [b async for b in src.stream(["111", "222"], 0)]
    finally:
        await src.close()

    assert {b.token_id for b in books} == {"111", "222", "333"}
    assert len(books) == 5  # book, price_change, last_trade, best_bid_ask, list-book
    assert books[1].best_bid == 0.41
    assert books[2].last_trade == 0.46
    assert books[3].spread == 0.1
    assert ws.sent and "assets_ids" in ws.sent[0] and "custom_feature_enabled" in ws.sent[0]


def test_polymarket_clob_event_edge_branches():
    books: dict[str, OrderBookSnapshot] = {}
    assert _snapshots_from_clob_event({"event_type": "book"}, "live", books) == []

    books["tok"] = OrderBookSnapshot(
        token_id="tok",
        bids=[BookLevel(price=0.40, size=1.0)],
        asks=[BookLevel(price=0.60, size=1.0)],
        last_trade=0.50,
    )
    out = _snapshots_from_clob_event(
        {
            "event_type": "price_change",
            "timestamp": "not-an-int",
            "price_changes": [
                "bad-shape",
                {"asset_id": "", "price": "0.41", "size": "1", "side": "BUY"},
                {"asset_id": "tok", "price": None, "size": "1", "side": "BUY"},
                {"asset_id": "tok", "price": "0.40", "size": "0", "side": "BUY"},
                {"asset_id": "tok", "price": "0.61", "size": "2", "side": "SELL"},
                {"asset_id": "tok", "price": "0.70", "size": "1", "side": "HOLD"},
            ],
        },
        "live",
        books,
    )
    assert len(out) == 2
    assert all(s.sequence > 0 for s in out)
    assert [level.price for level in books["tok"].bids] == []
    assert {level.price for level in books["tok"].asks} == {0.60, 0.61}

    assert _snapshots_from_clob_event({"event_type": "last_trade_price"}, "live", books) == []
    trade = _snapshots_from_clob_event(
        {"event_type": "last_trade_price", "asset_id": "tok", "price": "0.42", "seq": 7},
        "live",
        books,
    )[0]
    assert trade.last_trade == 0.42

    assert _snapshots_from_clob_event({"event_type": "best_bid_ask"}, "live", books) == []
    bid_only = _snapshots_from_clob_event(
        {"event_type": "best_bid_ask", "asset_id": "new-bid", "best_bid": "0.33"},
        "live",
        books,
    )[0]
    ask_only = _snapshots_from_clob_event(
        {"event_type": "best_bid_ask", "asset_id": "new-ask", "best_ask": "0.67"},
        "live",
        books,
    )[0]
    assert bid_only.best_bid == 0.33 and bid_only.best_ask is None
    assert ask_only.best_bid is None and ask_only.best_ask == 0.67
    assert _snapshots_from_clob_event({"event_type": "new_market"}, "live", books) == []
