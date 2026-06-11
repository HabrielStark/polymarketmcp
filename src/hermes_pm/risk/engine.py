"""Deterministic risk engine (FR-RISK-001..007, Section 14).

``RiskEngine.evaluate`` is a pure function of its :class:`RiskContext`: no
randomness, no I/O, no LLM. The same inputs always yield the same
:class:`RiskDecision`, and every rejection/modification carries machine-readable
``reasons`` and ``violated_rules`` (FR-RISK-005). The exact policy ``version`` is
recorded on every decision (FR-RISK-007)."""

from __future__ import annotations

from dataclasses import dataclass, field

from hermes_pm.config import RiskPolicy
from hermes_pm.execution.economics import break_even_probability, normalized_ev
from hermes_pm.models import (
    Campaign,
    Market,
    Mode,
    OrderBookSnapshot,
    RiskDecision,
    RiskResult,
    Signal,
    SourceType,
    TradeIntent,
)
from hermes_pm.util.ids import idempotency_key
from hermes_pm.util.timeutil import iso_to_ms, now_ms


@dataclass
class RiskContext:
    """All inputs the engine needs, precomputed by the daemon (keeps the engine
    pure and trivially testable)."""

    intent: TradeIntent
    market: Market
    campaign: Campaign
    policy: RiskPolicy
    book: OrderBookSnapshot | None = None
    book_is_stale: bool = True
    data_age_ms: int = 2**31
    evidence: list[Signal] = field(default_factory=list)
    market_exposure_usd: float = 0.0
    category_exposure_usd: float = 0.0
    correlated_exposure_usd: float = 0.0
    total_exposure_usd: float = 0.0
    realized_pnl_today: float = 0.0
    realized_pnl_campaign: float = 0.0
    last_size_on_market_usd: float | None = None
    market_recent_loss: bool = False
    eval_ms: int = field(default_factory=now_ms)


