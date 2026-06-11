"""MCP tool specifications (Section 10.2, 10.5).

Every tool has a strict JSON Schema with ``additionalProperties: false`` so
unknown fields are rejected (MCP-SR-005, NFR-SEC-003). Each spec maps to a method
on the :class:`TradingDaemon`. The live tools accept ONLY references to a
risk-approved intent — never raw market/side/size/price (FR-LIVE-004, AC-006)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

_S = {"type": "string"}
_N = {"type": "number"}
_I = {"type": "integer"}
_B = {"type": "boolean"}


def _obj(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": props,
        "required": required or [],
        "additionalProperties": False,
    }


@dataclass
class ToolSpec:
    name: str
    description: str
    schema: dict[str, Any]
    method: str
    is_async: bool = False
    tags: list[str] = field(default_factory=list)


_RISK_PROFILE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "max_single_trade_risk_pct": _N, "max_market_exposure_pct": _N,
        "max_category_exposure_pct": _N, "max_correlated_exposure_pct": _N,
        "daily_loss_stop_pct": _N, "campaign_loss_stop_pct": _N,
        "min_orderbook_depth_usd": _N, "max_spread": _N, "max_data_staleness_ms": _I,
        "min_primary_sources": _I, "min_secondary_sources": _I, "min_confidence": _N,
        "max_source_age_ratio": _N, "fee_bps": _N, "slippage_bps": _N,
    },
}

TOOL_SPECS: list[ToolSpec] = [
    # ---- System ---------------------------------------------------------- #
    ToolSpec("get_system_status", "Health, connectivity, mode, locks.", _obj({}), "get_system_status"),
    ToolSpec("get_config", "Effective config (secrets redacted).", _obj({}), "get_config"),
    ToolSpec("update_config", "Update a small allow-list of runtime settings.",
             _obj({"updates": {"type": "object"}}, ["updates"]), "update_config"),
    ToolSpec("get_dashboard_url", "Local dashboard URL.",
             _obj({"campaign_id": _S}), "get_dashboard_url"),
    ToolSpec("emergency_stop", "Freeze new actions; cancel open paper orders; audit.",
             _obj({"campaign_id": _S}), "emergency_stop"),
    # ---- Market discovery ------------------------------------------------ #
    ToolSpec("search_markets", "Search/normalize markets with filters.",
             _obj({"filters": {"type": "object"}, "limit": _I}), "search_markets"),
    ToolSpec("get_market_details", "Normalized market metadata + tradability.",
             _obj({"market_id": _S}, ["market_id"]), "get_market_details"),
    ToolSpec("get_resolution_rules", "Resolution rules + source links.",
             _obj({"market_id": _S}, ["market_id"]), "get_resolution_rules"),
    ToolSpec("build_watchlist", "Eligible tradable market ids under filters.",
             _obj({"filters": {"type": "object"}}), "build_watchlist"),
    # ---- Market data ----------------------------------------------------- #
    ToolSpec("get_market_snapshot", "Cached best bid/ask/spread/mid + staleness.",
             _obj({"token_id": _S}, ["token_id"]), "get_market_snapshot"),
    ToolSpec("get_order_book", "Full cached order book + staleness.",
             _obj({"token_id": _S}, ["token_id"]), "get_order_book"),
    ToolSpec("subscribe_markets", "Subscribe market tokens to live data.",
             _obj({"market_ids": {"type": "array", "items": _S}}, ["market_ids"]),
             "subscribe_markets", is_async=True),
    ToolSpec("get_price_history", "Stored snapshot mid/bid/ask history.",
             _obj({"token_id": _S, "limit": _I}, ["token_id"]), "get_price_history"),
    ToolSpec("get_liquidity_summary", "Spread + bid/ask depth in USD.",
             _obj({"token_id": _S}, ["token_id"]), "get_liquidity_summary"),
    # ---- Signals --------------------------------------------------------- #
    ToolSpec("gather_evidence", "Gather (or counter-search) signals for a market.",
             _obj({"market_id": _S, "allowed": {"type": "array", "items": _S}, "counter": _B},
                  ["market_id"]), "gather_evidence", is_async=True),
    ToolSpec("get_social_signal_summary", "Sanitized social signal summary.",
             _obj({"market_id": _S}, ["market_id"]), "get_social_signal_summary"),
    ToolSpec("get_source_evidence", "All stored evidence for a market.",
             _obj({"market_id": _S}, ["market_id"]), "get_source_evidence"),
    ToolSpec("get_weather_signal_summary", "Weather adapter signals.",
             _obj({"market_id": _S}, ["market_id"]), "get_weather_signal_summary", is_async=True),
    ToolSpec("get_sports_signal_summary", "Sports adapter signals.",
             _obj({"market_id": _S}, ["market_id"]), "get_sports_signal_summary", is_async=True),
    # ---- Campaign control ------------------------------------------------ #
    ToolSpec("start_paper_campaign", "Start a paper campaign; returns dashboard URL.",
             _obj({"campaign_name": _S, "duration_hours": _N, "paper_bankroll_usd": _N,
                   "market_filters": {"type": "object"}, "risk_profile": _RISK_PROFILE_SCHEMA,
                   "allowed_signal_sources": {"type": "array", "items": _S}},
                  ["campaign_name", "duration_hours", "paper_bankroll_usd"]), "start_paper_campaign"),
    ToolSpec("pause_campaign", "Pause a campaign.", _obj({"campaign_id": _S}, ["campaign_id"]),
             "pause_campaign"),
    ToolSpec("resume_campaign", "Resume a campaign.", _obj({"campaign_id": _S}, ["campaign_id"]),
             "resume_campaign"),
    ToolSpec("stop_campaign", "Stop a campaign (cancels open orders).",
             _obj({"campaign_id": _S}, ["campaign_id"]), "stop_campaign"),
    ToolSpec("get_campaign_report", "Campaign state + portfolio + metrics.",
             _obj({"campaign_id": _S}, ["campaign_id"]), "get_campaign_report"),
    ToolSpec("get_promotion_report", "Sober promotion report + verdicts.",
             _obj({"campaign_id": _S}, ["campaign_id"]), "get_promotion_report", is_async=True),
    # ---- Trading intent -------------------------------------------------- #
    ToolSpec("propose_trade_intent", "Create a structured trade intent (not an order).",
             _obj({"campaign_id": _S, "market_id": _S, "outcome": _S, "side": {"enum": ["BUY", "SELL"]},
                   "limit_price": _N, "max_size_usd": _N, "thesis": _S,
                   "evidence_refs": {"type": "array", "items": _S}, "confidence": _N,
                   "expires_at": _S, "counter_thesis": _S, "invalidation_criteria": _S,
                   "order_type": {"enum": ["limit", "marketable_limit"]}, "prompt_version": _S},
                  ["campaign_id", "market_id", "outcome", "side", "limit_price", "max_size_usd",
                   "thesis", "expires_at"]), "propose_trade_intent"),
    ToolSpec("simulate_trade_intent", "Dry-run fill projection (no side effects).",
             _obj({"trade_intent_id": _S}, ["trade_intent_id"]), "simulate_trade_intent"),
    ToolSpec("risk_check_trade_intent", "Deterministic risk decision.",
             _obj({"trade_intent_id": _S}, ["trade_intent_id"]), "risk_check_trade_intent"),
    ToolSpec("explain_risk_rejection", "Machine-readable rejection explanation.",
             _obj({"risk_decision_id": _S}, ["risk_decision_id"]), "explain_risk_rejection"),
    # ---- Paper execution ------------------------------------------------- #
    ToolSpec("paper_place_order", "Place a paper order from approved intent+decision.",
             _obj({"trade_intent_id": _S, "risk_decision_id": _S},
                  ["trade_intent_id", "risk_decision_id"]), "paper_place_order"),
    ToolSpec("paper_cancel_order", "Cancel an open paper order.",
             _obj({"paper_order_id": _S}, ["paper_order_id"]), "paper_cancel_order"),
    ToolSpec("paper_get_orders", "All paper orders for a campaign.",
             _obj({"campaign_id": _S}, ["campaign_id"]), "paper_get_orders"),
    ToolSpec("paper_get_portfolio", "Paper portfolio (PAPER-labelled).",
             _obj({"campaign_id": _S}, ["campaign_id"]), "paper_get_portfolio"),
    ToolSpec("paper_mark_to_market", "Re-mark positions and return portfolio.",
             _obj({"campaign_id": _S}, ["campaign_id"]), "paper_mark_to_market"),
    # ---- Live execution (locked, reference-only) ------------------------- #
    ToolSpec("live_place_order_intent", "Reference-only live placement (compliance-locked).",
             _obj({"trade_intent_id": _S, "risk_decision_id": _S, "user_confirmation_token": _S},
                  ["trade_intent_id", "risk_decision_id"]), "live_place_order_intent",
             is_async=True),
    ToolSpec("live_cancel_order", "Cancel-only live action (always permitted).",
             _obj({"order_ref": _S}, ["order_ref"]), "live_cancel_order", is_async=True),
    ToolSpec("live_get_open_orders", "List live open orders (empty while locked).",
             _obj({}), "live_get_open_orders", is_async=True),
    # ---- Learning -------------------------------------------------------- #
    ToolSpec("write_lesson", "Write a compact structured lesson.",
             _obj({"campaign_id": _S, "trigger": _S, "observation": _S, "rule": _S, "pattern": _S,
                   "confidence": _N, "valid_until": _S, "source_refs": {"type": "array", "items": _S},
                   "memory_target": {"enum": ["active", "session", "audit_only"]},
                   "supporting_evidence_count": _I, "human_confirmed": _B},
                  ["campaign_id", "trigger", "observation", "rule"]), "write_lesson"),
    ToolSpec("list_lessons", "List lessons.", _obj({"campaign_id": _S}), "list_lessons"),
    ToolSpec("search_past_decisions", "Search past intents/theses.",
             _obj({"query": _S, "campaign_id": _S}, ["query"]), "search_past_decisions"),
    ToolSpec("generate_postmortem", "Classify a closed trade's drivers.",
             _obj({"campaign_id": _S, "trade_intent_id": _S},
                  ["campaign_id", "trade_intent_id"]), "generate_postmortem"),
    ToolSpec("create_skill_candidate", "Export a reusable skill candidate.",
             _obj({"name": _S, "description": _S, "steps": {"type": "array", "items": _S},
                   "source_refs": {"type": "array", "items": _S}},
                  ["name", "description", "steps"]), "create_skill_candidate"),
    # ---- Audit ----------------------------------------------------------- #
    ToolSpec("get_audit_events", "Recent audit events.",
             _obj({"campaign_id": _S, "limit": _I, "event_type": _S}), "get_audit_events"),
    ToolSpec("export_campaign_audit", "Redacted, verifiable audit bundle.",
             _obj({"campaign_id": _S}), "export_campaign_audit"),
    ToolSpec("replay_decision", "Replay/reproduce a risk decision from stored data.",
             _obj({"risk_decision_id": _S}, ["risk_decision_id"]), "replay_decision"),
]

TOOLS_BY_NAME = {t.name: t for t in TOOL_SPECS}
