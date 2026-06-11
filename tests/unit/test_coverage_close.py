"""Coverage-closing tests for real features that were under-exercised:
prompts, postmortem classification, lesson reinforcement, the Hermes bridge,
campaign lifecycle transitions, the risk confidence gate, and the live-process
command handler."""

from __future__ import annotations

import pytest

from hermes_pm.audit.store import AuditStore
from hermes_pm.config import RiskPolicy
from hermes_pm.data.cache import OrderBookCache
from hermes_pm.events import EventBus
from hermes_pm.execution.paper_engine import PaperEngine
from hermes_pm.learning.hermes_bridge import HermesBridge
from hermes_pm.learning.lessons import LessonService
from hermes_pm.learning.postmortem import PostmortemEngine
from hermes_pm.mcp.prompts import PROMPT_SPECS, render_prompt
from hermes_pm.models import (
    BookLevel,
    Campaign,
    CloseStatus,
    Fill,
    Lesson,
    MemoryTarget,
    Order,
    OrderBookSnapshot,
    Position,
    RiskDecision,
    RiskResult,
    Side,
    Signal,
    SourceType,
    TradeIntent,
)
from hermes_pm.persistence.db import Database


# --- MCP prompts: render all six -------------------------------------------- #
@pytest.mark.parametrize("spec", PROMPT_SPECS, ids=lambda s: s.name)
def test_every_prompt_renders_nonempty(spec):
    text = render_prompt(spec.name, {a["name"]: "x" for a in spec.arguments})
    assert isinstance(text, str) and len(text) > 40


def test_unknown_prompt_render():
    assert "Unknown prompt" in render_prompt("does_not_exist", {})


# --- Postmortem classification branches (FR-LEARN-002) ---------------------- #
def _pm_inputs(realized, fill_price=0.50, limit=0.50, social=0):
    intent = TradeIntent(campaign_id="c", market_id="m", token_id="tok", side=Side.BUY,
                         limit_price=limit, max_size_usd=10, thesis="t", counter_thesis="c",
                         confidence=0.6, expires_at="2026-12-30T00:00:00Z")
    order = Order(campaign_id="c", intent_id=intent.intent_id, risk_decision_id="rd",
                  market_id="m", token_id="tok", side=Side.BUY, order_type=intent.order_type,
                  price=limit, size_usd=10)
    fills = [Fill(order_id=order.order_id, price=fill_price, size_usd=10, shares=10 / fill_price,
                  snapshot_id="snap")]
    pos = Position(campaign_id="c", market_id="m", token_id="tok", realized_pnl=realized,
                   close_status=CloseStatus.CLOSED)
    sigs = [Signal(market_id="m", source_type=SourceType.SOCIAL, source_ref=f"x{i}",
                   text_summary="v", trust_score=0.2) for i in range(social)]
    return intent, order, fills, pos, sigs


def test_postmortem_win_loss_flat_and_failure_modes():
    pm = PostmortemEngine()
    # win
    i, o, f, p, s = _pm_inputs(realized=5.0)
    assert pm.analyze_position("c", i, o, f, p, s)["failure_mode"] == "thesis_correct"
    # flat
    i, o, f, p, s = _pm_inputs(realized=0.0)
    assert pm.analyze_position("c", i, o, f, p, s)["failure_mode"] == "random_variance"
    # loss + stale entry
    i, o, f, p, s = _pm_inputs(realized=-3.0)
    assert pm.analyze_position("c", i, o, f, p, s, entry_was_stale=True)["failure_mode"] == "stale_data"
    # loss + high slippage (fill 0.55 vs limit 0.50)
    i, o, f, p, s = _pm_inputs(realized=-3.0, fill_price=0.55, limit=0.50)
    assert pm.analyze_position("c", i, o, f, p, s)["failure_mode"] == "liquidity_error"
    # loss + social-dominated evidence
    i, o, f, p, s = _pm_inputs(realized=-3.0, social=3)
    assert pm.analyze_position("c", i, o, f, p, s)["failure_mode"] == "social_hype"
    # plain loss
    i, o, f, p, s = _pm_inputs(realized=-3.0)
    assert pm.analyze_position("c", i, o, f, p, s)["failure_mode"] == "thesis_incorrect"


def test_postmortem_rejection():
    d = RiskDecision(intent_id="i", campaign_id="c", result=RiskResult.REJECT,
                     violated_rules=["stale_market_data"], reasons=["x"])
    out = PostmortemEngine().analyze_rejection(d)
    assert out["outcome"] == "rejected" and out["failure_mode"] == "risk_limit"


# --- Lessons reinforcement + active promotion (FR-LEARN-006) ---------------- #
def test_lesson_reinforce_and_active(db):
    svc = LessonService(db, EventBus())
    # ACTIVE requested with insufficient evidence -> downgraded to SESSION
    lesson = svc.create("c", trigger="t", observation="o", rule="r",
                        memory_target=MemoryTarget.ACTIVE, supporting_evidence_count=1)
    assert lesson.memory_target is MemoryTarget.SESSION
    # reinforce raises supporting evidence
    again = svc.reinforce(lesson.lesson_id)
    assert again.supporting_evidence_count == 2
    # human-confirmed ACTIVE stays ACTIVE
    confirmed = svc.create("c", trigger="t2", observation="o", rule="r2",
                           memory_target=MemoryTarget.ACTIVE, human_confirmed=True)
    assert confirmed.memory_target is MemoryTarget.ACTIVE
    assert len(svc.list("c")) == 2
    assert svc.reinforce("nope") is None


