"""Command-line entrypoints.

  hermes-pm-mcp        run the MCP stdio server (connect an MCP-capable agent)
  hermes-pm-dashboard  run the local dashboard
  hermes-pm-demo       run a scripted paper campaign, then serve the dashboard
"""

from __future__ import annotations

import argparse
import asyncio

from hermes_pm.config import load_settings
from hermes_pm.daemon.core import TradingDaemon


def run_mcp() -> None:
    from hermes_pm.mcp.server import run_stdio

    asyncio.run(run_stdio(load_settings()))


def run_mcp_http() -> None:
    from hermes_pm.mcp.http_server import run_http

    run_http(load_settings(mcp_http_enabled=True))


def run_dashboard() -> None:
    from hermes_pm.dashboard.server import run_dashboard as _run

    _run(load_settings())


async def _scripted_campaign(d: TradingDaemon) -> str:
    """Drive a small but complete paper campaign so the dashboard has data."""
    await d.start()
    await asyncio.sleep(0.4)
    camp = d.start_paper_campaign(
        campaign_name="demo-48h", duration_hours=48, paper_bankroll_usd=1000,
        market_filters={"categories": ["weather", "sports"]},
    )
    cid = camp["campaign_id"]
    for mid in camp["watchlist"][:3]:
        await d.gather_evidence(mid)
        await d.gather_evidence(mid, counter=True)
        market = d.get_market_details(mid)
        tok = market["token_ids"]["YES"]
        snap = d.get_market_snapshot(tok)
        if not snap.get("best_ask"):
            continue
        evidence = d.get_source_evidence(mid)
        refs = [e["source_ref"] for e in evidence if e["source_type"] in ("primary", "secondary")][:2]
        intent = d.propose_trade_intent(
            campaign_id=cid, market_id=mid, outcome="YES", side="BUY",
            limit_price=round(snap["best_ask"] + 0.02, 2), max_size_usd=10,
            thesis="model estimate exceeds market-implied probability",
            counter_thesis="market may price private information we lack",
            invalidation_criteria="resolves before campaign end or thesis source retracts",
            evidence_refs=refs, confidence=0.62, expires_at="2026-12-30T00:00:00Z",
        )
        if intent["status"] == "rejected_schema":
            continue
        rc = d.risk_check_trade_intent(intent["trade_intent_id"])
        if rc["decision"] in ("approve", "modify"):
            d.paper_place_order(intent["trade_intent_id"], rc["risk_decision_id"])
            d.generate_postmortem(cid, intent["trade_intent_id"])
    d.write_lesson(cid, trigger="thin order book on a candidate market",
                   observation="fills were partial and slippage rose",
                   rule="require >=200 USD depth on the taking side before sizing up",
                   memory_target="session", supporting_evidence_count=1)
    await d.get_promotion_report(cid)
    return cid


def run_demo() -> None:
    parser = argparse.ArgumentParser(description="Hermes-PM demo: scripted campaign + dashboard")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--no-serve", action="store_true", help="run campaign then exit")
    args = parser.parse_args()
    overrides = {"data_dir": "./.hermes_pm_demo"}
    if args.port:
        overrides["dashboard_port"] = args.port
    settings = load_settings(**overrides)

    async def main() -> None:
        daemon = TradingDaemon(settings)
        cid = await _scripted_campaign(daemon)
        url = daemon.get_dashboard_url(cid)
        if args.no_serve:
            report = await daemon.get_promotion_report(cid)
            print(f"campaign {cid} done; verdicts={report['verdicts']}")
            await daemon.stop()
            return
        print(f"\n  Hermes-PM demo ready.\n  Dashboard: {url}\n  (Ctrl+C to stop)\n")
        from hermes_pm.dashboard.server import _serve

        try:
            await _serve(daemon)
        finally:
            await daemon.stop()

    asyncio.run(main())


if __name__ == "__main__":
    run_demo()
