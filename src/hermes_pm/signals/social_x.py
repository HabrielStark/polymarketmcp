"""X / social adapter.

Ingests public posts ONLY through the official X API recent-search endpoint and
NEVER scrapes the website or circumvents rate limits (FR-SOC-001, COMP-006). All
text is sanitized as untrusted before exposure (FR-SOC-003), provenance is kept
(FR-SOC-002), and X is treated as a delayed (seconds-level) signal, not a
sub-second one (FR-SOC-005). Counter-signal search returns contradictory stance
(FR-SOC-007).

When ``x_api_enabled`` is false (default) or no bearer token is configured, the
adapter operates in deterministic offline mode producing clearly-labelled
synthetic posts so the rest of the system runs without network access."""

from __future__ import annotations

import hashlib

import httpx

from hermes_pm.config import Settings
from hermes_pm.errors import RateLimitedError
from hermes_pm.models import Market, Signal, SignalStance, SourceType
from hermes_pm.signals.base import AdapterMeta, SignalAdapter, build_signal

_META = AdapterMeta(
    name="x_social",
    source_class=SourceType.SOCIAL,
    latency_class="delayed",  # FR-SOC-005: X Filtered Stream P99 ~ seconds [S13]
    update_frequency_s=15,
    source_authority="x_api_recent_search",
    reliability=0.30,  # social is the lowest-trust class
    licensing="x_developer_policy",
    suitable_for_realtime=False,
)


def _seed(*parts: str) -> int:
    return int(hashlib.sha256("|".join(parts).encode()).hexdigest()[:8], 16)


class XSocialAdapter(SignalAdapter):
    meta = _META

    def __init__(self, settings: Settings, transport=None) -> None:
        self._s = settings
        self._transport = transport  # optional httpx transport for contract tests

    async def fetch(self, market: Market, *, counter: bool = False) -> list[Signal]:
        if self._s.x_api_enabled and self._s.x_api_bearer_token:
            return await self._fetch_live(market, counter=counter)
        return self._fetch_offline(market, counter=counter)

    # -- live (official API only) ------------------------------------------ #
    async def _fetch_live(self, market: Market, *, counter: bool) -> list[Signal]:
        stance_word = "unlikely false" if counter else "likely true"
        query = f'"{market.question[:60]}" {stance_word} -is:retweet lang:en'
        headers = {"Authorization": f"Bearer {self._s.x_api_bearer_token}"}
        params = {"query": query, "max_results": 10,
                  "tweet.fields": "created_at,public_metrics,lang"}
        async with httpx.AsyncClient(timeout=10.0, transport=self._transport) as client:
            resp = await client.get(
                "https://api.x.com/2/tweets/search/recent", params=params, headers=headers
            )
            if resp.status_code == 429:  # respect rate limits; do not circumvent
                raise RateLimitedError("X API rate limited", retry_after=resp.headers.get("retry-after"))
            resp.raise_for_status()
            payload = resp.json()
        out: list[Signal] = []
        for post in payload.get("data", []):
            out.append(
                build_signal(
                    market.market_id, self.meta,
                    source_ref=f"x://tweet/{post.get('id')}",
                    raw_text=str(post.get("text", "")),
                    stance=SignalStance.BEARISH if counter else SignalStance.BULLISH,
                    confidence=0.4, novelty=0.5,
                )
            )
        return out

    # -- offline deterministic mode ---------------------------------------- #
    def _fetch_offline(self, market: Market, *, counter: bool) -> list[Signal]:
        rng = _seed(market.market_id, "counter" if counter else "main")
        n = 2 + (rng % 2)
        out: list[Signal] = []
        for i in range(n):
            bullish = (rng + i) % 2 == 0
            if counter:
                bullish = not bullish
            stance = SignalStance.BULLISH if bullish else SignalStance.BEARISH
            text = (
                f"[synthetic] Discussion on '{market.question}': commentators argue the outcome "
                f"is {'likely' if bullish else 'unlikely'} given recent developments."
            )
            out.append(
                build_signal(
                    market.market_id, self.meta,
                    source_ref=f"synthetic://x/{market.market_id}/{'c' if counter else 'm'}/{i}",
                    raw_text=text, stance=stance,
                    confidence=0.35, novelty=round(0.3 + 0.1 * i, 3),
                )
            )
        return out
