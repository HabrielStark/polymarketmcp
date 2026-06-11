"""Campaign evaluation metrics (Section 15.1).

Computed deterministically from persisted intents, decisions, orders, fills, and
positions. Where a metric needs a forecast probability we use the agent's stated
confidence and clearly label it as such; calibration/Brier on a 1-3 day paper run
is explicitly flagged as low-sample (Section 15.2)."""

from __future__ import annotations

from typing import Any

from hermes_pm.models import Campaign, CloseStatus, RiskResult
from hermes_pm.persistence.db import Database


class CampaignEvaluator:
    def __init__(self, db: Database) -> None:
        self.db = db

    def evaluate(self, campaign: Campaign, portfolio: dict[str, Any]) -> dict[str, Any]:
        cid = campaign.campaign_id
        intents = self.db.list_intents(cid)
        decisions = self.db.list_risk_decisions(cid)
        orders = self.db.list_orders(cid)
        positions = self.db.list_positions(cid)
        intents_by_id = {t.intent_id: t for t in intents}

        closed = [p for p in positions if p.close_status is CloseStatus.CLOSED and abs(p.realized_pnl) > 1e-9]
        wins = [p for p in closed if p.realized_pnl > 0]
        losses = [p for p in closed if p.realized_pnl < 0]
        gross_win = sum(p.realized_pnl for p in wins)
        gross_loss = abs(sum(p.realized_pnl for p in losses))
        hit_rate = round(len(wins) / len(closed), 4) if closed else None
        profit_factor = (
            round(gross_win / gross_loss, 4) if gross_loss > 1e-9 else (None if not wins else float("inf"))
        )

        # Brier / calibration proxy from agent confidence vs realized win.
        brier_terms, baseline_diffs = [], []
        for p in closed:
            order = next((o for o in orders if o.token_id == p.token_id), None)
            intent = intents_by_id.get(order.intent_id) if order else None
            if intent is not None:
                outcome = 1.0 if p.realized_pnl > 0 else 0.0
                brier_terms.append((intent.confidence - outcome) ** 2)
                baseline_diffs.append(intent.confidence - order.price)
        brier = round(sum(brier_terms) / len(brier_terms), 4) if brier_terms else None
        baseline_edge = (
            round(sum(baseline_diffs) / len(baseline_diffs), 4) if baseline_diffs else None
        )

        # Slippage model error: avg |intended price - avg fill price|.
        slip_terms = []
        for o in orders:
            fills = self.db.list_fills(o.order_id)
            if fills:
                avg = sum(f.price * f.size_usd for f in fills) / sum(f.size_usd for f in fills)
                slip_terms.append(abs(avg - o.price))
        slippage_error = round(sum(slip_terms) / len(slip_terms), 6) if slip_terms else 0.0

        rejections = [d for d in decisions if d.result is RiskResult.REJECT]
        modifications = [d for d in decisions if d.result is RiskResult.MODIFY]

        # Source reliability across watchlist evidence.
        all_signals = [s for mid in campaign.watchlist for s in self.db.list_signals(mid)]
        avg_trust = (
            round(sum(s.trust_score for s in all_signals) / len(all_signals), 4)
            if all_signals
            else None
        )
        tainted = sum(1 for s in all_signals if s.suspected_injection)

        markets_touched = {t.market_id for t in intents}
        return {
            "net_pnl": portfolio["net_pnl"],
            "max_drawdown": portfolio["max_drawdown"],
            "equity": portfolio["equity"],
            "hit_rate": hit_rate,
            "profit_factor": profit_factor,
            "brier_score": brier,
            "brier_note": "agent-confidence proxy; low sample on short campaigns",
            "market_baseline_edge": baseline_edge,
            "slippage_model_error": slippage_error,
            "risk_rejections": len(rejections),
            "risk_modifications": len(modifications),
            "rejection_reasons": sorted({r for d in rejections for r in d.violated_rules}),
            "source_avg_trust": avg_trust,
            "tainted_evidence_count": tainted,
            "decision_sample_size": len(intents),
            "markets_count": len(markets_touched),
            "closed_positions": len(closed),
            "ledger_balanced": portfolio["ledger_balanced"],
        }
