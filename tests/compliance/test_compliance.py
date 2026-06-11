"""Compliance tests (SRS 19.1 Compliance row; FR-LIVE-*, COMP-*, AC-006/007)."""

from __future__ import annotations

import pytest

from hermes_pm.audit.store import AuditStore
from hermes_pm.config import load_settings
from hermes_pm.execution.live_adapter import LiveAdapter, SigningVault
from hermes_pm.models import RiskDecision, RiskResult
from hermes_pm.persistence.db import Database


def _adapter(**override):
    s = load_settings(**override)
    db = Database(":memory:")
    return LiveAdapter(s, AuditStore(db), db.get_risk_decision), db


async def test_live_disabled_by_default(daemon):
    out = await daemon.live_place_order_intent("ti", "rd")
    assert out["status"] == "blocked"
    assert out["compliance_state"]["live_enabled"] is False
    assert out["compliance_state"]["all_pass"] is False


async def test_all_gates_must_pass_even_with_flags(monkeypatch):
    # Even if every operator flag is set, the locked vault keeps it blocked.
    adapter, db = _adapter(live_enabled=True, operator_age_verified=True,
                           operator_jurisdiction_allowed=True, operator_acknowledged_risk=True)
    out = await adapter.place_order_intent("ti", "rd", "confirm")
    assert out["status"] == "blocked"
    assert out["compliance_state"]["signing_vault_available"] is False


async def test_geoblock_fail_closed(daemon):
    # synthetic source -> geoblock cannot be verified -> treated as blocked
    state = await daemon.live._gate.evaluate(None, None, daemon.live._vault)
    assert state["geoblock_pass"] is False


def test_signing_vault_never_signs_or_exposes():
    v = SigningVault()
    assert v.available is False
    assert v.status()["exposes_secrets"] is False
    with pytest.raises(PermissionError):
        v.sign("any-ref")


async def test_cancel_only_always_allowed(daemon):
    out = await daemon.live_cancel_order("ref-1")
    assert out["cancelled"] is True


async def test_age_jurisdiction_gates_reported(monkeypatch):
    adapter, db = _adapter(live_enabled=True)  # age/jurisdiction NOT verified
    state = await adapter._gate.evaluate(
        RiskDecision(intent_id="i", campaign_id="c", result=RiskResult.APPROVE), "tok", adapter._vault
    )
    assert state["operator_age_verified"] is False
    assert state["jurisdiction_allowed"] is False
    assert state["all_pass"] is False


async def test_emergency_stop_records_audit_and_blocks(daemon):
    daemon.start_paper_campaign(campaign_name="c", duration_hours=24, paper_bankroll_usd=500)
    res = daemon.emergency_stop()
    assert res["emergency_stop"] is True and res["audit_event_id"]
    # new actions blocked
    with pytest.raises(Exception):
        daemon.start_paper_campaign(campaign_name="x", duration_hours=24, paper_bankroll_usd=500)
    # audit event present
    events = daemon.get_audit_events(limit=50)
    assert any(e["type"] == "emergency_stop" for e in events)


async def test_compliance_freeze_on_change(daemon):
    daemon.live.freeze("jurisdiction changed")
    state = await daemon.live._gate.evaluate(None, None, daemon.live._vault)
    assert state["not_frozen"] is False
