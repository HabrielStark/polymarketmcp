"""Replay / paper-parity tests (SRS 19.1 Paper/Replay row; FR-DATA-005, AC-004)."""

from __future__ import annotations

import pytest

from hermes_pm.replay.engine import ReplayEngine

pytestmark = pytest.mark.asyncio


async def test_orders_replay_from_snapshots(populated):
    daemon, cid = populated
    orders = daemon.db.list_orders(cid)
    filled = [o for o in orders if o.fills]
    assert filled, "scripted campaign should have produced at least one fill"
    engine = ReplayEngine(daemon)
    for o in filled:
        result = engine.replay_order(o.order_id)
        assert result["match"] is True


async def test_decision_replay_deterministic(populated):
    daemon, cid = populated
    decisions = daemon.db.list_risk_decisions(cid)
    assert decisions
    engine = ReplayEngine(daemon)
    for d in decisions:
        r = engine.replay_decision(d.decision_id)
        assert r["idempotency_key_matches"] is True
        assert r["result_matches"] is True


async def test_campaign_replay_reproduces_equity(populated):
    daemon, cid = populated
    r = ReplayEngine(daemon).replay_campaign(cid)
    assert r["equity_match"] is True
    assert r["ledger_balanced"] is True


async def test_replay_via_daemon_tool(populated):
    daemon, cid = populated
    decisions = daemon.db.list_risk_decisions(cid)
    if decisions:
        out = daemon.replay_decision(decisions[0].decision_id)
        assert out["result_matches"] is True
