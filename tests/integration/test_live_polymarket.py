"""Network-gated LIVE integration test against the real Polymarket public API.

Skipped by default. The public market-data API needs no key [S6]; run it with::

    HPM_RUN_LIVE_TESTS=1 python -m pytest tests/integration/test_live_polymarket.py -q

This permanently proves the live path is *real* — discovery, normalization,
categorization from the live tag taxonomy, resolution clarity via the UMA oracle,
outcome-price parsing, FR-MD-005 liquidity, and a real CLOB order-book snapshot —
rather than only mocked payloads. It is the executable guarantee behind
``HPM_MARKET_DATA_SOURCE=live``."""

from __future__ import annotations

import os

import pytest
import pytest_asyncio

from hermes_pm.config import load_settings
from hermes_pm.data.discovery import DiscoveryEngine
from hermes_pm.data.polymarket_client import PolymarketSource

_RUN_LIVE = os.environ.get("HPM_RUN_LIVE_TESTS") == "1"

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not _RUN_LIVE, reason="live network test — set HPM_RUN_LIVE_TESTS=1 to run"
    ),
]


@pytest_asyncio.fixture
async def live_source():
    src = PolymarketSource(load_settings(market_data_source="live"))
    try:
        yield src
    finally:
        await src.close()


async def test_live_discovery_is_real(live_source):
    markets = await live_source.discover_markets()
    assert len(markets) >= 10, "expected a non-trivial live market universe"

    # Categorization is real and varied — not the old hardcoded 'uncategorized'.
    categories = {m.category for m in markets}
    assert categories - {"uncategorized"}, "no real categories derived from live tags"

    # Resolution clarity works for real markets (UMA oracle path) so the
    # tradability gate is no longer empty under live data.
    tradable = [m for m in markets if DiscoveryEngine.is_tradable(m)[0]]
    assert tradable, "no live market passed tradability (resolution + order book)"
    assert any(m.resolution_source.startswith("uma:") for m in tradable)

    # FR-MD-001: outcome prices parsed from the live payload, in [0, 1].
    priced = [m for m in markets if m.outcome_prices]
    assert priced, "no live market carried parseable outcome prices"
    for price in priced[0].outcome_prices.values():
        assert 0.0 <= price <= 1.0

    # FR-MD-005: liquidity present, and the live filter actually filters.
    assert any(m.liquidity_usd for m in markets)
    liquid = DiscoveryEngine.build_watchlist(markets, {"min_liquidity": 1000.0})
    assert 0 < len(liquid) <= len(markets)


async def test_live_order_book_snapshot_is_real(live_source):
    markets = await live_source.discover_markets()
    tradable = [m for m in markets if DiscoveryEngine.is_tradable(m)[0] and m.token_ids]
    assert tradable
    token = next(iter(tradable[0].token_ids.values()))
    snap = await live_source.snapshot(token)
    assert snap is not None and snap.source == "live"
    if snap.best_bid is not None:
        assert 0.0 <= snap.best_bid <= 1.0
    if snap.best_ask is not None:
        assert 0.0 <= snap.best_ask <= 1.0


async def test_live_geoblock_check_returns_decision(live_source):
    result = await live_source.geoblock_check()
    assert isinstance(result, dict) and "blocked" in result
