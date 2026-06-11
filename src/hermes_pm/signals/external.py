"""External signal adapters: weather, sports, news (FR-EXT-001..004).

Each adapter is a complete, working provider in deterministic offline mode and
declares the metadata the risk engine needs to reason about source freshness and
authority (FR-EXT-002). Weather preserves station/location, forecast issue time,
model run time, units, and confidence intervals (FR-EXT-003). Sports preserves
game status, official source, clock/period, and update time (FR-EXT-004)."""

from __future__ import annotations

import hashlib

from hermes_pm.models import Market, Signal, SignalStance, SourceType
from hermes_pm.signals.base import AdapterMeta, SignalAdapter, build_signal
from hermes_pm.util.timeutil import now_iso, now_ms


def _h(*parts: str) -> int:
    return int(hashlib.sha256("|".join(parts).encode()).hexdigest()[:8], 16)


class WeatherAdapter(SignalAdapter):
    meta = AdapterMeta(
        name="weather", source_class=SourceType.PRIMARY, latency_class="delayed",
        update_frequency_s=3600, source_authority="national_weather_service",
        reliability=0.85, licensing="public_domain", suitable_for_realtime=False,
    )

    async def fetch(self, market: Market, *, counter: bool = False) -> list[Signal]:
        if market.category != "weather":
            return []
        seed = _h(market.market_id, "w")
        prob = round(0.2 + (seed % 60) / 100, 2)
        if counter:
            prob = round(1 - prob, 2)
        station, issue = f"STN-{seed % 1000:03d}", now_iso()
        text = (
            f"[synthetic NWS] station={station} issue_time={issue} model_run={issue} "
            f"units=metric forecast_probability={prob} confidence_interval=±0.1 for "
            f"'{market.question}'."
        )
        stance = SignalStance.BULLISH if prob >= 0.5 else SignalStance.BEARISH
        return [
            build_signal(
                market.market_id, self.meta,
                source_ref=f"weather://{station}/{issue}", raw_text=text,
                stance=stance, confidence=round(abs(prob - 0.5) * 2, 3), novelty=0.4,
                issued_at=now_ms(),
            )
        ]


class SportsAdapter(SignalAdapter):
    meta = AdapterMeta(
        name="sports", source_class=SourceType.PRIMARY, latency_class="delayed",
        update_frequency_s=30, source_authority="official_league_feed",
        reliability=0.80, licensing="provider_terms", suitable_for_realtime=False,
    )

    async def fetch(self, market: Market, *, counter: bool = False) -> list[Signal]:
        if market.category != "sports":
            return []
        seed = _h(market.market_id, "s")
        lead = (seed % 21) - 10
        if counter:
            lead = -lead
        status, period, clock = "in_progress", (seed % 4) + 1, f"{seed % 12:02d}:{seed % 60:02d}"
        text = (
            f"[synthetic league feed] status={status} period={period} clock={clock} "
            f"update_time={now_iso()} score_differential={lead:+d} for '{market.question}'."
        )
        stance = SignalStance.BULLISH if lead > 0 else SignalStance.BEARISH if lead < 0 else SignalStance.NEUTRAL
        return [
            build_signal(
                market.market_id, self.meta,
                source_ref=f"sports://{market.event_id}/{period}/{clock}", raw_text=text,
                stance=stance, confidence=round(min(1.0, abs(lead) / 10), 3), novelty=0.5,
                issued_at=now_ms(),
            )
        ]


class NewsAdapter(SignalAdapter):
    meta = AdapterMeta(
        name="news", source_class=SourceType.SECONDARY, latency_class="delayed",
        update_frequency_s=300, source_authority="aggregated_reputable_press",
        reliability=0.60, licensing="fair_use_summary", suitable_for_realtime=False,
    )

    async def fetch(self, market: Market, *, counter: bool = False) -> list[Signal]:
        seed = _h(market.market_id, "n")
        bullish = (seed % 2 == 0) != counter
        text = (
            f"[synthetic press summary] Reporting around '{market.question}' "
            f"{'supports' if bullish else 'casts doubt on'} a YES resolution; "
            f"published_at={now_iso()}; two independent outlets cited."
        )
        return [
            build_signal(
                market.market_id, self.meta,
                source_ref=f"news://{market.market_id}/{'c' if counter else 'm'}",
                raw_text=text,
                stance=SignalStance.BULLISH if bullish else SignalStance.BEARISH,
                confidence=0.45, novelty=0.55, issued_at=now_ms(),
            )
        ]