class RiskEngine:
    def evaluate(self, ctx: RiskContext) -> RiskDecision:
        p = ctx.policy
        bankroll = max(1e-9, ctx.campaign.bankroll)
        reasons: list[str] = []
        violations: list[str] = []
        intent = ctx.intent
        side = intent.side
        requested = intent.max_size_usd

        # --- Hard gates (reject) ------------------------------------------- #
        if not ctx.market.has_clear_resolution:
            violations.append("ambiguous_or_missing_resolution_rules")  # FR-RISK-004/FR-MD-004

        if ctx.book_is_stale or ctx.data_age_ms > p.max_data_staleness_ms:
            violations.append("stale_market_data")  # FR-RISK-004 / FR-DATA-004
            reasons.append(f"data_age_ms={ctx.data_age_ms} > {p.max_data_staleness_ms}")

        spread = ctx.book.spread if ctx.book else None
        if ctx.book is None or spread is None:
            violations.append("no_two_sided_market")
        elif spread > p.max_spread:
            violations.append("spread_too_wide")
            reasons.append(f"spread={spread} > max_spread={p.max_spread}")

        depth = ctx.book.depth_usd(side) if ctx.book else 0.0
        if depth < p.min_orderbook_depth_usd:
            violations.append("insufficient_orderbook_depth")  # FR-RISK-002
            reasons.append(f"depth_usd={depth} < min={p.min_orderbook_depth_usd}")

        # Evidence quality (FR-RISK-004, 14.1 minimum evidence count).
        primary = sum(1 for s in ctx.evidence if s.source_type is SourceType.PRIMARY)
        secondary = sum(1 for s in ctx.evidence if s.source_type is SourceType.SECONDARY)
        if not (primary >= p.min_primary_sources or secondary >= p.min_secondary_sources):
            violations.append("insufficient_evidence")
            reasons.append(
                f"need >={p.min_primary_sources} primary or >={p.min_secondary_sources} secondary; "
                f"have primary={primary} secondary={secondary}"
            )
        if any(s.suspected_injection for s in ctx.evidence):
            violations.append("tainted_evidence_suspected_injection")  # NFR-SEC-004

        # Source freshness vs market horizon (FR-EXT-005).
        if self._source_stale_for_horizon(ctx):
            violations.append("evidence_stale_for_horizon")

        # Adversarial thinking (FR-TI-005, 14.1).
        if p.require_thesis_and_counter_thesis and not intent.counter_thesis.strip():
            violations.append("missing_counter_thesis")
        if not intent.thesis.strip():
            violations.append("missing_thesis")
        if intent.confidence < p.min_confidence:
            violations.append("confidence_below_minimum")

        # --- Sizing / exposure caps (modify) ------------------------------- #
        caps = [requested]
        caps.append(p.max_single_trade_risk_pct * bankroll)  # FR-RISK-003 / 14.1
        caps.append(max(0.0, p.max_market_exposure_pct * bankroll - ctx.market_exposure_usd))
        caps.append(max(0.0, p.max_category_exposure_pct * bankroll - ctx.category_exposure_usd))
        caps.append(
            max(0.0, p.max_correlated_exposure_pct * bankroll - ctx.correlated_exposure_usd)
        )
        approved_size = round(min(caps), 6)

        if approved_size < requested - 1e-9:
            reasons.append(
                f"size reduced {requested}->{approved_size} by exposure caps "
                f"(single/market/category/correlated)"
            )
        if approved_size <= 0.0:
            violations.append("exposure_capacity_exhausted")

        # Prohibited: increasing size after a loss / martingale (14.2).
        if (
            not p.allow_size_increase_after_loss
            and ctx.market_recent_loss
            and ctx.last_size_on_market_usd is not None
            and approved_size > ctx.last_size_on_market_usd + 1e-9
        ):
            approved_size = round(ctx.last_size_on_market_usd, 6)
            reasons.append("size capped to prior size after recent loss (no martingale)")

        # --- Loss stops (reject) ------------------------------------------- #
        if ctx.realized_pnl_today <= -p.daily_loss_stop_pct * bankroll:
            violations.append("daily_loss_stop_hit")  # FR-RISK-002 / 14.1
        if ctx.realized_pnl_campaign <= -p.campaign_loss_stop_pct * bankroll:
            violations.append("campaign_loss_stop_hit")

        # --- Economics (FR-TI-004) ----------------------------------------- #
        be = break_even_probability(side, intent.limit_price, p.fee_bps, p.slippage_bps)
        ev = normalized_ev(side, intent.limit_price, intent.confidence, p.fee_bps, p.slippage_bps)

        # --- Required confirmations for live-eligible mode ----------------- #
        confirmations: list[str] = []
        if ctx.campaign.mode is Mode.LIVE_ELIGIBLE:
            confirmations = [
                "operator_age_verified",
                "jurisdiction_allowed",
                "geoblock_pass",
                "platform_terms_accepted",
                "explicit_live_confirmation",
            ]

        # --- Resolve result ------------------------------------------------ #
        if violations:
            result = RiskResult.REJECT
            approved_size_out: float | None = None
            approved_price_out: float | None = None
        elif approved_size < requested - 1e-9:
            result = RiskResult.MODIFY
            approved_size_out = approved_size
            approved_price_out = intent.limit_price
        else:
            result = RiskResult.APPROVE
            approved_size_out = approved_size
            approved_price_out = intent.limit_price

        exposure_after = {
            "market_pct": round(
                (ctx.market_exposure_usd + (approved_size_out or 0.0)) / bankroll, 6
            ),
            "category_pct": round(
                (ctx.category_exposure_usd + (approved_size_out or 0.0)) / bankroll, 6
            ),
            "total_pct": round((ctx.total_exposure_usd + (approved_size_out or 0.0)) / bankroll, 6),
            "break_even_probability": be,
            "normalized_ev": ev,
        }

        decision = RiskDecision(
            intent_id=intent.intent_id,
            campaign_id=intent.campaign_id,
            result=result,
            approved_size_usd=approved_size_out,
            approved_limit_price=approved_price_out,
            reasons=reasons or (["approved within all limits"] if result is RiskResult.APPROVE else []),
            violated_rules=violations,
            policy_version=p.version,
            data_freshness_ms=ctx.data_age_ms,
            exposure_after_trade=exposure_after,
            required_user_confirmations=confirmations,
        )
        decision.idempotency_key = idempotency_key(
            "risk", intent.intent_id, p.version, requested, intent.limit_price
        )
        return decision

    @staticmethod
    def _source_stale_for_horizon(ctx: RiskContext) -> bool:
        """A thesis must not rest on evidence that is old relative to the time
        remaining to resolution (FR-EXT-005). Uses ``ctx.eval_ms`` so the check
        is reproducible on replay."""
        if not ctx.market.end_time or not ctx.evidence:
            return False
        try:
            ttr = iso_to_ms(ctx.market.end_time) - ctx.eval_ms
        except ValueError:
            return False
        if ttr <= 0:
            return False
        budget = ctx.policy.max_source_age_ratio * ttr
        for s in ctx.evidence:
            if s.issued_at is not None and (ctx.eval_ms - s.issued_at) > budget:
                return True
        return False
