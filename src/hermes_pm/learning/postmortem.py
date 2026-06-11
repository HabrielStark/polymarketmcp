"""Postmortem engine (FR-LEARN-001/002).

Deterministically classifies the dominant driver of a closed position or a large
rejection from the evidence actually recorded: realized P&L, slippage error
(expected vs simulated execution), data staleness at entry, and the dominant
evidence source class. It does not fabricate ground truth it cannot observe."""

from __future__ import annotations

from typing import Any

from hermes_pm.models import (
    FailureMode,
    Fill,
    Order,
    Position,
    RiskDecision,
    Signal,
    SourceType,
    TradeIntent,
)


class PostmortemEngine:
    def analyze_position(
        self,
        campaign_id: str,
        intent: TradeIntent,
        order: Order,
        fills: list[Fill],
        position: Position,
        signals: list[Signal],
        entry_was_stale: bool = False,
    ) -> dict[str, Any]:
        realized = round(position.realized_pnl, 6)
        outcome = "win" if realized > 1e-6 else "loss" if realized < -1e-6 else "flat"

        # Slippage error: difference between intended price and avg simulated fill.
        avg_fill = (
            round(sum(f.price * f.size_usd for f in fills) / sum(f.size_usd for f in fills), 6)
            if fills
            else None
        )
        slippage_error = (
            round(abs(avg_fill - intent.limit_price), 6) if avg_fill is not None else None
        )

        social = sum(1 for s in signals if s.source_type is SourceType.SOCIAL)
        primary = sum(1 for s in signals if s.source_type is SourceType.PRIMARY)
        social_dominated = social > 0 and social >= max(1, primary) * 2

        failure_mode = self._classify(
            outcome, entry_was_stale, slippage_error, social_dominated
        )
        drivers = self._drivers(outcome, entry_was_stale, slippage_error, social_dominated, signals)
        return {
            "campaign_id": campaign_id,
            "intent_id": intent.intent_id,
            "order_id": order.order_id,
            "outcome": outcome,
            "realized_pnl": realized,
            "avg_fill_price": avg_fill,
            "intended_price": intent.limit_price,
            "slippage_error": slippage_error,
            "failure_mode": failure_mode.value,
            "drivers": drivers,
            "evidence_count": len(signals),
            "social_dominated_evidence": social_dominated,
            "thesis": intent.thesis,
            "counter_thesis": intent.counter_thesis,
        }

    def analyze_rejection(self, decision: RiskDecision) -> dict[str, Any]:
        return {
            "intent_id": decision.intent_id,
            "outcome": "rejected",
            "failure_mode": FailureMode.RISK_LIMIT.value,
            "drivers": decision.violated_rules,
            "reasons": decision.reasons,
        }

    @staticmethod
    def _classify(
        outcome: str, stale: bool, slippage_error: float | None, social_dominated: bool
    ) -> FailureMode:
        if outcome == "win":
            return FailureMode.THESIS_CORRECT
        if outcome == "flat":
            return FailureMode.RANDOM_VARIANCE
        # loss
        if stale:
            return FailureMode.STALE_DATA
        if slippage_error is not None and slippage_error > 0.03:
            return FailureMode.LIQUIDITY_ERROR
        if social_dominated:
            return FailureMode.SOCIAL_HYPE
        return FailureMode.THESIS_INCORRECT

    @staticmethod
    def _drivers(
        outcome: str,
        stale: bool,
        slippage_error: float | None,
        social_dominated: bool,
        signals: list[Signal],
    ) -> list[str]:
        drivers: list[str] = []
        if stale:
            drivers.append("entry against stale data")
        if slippage_error is not None and slippage_error > 0.03:
            drivers.append(f"high slippage error {slippage_error}")
        if social_dominated:
            drivers.append("evidence dominated by low-trust social sources")
        if any(s.suspected_injection for s in signals):
            drivers.append("some evidence flagged as suspected prompt injection")
        if not drivers:
            drivers.append("thesis vs realized outcome divergence" if outcome == "loss" else "thesis confirmed")
        return drivers
