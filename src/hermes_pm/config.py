"""System configuration and the versioned risk policy.

Defaults implement the conservative limits mandated by SRS Section 14.1 and
FR-RISK-003 (small size, tight stops, no leverage, no martingale). The risk
policy is content-addressed: its ``version`` is a hash of its own fields so that
every RiskDecision can record the exact policy used (FR-RISK-007)."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

from hermes_pm.util.hashing import hash_obj


class RiskPolicy(BaseModel):
    """Deterministic, versioned risk limits. Immutable once constructed."""

    model_config = {"frozen": True, "extra": "forbid"}

    # Exposure / sizing (fractions of paper bankroll) — SRS 14.1.
    max_single_trade_risk_pct: float = Field(default=0.01, ge=0.0, le=1.0, allow_inf_nan=False)
    max_market_exposure_pct: float = Field(default=0.05, ge=0.0, le=1.0, allow_inf_nan=False)
    max_category_exposure_pct: float = Field(default=0.15, ge=0.0, le=1.0, allow_inf_nan=False)
    max_correlated_exposure_pct: float = Field(default=0.20, ge=0.0, le=1.0, allow_inf_nan=False)

    # Loss stops (fractions of bankroll).
    daily_loss_stop_pct: float = Field(default=0.05, ge=0.0, le=1.0, allow_inf_nan=False)
    campaign_loss_stop_pct: float = Field(default=0.10, ge=0.0, le=1.0, allow_inf_nan=False)

    # Microstructure gates.
    min_orderbook_depth_usd: float = Field(default=200.0, ge=0.0, allow_inf_nan=False)
    max_spread: float = Field(default=0.05, ge=0.0, le=1.0, allow_inf_nan=False)  # 0..1 price units
    max_data_staleness_ms: int = Field(default=5_000, ge=0)

    # Evidence quality (FR-RISK-004, 14.1 minimum evidence count). No upper bound on
    # the floors below: a higher floor only makes the policy *stricter*, never looser.
    min_primary_sources: int = Field(default=1, ge=0)
    min_secondary_sources: int = Field(default=2, ge=0)
    require_thesis_and_counter_thesis: bool = True
    min_confidence: float = Field(default=0.0, ge=0.0, allow_inf_nan=False)
    max_source_age_ratio: float = Field(default=0.5, ge=0.0, allow_inf_nan=False)

    # Cost / fill model used for EV and break-even (FR-TI-004, FR-PAPER-002).
    # bps are floored at 0 so costs can never be made *optimistic* (a negative
    # slippage would silently inflate EV and undermine the risk guarantee).
    fee_bps: float = Field(default=0.0, ge=0.0, allow_inf_nan=False)
    slippage_bps: float = Field(default=50.0, ge=0.0, allow_inf_nan=False)

    # Hard guards (FR-RISK-003, 14.2).
    allow_martingale: bool = False
    allow_leverage: bool = False
    allow_size_increase_after_loss: bool = False

    @computed_field  # type: ignore[prop-decorator]
    @property
    def version(self) -> str:
        # Hash declared fields only; never call model_dump() here (it would
        # re-enter this computed field and recurse infinitely).
        payload = {name: getattr(self, name) for name in type(self).model_fields}
        return "rp-" + hash_obj(payload)[:12]


class Settings(BaseSettings):
    """Process configuration. Environment variables use the ``HPM_`` prefix."""

    model_config = SettingsConfigDict(env_prefix="HPM_", extra="ignore")

    data_dir: Path = Path("./.hermes_pm_data")
    db_filename: str = "hermes_pm.sqlite3"

    # Market-data source: "synthetic" (deterministic, offline, default for
    # tests/demo), "replay" (from a recorded file), or "live" (Polymarket).
    market_data_source: str = "synthetic"
    synthetic_seed: int = 1337
    synthetic_market_count: int = 6
    replay_file: Path | None = None

    # Polymarket endpoints (used only when market_data_source == "live").
    gamma_base_url: str = "https://gamma-api.polymarket.com"
    clob_base_url: str = "https://clob.polymarket.com"
    clob_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    geoblock_url: str = "https://polymarket.com/api/geoblock"

    # Staleness / reliability.
    ws_reconnect_stale_ms: int = 5_000
    reconcile_interval_ms: int = 10_000

    # Dashboard (must bind to localhost by default — MCP-SR-002 / NFR-SEC-006).
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8787
    dashboard_token: str | None = None  # required if host != 127.0.0.1

    # MCP Streamable HTTP (off by default; stdio is preferred — MCP-SR-001).
    mcp_http_enabled: bool = False
    mcp_http_host: str = "127.0.0.1"
    mcp_http_port: int = 8989
    mcp_http_token: str | None = None

    # Live adapter — disabled by default (FR-LIVE-001). Real activation also
    # requires passing every compliance gate; this flag alone is insufficient.
    live_enabled: bool = False
    operator_age_verified: bool = False
    operator_jurisdiction_allowed: bool = False
    operator_acknowledged_risk: bool = False
    # NFR-SEC-005: prompt-injection / red-team suite must be signed off before live.
    red_team_passed: bool = False

    # Secret storage (NFR-SEC-001). "env" (default, paper needs none),
    # "encrypted_file" (Fernet at rest), or "keyring" (OS keychain).
    secret_store: str = "env"  # noqa: S105 - backend selector, not a secret
    secret_store_path: Path | None = None
    secret_master_passphrase: str | None = None
    signing_key_name: str = "live_signing_key"
    # NFR-SEC-007: run the locked live adapter in a separate OS process.
    live_process_isolation: bool = False

    # Social / external signals (off by default; require explicit keys).
    x_api_bearer_token: str | None = None
    x_api_enabled: bool = False

    default_risk_policy: RiskPolicy = Field(default_factory=RiskPolicy)

    @property
    def db_path(self) -> Path:
        return self.data_dir / self.db_filename

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)


def load_settings(**overrides: object) -> Settings:
    s = Settings(**overrides)
    s.ensure_dirs()
    return s
