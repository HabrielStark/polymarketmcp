"""Market-data sources behind a single interface so the engine is agnostic to
whether data is synthetic (deterministic/offline, default), replayed from a
recording, or live from Polymarket.

  * ``SyntheticSource`` — seeded, deterministic markets + evolving books. Lets
    the whole system run and be tested fully offline and reproducibly.
  * ``ReplaySource`` — replays a recorded JSONL stream (FR-DATA-005, AC-004).
  * ``PolymarketSource`` lives in ``polymarket_client`` (live; opt-in)."""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Protocol

from hermes_pm.models import BookLevel, Market, OrderBookSnapshot
from hermes_pm.util.timeutil import now_ms

_CATEGORIES = ["weather", "sports"]


class MarketDataSource(Protocol):
    name: str

    async def discover_markets(self) -> list[Market]: ...
    async def snapshot(self, token_id: str) -> OrderBookSnapshot | None: ...
    def stream(self, token_ids: list[str], interval_ms: int) -> AsyncIterator[OrderBookSnapshot]: ...
    async def close(self) -> None: ...


class SyntheticSource:
    """Deterministic synthetic market universe and order-book generator."""

    name = "synthetic"

    def __init__(self, seed: int = 1337, market_count: int = 6, base_size: float = 300.0) -> None:
        # Deterministic, NON-cryptographic RNG: this only generates synthetic test
        # market prices and must be reproducible from a seed.
        self._rng = random.Random(seed)  # noqa: S311
        self._market_count = market_count
        self._base_size = base_size
        self._markets: list[Market] = []
        self._p: dict[str, float] = {}  # token_id -> true probability
        self._seq: dict[str, int] = {}
        self._yes_of_market: dict[str, str] = {}  # market_id -> yes token
        self._no_of_market: dict[str, str] = {}
        self._build_markets()

    def _build_markets(self) -> None:
        for i in range(self._market_count):
            cat = _CATEGORIES[i % len(_CATEGORIES)]
            mid = f"mkt-{i:04d}"
            yes, no = f"tok-{i}-yes", f"tok-{i}-no"
            # Deliberately make one market non-order-book and one ambiguous to
            # exercise FR-MD-002 / FR-MD-004 discovery filters.
            enable_ob = not (i == self._market_count - 1)
            rules = "" if i == self._market_count - 2 else (
                f"Resolves YES if the official {cat} outcome for event {i} is confirmed by the "
                f"designated authority before the end date; otherwise NO."
            )
            p0 = round(self._rng.uniform(0.25, 0.75), 2)
            self._p[yes] = p0
            self._p[no] = round(1 - p0, 2)
            self._seq[yes] = self._seq[no] = 0
            self._yes_of_market[mid] = yes
            self._no_of_market[mid] = no
            self._markets.append(
                Market(
                    market_id=mid,
                    event_id=f"evt-{i:04d}",
                    condition_id=f"cond-{i:04d}",
                    question_id=f"q-{i:04d}",
                    question=f"Will synthetic {cat} event #{i} resolve YES?",
                    category=cat,
                    outcomes=["YES", "NO"],
                    token_ids={"YES": yes, "NO": no},
                    resolution_rules=rules,
                    resolution_source=("" if not rules else f"official-{cat}-authority"),
                    source_links=([] if not rules else [f"https://example.org/{cat}/{i}"]),
                    end_time="2026-12-31T23:59:59Z",
                    enable_order_book=enable_ob,
                    tags=[cat, "synthetic", "liquid" if enable_ob else "illiquid"],
                )
            )

    async def discover_markets(self) -> list[Market]:
        return list(self._markets)

    def _build_book(self, token_id: str) -> OrderBookSnapshot:
        p = self._p.get(token_id, 0.5)
        seq = self._seq.get(token_id, 0)
        tick = 0.01
        bb = round(max(tick, p - 0.01), 2)
        ba = round(min(1 - tick, p + 0.01), 2)
        if ba <= bb:
            ba = round(min(1 - tick, bb + tick), 2)
        bids = [
            BookLevel(price=round(bb - i * tick, 2), size=round(self._base_size * (1 + i * 0.3), 2))
            for i in range(5)
            if (bb - i * tick) > 0
        ]
        asks = [
            BookLevel(price=round(ba + i * tick, 2), size=round(self._base_size * (1 + i * 0.3), 2))
            for i in range(5)
            if (ba + i * tick) < 1
        ]
        return OrderBookSnapshot(
            token_id=token_id, bids=bids, asks=asks, last_trade=p, sequence=seq,
            source=self.name, received_at=now_ms(),
        )

    async def snapshot(self, token_id: str) -> OrderBookSnapshot | None:
        if token_id not in self._p:
            return None
        return self._build_book(token_id)

    def _advance(self) -> None:
        for mid, yes in self._yes_of_market.items():
            step = self._rng.uniform(-0.02, 0.02)
            new_p = min(0.97, max(0.03, round(self._p[yes] + step, 2)))
            self._p[yes] = new_p
            self._p[self._no_of_market[mid]] = round(1 - new_p, 2)
            self._seq[yes] += 1
            self._seq[self._no_of_market[mid]] += 1

    async def stream(
        self, token_ids: list[str], interval_ms: int
    ) -> AsyncIterator[OrderBookSnapshot]:
        while True:
            self._advance()
            for tid in token_ids:
                if tid in self._p:
                    yield self._build_book(tid)
            await asyncio.sleep(max(0.0, interval_ms / 1000))

    def record(self, token_ids: list[str], steps: int) -> list[OrderBookSnapshot]:
        """Deterministic, time-independent snapshot sequence for recording/replay."""
        out: list[OrderBookSnapshot] = []
        for _ in range(steps):
            self._advance()
            out.extend(self._build_book(t) for t in token_ids if t in self._p)
        return out

    async def close(self) -> None:  # pragma: no cover - nothing to release
        return None


class ReplaySource:
    """Replays a recorded session: a JSON file with ``markets`` and ``snapshots``."""

    name = "replay"

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        data = json.loads(self._path.read_text(encoding="utf-8"))
        self._markets = [Market.model_validate(m) for m in data.get("markets", [])]
        self._snapshots = [OrderBookSnapshot.model_validate(s) for s in data.get("snapshots", [])]
        self._latest: dict[str, OrderBookSnapshot] = {}
        for s in self._snapshots:
            self._latest[s.token_id] = s

    async def discover_markets(self) -> list[Market]:
        return list(self._markets)

    async def snapshot(self, token_id: str) -> OrderBookSnapshot | None:
        return self._latest.get(token_id)

    async def stream(
        self, token_ids: list[str], interval_ms: int
    ) -> AsyncIterator[OrderBookSnapshot]:
        wanted = set(token_ids)
        for s in self._snapshots:
            if s.token_id in wanted:
                yield s.model_copy(update={"received_at": now_ms()})
                await asyncio.sleep(max(0.0, interval_ms / 1000))

    async def close(self) -> None:  # pragma: no cover
        return None

    @staticmethod
    def write_recording(
        path: str | Path, markets: list[Market], snapshots: list[OrderBookSnapshot]
    ) -> None:
        Path(path).write_text(
            json.dumps(
                {
                    "markets": [m.model_dump(mode="json") for m in markets],
                    "snapshots": [s.model_dump(mode="json") for s in snapshots],
                }
            ),
            encoding="utf-8",
        )
