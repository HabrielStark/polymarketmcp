"""MCP resources (Section 10.3). Application-driven, URI-addressed state."""

from __future__ import annotations

from typing import Any

from hermes_pm.daemon.core import TradingDaemon

RESOURCE_TEMPLATES = [
    ("system://status", "System health, connectivity, mode, locks."),
    ("campaign://{campaign_id}/summary", "Campaign state, P&L, drawdown, promotion readiness."),
    ("market://{market_id}", "Normalized market metadata, rules, source status."),
    ("orderbook://{token_id}", "Best bid/ask, depth, spread, staleness flags."),
    ("portfolio://paper/{campaign_id}", "Paper positions, orders, fills, P&L."),
    ("risk://limits/{campaign_id}", "Active risk policy + remaining capacity."),
    ("signals://{market_id}/social", "Sanitized social signal summary."),
    ("lessons://campaign/{campaign_id}", "Lessons + skill candidates."),
    ("audit://event/{event_id}", "Immutable audit event."),
]


def resolve_resource(d: TradingDaemon, uri: str) -> dict[str, Any]:
    """Resolve a resource URI to a JSON-serializable payload."""
    scheme, _, rest = uri.partition("://")
    parts = rest.split("/")
    if scheme == "system":
        return d.get_system_status()
    if scheme == "campaign" and len(parts) >= 2 and parts[1] == "summary":
        return d.get_campaign_report(parts[0])
    if scheme == "market":
        return d.get_market_details(parts[0])
    if scheme == "orderbook":
        return d.get_order_book(parts[0])
    if scheme == "portfolio" and len(parts) >= 2 and parts[0] == "paper":
        return d.paper_get_portfolio(parts[1])
    if scheme == "risk" and len(parts) >= 2 and parts[0] == "limits":
        cid = parts[1]
        policy = d.campaigns.policy_for(cid)
        return {"campaign_id": cid, "policy": policy.model_dump(),
                "portfolio": d.paper_get_portfolio(cid)}
    if scheme == "signals" and len(parts) >= 2 and parts[1] == "social":
        return d.get_social_signal_summary(parts[0])
    if scheme == "lessons" and len(parts) >= 2 and parts[0] == "campaign":
        return {"campaign_id": parts[1], "lessons": d.list_lessons(parts[1])}
    if scheme == "audit" and len(parts) >= 2 and parts[0] == "event":
        ev = d.audit.get(parts[1])
        return ev.model_dump(mode="json") if ev else {"error": "event not found"}
    return {"error": f"unknown or malformed resource uri: {uri}"}
