"""Integration tests: signal adapters + registry (FR-SOC, FR-EXT)."""

from __future__ import annotations

import pytest

from hermes_pm.data.sources import SyntheticSource
from hermes_pm.events import EventBus
from hermes_pm.metrics.registry import Metrics
from hermes_pm.models import Signal, SourceType
from hermes_pm.persistence.db import Database
from hermes_pm.signals.base import AdapterMeta, SignalAdapter
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
    names = {m["name"] for m in reg.adapter_catalog()}
    assert {"x_social", "weather", "sports", "crypto", "macro", "official_data", "news"} <= names
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


class _FailingXAdapter(SignalAdapter):
    meta = AdapterMeta(
        name="x_social",
        source_class=SourceType.SOCIAL,
        latency_class="delayed",
        update_frequency_s=60,
        source_authority="official_api",
        reliability=0.3,
        licensing="configured_operator_access",
        suitable_for_realtime=False,
    )

    async def fetch(self, market, *, counter: bool = False) -> list[Signal]:
        raise RuntimeError("x stream down")


async def test_x_adapter_failure_increments_disconnect_metric(settings):
    metrics = Metrics()
    db = Database(":memory:")
    reg = SignalRegistry(settings, db, EventBus(), metrics)
    markets = await SyntheticSource(market_count=1).discover_markets()
    reg.adapters["x_social"] = _FailingXAdapter()
    try:
        with pytest.raises(RuntimeError, match="x stream down"):
            await reg.gather(markets[0], allowed=["x_social"])
        assert "hpm_x_stream_disconnects_total 1.0" in metrics.render().decode()
    finally:
        db.close()


async def test_adapter_failure_without_metrics_reraises(settings):
    db = Database(":memory:")
    reg = SignalRegistry(settings, db, EventBus())
    markets = await SyntheticSource(market_count=1).discover_markets()
    reg.adapters["x_social"] = _FailingXAdapter()
    try:
        with pytest.raises(RuntimeError, match="x stream down"):
            await reg.gather(markets[0], allowed=["x_social"])
    finally:
        db.close()
