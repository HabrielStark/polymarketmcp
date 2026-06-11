"""MCP prompts (Section 10.4). These encode the supervised agent loop (16.1) and
the non-negotiable constraints: paper-first, deterministic risk gate, adversarial
thesis/counter-thesis, source provenance, and no live execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PromptSpec:
    name: str
    description: str
    arguments: list[dict[str, Any]] = field(default_factory=list)


def _arg(name: str, desc: str, required: bool = True) -> dict[str, Any]:
    return {"name": name, "description": desc, "required": required}


PROMPT_SPECS: list[PromptSpec] = [
    PromptSpec("research_market", "Guide research of one market end to end.",
               [_arg("market_id", "Market to research"),
                _arg("horizon", "Time horizon", False),
                _arg("allowed_sources", "Comma-separated allowed signal sources", False)]),
    PromptSpec("paper_campaign_manager", "Run a paper campaign under constraints.",
               [_arg("campaign_id", "Campaign id"),
                _arg("bankroll", "Paper bankroll USD", False),
                _arg("duration", "Duration hours", False),
                _arg("market_universe", "Market filter description", False),
                _arg("limits", "Risk limit notes", False)]),
    PromptSpec("trade_intent_reviewer", "Critique one trade intent before execution.",
               [_arg("trade_intent_id", "Intent to review")]),
    PromptSpec("postmortem_closed_trade", "Analyze a closed trade and propose one lesson.",
               [_arg("trade_id", "Trade/intent id"), _arg("campaign_id", "Campaign id")]),
    PromptSpec("promotion_report", "Decide whether results justify more paper or review.",
               [_arg("campaign_id", "Campaign id"), _arg("evaluation_window", "Window", False)]),
    PromptSpec("live_supervisor", "Operate live-eligible mode with maximum conservatism.",
               [_arg("campaign_id", "Campaign id"),
                _arg("confirmation_policy", "Confirmation policy", False)]),
]

PROMPTS_BY_NAME = {p.name: p for p in PROMPT_SPECS}


def render_prompt(name: str, args: dict[str, str]) -> str:
    a = args or {}
    if name == "research_market":
        mid = a.get("market_id", "<market_id>")
        return (
            f"You are researching market {mid} for a PAPER prediction-market campaign.\n"
            "Follow this loop strictly:\n"
            f"1. get_resolution_rules({mid}) — refuse to trade if rules are ambiguous.\n"
            f"2. get_market_snapshot / get_liquidity_summary — reject stale or thin books.\n"
            f"3. gather_evidence({mid}) then gather_evidence({mid}, counter=true) — you MUST seek\n"
            "   contradictory evidence before any sizeable intent.\n"
            "4. search_past_decisions to check whether a similar thesis already exists.\n"
            "5. Form a thesis AND an explicit counter-thesis (why the market may be right).\n"
            "6. Only propose_trade_intent if evidence, liquidity, and freshness are sufficient.\n"
            f"Allowed sources: {a.get('allowed_sources', 'official + social-for-context')}. "
            f"Horizon: {a.get('horizon', 'campaign default')}."
        )
    if name == "paper_campaign_manager":
        cid = a.get("campaign_id", "<campaign_id>")
        return (
            f"Manage PAPER campaign {cid}. Never enable live mode. For each candidate market run\n"
            "the research loop, submit every intent to risk_check_trade_intent, and only call\n"
            "paper_place_order on approve/modify. Monitor fills, mark-to-market, and write a\n"
            "postmortem + compact lesson for every closed position. Keep the operator's limits:\n"
            f"bankroll={a.get('bankroll', 'as configured')}, duration={a.get('duration', 'as configured')}, "
            f"universe={a.get('market_universe', 'as configured')}, limits={a.get('limits', 'defaults')}."
        )
    if name == "trade_intent_reviewer":
        tid = a.get("trade_intent_id", "<intent_id>")
        return (
            f"Critique trade intent {tid} adversarially. Call simulate_trade_intent and\n"
            f"risk_check_trade_intent. Check: is there a counter-thesis? Is evidence primary or\n"
            "merely social? Is there correlated exposure? Is the book fresh and deep enough?\n"
            "Recommend approve / shrink / reject with explicit reasons."
        )
    if name == "postmortem_closed_trade":
        return (
            f"Analyze closed trade {a.get('trade_id', '<id>')} in campaign "
            f"{a.get('campaign_id', '<cid>')}. Call generate_postmortem, classify the dominant\n"
            "driver, then write ONE compact lesson via write_lesson. Do NOT promote a single\n"
            "lucky/unlucky result to active memory without repeated evidence."
        )
    if name == "promotion_report":
        return (
            f"Produce the promotion report for campaign {a.get('campaign_id', '<cid>')} via\n"
            "get_promotion_report. State plainly whether results are statistically weak,\n"
            "operationally safe, and compliance-eligible. A positive paper run is evidence for\n"
            "MORE testing, never proof of profit, and never an unlock of live mode."
        )
    if name == "live_supervisor":
        return (
            f"Operate campaign {a.get('campaign_id', '<cid>')} in live-eligible supervision with\n"
            "MAXIMUM conservatism. Live execution is compliance-locked: live_place_order_intent\n"
            "accepts only references to a risk-approved intent and will return 'blocked' unless\n"
            "every eligibility/jurisdiction/age/geoblock/confirmation gate passes. Never attempt\n"
            f"to bypass a gate. Confirmation policy: {a.get('confirmation_policy', 'explicit per-order')}."
        )
    return f"Unknown prompt: {name}"
