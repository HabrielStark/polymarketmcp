"""Signal registry: orchestrates adapters, persists signals with provenance
(FR-SOC-002/006), summarizes them across the dimensions in FR-SOC-004, and runs
counter-signal search (FR-SOC-007)."""

from __future__ import annotations

import statistics

from hermes_pm.config import Settings
from hermes_pm.events import EventBus, EventType
from hermes_pm.models import Market, Signal, SignalStance, SourceType
from hermes_pm.persistence.db import Database
from hermes_pm.signals.base import SignalAdapter
from hermes_pm.signals.external import NewsAdapter, SportsAdapter, WeatherAdapter
from hermes_pm.signals.social_x import XSocialAdapter

_STANCE_SCORE = {
    SignalStance.BULLISH: 1.0,
    SignalStance.BEARISH: -1.0,
    SignalStance.NEUTRAL: 0.0,
    SignalStance.MIXED: 0.0,
}


class SignalRegistry:
    def __init__(self, settings: Settings, db: Database, bus: EventBus) -> None:
        self._s = settings
        self.db = db
        self.bus = bus
        self.adapters: dict[str, SignalAdapter] = {
            "x_social": XSocialAdapter(settings),
            "weather": WeatherAdapter(),
            "sports": SportsAdapter(),
            "news": NewsAdapter(),
        }

    def adapter_catalog(self) -> list[dict]:
        return [a.meta.model_dump() for a in self.adapters.values()]

    async def gather(
        self, market: Market, allowed: list[str] | None = None, *, counter: bool = False
    ) -> list[Signal]:
        out: list[Signal] = []
        for name, adapter in self.adapters.items():
            if allowed is not None and name not in allowed:
                continue
            signals = await adapter.fetch(market, counter=counter)
            for sig in signals:
                self.db.save_signal(sig)
                out.append(sig)
                self.bus.publish(
                    EventType.SIGNAL,
                    {"market_id": market.market_id, "adapter": name, "stance": sig.stance.value,
                     "source_ref": sig.source_ref, "suspected_injection": sig.suspected_injection},
                )
        return out

    async def counter_signal_search(
        self, market: Market, allowed: list[str] | None = None
    ) -> list[Signal]:
        """Contradictory-evidence gathering required before large intents (FR-SOC-007)."""
        return await self.gather(market, allowed, counter=True)

    def summary(self, market_id: str) -> dict:
        """Summarize stored signals by stance, credibility, disagreement,
        velocity, novelty, and provenance (FR-SOC-004/006)."""
        signals = self.db.list_signals(market_id)
        if not signals:
            return {"market_id": market_id, "count": 0, "sources": [], "stance": "none"}
        scores = [_STANCE_SCORE[s.stance] for s in signals]
        net = statistics.fmean(scores)
        disagreement = round(statistics.pstdev(scores), 4) if len(scores) > 1 else 0.0
        by_class = {
            sc.value: sum(1 for s in signals if s.source_type is sc) for sc in SourceType
        }
        timestamps = sorted(s.timestamp for s in signals)
        span_s = max(1, (timestamps[-1] - timestamps[0]) / 1000)
        return {
            "market_id": market_id,
            "count": len(signals),
            "net_stance_score": round(net, 4),
            "stance": (
                "bullish" if net > 0.2 else "bearish" if net < -0.2 else "mixed_or_neutral"
            ),
            "disagreement": disagreement,
            "avg_trust": round(statistics.fmean(s.trust_score for s in signals), 4),
            "max_novelty": round(max(s.novelty for s in signals), 4),
            "velocity_per_min": round(len(signals) / (span_s / 60), 4),
            "by_source_class": by_class,
            "suspected_injection_count": sum(1 for s in signals if s.suspected_injection),
            "provenance": [
                {"signal_id": s.signal_id, "source_ref": s.source_ref, "adapter": s.adapter,
                 "stance": s.stance.value, "trust": s.trust_score}
                for s in signals
            ],
            "provenance_graph": self._provenance_graph(market_id, signals),
        }

    @staticmethod
    def _provenance_graph(market_id: str, signals: list[Signal]) -> dict:
        """Navigable graph (FR-SOC-006): market -> signal -> source/adapter, so the
        dashboard can show which posts/sources influenced a decision."""
        nodes = [{"id": f"market:{market_id}", "type": "market"}]
        edges = []
        seen_src: set[str] = set()
        for s in signals:
            sid = f"signal:{s.signal_id}"
            nodes.append({"id": sid, "type": "signal", "stance": s.stance.value,
                          "trust": s.trust_score, "adapter": s.adapter,
                          "suspected_injection": s.suspected_injection})
            edges.append({"from": f"market:{market_id}", "to": sid, "rel": "has_signal"})
            src = f"source:{s.source_ref}"
            if s.source_ref not in seen_src:
                nodes.append({"id": src, "type": "source", "source_type": s.source_type.value})
                seen_src.add(s.source_ref)
            edges.append({"from": sid, "to": src, "rel": "derived_from"})
        return {"nodes": nodes, "edges": edges}
