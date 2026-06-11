"""Signal adapter contract.

Every adapter declares its latency, update frequency, source authority,
reliability, licensing, and real-time suitability (FR-EXT-002), and builds
:class:`Signal` objects whose text has already been sanitized (FR-SOC-003) with
full provenance recorded (FR-SOC-002)."""

from __future__ import annotations

import abc

from pydantic import BaseModel

from hermes_pm.models import Market, Signal, SignalStance, SourceType
from hermes_pm.util.sanitize import sanitize_untrusted
from hermes_pm.util.timeutil import now_ms


class AdapterMeta(BaseModel):
    model_config = {"frozen": True}
    name: str
    source_class: SourceType
    latency_class: str  # "realtime" | "delayed"
    update_frequency_s: int
    source_authority: str
    reliability: float  # 0..1
    licensing: str
    suitable_for_realtime: bool


def build_signal(
    market_id: str,
    meta: AdapterMeta,
    *,
    source_ref: str,
    raw_text: str,
    stance: SignalStance,
    confidence: float,
    novelty: float,
    issued_at: int | None = None,
) -> Signal:
    """Construct a fully-sanitized, provenance-tagged signal."""
    clean = sanitize_untrusted(raw_text)
    flags: list[str] = []
    if clean.suspected_injection:
        flags.append("suspected_prompt_injection")
    return Signal(
        market_id=market_id,
        source_type=meta.source_class,
        source_ref=source_ref,
        text_summary=clean.text,
        stance=stance,
        confidence=round(confidence, 4),
        novelty=round(novelty, 4),
        trust_score=round(meta.reliability, 4),
        issued_at=issued_at,
        timestamp=now_ms(),
        adapter=meta.name,
        latency_class=meta.latency_class,
        policy_flags=flags,
        suspected_injection=clean.suspected_injection,
    )


class SignalAdapter(abc.ABC):
    meta: AdapterMeta

    @abc.abstractmethod
    async def fetch(self, market: Market, *, counter: bool = False) -> list[Signal]:
        """Return signals for ``market``. ``counter=True`` requests contradictory
        evidence for counter-signal search (FR-SOC-007)."""
        raise NotImplementedError
