"""Campaign manager (Section 8 modes, AC-001).

Owns campaign creation/validation and lifecycle transitions
(running/paused/stopped/completed) and the per-campaign risk policy so every
decision can be tied to the exact versioned limits in force."""

from __future__ import annotations

from typing import Any

from hermes_pm.audit.store import AuditStore
from hermes_pm.config import RiskPolicy, Settings
from hermes_pm.errors import StateError, ValidationError
from hermes_pm.events import EventBus, EventType
from hermes_pm.execution.paper_engine import PaperEngine
from hermes_pm.models import Campaign, CampaignStatus, Mode
from hermes_pm.persistence.db import Database

_ACTIVE = {CampaignStatus.RUNNING, CampaignStatus.PAUSED}


class CampaignManager:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        bus: EventBus,
        audit: AuditStore,
        paper_engine: PaperEngine,
    ) -> None:
        self._s = settings
        self.db = db
        self.bus = bus
        self.audit = audit
        self.paper = paper_engine

    def _policy_key(self, cid: str) -> str:
        return f"risk_policy:{cid}"

    def policy_for(self, campaign_id: str) -> RiskPolicy:
        raw = self.db.kv_get(self._policy_key(campaign_id))
        return RiskPolicy.model_validate(raw) if raw else self._s.default_risk_policy

    @staticmethod
    def _safe_policy(default: RiskPolicy, overrides: dict[str, Any]) -> RiskPolicy:
        """Build a policy from operator overrides that is **never looser** than the
        default (FR-RISK-003, Section 14.2). Exposure/loss/spread/staleness caps may
        only shrink; depth/evidence/confidence/cost floors may only grow; and the
        prohibited-behaviour switches are forced off regardless of input."""
        d = default.model_dump(exclude={"version"})
        only_decrease = {
            "max_single_trade_risk_pct", "max_market_exposure_pct", "max_category_exposure_pct",
            "max_correlated_exposure_pct", "daily_loss_stop_pct", "campaign_loss_stop_pct",
            "max_spread", "max_data_staleness_ms", "max_source_age_ratio",
        }
        only_increase = {
            "min_orderbook_depth_usd", "min_primary_sources", "min_secondary_sources",
            "min_confidence", "fee_bps", "slippage_bps",
        }
        for key, val in (overrides or {}).items():
            if key in only_decrease and isinstance(val, (int, float)):
                d[key] = max(0.0, min(d[key], float(val)))
            elif key in only_increase and isinstance(val, (int, float)):
                d[key] = max(d[key], float(val))
            # all other keys (incl. the allow_* switches and require_thesis...) are ignored
        d["require_thesis_and_counter_thesis"] = True
        d["allow_martingale"] = False
        d["allow_leverage"] = False
        d["allow_size_increase_after_loss"] = False
        return RiskPolicy(**d)

    def create(
        self,
        *,
        name: str,
        duration_hours: float,
        paper_bankroll_usd: float,
        market_filters: dict[str, Any] | None = None,
        risk_profile: dict[str, Any] | None = None,
        allowed_signal_sources: list[str] | None = None,
        mode: Mode = Mode.PAPER,
        dashboard_url: str = "",
    ) -> Campaign:
        if paper_bankroll_usd <= 0:
            raise ValidationError("paper_bankroll_usd must be > 0", code="validation_error")
        if duration_hours <= 0:
            raise ValidationError("duration_hours must be > 0", code="validation_error")
        if mode is not Mode.PAPER:
            raise ValidationError(
                "only PAPER campaigns can be started here; live is compliance-locked",
                code="compliance_locked",
            )
        try:
            policy = (
                self._safe_policy(self._s.default_risk_policy, risk_profile)
                if risk_profile
                else self._s.default_risk_policy
            )
        except Exception as exc:  # noqa: BLE001
            raise ValidationError(f"invalid risk_profile: {exc}", code="validation_error") from exc

        campaign = Campaign(
            mode=mode,
            name=name,
            duration_hours=duration_hours,
            bankroll=paper_bankroll_usd,
            market_filters=market_filters or {},
            allowed_signal_sources=allowed_signal_sources or [],
            risk_policy_version=policy.version,
            status=CampaignStatus.RUNNING,
            dashboard_url=dashboard_url,
        )
        self.db.kv_set(self._policy_key(campaign.campaign_id), policy.model_dump(exclude={"version"}))
        self.db.save_campaign(campaign)
        self.paper.init_campaign(campaign)
        self.audit.append(
            "campaign_started", actor="operator", summary=f"paper campaign {name}",
            inputs={"duration_hours": duration_hours, "bankroll": paper_bankroll_usd,
                    "filters": market_filters, "risk_policy_version": policy.version},
            outputs={"campaign_id": campaign.campaign_id}, campaign_id=campaign.campaign_id,
        )
        self.bus.publish(EventType.CAMPAIGN_UPDATE,
                         {"campaign_id": campaign.campaign_id, "status": campaign.status.value})
        return campaign

    def _get(self, campaign_id: str) -> Campaign:
        c = self.db.get_campaign(campaign_id)
        if c is None:
            raise ValidationError(f"campaign not found: {campaign_id}", code="not_found")
        return c

    def _transition(self, campaign_id: str, to: CampaignStatus, allowed_from: set) -> Campaign:
        c = self._get(campaign_id)
        if c.status not in allowed_from:
            raise StateError(
                f"cannot move campaign from {c.status.value} to {to.value}", code="state_error"
            )
        c.status = to
        self.db.save_campaign(c)
        self.audit.append(f"campaign_{to.value}", actor="operator", campaign_id=campaign_id)
        self.bus.publish(EventType.CAMPAIGN_UPDATE, {"campaign_id": campaign_id, "status": to.value})
        return c

    def pause(self, campaign_id: str) -> Campaign:
        return self._transition(campaign_id, CampaignStatus.PAUSED, {CampaignStatus.RUNNING})

    def resume(self, campaign_id: str) -> Campaign:
        return self._transition(campaign_id, CampaignStatus.RUNNING, {CampaignStatus.PAUSED})

    def stop(self, campaign_id: str) -> Campaign:
        self.paper.cancel_all(campaign_id)
        return self._transition(campaign_id, CampaignStatus.STOPPED, _ACTIVE)

    def complete(self, campaign_id: str) -> Campaign:
        return self._transition(campaign_id, CampaignStatus.COMPLETED, _ACTIVE)

    def is_active(self, campaign_id: str) -> bool:
        return self._get(campaign_id).status in _ACTIVE
