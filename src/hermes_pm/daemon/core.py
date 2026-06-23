"""Core Trading Daemon — the Fast-Lane orchestrator and single facade used by
both the MCP server and the dashboard.

It owns the cache, audit, persistence, market data, risk, paper, and live
engines; wires market-data snapshots into the paper engine (so resting orders
fill and positions mark-to-market); enforces operating modes and the emergency
stop (Section 8, AC-007); and exposes every tool operation from Section 10.2 as
a deterministic method returning JSON-serializable data."""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

from hermes_pm.audit.store import AuditStore
from hermes_pm.campaign.evaluation import CampaignEvaluator
from hermes_pm.campaign.manager import CampaignManager
from hermes_pm.campaign.promotion import build_promotion_report
from hermes_pm.config import Settings
from hermes_pm.data.cache import OrderBookCache
from hermes_pm.data.discovery import DiscoveryEngine
from hermes_pm.data.market_data import MarketDataEngine
from hermes_pm.data.polymarket_client import PolymarketSource
from hermes_pm.data.sources import ReplaySource, SyntheticSource
from hermes_pm.errors import EmergencyStopError, NotFoundError, StateError, ValidationError
from hermes_pm.events import EventBus, EventType
from hermes_pm.execution.intents import IntentService
from hermes_pm.execution.live_adapter import LiveAdapter
from hermes_pm.execution.paper_engine import PaperEngine
from hermes_pm.learning.hermes_bridge import HermesBridge
from hermes_pm.learning.lessons import LessonService
from hermes_pm.learning.postmortem import PostmortemEngine
from hermes_pm.metrics.registry import Metrics
from hermes_pm.models import (
    CampaignStatus,
    MemoryTarget,
    Mode,
    OrderType,
    Side,
)
from hermes_pm.persistence.db import Database
from hermes_pm.persistence.redact import redact
from hermes_pm.risk.engine import RiskContext, RiskEngine
from hermes_pm.signals.registry import SignalRegistry
from hermes_pm.util.timeutil import now_ms


def make_source(settings: Settings):
    if settings.market_data_source == "live":
        return PolymarketSource(settings)
    if settings.market_data_source == "replay":
        if not settings.replay_file:
            raise ValidationError("replay_file required for replay source", code="config_error")
        return ReplaySource(settings.replay_file)
    return SyntheticSource(settings.synthetic_seed, settings.synthetic_market_count)


