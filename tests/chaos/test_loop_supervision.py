"""Background-loop supervision (FR-DATA-004 / NFR-REL-003 hardening).

A safety-critical loop (staleness detection) must never die silently: if it
crashes it has to be restarted and the failure made observable. A transient
per-token reconcile error must be counted, not blindly swallowed.
"""

from __future__ import annotations

import asyncio

import pytest

from hermes_pm.config import load_settings
from hermes_pm.data.cache import OrderBookCache
from hermes_pm.data.market_data import MarketDataEngine
from hermes_pm.events import EventBus
from hermes_pm.persistence.db import Database

pytestmark = pytest.mark.asyncio


class _Src:
    """Minimal MarketDataSource stand-in; snapshot() can be made to fail."""

    def __init__(self, snapshot_exc: Exception | None = None) -> None:
        self._snapshot_exc = snapshot_exc

    async def discover_markets(self):
        return []

    async def snapshot(self, token_id: str):
        if self._snapshot_exc is not None:
            raise self._snapshot_exc
        return None

    async def stream(self, token_ids, interval_ms):
        if False:  # pragma: no cover - empty async generator
            yield None

    async def close(self):
        return None


def _engine(tmp_path, source: _Src | None = None) -> MarketDataEngine:
    settings = load_settings(data_dir=str(tmp_path))
    return MarketDataEngine(
        settings, source or _Src(), OrderBookCache(5000), Database(":memory:"), EventBus()
    )


async def test_supervised_loop_restarts_after_crash(tmp_path):
    eng = _engine(tmp_path)
    eng._running = True
    eng._loop_backoff = 0.01
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")  # first run crashes
        eng._running = False  # second run exits cleanly

    await asyncio.wait_for(eng._supervised(flaky, "staleness"), timeout=2)
    assert calls["n"] == 2          # it was restarted, not left dead
    assert eng.loop_failures == 1   # the crash was counted (observable)


async def test_supervised_loop_propagates_cancellation(tmp_path):
    eng = _engine(tmp_path)
    eng._running = True
    started = asyncio.Event()

    async def forever():
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(eng._supervised(forever, "stream"))
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert eng.loop_failures == 0  # cooperative shutdown is not a crash


async def test_reconcile_counts_source_errors_without_dying(tmp_path):
    eng = _engine(tmp_path, source=_Src(snapshot_exc=RuntimeError("source down")))
    eng._subscribed = {"tok-a", "tok-b"}
    await eng._reconcile_once()         # must not raise despite source failing
    assert eng.reconcile_errors == 2    # both errors observed, not swallowed


async def test_supervised_loop_keeps_restarting_until_stopped(tmp_path):
    eng = _engine(tmp_path)
    eng._running = True
    eng._loop_backoff = 0.01
    calls = {"n": 0}

    async def always_crashes():
        calls["n"] += 1
        if calls["n"] >= 3:
            eng._running = False  # let the test terminate
        raise RuntimeError(f"crash {calls['n']}")

    await asyncio.wait_for(eng._supervised(always_crashes, "reconcile"), timeout=2)
    assert calls["n"] == 3
    assert eng.loop_failures == 3  # every crash counted; subsystem never silently dead
