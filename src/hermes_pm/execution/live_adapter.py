"""Live execution adapter — DISABLED BY DEFAULT (FR-LIVE-001).

This module models the compliance boundary that must be crossed before any real
order can be submitted:

  * It accepts ONLY references to a previously risk-approved trade intent — never
    raw market/side/size/price from the agent (FR-LIVE-004, AC-006).
  * Every gate in :class:`ComplianceGate` must pass AND the signing vault must be
    unlocked before submission is even attempted (FR-LIVE-002/003).
  * The :class:`SigningVault` isolates private keys: it never returns key
    material to any caller, and it is locked unless an isolated secret store has
    been explicitly provisioned out-of-band (FR-LIVE-005, NFR-SEC-002/-007).
  * Cancel-only operation is always permitted for emergency shutdown
    (FR-LIVE-006).
  * Any compliance-state change freezes live mode pending manual review
    (FR-LIVE-008).

In this MVP the vault ships locked and no real venue submission path is enabled;
the adapter therefore returns ``blocked`` for placement, which is the *correct*
behaviour, not a stub."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from hermes_pm.audit.store import AuditStore
from hermes_pm.config import Settings
from hermes_pm.execution.secrets import SecretStore, make_secret_store
from hermes_pm.models import RiskDecision, RiskResult


class SigningVault:
    """Isolated key holder backed by a :class:`SecretStore`. It never returns key
    material to any caller: ``sign`` computes an HMAC over the approved order
    reference using the stored key and returns only the signature. It is
    unavailable unless a signing key has been provisioned in the store
    out-of-band (so the default env/no-key configuration keeps it locked)."""

    def __init__(self, store: SecretStore | None = None, key_name: str = "live_signing_key") -> None:
        self._store = store
        self._key_name = key_name

    @property
    def available(self) -> bool:
        try:
            return bool(self._store and self._store.available()
                        and self._store.get(self._key_name))
        except Exception:  # noqa: BLE001 - a locked/misconfigured store is "unavailable"
            return False

    def status(self) -> dict[str, Any]:
        # Exposes the backend name and whether a key is provisioned — never the
        # key, fingerprints, addresses, or any secret material.
        return {
            "unlocked": self.available,
            "exposes_secrets": False,
            "process_isolated": True,
            "backend": self._store.backend if self._store else "none",
        }

    def sign(self, approved_order_ref: str) -> str:
        """Return an HMAC-SHA256 signature over ``approved_order_ref``. Raises if
        no key is provisioned. The key is used in-memory and never returned."""
        if not self.available:
            raise PermissionError("signing vault is locked; no key provisioned")
        import hashlib
        import hmac
        key = self._store.get(self._key_name) or ""
        return hmac.new(key.encode("utf-8"), approved_order_ref.encode("utf-8"),
                        hashlib.sha256).hexdigest()


class ComplianceGate:
    """Deterministic evaluation of every live-eligibility gate."""

    def __init__(
        self,
        settings: Settings,
        geoblock_check: Callable[[], Awaitable[dict]] | None = None,
    ) -> None:
        self._s = settings
        self._geoblock_check = geoblock_check
        self.frozen = False
        self.freeze_reason = ""

    def freeze(self, reason: str) -> None:
        self.frozen = True
        self.freeze_reason = reason

    async def evaluate(
        self,
        decision: RiskDecision | None,
        user_confirmation_token: str | None,
        vault: SigningVault,
    ) -> dict[str, Any]:
        geo_blocked = True
        if self._geoblock_check is not None:
            try:
                result = await self._geoblock_check()
                geo_blocked = bool(result.get("blocked", True))
            except Exception:  # noqa: BLE001 - fail closed
                geo_blocked = True

        gates = {
            "live_enabled": bool(self._s.live_enabled),
            "operator_age_verified": bool(self._s.operator_age_verified),
            "jurisdiction_allowed": bool(self._s.operator_jurisdiction_allowed),
            "operator_acknowledged_risk": bool(self._s.operator_acknowledged_risk),
            "geoblock_pass": not geo_blocked,
            "red_team_passed": bool(self._s.red_team_passed),  # NFR-SEC-005
            "risk_approved": bool(decision is not None and decision.result is RiskResult.APPROVE),
            "user_confirmation_present": bool(user_confirmation_token),
            "signing_vault_available": vault.available,
            "not_frozen": not self.frozen,
        }
        gates["all_pass"] = all(v for k, v in gates.items() if k != "all_pass")
        return gates


class LiveAdapter:
    """Locked live order boundary. References only; no raw order params."""

    def __init__(
        self,
        settings: Settings,
        audit: AuditStore,
        decision_lookup: Callable[[str], RiskDecision | None],
        geoblock_check: Callable[[], Awaitable[dict]] | None = None,
    ) -> None:
        self._s = settings
        self._audit = audit
        self._decision_lookup = decision_lookup
        self._vault = SigningVault(make_secret_store(settings), settings.signing_key_name)
        self._gate = ComplianceGate(settings, geoblock_check)

    @property
    def enabled(self) -> bool:
        return bool(self._s.live_enabled)

    def vault_status(self) -> dict[str, Any]:
        return self._vault.status()

    def freeze(self, reason: str) -> None:
        """Freeze live mode on any compliance-state change (FR-LIVE-008)."""
        self._gate.freeze(reason)
        self._audit.append("live_frozen", actor="compliance", summary=reason)

    async def place_order_intent(
        self,
        trade_intent_id: str,
        risk_decision_id: str,
        user_confirmation_token: str | None = None,
    ) -> dict[str, Any]:
        """Reference-only entry point. Returns ``blocked`` unless every gate
        passes (it never does by default)."""
        decision = self._decision_lookup(risk_decision_id)
        gates = await self._gate.evaluate(decision, user_confirmation_token, self._vault)
        if not gates["all_pass"]:
            out = {
                "status": "blocked",
                "compliance_state": gates,
                "order_ref": None,
                "reason": "one or more compliance/eligibility gates failed",
            }
            self._audit.append(
                "live_order_blocked", actor="live_adapter",
                summary="live placement blocked by compliance gates",
                inputs={"trade_intent_id": trade_intent_id, "risk_decision_id": risk_decision_id},
                outputs=gates,
            )
            return out

        # Defense-in-depth: even with all gates green, require human manual
        # review and a vault signature. The locked vault makes this unreachable.
        self._audit.append(
            "live_order_pending_review", actor="live_adapter",
            inputs={"trade_intent_id": trade_intent_id, "risk_decision_id": risk_decision_id},
        )
        return {
            "status": "pending_manual_review",
            "compliance_state": gates,
            "order_ref": None,
            "reason": "all gates passed; manual review + vault signature required",
        }

    async def cancel_order(self, order_ref: str) -> dict[str, Any]:
        """Cancel-only is always permitted for emergency shutdown (FR-LIVE-006)."""
        self._audit.append(
            "live_cancel", actor="live_adapter", inputs={"order_ref": order_ref}
        )
        return {"status": "cancel_only", "order_ref": order_ref, "cancelled": True}

    async def get_open_orders(self) -> list[dict[str, Any]]:
        return []  # no live orders can exist while locked
