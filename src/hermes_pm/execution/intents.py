"""Trade-intent lifecycle (FR-TI-001..006).

The agent proposes *intents*, never orders (FR-TI-001). This service validates
required content (FR-TI-003), computes break-even probability and normalized EV
(FR-TI-004), enforces thesis + counter-thesis (FR-TI-005), and surfaces similar
past decisions so the agent cannot blindly reuse a thesis (FR-TI-006)."""

from __future__ import annotations

from hermes_pm.config import RiskPolicy
from hermes_pm.errors import NotFoundError, ValidationError
from hermes_pm.execution.economics import break_even_probability, normalized_ev
from hermes_pm.models import Campaign, Market, OrderType, Side, TradeIntent
from hermes_pm.persistence.db import Database
from hermes_pm.util.ids import idempotency_key
from hermes_pm.util.timeutil import iso_to_ms, now_ms


class IntentService:
    def __init__(self, db: Database, policy: RiskPolicy) -> None:
        self.db = db
        self.policy = policy

    def create(
        self,
        campaign: Campaign,
        market: Market,
        *,
        outcome: str,
        side: Side,
        limit_price: float,
        max_size_usd: float,
        thesis: str,
        expires_at: str,
        confidence: float,
        counter_thesis: str = "",
        invalidation_criteria: str = "",
        evidence_refs: list[str] | None = None,
        order_type: OrderType = OrderType.MARKETABLE_LIMIT,
        token_id: str | None = None,
        created_by: str = "agent",
        prompt_version: str = "",
        policy: RiskPolicy | None = None,
    ) -> TradeIntent:
        policy = policy or self.policy
        evidence_refs = evidence_refs or []
        tok = token_id or market.token_ids.get(outcome.upper())
        if not tok:
            raise NotFoundError(
                f"no token for outcome {outcome!r} in market {market.market_id}",
                code="not_found",
            )

        intent = TradeIntent(
            campaign_id=campaign.campaign_id,
            market_id=market.market_id,
            token_id=tok,
            outcome=outcome.upper(),
            side=side,
            order_type=order_type,
            limit_price=limit_price,
            max_size_usd=max_size_usd,
            thesis=thesis,
            counter_thesis=counter_thesis,
            invalidation_criteria=invalidation_criteria,
            evidence_refs=evidence_refs,
            confidence=confidence,
            expires_at=expires_at,
            created_by=created_by,
            prompt_version=prompt_version,
        )

        # Economics (FR-TI-004): break-even from price+costs; EV from agent prob.
        intent.break_even_probability = break_even_probability(
            side, limit_price, policy.fee_bps, policy.slippage_bps
        )
        intent.normalized_ev = normalized_ev(
            side, limit_price, confidence, policy.fee_bps, policy.slippage_bps
        )

        # Required-content validation (FR-TI-003 / FR-TI-002 / FR-TI-005).
        missing: list[str] = []
        if not evidence_refs:
            missing.append("evidence_refs")
        if not market.has_clear_resolution:
            missing.append("market_resolution_rules")
        if not counter_thesis.strip():
            missing.append("counter_thesis")  # FR-TI-005
        if not invalidation_criteria.strip():
            missing.append("invalidation_criteria")
        try:
            if iso_to_ms(expires_at) <= now_ms():
                missing.append("expires_at_in_future")
        except ValueError as exc:
            raise ValidationError(f"invalid expires_at: {expires_at!r}", code="schema_rejected") from exc

        intent.missing_fields = missing
        intent.status = "needs_more_evidence" if missing else "created"
        intent.idempotency_key = idempotency_key(
            "intent", campaign.campaign_id, market.market_id, tok, side.value,
            limit_price, max_size_usd, expires_at, thesis, counter_thesis,
            confidence, sorted(evidence_refs),
        )
        return self.db.save_intent(intent)

    def similar_past_intents(self, campaign_id: str, market_id: str, exclude_id: str) -> list[str]:
        """FR-TI-006: prior decisions on the same market the agent should compare."""
        return [
            t.intent_id
            for t in self.db.list_intents(campaign_id)
            if t.market_id == market_id and t.intent_id != exclude_id
        ]
