"""Domain data model — every entity in SRS Section 13 plus the enums and
order-book structures they depend on. Prices are probabilities in [0, 1] USD per
share; sizes are USD notional unless explicitly named ``shares``."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator

from hermes_pm.util.hashing import hash_obj
from hermes_pm.util.ids import new_id
from hermes_pm.util.timeutil import now_iso, now_ms


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #
class Mode(str, Enum):
    RESEARCH = "research"
    PAPER = "paper"
    REVIEW = "review"
    LIVE_ELIGIBLE = "live_eligible"
    EMERGENCY = "emergency"


class CampaignStatus(str, Enum):
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"
    COMPLETED = "completed"
    REJECTED = "rejected"


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    LIMIT = "limit"  # passive, rests in book
    MARKETABLE_LIMIT = "marketable_limit"  # crosses spread (Polymarket "market" — S9)


class OrderStatus(str, Enum):
    ACCEPTED = "accepted"
    OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class RiskResult(str, Enum):
    APPROVE = "approve"
    MODIFY = "modify"
    REJECT = "reject"


class SignalStance(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"
    MIXED = "mixed"


class SourceType(str, Enum):
    PRIMARY = "primary"  # official / authoritative resolution source
    SECONDARY = "secondary"  # reputable reporting
    SOCIAL = "social"  # X / social conversation (lowest trust)


class CloseStatus(str, Enum):
    OPEN = "open"
    CLOSED = "closed"


class FailureMode(str, Enum):
    THESIS_CORRECT = "thesis_correct"
    THESIS_INCORRECT = "thesis_incorrect"
    TIMING_ERROR = "timing_error"
    LIQUIDITY_ERROR = "liquidity_error"
    SOURCE_ERROR = "source_error"
    RESOLUTION_RULE_ERROR = "resolution_rule_error"
    SOCIAL_HYPE = "social_hype"
    STALE_DATA = "stale_data"
    RISK_LIMIT = "risk_limit"
    RANDOM_VARIANCE = "random_variance"


class MemoryTarget(str, Enum):
    ACTIVE = "active"  # MEMORY.md — only compact, repeated lessons
    SESSION = "session"  # Hermes session search
    AUDIT_ONLY = "audit_only"


# --------------------------------------------------------------------------- #
# Order book (FR-DATA-002, entity OrderBookSnapshot / Token)
# --------------------------------------------------------------------------- #
class BookLevel(BaseModel):
    model_config = {"extra": "forbid"}
    price: float = Field(ge=0.0, le=1.0)
    size: float = Field(ge=0.0)  # USD notional resting at this level


class OrderBookSnapshot(BaseModel):
    """Immutable point-in-time book. ``bids`` sorted desc by price, ``asks`` asc."""

    model_config = {"extra": "forbid"}
    snapshot_id: str = Field(default_factory=lambda: new_id("ob"))
    token_id: str
    bids: list[BookLevel] = Field(default_factory=list)
    asks: list[BookLevel] = Field(default_factory=list)
    last_trade: float | None = None
    sequence: int = 0
    received_at: int = Field(default_factory=now_ms)
    source: str = "synthetic"
    checksum: str = ""

    def model_post_init(self, _ctx: Any) -> None:
        if not self.checksum:
            object.__setattr__(self, "checksum", self.compute_checksum())

    def compute_checksum(self) -> str:
        return hash_obj(
            {
                "token_id": self.token_id,
                "bids": [(b.price, b.size) for b in self.bids],
                "asks": [(a.price, a.size) for a in self.asks],
                "sequence": self.sequence,
            }
        )

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    @property
    def spread(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return round(self.best_ask - self.best_bid, 6)

    @property
    def mid(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return self.last_trade
        return round((self.best_bid + self.best_ask) / 2, 6)

    def depth_usd(self, side: Side) -> float:
        levels = self.asks if side is Side.BUY else self.bids
        return round(sum(level.size for level in levels), 6)

    def is_stale(self, max_age_ms: int, now: int | None = None) -> bool:
        now = now if now is not None else now_ms()
        return (now - self.received_at) > max_age_ms


# --------------------------------------------------------------------------- #
# Market / Token (Section 13, FR-MD-001..005)
# --------------------------------------------------------------------------- #
class Market(BaseModel):
    model_config = {"extra": "forbid"}
    market_id: str
    event_id: str
    condition_id: str
    question_id: str = ""
    question: str
    category: str = "uncategorized"
    outcomes: list[str] = Field(default_factory=lambda: ["YES", "NO"])
    token_ids: dict[str, str] = Field(default_factory=dict)  # outcome -> token_id
    resolution_rules: str = ""
    resolution_source: str = ""
    source_links: list[str] = Field(default_factory=list)
    end_time: str | None = None  # ISO-8601
    enable_order_book: bool = False
    tags: list[str] = Field(default_factory=list)

    @property
    def has_clear_resolution(self) -> bool:
        """FR-MD-003/004: rules + source must be present and unambiguous."""
        return bool(self.resolution_rules.strip()) and bool(self.resolution_source.strip())


class Token(BaseModel):
    model_config = {"extra": "forbid"}
    token_id: str
    market_id: str
    outcome: str
    best_bid: float | None = None
    best_ask: float | None = None
    last_trade_price: float | None = None
    spread: float | None = None
    depth: float | None = None
    tick_size: float = 0.01
    stale_after_ms: int = 5_000


# --------------------------------------------------------------------------- #
# Signals (FR-SOC-*, FR-EXT-*)
# --------------------------------------------------------------------------- #
class Signal(BaseModel):
    model_config = {"extra": "forbid"}
    signal_id: str = Field(default_factory=lambda: new_id("sig"))
    market_id: str
    source_type: SourceType
    source_ref: str  # provenance: URL / post id / station id / API ref
    text_summary: str  # already sanitized
    stance: SignalStance = SignalStance.NEUTRAL
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    novelty: float = Field(default=0.0, ge=0.0, le=1.0)
    trust_score: float = Field(default=0.0, ge=0.0, le=1.0)
    issued_at: int | None = None  # source's own timestamp (ms)
    timestamp: int = Field(default_factory=now_ms)  # ingest time
    adapter: str = ""
    latency_class: str = "delayed"  # "realtime" | "delayed"
    policy_flags: list[str] = Field(default_factory=list)
    suspected_injection: bool = False


# --------------------------------------------------------------------------- #
# Trade intent (FR-TI-001..006)
# --------------------------------------------------------------------------- #
class TradeIntent(BaseModel):
    model_config = {"extra": "forbid"}
    intent_id: str = Field(default_factory=lambda: new_id("ti"))
    campaign_id: str
    market_id: str
    token_id: str
    outcome: str = "YES"
    side: Side
    order_type: OrderType = OrderType.MARKETABLE_LIMIT
    limit_price: float = Field(ge=0.0, le=1.0)
    max_size_usd: float = Field(gt=0.0)
    thesis: str  # FR-TI-005: why the market price may be wrong
    counter_thesis: str = ""  # FR-TI-005: why the agent may be wrong
    invalidation_criteria: str = ""  # FR-TI-002
    evidence_refs: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    expires_at: str  # ISO-8601
    created_by: str = "agent"
    prompt_version: str = ""  # NFR-OBS-002: agent prompt version traceability
    created_at: str = Field(default_factory=now_iso)
    idempotency_key: str = ""

    # Computed economics (FR-TI-004), filled by the intent service.
    break_even_probability: float | None = None
    normalized_ev: float | None = None
    status: str = "created"
    missing_fields: list[str] = Field(default_factory=list)

    @field_validator("thesis")
    @classmethod
    def _thesis_nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("thesis is required and must be non-empty")
        return v


# --------------------------------------------------------------------------- #
# Risk decision (Section 14.3)
# --------------------------------------------------------------------------- #
class RiskDecision(BaseModel):
    model_config = {"extra": "forbid"}
    decision_id: str = Field(default_factory=lambda: new_id("rd"))
    intent_id: str
    campaign_id: str
    result: RiskResult
    approved_size_usd: float | None = None
    approved_limit_price: float | None = None
    reasons: list[str] = Field(default_factory=list)
    violated_rules: list[str] = Field(default_factory=list)
    policy_version: str = ""
    data_freshness_ms: int = 0
    exposure_after_trade: dict[str, float] = Field(default_factory=dict)
    required_user_confirmations: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=now_iso)
    idempotency_key: str = ""


# --------------------------------------------------------------------------- #
# Orders / Fills (Section 13)
# --------------------------------------------------------------------------- #
class Fill(BaseModel):
    model_config = {"extra": "forbid"}
    fill_id: str = Field(default_factory=lambda: new_id("fill"))
    order_id: str
    price: float
    size_usd: float
    shares: float
    simulated_or_real: str = "simulated"
    liquidity_source: str = "book"  # which book level / reason
    snapshot_id: str = ""  # provenance for replay (FR-PAPER-005, AC-004)
    reason: str = ""
    created_at: str = Field(default_factory=now_iso)
    created_ms: int = Field(default_factory=now_ms)


class Order(BaseModel):
    model_config = {"extra": "forbid"}
    order_id: str = Field(default_factory=lambda: new_id("ord"))
    mode: Mode = Mode.PAPER
    campaign_id: str
    intent_id: str
    risk_decision_id: str
    market_id: str
    token_id: str
    side: Side
    order_type: OrderType
    price: float
    size_usd: float
    filled_size_usd: float = 0.0
    venue_ref: str | None = None  # always None in paper mode
    status: OrderStatus = OrderStatus.ACCEPTED
    fills: list[Fill] = Field(default_factory=list)
    created_at: str = Field(default_factory=now_iso)
    updated_at: str = Field(default_factory=now_iso)
    idempotency_key: str = ""

    @property
    def remaining_usd(self) -> float:
        return round(self.size_usd - self.filled_size_usd, 6)


# --------------------------------------------------------------------------- #
# Position (Section 13)
# --------------------------------------------------------------------------- #
class Position(BaseModel):
    model_config = {"extra": "forbid"}
    position_id: str = Field(default_factory=lambda: new_id("pos"))
    campaign_id: str
    market_id: str
    token_id: str
    outcome: str = "YES"
    shares: float = 0.0  # signed: + long, - short
    avg_price: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    mark_price: float | None = None
    close_status: CloseStatus = CloseStatus.OPEN

    @property
    def notional(self) -> float:
        mark = self.mark_price if self.mark_price is not None else self.avg_price
        return round(abs(self.shares) * mark, 6)


# --------------------------------------------------------------------------- #
# Campaign (Section 13, Section 8)
# --------------------------------------------------------------------------- #
class Campaign(BaseModel):
    model_config = {"extra": "forbid"}
    campaign_id: str = Field(default_factory=lambda: new_id("camp"))
    mode: Mode = Mode.PAPER
    name: str
    start_time: str = Field(default_factory=now_iso)
    start_ms: int = Field(default_factory=now_ms)
    end_time: str | None = None
    duration_hours: float = 48.0
    bankroll: float = 1000.0
    market_filters: dict[str, Any] = Field(default_factory=dict)
    risk_policy_version: str = ""
    allowed_signal_sources: list[str] = Field(default_factory=list)
    status: CampaignStatus = CampaignStatus.RUNNING
    dashboard_url: str = ""
    watchlist: list[str] = Field(default_factory=list)  # market_ids

    @property
    def end_ms(self) -> int:
        return self.start_ms + int(self.duration_hours * 3_600_000)


# --------------------------------------------------------------------------- #
# Lesson (FR-LEARN-003)
# --------------------------------------------------------------------------- #
class Lesson(BaseModel):
    model_config = {"extra": "forbid"}
    lesson_id: str = Field(default_factory=lambda: new_id("les"))
    campaign_id: str
    trigger: str
    observation: str
    pattern: str = ""  # mistake or success pattern
    rule: str  # the new rule
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    valid_until: str | None = None  # valid-until condition
    source_refs: list[str] = Field(default_factory=list)
    memory_target: MemoryTarget = MemoryTarget.SESSION
    supporting_evidence_count: int = 1  # FR-LEARN-006: no single-lucky-trade rules
    created_at: str = Field(default_factory=now_iso)


# --------------------------------------------------------------------------- #
# Audit event (Section 13, NFR-OBS-001) — hash-chained
# --------------------------------------------------------------------------- #
class AuditEvent(BaseModel):
    model_config = {"extra": "forbid"}
    event_id: str = Field(default_factory=lambda: new_id("ev"))
    seq: int = 0
    type: str
    actor: str = "system"
    summary: str = ""
    input_hash: str = ""
    output_hash: str = ""
    references: dict[str, Any] = Field(default_factory=dict)
    payload: dict[str, Any] = Field(default_factory=dict)
    timestamp: str = Field(default_factory=now_iso)
    timestamp_ms: int = Field(default_factory=now_ms)
    latency_ms: float = 0.0
    previous_event_hash: str = ""
    event_hash: str = ""
