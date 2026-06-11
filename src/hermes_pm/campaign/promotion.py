"""Promotion report (Section 15.2/15.3, AC-008).

NEVER auto-unlocks live mode; at most it recommends an eligibility *review*. It
explicitly states sample-size limitations and reports three blunt verdicts:
``statistically_weak``, ``operationally_safe``, and ``compliance_eligible``."""

from __future__ import annotations

from typing import Any

from hermes_pm.models import Campaign

MIN_DECISIONS_FOR_CONFIDENCE = 30


def build_promotion_report(
    campaign: Campaign,
    metrics: dict[str, Any],
    *,
    compliance_state: dict[str, Any],
    operational: dict[str, Any],
    lessons_count: int,
    audit_chain_ok: bool,
) -> dict[str, Any]:
    sample = metrics["decision_sample_size"]
    statistically_weak = (
        sample < MIN_DECISIONS_FOR_CONFIDENCE or campaign.duration_hours <= 72
    )

    # Operational safety (PC-002): no unresolved outages/desyncs/defects/audit gaps.
    operationally_safe = bool(
        audit_chain_ok
        and metrics["ledger_balanced"]
        and operational.get("data_outages", 0) == 0
        and operational.get("fill_sim_errors", 0) == 0
    )

    compliance_eligible = bool(compliance_state.get("all_pass", False))

    # PC criteria evaluation -------------------------------------------------
    pc: dict[str, bool] = {}
    pc["PC-001_eligibility"] = compliance_eligible
    pc["PC-002_no_operational_defects"] = operationally_safe
    # PC-003: positive P&L must not rest on optimistic fills. We run pessimistic;
    # flag only if profit appeared with implausibly low slippage error.
    pc["PC-003_fills_not_optimistic"] = not (
        metrics["net_pnl"] > 0 and metrics["slippage_model_error"] < 0.0
    )
    within_drawdown = metrics["max_drawdown"] <= campaign.bankroll * 0.10 + 1e-6
    pc["PC-004_within_risk_limits"] = within_drawdown
    # PC-005: agent must not repeatedly try to bypass risk rules.
    pc["PC-005_no_risk_bypass_attempts"] = metrics["risk_rejections"] <= max(5, sample)
    pc["PC-006_positive_run_is_not_proof"] = True  # always asserted as a caveat

    recommendation = _recommend(
        statistically_weak, operationally_safe, compliance_eligible, metrics, pc
    )

    return {
        "campaign_id": campaign.campaign_id,
        "verdicts": {
            "statistically_weak": statistically_weak,
            "operationally_safe": operationally_safe,
            "compliance_eligible": compliance_eligible,
        },
        # Section 15.3 structure (8 parts).
        "1_campaign_summary": {
            "duration_hours": campaign.duration_hours,
            "bankroll": campaign.bankroll,
            "market_universe": campaign.watchlist,
            "intents": metrics["decision_sample_size"],
            "closed_positions": metrics["closed_positions"],
        },
        "2_performance": {
            "net_pnl": metrics["net_pnl"],
            "max_drawdown": metrics["max_drawdown"],
            "hit_rate": metrics["hit_rate"],
            "profit_factor": metrics["profit_factor"],
            "brier_score": metrics["brier_score"],
            "market_baseline_edge": metrics["market_baseline_edge"],
        },
        "3_execution_quality": {
            "slippage_model_error": metrics["slippage_model_error"],
            "ledger_balanced": metrics["ledger_balanced"],
            "data_outages": operational.get("data_outages", 0),
        },
        "4_risk_quality": {
            "risk_rejections": metrics["risk_rejections"],
            "risk_modifications": metrics["risk_modifications"],
            "rejection_reasons": metrics["rejection_reasons"],
            "within_risk_limits": within_drawdown,
        },
        "5_source_quality": {
            "source_avg_trust": metrics["source_avg_trust"],
            "tainted_evidence_count": metrics["tainted_evidence_count"],
        },
        "6_learning": {"lessons_written": lessons_count},
        "7_compliance": {
            "live_adapter_enabled": compliance_state.get("live_enabled", False),
            "gates": compliance_state,
            "audit_chain_ok": audit_chain_ok,
        },
        "8_recommendation": recommendation,
        "pc_criteria": pc,
        "sample_size_warning": (
            f"Only {sample} decisions over {campaign.duration_hours}h — too small to prove a "
            "durable edge; treat any positive result as a reason for MORE paper testing."
            if statistically_weak
            else None
        ),
    }


def _recommend(
    weak: bool,
    safe: bool,
    eligible: bool,
    metrics: dict[str, Any],
    pc: dict[str, bool],
) -> str:
    if not safe:
        return "continue_paper: operational defects or audit/ledger integrity issues must be fixed first."
    if not all(pc.values()):
        return "continue_paper: one or more promotion criteria failed; keep testing in paper mode."
    if weak:
        return (
            "continue_paper: results are operationally clean but the sample is statistically weak. "
            "Run more/longer paper campaigns before any live eligibility review."
        )
    if not eligible:
        return (
            "paper_only: results are clean and sufficiently sampled, but live eligibility/compliance "
            "gates are NOT satisfied. A human compliance review is required; live remains locked."
        )
    return (
        "eligibility_review_only: clean, sufficiently sampled, and compliance gates pass. "
        "This recommends a HUMAN live-eligibility review — it does NOT unlock live trading."
    )
