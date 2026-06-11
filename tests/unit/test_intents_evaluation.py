"""Unit tests: intent lifecycle (FR-TI) and evaluation/promotion (Section 15)."""

from __future__ import annotations

from hermes_pm.campaign.promotion import build_promotion_report
from hermes_pm.config import RiskPolicy
from hermes_pm.execution.intents import IntentService
from hermes_pm.models import Campaign, Market, Side


def _market():
    return Market(market_id="m", event_id="e", condition_id="c", question="q?",
                  enable_order_book=True, resolution_rules="r", resolution_source="s",
                  token_ids={"YES": "tok"}, category="weather")


def test_intent_computes_economics(db):
    svc = IntentService(db, RiskPolicy())
    camp = Campaign(name="c", bankroll=1000)
    ti = svc.create(camp, _market(), outcome="YES", side=Side.BUY, limit_price=0.5, max_size_usd=10,
                    thesis="t", counter_thesis="ct", invalidation_criteria="inv",
                    evidence_refs=["off://1"], confidence=0.7, expires_at="2026-12-30T00:00:00Z")
    assert ti.break_even_probability is not None
    assert ti.normalized_ev is not None
    assert ti.status == "created"


def test_intent_needs_more_evidence_without_refs(db):
    svc = IntentService(db, RiskPolicy())
    camp = Campaign(name="c", bankroll=1000)
    ti = svc.create(camp, _market(), outcome="YES", side=Side.BUY, limit_price=0.5, max_size_usd=10,
                    thesis="t", counter_thesis="ct", invalidation_criteria="inv",
                    evidence_refs=[], confidence=0.7, expires_at="2026-12-30T00:00:00Z")
    assert ti.status == "needs_more_evidence"
    assert "evidence_refs" in ti.missing_fields


def test_intent_missing_counter_thesis_flagged(db):
    svc = IntentService(db, RiskPolicy())
    camp = Campaign(name="c", bankroll=1000)
    ti = svc.create(camp, _market(), outcome="YES", side=Side.BUY, limit_price=0.5, max_size_usd=10,
                    thesis="t", counter_thesis="", invalidation_criteria="inv",
                    evidence_refs=["off://1"], confidence=0.7, expires_at="2026-12-30T00:00:00Z")
    assert "counter_thesis" in ti.missing_fields


def test_intent_expired_flagged(db):
    svc = IntentService(db, RiskPolicy())
    camp = Campaign(name="c", bankroll=1000)
    ti = svc.create(camp, _market(), outcome="YES", side=Side.BUY, limit_price=0.5, max_size_usd=10,
                    thesis="t", counter_thesis="ct", invalidation_criteria="inv",
                    evidence_refs=["off://1"], confidence=0.7, expires_at="2000-01-01T00:00:00Z")
    assert "expires_at_in_future" in ti.missing_fields


def _metrics(**kw):
    base = dict(net_pnl=10.0, max_drawdown=5.0, equity=1010.0, hit_rate=0.6, profit_factor=1.5,
                brier_score=0.2, market_baseline_edge=0.05, slippage_model_error=0.01,
                risk_rejections=1, risk_modifications=0, rejection_reasons=[], source_avg_trust=0.7,
                tainted_evidence_count=0, decision_sample_size=5, markets_count=3,
                closed_positions=2, ledger_balanced=True)
    base.update(kw)
    return base


def test_promotion_blocks_live_by_default():
    camp = Campaign(name="c", bankroll=1000, duration_hours=48)
    rep = build_promotion_report(camp, _metrics(), compliance_state={"all_pass": False, "live_enabled": False},
                                 operational={"data_outages": 0, "fill_sim_errors": 0},
                                 lessons_count=1, audit_chain_ok=True)
    assert rep["verdicts"]["compliance_eligible"] is False
    assert rep["verdicts"]["statistically_weak"] is True  # 48h short
    assert "paper" in rep["8_recommendation"].lower()


def test_promotion_flags_operational_defect():
    camp = Campaign(name="c", bankroll=1000, duration_hours=200)
    rep = build_promotion_report(camp, _metrics(ledger_balanced=False),
                                 compliance_state={"all_pass": False},
                                 operational={"data_outages": 2, "fill_sim_errors": 0},
                                 lessons_count=0, audit_chain_ok=True)
    assert rep["verdicts"]["operationally_safe"] is False
    assert rep["8_recommendation"].startswith("continue_paper")


def test_promotion_drawdown_breach_fails_pc004():
    camp = Campaign(name="c", bankroll=1000, duration_hours=200)
    rep = build_promotion_report(camp, _metrics(max_drawdown=200.0),
                                 compliance_state={"all_pass": False},
                                 operational={"data_outages": 0, "fill_sim_errors": 0},
                                 lessons_count=0, audit_chain_ok=True)
    assert rep["pc_criteria"]["PC-004_within_risk_limits"] is False
