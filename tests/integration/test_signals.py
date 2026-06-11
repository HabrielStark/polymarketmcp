"""Integration tests: signal adapters + registry (FR-SOC, FR-EXT)."""

from __future__ import annotations

import pytest

from hermes_pm.data.sources import SyntheticSource
from hermes_pm.events import EventBus
from hermes_pm.persistence.db import Database
from hermes_pm.signals.registry import SignalRegistry

pytestmark = pytest.mark.asyncio


async def _registry(settings):
    return SignalRegistry(settings, Database(":memory:"), EventBus()), await SyntheticSource(
        market_count=4).discover_markets()


async def test_gather_multisource_and_provenance(settings):
    reg, markets = await _registry(settings)
    weather = next(m for m in markets if m.category == "weather")
    sigs = await reg.gather(weather)
    adapters = {s.adapter for s in sigs}
    assert "weather" in adapters and "news" in adapters
    summary = reg.summary(weather.market_id)
    assert summary["count"] == len(sigs)
    assert len(summary["provenance"]) == len(sigs)


async def test_counter_signal_search(settings):
    reg, markets = await _registry(settings)
    m = markets[0]
    base = await reg.gather(m)
    counter = await reg.counter_signal_search(m)
    assert len(counter) > 0
    # counter refs differ from main refs
    assert {s.source_ref for s in counter} != {s.source_ref for s in base}


async def test_adapter_metadata_complete(settings):
    reg, _ = await _registry(settings)
    for meta in reg.adapter_catalog():
        for field in ("latency_class", "source_authority", "reliability", "licensing",
                      "suitable_for_realtime"):
            assert field in meta


async def test_social_is_delayed_not_realtime(settings):
    reg, _ = await _registry(settings)
    x = next(m for m in reg.adapter_catalog() if m["name"] == "x_social")
    assert x["latency_class"] == "delayed" and x["suitable_for_realtime"] is False


async def test_evidence_sanitized_and_flagged(settings):
    reg, markets = await _registry(settings)
    sigs = await reg.gather(markets[0])
    assert all(isinstance(s.text_summary, str) for s in sigs)
    # social signals are lowest trust
    social = [s for s in sigs if s.adapter == "x_social"]
    assert all(s.trust_score <= 0.4 for s in social)