class TradingDaemon:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        settings.ensure_dirs()
        self.db = Database(settings.db_path)
        self.bus = EventBus()
        self.audit = AuditStore(self.db)
        self.cache = OrderBookCache(settings.ws_reconnect_stale_ms)
        self.metrics = Metrics()
        self.source = make_source(settings)
        self.market_data = MarketDataEngine(settings, self.source, self.cache, self.db, self.bus)
        self.signals = SignalRegistry(settings, self.db, self.bus, self.metrics)
        self.intents = IntentService(self.db, settings.default_risk_policy)
        self.risk = RiskEngine()
        self.paper = PaperEngine(self.db, self.cache, self.bus, self.audit, settings.default_risk_policy)
        self.campaigns = CampaignManager(settings, self.db, self.bus, self.audit, self.paper)
        self.evaluator = CampaignEvaluator(self.db)
        self.postmortem = PostmortemEngine()
        self.lessons = LessonService(self.db, self.bus)
        self.hermes = HermesBridge(settings.data_dir)
        self.live = LiveAdapter(
            settings,
            self.audit,
            self.db.get_risk_decision,
            self._geoblock_check,
            load_vault=not settings.live_process_isolation,
            process_isolated=settings.live_process_isolation,
        )
        self._emergency = bool(self.db.kv_get("emergency_stop", False))
        self._started = False
        self._bg: list[asyncio.Task] = []
        self._live_client = None  # set lazily when live_process_isolation is on

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        if self._started:
            return
        await self.market_data.start()
        # Wire market-data snapshots into the paper engine (resting fills + MTM).
        self._bg.append(asyncio.create_task(self._consume_market_data(), name="paper-md"))
        self._started = True

    async def _consume_market_data(self) -> None:
        async for event in self.bus.stream():
            if event.type == EventType.CONNECTIVITY:
                if event.data.get("status") == "throttled":
                    self.metrics.api_throttles.inc()
                continue
            if event.type != EventType.MARKET_DATA:
                continue
            tid = event.data.get("token_id")
            if not tid or event.data.get("reconcile_gap"):
                continue
            snap = self.cache.get(tid)
            if snap is not None:
                with contextlib.suppress(Exception):
                    self.paper.on_book_update(snap)
            self.metrics.market_data_lag_ms.set(self.cache.age_ms(tid))
            self.metrics.stale_tokens.set(len(self.cache.sweep_stale()))
            self.metrics.ws_reconnects.set(self.market_data.reconnects)

    async def stop(self) -> None:
        for t in self._bg:
            t.cancel()
        for t in self._bg:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        if self._live_client is not None:
            with contextlib.suppress(Exception):
                await self._live_client.stop()
        await self.market_data.stop()
        self._started = False

    async def _geoblock_check(self) -> dict:
        if isinstance(self.source, PolymarketSource):
            return await self.source.geoblock_check()
        return {"blocked": True, "reason": "no live data source; cannot verify region"}

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #
    def _guard_new_actions(self) -> None:
        if self._emergency:
            raise EmergencyStopError("emergency stop active; new actions blocked until reset")

    def _require_campaign(self, campaign_id: str):
        c = self.db.get_campaign(campaign_id)
        if c is None:
            raise NotFoundError(f"campaign not found: {campaign_id}")
        return c

    def _audit_tool(self, tool: str, inputs: Any, outputs: Any, latency_ms: float,
                    campaign_id: str | None = None, actor: str = "agent") -> None:
        status = "ok" if not (isinstance(outputs, dict) and outputs.get("error")) else "error"
        self.metrics.tool_calls.labels(tool=tool, status=status).inc()
        self.audit.append(
            f"tool:{tool}", actor=actor, summary=tool, inputs=inputs, outputs=outputs,
            latency_ms=latency_ms, campaign_id=campaign_id,
        )

    def _exposures(self, campaign_id: str, market_id: str, category: str) -> dict[str, float]:
        positions = self.db.list_positions(campaign_id)
        def notional(p) -> float:
            return abs(p.shares) * (p.mark_price if p.mark_price is not None else p.avg_price)
        market_exp = sum(notional(p) for p in positions if p.market_id == market_id)
        cat_exp = 0.0
        for p in positions:
            m = self.db.get_market(p.market_id)
            if m is not None and m.category == category:
                cat_exp += notional(p)
        total = sum(notional(p) for p in positions)
        return {"market": round(market_exp, 6), "category": round(cat_exp, 6),
                "total": round(total, 6)}

    def _realized(self, campaign_id: str) -> tuple[float, float]:
        from hermes_pm.execution.ledger import REALIZED
        rows = self.db.list_ledger(campaign_id)
        cutoff = now_ms() - 86_400_000
        campaign = round(sum(r["credit"] - r["debit"] for r in rows if r["account"] == REALIZED), 6)
        today = round(
            sum(r["credit"] - r["debit"] for r in rows
                if r["account"] == REALIZED and r["created_ms"] >= cutoff),
            6,
        )
        return today, campaign

    def _evidence_for(self, market_id: str, evidence_refs: list[str]):
        refs = set(evidence_refs)
        return [
            s for s in self.db.list_signals(market_id)
            if s.source_ref in refs or s.signal_id in refs
        ]

    def _build_risk_context(self, campaign, intent) -> RiskContext:
        market = self.db.get_market(intent.market_id)
        if market is None:
            raise NotFoundError(f"market not found: {intent.market_id}")
        book = self.cache.get(intent.token_id)
        exp = self._exposures(campaign.campaign_id, intent.market_id, market.category)
        today, camp = self._realized(campaign.campaign_id)
        positions = self.db.list_positions(campaign.campaign_id)
        recent_loss = any(
            p.realized_pnl < 0 for p in positions if p.market_id == intent.market_id
        )
        prior = [o for o in self.db.list_orders(campaign.campaign_id) if o.market_id == intent.market_id]
        last_size = prior[-1].size_usd if prior else None
        return RiskContext(
            intent=intent, market=market, campaign=campaign,
            policy=self.campaigns.policy_for(campaign.campaign_id), book=book,
            book_is_stale=self.cache.is_stale(intent.token_id),
            data_age_ms=self.cache.age_ms(intent.token_id),
            evidence=self._evidence_for(intent.market_id, intent.evidence_refs),
            market_exposure_usd=exp["market"], category_exposure_usd=exp["category"],
            correlated_exposure_usd=exp["total"], total_exposure_usd=exp["total"],
            realized_pnl_today=today, realized_pnl_campaign=camp,
            last_size_on_market_usd=last_size, market_recent_loss=recent_loss,
        )

    def _persist_risk_context(self, decision_id: str, ctx: RiskContext) -> None:
        """Snapshot the exact engine inputs so a decision replays deterministically."""
        self.db.kv_set(f"risk_ctx:{decision_id}", {
            "intent_id": ctx.intent.intent_id,
            "campaign_id": ctx.campaign.campaign_id,
            "market_id": ctx.market.market_id,
            "book_snapshot_id": ctx.book.snapshot_id if ctx.book else None,
            "book_is_stale": ctx.book_is_stale,
            "data_age_ms": ctx.data_age_ms,
            "evidence": [s.model_dump(mode="json") for s in ctx.evidence],
            "market_exposure_usd": ctx.market_exposure_usd,
            "category_exposure_usd": ctx.category_exposure_usd,
            "correlated_exposure_usd": ctx.correlated_exposure_usd,
            "total_exposure_usd": ctx.total_exposure_usd,
            "realized_pnl_today": ctx.realized_pnl_today,
            "realized_pnl_campaign": ctx.realized_pnl_campaign,
            "last_size_on_market_usd": ctx.last_size_on_market_usd,
            "market_recent_loss": ctx.market_recent_loss,
            "eval_ms": ctx.eval_ms,
        })

    def rebuild_risk_context(self, decision_id: str) -> RiskContext | None:
        """Reconstruct the exact RiskContext stored for ``decision_id`` (replay)."""
        from hermes_pm.models import Signal
        snap = self.db.kv_get(f"risk_ctx:{decision_id}")
        if not snap:
            return None
        intent = self.db.get_intent(snap["intent_id"])
        market = self.db.get_market(snap["market_id"])
        campaign = self.db.get_campaign(snap["campaign_id"])
        if intent is None or market is None or campaign is None:
            return None
        book = self.db.get_snapshot(snap["book_snapshot_id"]) if snap["book_snapshot_id"] else None
        return RiskContext(
            intent=intent, market=market, campaign=campaign,
            policy=self.campaigns.policy_for(campaign.campaign_id), book=book,
            book_is_stale=snap["book_is_stale"], data_age_ms=snap["data_age_ms"],
            evidence=[Signal.model_validate(s) for s in snap["evidence"]],
            market_exposure_usd=snap["market_exposure_usd"],
            category_exposure_usd=snap["category_exposure_usd"],
            correlated_exposure_usd=snap["correlated_exposure_usd"],
            total_exposure_usd=snap["total_exposure_usd"],
            realized_pnl_today=snap["realized_pnl_today"],
            realized_pnl_campaign=snap["realized_pnl_campaign"],
            last_size_on_market_usd=snap["last_size_on_market_usd"],
            market_recent_loss=snap["market_recent_loss"], eval_ms=snap["eval_ms"],
        )

    # ================================================================== #
    # SYSTEM tools
    # ================================================================== #
    def get_system_status(self) -> dict[str, Any]:
        active = [c for c in self.db.list_campaigns() if c.status in (CampaignStatus.RUNNING, CampaignStatus.PAUSED)]
        return {
            "mode": "emergency" if self._emergency else "paper",
            "emergency_stop": self._emergency,
            "market_data_source": self.settings.market_data_source,
            "subscribed_tokens": len(self.cache.tokens()),
            "stale_tokens": len(self.cache.sweep_stale()),
            "connectivity_lost": self.cache.connectivity_lost,
            "ws_reconnects": self.market_data.reconnects,
            "active_campaigns": [c.campaign_id for c in active],
            "live_adapter_enabled": self.live.enabled,
            "signing_vault": self.live.vault_status(),
            "audit_head": self.audit.last_hash,
            "default_risk_policy_version": self.settings.default_risk_policy.version,
        }

    def get_config(self) -> dict[str, Any]:
        cfg = redact(self.settings.model_dump(mode="json"))
        cfg["default_risk_policy"] = self.settings.default_risk_policy.model_dump()
        return cfg

    def update_config(self, updates: dict[str, Any]) -> dict[str, Any]:
        # Only a safe allow-list may be changed at runtime; never secrets or the
        # live-enable flag (that requires the compliance process).
        allowed = {"reconcile_interval_ms", "ws_reconnect_stale_ms", "synthetic_market_count"}
        applied = {}
        for k, v in updates.items():
            if k in allowed:
                setattr(self.settings, k, v)
                applied[k] = v
        return {"applied": applied, "ignored": [k for k in updates if k not in allowed]}

    def get_dashboard_url(self, campaign_id: str | None = None) -> str:
        base = f"http://{self.settings.dashboard_host}:{self.settings.dashboard_port}/"
        return f"{base}?campaign={campaign_id}" if campaign_id else base

    def emergency_stop(self, campaign_id: str | None = None) -> dict[str, Any]:
        """Freeze new actions, cancel open paper orders, freeze live (AC-007)."""
        self._emergency = True
        self.db.kv_set("emergency_stop", True)
        cancelled = 0
        targets = [campaign_id] if campaign_id else [c.campaign_id for c in self.db.list_campaigns()]
        for cid in targets:
            c = self.db.get_campaign(cid)
            if c and c.status in (CampaignStatus.RUNNING, CampaignStatus.PAUSED):
                cancelled += self.paper.cancel_all(cid)
                c.status = CampaignStatus.STOPPED
                self.db.save_campaign(c)
        self.live.freeze("emergency_stop")
        ev = self.audit.append(
            EventType.EMERGENCY_STOP, actor="operator",
            summary="emergency stop engaged", inputs={"campaign_id": campaign_id},
            outputs={"cancelled_orders": cancelled}, campaign_id=campaign_id,
        )
        self.bus.publish(EventType.EMERGENCY_STOP, {"campaign_id": campaign_id, "cancelled": cancelled})
        return {"emergency_stop": True, "cancelled_orders": cancelled, "audit_event_id": ev.event_id}

    def reset_emergency(self) -> dict[str, Any]:
        self._emergency = False
        self.db.kv_set("emergency_stop", False)
        self.audit.append("emergency_reset", actor="operator")
        return {"emergency_stop": False}

    # ================================================================== #
    # MARKET DISCOVERY / DATA tools
    # ================================================================== #
    def search_markets(self, filters: dict[str, Any] | None = None, limit: int = 50) -> list[dict]:
        filters = filters or {}
        markets = self.market_data.markets or self.db.list_markets()
        # The live order book is authoritative for liquidity/spread here, so apply
        # those against it below. Volume has no order-book equivalent, so keep the
        # static Gamma min_volume filter in the discovery pass.
        static_filters = {
            k: v
            for k, v in filters.items()
            if k not in ("min_liquidity", "max_spread", "min_liquidity_usd")
        }
        wl = DiscoveryEngine.build_watchlist(markets, {**static_filters, "require_tradable": False})
        min_liq = filters.get("min_liquidity_usd", filters.get("min_liquidity"))
        max_spread = filters.get("max_spread")
        out = []
        for m in wl:
            tradable, reasons = DiscoveryEngine.is_tradable(m)
            yes = m.token_ids.get("YES")
            book = self.cache.get(yes) if yes else None
            spread = book.spread if book else None
            liq = (book.depth_usd(Side.BUY) + book.depth_usd(Side.SELL)) if book else None
            # FR-MD-005 liquidity / spread filters (applied against live data).
            if min_liq is not None and (liq is None or liq < min_liq):
                continue
            if max_spread is not None and (spread is None or spread > max_spread):
                continue
            out.append({**m.model_dump(mode="json"), "tradable": tradable,
                        "tradable_reasons": reasons, "spread": spread, "liquidity_usd": liq})
            if len(out) >= limit:
                break
        return out

    def get_market_details(self, market_id: str) -> dict[str, Any]:
        m = self.db.get_market(market_id)
        if m is None:
            raise NotFoundError(f"market not found: {market_id}")
        tradable, reasons = DiscoveryEngine.is_tradable(m)
        return {**m.model_dump(mode="json"), "tradable": tradable, "tradable_reasons": reasons}

    def get_resolution_rules(self, market_id: str) -> dict[str, Any]:
        m = self.db.get_market(market_id)
        if m is None:
            raise NotFoundError(f"market not found: {market_id}")
        return {
            "market_id": market_id, "resolution_rules": m.resolution_rules,
            "resolution_source": m.resolution_source, "source_links": m.source_links,
            "has_clear_resolution": m.has_clear_resolution,
        }

    def build_watchlist(self, filters: dict[str, Any] | None = None) -> list[str]:
        markets = self.market_data.markets or self.db.list_markets()
        return [m.market_id for m in DiscoveryEngine.build_watchlist(markets, filters or {})]

    def get_market_snapshot(self, token_id: str) -> dict[str, Any]:
        t0 = time.perf_counter()
        book = self.cache.get(token_id)
        out = (
            {"token_id": token_id, "exists": False, "stale": True}
            if book is None
            else {
                "token_id": token_id, "exists": True, "best_bid": book.best_bid,
                "best_ask": book.best_ask, "spread": book.spread, "mid": book.mid,
                "sequence": book.sequence, "received_at": book.received_at,
                "age_ms": self.cache.age_ms(token_id), "stale": self.cache.is_stale(token_id),
                "snapshot_id": book.snapshot_id,
            }
        )
        self.metrics.lat_snapshot.observe((time.perf_counter() - t0) * 1000)
        return out

    def get_order_book(self, token_id: str) -> dict[str, Any]:
        book = self.cache.get(token_id)
        if book is None:
            return {"token_id": token_id, "exists": False}
        return {**book.model_dump(mode="json"), "stale": self.cache.is_stale(token_id),
                "age_ms": self.cache.age_ms(token_id)}

    async def subscribe_markets(self, market_ids: list[str]) -> dict[str, Any]:
        tokens = []
        for mid in market_ids:
            m = self.db.get_market(mid)
            if m:
                tokens.extend(m.token_ids.values())
        await self.market_data.subscribe(tokens)
        return {"subscribed_markets": market_ids, "subscribed_tokens": tokens}

    def get_price_history(self, token_id: str, limit: int = 200) -> list[dict]:
        snaps = self.db.list_snapshots(token_id)[-limit:]
        return [
            {"sequence": s.sequence, "received_at": s.received_at, "mid": s.mid,
             "best_bid": s.best_bid, "best_ask": s.best_ask, "last_trade": s.last_trade}
            for s in snaps
        ]

    def get_liquidity_summary(self, token_id: str) -> dict[str, Any]:
        book = self.cache.get(token_id)
        if book is None:
            return {"token_id": token_id, "exists": False}
        return {
            "token_id": token_id, "spread": book.spread,
            "bid_depth_usd": book.depth_usd(Side.SELL), "ask_depth_usd": book.depth_usd(Side.BUY),
            "levels_bid": len(book.bids), "levels_ask": len(book.asks),
            "stale": self.cache.is_stale(token_id),
        }

    # ================================================================== #
    # SIGNALS tools
    # ================================================================== #
    async def gather_evidence(self, market_id: str, allowed: list[str] | None = None,
                              counter: bool = False) -> dict[str, Any]:
        m = self.db.get_market(market_id)
        if m is None:
            raise NotFoundError(f"market not found: {market_id}")
        sigs = await self.signals.gather(m, allowed, counter=counter)
        return {"market_id": market_id, "count": len(sigs),
                "signal_refs": [s.source_ref for s in sigs],
                "suspected_injection": sum(1 for s in sigs if s.suspected_injection)}

    def get_social_signal_summary(self, market_id: str) -> dict[str, Any]:
        return self.signals.summary(market_id)

    def get_source_evidence(self, market_id: str) -> list[dict]:
        return [s.model_dump(mode="json") for s in self.db.list_signals(market_id)]

    def purge_old_signals(self, retention_hours: float = 168.0) -> dict[str, Any]:
        """Enforce social/external data retention (NFR-PRIV-003, COMP-006)."""
        before = now_ms() - int(retention_hours * 3_600_000)
        if retention_hours <= 0:
            # Retain nothing: include signals created in the same millisecond as
            # the purge request, avoiding a race in fast local/coverage runs.
            before += 1
        removed = self.db.purge_signals_before(before)
        self.audit.append("signals_purged", actor="system",
                          outputs={"removed": removed, "retention_hours": retention_hours})
        return {"removed": removed, "retention_hours": retention_hours}

    async def get_weather_signal_summary(self, market_id: str) -> dict[str, Any]:
        m = self._require_market(market_id)
        sigs = await self.signals.adapters["weather"].fetch(m)
        for s in sigs:
            self.db.save_signal(s)
        return {"market_id": market_id, "signals": [s.model_dump(mode="json") for s in sigs]}

    async def get_sports_signal_summary(self, market_id: str) -> dict[str, Any]:
        m = self._require_market(market_id)
        sigs = await self.signals.adapters["sports"].fetch(m)
        for s in sigs:
            self.db.save_signal(s)
        return {"market_id": market_id, "signals": [s.model_dump(mode="json") for s in sigs]}

    def _require_market(self, market_id: str):
        m = self.db.get_market(market_id)
        if m is None:
            raise NotFoundError(f"market not found: {market_id}")
        return m

    # ================================================================== #
    # CAMPAIGN tools
    # ================================================================== #
    def start_paper_campaign(
        self, *, campaign_name: str, duration_hours: float, paper_bankroll_usd: float,
        market_filters: dict[str, Any] | None = None, risk_profile: dict[str, Any] | None = None,
        allowed_signal_sources: list[str] | None = None,
    ) -> dict[str, Any]:
        self._guard_new_actions()
        t0 = time.perf_counter()
        try:
            campaign = self.campaigns.create(
                name=campaign_name, duration_hours=duration_hours,
                paper_bankroll_usd=paper_bankroll_usd, market_filters=market_filters,
                risk_profile=risk_profile, allowed_signal_sources=allowed_signal_sources,
                mode=Mode.PAPER,
            )
        except ValidationError as exc:
            return {"status": "rejected", "error": exc.to_dict()}
        markets = self.market_data.markets or self.db.list_markets()
        watchlist = [m.market_id for m in DiscoveryEngine.build_watchlist(markets, market_filters or {})]
        campaign.watchlist = watchlist
        campaign.dashboard_url = self.get_dashboard_url(campaign.campaign_id)
        self.db.save_campaign(campaign)
        out = {
            "campaign_id": campaign.campaign_id, "dashboard_url": campaign.dashboard_url,
            "active_limits": self.campaigns.policy_for(campaign.campaign_id).model_dump(),
            "watchlist": watchlist, "status": "running",
        }
        self._audit_tool("start_paper_campaign", {"name": campaign_name},
                         {"campaign_id": campaign.campaign_id}, (time.perf_counter() - t0) * 1000,
                         campaign.campaign_id, actor="operator")
        return out

    def pause_campaign(self, campaign_id: str) -> dict[str, Any]:
        return {"status": self.campaigns.pause(campaign_id).status.value}

    def resume_campaign(self, campaign_id: str) -> dict[str, Any]:
        self._guard_new_actions()
        return {"status": self.campaigns.resume(campaign_id).status.value}

    def stop_campaign(self, campaign_id: str) -> dict[str, Any]:
        return {"status": self.campaigns.stop(campaign_id).status.value}

    def get_campaign_report(self, campaign_id: str) -> dict[str, Any]:
        campaign = self._require_campaign(campaign_id)
        portfolio = self.paper.portfolio(campaign_id, campaign.bankroll)
        metrics = self.evaluator.evaluate(campaign, portfolio)
        return {"campaign": campaign.model_dump(mode="json"), "portfolio": portfolio,
                "metrics": metrics}

    def get_trade_detail(self, campaign_id: str, trade_intent_id: str) -> dict[str, Any]:
        """The 'why did this happen?' bundle for one trade (FR-DASH-004): thesis,
        counter-thesis, evidence, risk decision, order-book snapshot, fills, and
        outcome — every element linked to its audit event."""
        intent = self.db.get_intent(trade_intent_id)
        if intent is None:
            raise NotFoundError(f"intent not found: {trade_intent_id}")
        market = self.db.get_market(intent.market_id)
        decisions = [d for d in self.db.list_risk_decisions(campaign_id)
                     if d.intent_id == trade_intent_id]
        orders = [o for o in self.db.list_orders(campaign_id) if o.intent_id == trade_intent_id]
        fills = [f for o in orders for f in o.fills]
        snapshot = self.db.get_snapshot(fills[0].snapshot_id) if fills else None
        position = self.db.get_position(campaign_id, intent.token_id)
        evidence = self._evidence_for(intent.market_id, intent.evidence_refs)
        return {
            "intent": intent.model_dump(mode="json"),
            "thesis": intent.thesis,
            "counter_thesis": intent.counter_thesis,
            "invalidation_criteria": intent.invalidation_criteria,
            "resolution_rules": market.resolution_rules if market else "",
            "evidence": [s.model_dump(mode="json") for s in evidence],
            "risk_decisions": [d.model_dump(mode="json") for d in decisions],
            "orders": [o.model_dump(mode="json") for o in orders],
            "fills": [f.model_dump(mode="json") for f in fills],
            "entry_order_book": snapshot.model_dump(mode="json") if snapshot else None,
            "position": position.model_dump(mode="json") if position else None,
        }

    async def get_promotion_report(self, campaign_id: str) -> dict[str, Any]:
        campaign = self._require_campaign(campaign_id)
        portfolio = self.paper.portfolio(campaign_id, campaign.bankroll)
        metrics = self.evaluator.evaluate(campaign, portfolio)
        gates = await self.live._gate.evaluate(None, None, self.live._vault)  # noqa: SLF001
        gates["live_enabled"] = self.live.enabled
        operational = {
            "data_outages": 1 if self.cache.connectivity_lost else 0,
            "ws_reconnects": self.market_data.reconnects,
            "fill_sim_errors": 0,
        }
        report = build_promotion_report(
            campaign, metrics, compliance_state=gates, operational=operational,
            lessons_count=len(self.db.list_lessons(campaign_id)),
            audit_chain_ok=self.audit.verify_chain(campaign_id)["ok"],
        )
        self.audit.append("promotion_report", actor="agent", outputs=report["verdicts"],
                          campaign_id=campaign_id)
        return report

    # ================================================================== #
    # TRADING INTENT tools
    # ================================================================== #
    def propose_trade_intent(
        self, *, campaign_id: str, market_id: str, outcome: str, side: str,
        limit_price: float, max_size_usd: float, thesis: str, evidence_refs: list[str] | None = None,
        confidence: float = 0.5, expires_at: str, counter_thesis: str = "",
        invalidation_criteria: str = "", order_type: str = "marketable_limit",
        prompt_version: str = "",
    ) -> dict[str, Any]:
        self._guard_new_actions()
        campaign = self._require_campaign(campaign_id)
        market = self._require_market(market_id)
        try:
            intent = self.intents.create(
                campaign, market, outcome=outcome, side=Side(side), limit_price=limit_price,
                max_size_usd=max_size_usd, thesis=thesis, expires_at=expires_at,
                confidence=confidence, counter_thesis=counter_thesis,
                invalidation_criteria=invalidation_criteria, evidence_refs=evidence_refs or [],
                order_type=OrderType(order_type), prompt_version=prompt_version,
                policy=self.campaigns.policy_for(campaign_id),
            )
        except (ValidationError, NotFoundError) as exc:
            return {"status": "rejected_schema", "error": exc.to_dict()}
        self.bus.publish(EventType.INTENT_CREATED,
                         {"intent_id": intent.intent_id, "market_id": market_id, "status": intent.status})
        self.audit.append("intent_created", actor="agent", outputs=intent.model_dump(mode="json"),
                          campaign_id=campaign_id, references={"intent_id": intent.intent_id})
        return {
            "trade_intent_id": intent.intent_id, "normalized_ev": intent.normalized_ev,
            "break_even_probability": intent.break_even_probability,
            "missing_fields": intent.missing_fields, "status": intent.status,
            "similar_past_intents": self.intents.similar_past_intents(
                campaign_id, market_id, intent.intent_id
            ),
        }

    def simulate_trade_intent(self, trade_intent_id: str) -> dict[str, Any]:
        intent = self.db.get_intent(trade_intent_id)
        if intent is None:
            raise NotFoundError(f"intent not found: {trade_intent_id}")
        book = self.cache.get(intent.token_id)
        sim = self.paper.simulate_fill(intent.side, intent.limit_price, intent.max_size_usd, book)
        return {"trade_intent_id": trade_intent_id, "simulated": sim,
                "break_even_probability": intent.break_even_probability,
                "normalized_ev": intent.normalized_ev}

    def risk_check_trade_intent(self, trade_intent_id: str) -> dict[str, Any]:
        t0 = time.perf_counter()
        intent = self.db.get_intent(trade_intent_id)
        if intent is None:
            raise NotFoundError(f"intent not found: {trade_intent_id}")
        campaign = self._require_campaign(intent.campaign_id)
        ctx = self._build_risk_context(campaign, intent)
        decision = self.risk.evaluate(ctx)
        decision = self.db.save_risk_decision(decision)  # idempotent: returns persisted record
        self._persist_risk_context(decision.decision_id, ctx)
        self.metrics.risk_decisions.labels(result=decision.result.value).inc()
        self.metrics.lat_risk.observe((time.perf_counter() - t0) * 1000)
        self.bus.publish(EventType.RISK_DECISION, {
            "decision_id": decision.decision_id, "intent_id": intent.intent_id,
            "result": decision.result.value, "reasons": decision.reasons,
            "violated_rules": decision.violated_rules,
        })
        self.audit.append("risk_decision", actor="risk_engine",
                          inputs={"intent_id": intent.intent_id},
                          outputs=decision.model_dump(mode="json"), campaign_id=campaign.campaign_id,
                          references={"decision_id": decision.decision_id})
        return {
            "risk_decision_id": decision.decision_id, "decision": decision.result.value,
            "reasons": decision.reasons, "violated_rules": decision.violated_rules,
            "approved_price": decision.approved_limit_price,
            "approved_max_size_usd": decision.approved_size_usd,
            "required_confirmations": decision.required_user_confirmations,
            "policy_version": decision.policy_version,
            "exposure_after_trade": decision.exposure_after_trade,
        }

    def explain_risk_rejection(self, risk_decision_id: str) -> dict[str, Any]:
        d = self.db.get_risk_decision(risk_decision_id)
        if d is None:
            raise NotFoundError(f"risk decision not found: {risk_decision_id}")
        return {"decision_id": d.decision_id, "result": d.result.value,
                "violated_rules": d.violated_rules, "reasons": d.reasons,
                "policy_version": d.policy_version, "data_freshness_ms": d.data_freshness_ms}

    # ================================================================== #
    # PAPER EXECUTION tools
    # ================================================================== #
    def paper_place_order(self, trade_intent_id: str, risk_decision_id: str) -> dict[str, Any]:
        self._guard_new_actions()
        t0 = time.perf_counter()
        intent = self.db.get_intent(trade_intent_id)
        decision = self.db.get_risk_decision(risk_decision_id)
        if intent is None or decision is None:
            raise NotFoundError("intent or risk decision not found")
        if decision.intent_id != intent.intent_id:
            raise ValidationError("risk decision does not match intent", code="validation_error")
        campaign = self._require_campaign(intent.campaign_id)
        if campaign.status is not CampaignStatus.RUNNING:
            raise StateError(f"campaign not running: {campaign.status.value}", code="state_error")
        try:
            order = self.paper.place_order(campaign, intent, decision)
        except ValidationError as exc:
            self.metrics.fill_sim_errors.inc()
            return {"status": "rejected", "error": exc.to_dict()}
        self.metrics.fills.inc(len(order.fills))
        self.metrics.lat_order.observe((time.perf_counter() - t0) * 1000)
        out = {
            "paper_order_id": order.order_id, "status": order.status.value,
            "simulated_fills": [f.model_dump(mode="json") for f in order.fills],
            "portfolio_delta": self.paper.portfolio(campaign.campaign_id, campaign.bankroll),
        }
        self._audit_tool("paper_place_order", {"intent_id": trade_intent_id},
                         {"order_id": order.order_id, "status": order.status.value},
                         (time.perf_counter() - t0) * 1000, campaign.campaign_id)
        return out

    def paper_cancel_order(self, paper_order_id: str) -> dict[str, Any]:
        order = self.paper.cancel_order(paper_order_id)
        return {"paper_order_id": order.order_id, "status": order.status.value}

    def paper_get_orders(self, campaign_id: str) -> list[dict]:
        return [o.model_dump(mode="json") for o in self.db.list_orders(campaign_id)]

    def paper_get_portfolio(self, campaign_id: str) -> dict[str, Any]:
        campaign = self._require_campaign(campaign_id)
        return self.paper.portfolio(campaign_id, campaign.bankroll)

    def paper_mark_to_market(self, campaign_id: str) -> dict[str, Any]:
        self.paper.mark_to_market(campaign_id)
        campaign = self._require_campaign(campaign_id)
        return self.paper.portfolio(campaign_id, campaign.bankroll)

    # ================================================================== #
    # LIVE EXECUTION tools (locked — reference-only)
    # ================================================================== #
    async def _live_iface(self):
        """Return the live interface: the isolated subprocess client when
        ``live_process_isolation`` is on (keys never enter this process), else the
        in-process locked adapter."""
        if self.settings.live_process_isolation:
            if self._live_client is None:
                from hermes_pm.execution.live_process import LiveProcessClient
                self._live_client = LiveProcessClient(self.settings)
                await self._live_client.start()
            return self._live_client
        return self.live

    async def live_place_order_intent(
        self, trade_intent_id: str, risk_decision_id: str,
        user_confirmation_token: str | None = None,
    ) -> dict[str, Any]:
        iface = await self._live_iface()
        result = await iface.place_order_intent(
            trade_intent_id, risk_decision_id, user_confirmation_token
        )
        if self.settings.live_process_isolation:
            # The isolated process never writes the shared chain; audit here.
            self.audit.append("live_order_blocked", actor="live_adapter",
                              summary="isolated live placement", outputs=result)
        return result

    async def live_cancel_order(self, order_ref: str) -> dict[str, Any]:
        iface = await self._live_iface()
        return await iface.cancel_order(order_ref)

    async def live_get_open_orders(self) -> list[dict]:
        iface = await self._live_iface()
        return await iface.get_open_orders()

    # ================================================================== #
    # LEARNING tools
    # ================================================================== #
    def write_lesson(self, campaign_id: str, *, trigger: str, observation: str, rule: str,
                     pattern: str = "", confidence: float = 0.5, valid_until: str | None = None,
                     source_refs: list[str] | None = None, memory_target: str = "session",
                     supporting_evidence_count: int = 1, human_confirmed: bool = False) -> dict:
        lesson = self.lessons.create(
            campaign_id, trigger=trigger, observation=observation, rule=rule, pattern=pattern,
            confidence=confidence, valid_until=valid_until, source_refs=source_refs,
            memory_target=MemoryTarget(memory_target),
            supporting_evidence_count=supporting_evidence_count, human_confirmed=human_confirmed,
        )
        self.audit.append("lesson_written", actor="agent", outputs=lesson.model_dump(mode="json"),
                          campaign_id=campaign_id)
        return lesson.model_dump(mode="json")

    def list_lessons(self, campaign_id: str | None = None) -> list[dict]:
        return [lesson.model_dump(mode="json") for lesson in self.lessons.list(campaign_id)]

    def search_past_decisions(self, query: str, campaign_id: str | None = None) -> list[dict]:
        q = query.lower()
        campaigns = [campaign_id] if campaign_id else [c.campaign_id for c in self.db.list_campaigns()]
        out = []
        for cid in campaigns:
            for t in self.db.list_intents(cid):
                if q in t.thesis.lower() or q in t.market_id.lower() or q in t.counter_thesis.lower():
                    out.append({"intent_id": t.intent_id, "market_id": t.market_id,
                                "thesis": t.thesis, "status": t.status, "campaign_id": cid})
        return out

    def generate_postmortem(self, campaign_id: str, trade_intent_id: str) -> dict[str, Any]:
        intent = self.db.get_intent(trade_intent_id)
        if intent is None:
            raise NotFoundError(f"intent not found: {trade_intent_id}")
        orders = [o for o in self.db.list_orders(campaign_id) if o.intent_id == trade_intent_id]
        order = orders[-1] if orders else None
        fills = self.db.list_fills(order.order_id) if order else []
        position = self.db.get_position(campaign_id, intent.token_id)
        signals = self._evidence_for(intent.market_id, intent.evidence_refs)
        if order is None or position is None:
            pm = {"intent_id": trade_intent_id, "outcome": "no_fill_or_position",
                  "failure_mode": "n/a", "drivers": ["no order/position to analyze"]}
        else:
            pm = self.postmortem.analyze_position(
                campaign_id, intent, order, fills, position, signals,
                entry_was_stale=False,
            )
        self.bus.publish(EventType.POSTMORTEM, {"intent_id": trade_intent_id, "outcome": pm["outcome"]})
        self.audit.append("postmortem", actor="agent", outputs=pm, campaign_id=campaign_id)
        return pm

    def create_skill_candidate(self, name: str, description: str, steps: list[str],
                               source_refs: list[str] | None = None) -> dict[str, Any]:
        path = self.hermes.export_skill_candidate(name, description, steps, source_refs or [])
        return {"skill_candidate": name, "path": str(path)}

    def export_active_memory(self, campaign_id: str | None = None) -> dict[str, Any]:
        path = self.hermes.export_active_memory(self.db.list_lessons(campaign_id))
        return {"path": str(path)}

    # ================================================================== #
    # AUDIT tools
    # ================================================================== #
    def get_audit_events(self, campaign_id: str | None = None, limit: int = 100,
                         event_type: str | None = None) -> list[dict]:
        return [e.model_dump(mode="json")
                for e in self.audit.list_events(campaign_id, limit, event_type)]

    def export_campaign_audit(self, campaign_id: str | None = None) -> dict[str, Any]:
        return self.audit.export(campaign_id)

    def replay_decision(self, risk_decision_id: str) -> dict[str, Any]:
        from hermes_pm.replay.engine import ReplayEngine
        return ReplayEngine(self).replay_decision(risk_decision_id)