# --- Hermes bridge exports (FR-LEARN-004/005) ------------------------------- #
def test_hermes_bridge_exports(tmp_path):
    bridge = HermesBridge(tmp_path)
    lessons = [
        Lesson(campaign_id="c", trigger="active trig", observation="o", rule="active rule",
               memory_target=MemoryTarget.ACTIVE, source_refs=["s1"]),
        Lesson(campaign_id="c", trigger="x", observation="o", rule="session rule",
               memory_target=MemoryTarget.SESSION),
    ]
    mem = bridge.export_active_memory(lessons)
    body = mem.read_text(encoding="utf-8")
    assert "active rule" in body and "session rule" not in body  # only ACTIVE exported
    skill = bridge.export_skill_candidate("counter_check", "desc", ["a", "b"], ["lesson://1"])
    assert skill.exists() and "Skill Candidate" in skill.read_text(encoding="utf-8")


# --- Campaign lifecycle transitions (Section 8) ----------------------------- #
def test_campaign_transitions_and_illegal(db):
    from hermes_pm.campaign.manager import CampaignManager
    from hermes_pm.config import load_settings
    s = load_settings(data_dir=str(db.path) if db.path != ":memory:" else "./.t")
    eng = PaperEngine(db, OrderBookCache(), EventBus(), AuditStore(db), RiskPolicy())
    mgr = CampaignManager(s, db, EventBus(), AuditStore(db), eng)
    c = mgr.create(name="t", duration_hours=24, paper_bankroll_usd=500)
    assert mgr.pause(c.campaign_id).status.value == "paused"
    assert mgr.resume(c.campaign_id).status.value == "running"
    assert mgr.stop(c.campaign_id).status.value == "stopped"
    # illegal: pause a stopped campaign
    with pytest.raises(Exception):
        mgr.pause(c.campaign_id)
    # complete path
    c2 = mgr.create(name="t2", duration_hours=24, paper_bankroll_usd=500)
    assert mgr.complete(c2.campaign_id).status.value == "completed"
    # validation errors
    with pytest.raises(Exception):
        mgr.create(name="bad", duration_hours=0, paper_bankroll_usd=500)
    with pytest.raises(Exception):
        mgr.create(name="bad", duration_hours=24, paper_bankroll_usd=-1)


# --- Risk engine confidence gate (defensive branch) ------------------------- #
def test_risk_confidence_below_minimum():
    from hermes_pm.models import Market
    from hermes_pm.risk.engine import RiskContext, RiskEngine
    pol = RiskPolicy(min_confidence=0.9)
    intent = TradeIntent(campaign_id="c", market_id="m", token_id="tok", side=Side.BUY,
                         limit_price=0.51, max_size_usd=10, thesis="t", counter_thesis="c",
                         confidence=0.5, expires_at="2026-12-30T00:00:00Z")
    market = Market(market_id="m", event_id="e", condition_id="c", question="q?",
                    enable_order_book=True, resolution_rules="r", resolution_source="s",
                    token_ids={"YES": "tok"})
    book = OrderBookSnapshot(token_id="tok", bids=[BookLevel(price=0.49, size=500)],
                             asks=[BookLevel(price=0.51, size=500)])
    ev = [Signal(market_id="m", source_type=SourceType.PRIMARY, source_ref="o",
                 text_summary="x", trust_score=0.9)]
    d = RiskEngine().evaluate(RiskContext(intent=intent, market=market,
                                          campaign=Campaign(name="c", bankroll=1000), policy=pol,
                                          book=book, book_is_stale=False, data_age_ms=10,
                                          evidence=ev))
    assert "confidence_below_minimum" in d.violated_rules


# --- live_process command handler ------------------------------------------- #
async def test_live_process_handle_branches(tmp_path):
    from hermes_pm.config import load_settings
    from hermes_pm.execution.live_adapter import LiveAdapter
    from hermes_pm.execution.live_process import _handle, _NullAudit
    s = load_settings(data_dir=str(tmp_path), db_filename="lph.sqlite3")
    db = Database(s.db_path)
    adapter = LiveAdapter(s, _NullAudit(), db.get_risk_decision, geoblock_check=None)
    assert (await _handle(adapter, {"cmd": "status"}))["ok"] is True
    assert (await _handle(adapter, {"cmd": "open_orders"}))["result"] == []
    blocked = await _handle(adapter, {"cmd": "place_intent", "trade_intent_id": "x",
                                      "risk_decision_id": "y"})
    assert blocked["result"]["status"] == "blocked"
    assert (await _handle(adapter, {"cmd": "cancel", "order_ref": "o"}))["result"]["cancelled"]
    assert (await _handle(adapter, {"cmd": "bogus"}))["ok"] is False
