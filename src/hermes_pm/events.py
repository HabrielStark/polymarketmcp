"""In-process asynchronous event bus (Data/Execution/Observation lanes glue).

Publishers (market data, risk, paper engine, campaign manager) emit events;
subscribers (dashboard websocket, metrics, audit mirrors) consume them. The bus
never blocks a publisher: each subscriber has a bounded queue and the oldest
event is dropped on overflow, so the Fast Lane is never stalled by a slow
consumer (NFR-LAT-005/-007)."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

from hermes_pm.util.timeutil import now_ms


class EventType:
    """Canonical event-type tokens used across lanes."""

    SYSTEM_STATUS = "system_status"
    MODE_CHANGED = "mode_changed"
    EMERGENCY_STOP = "emergency_stop"
    MARKET_DISCOVERED = "market_discovered"
    MARKET_DATA = "market_data"
    BOOK_STALE = "book_stale"
    CONNECTIVITY = "connectivity"
    SIGNAL = "signal"
    INTENT_CREATED = "intent_created"
    RISK_DECISION = "risk_decision"
    ORDER_UPDATE = "order_update"
    FILL = "fill"
    POSITION_UPDATE = "position_update"
    CAMPAIGN_UPDATE = "campaign_update"
    LESSON = "lesson"
    POSTMORTEM = "postmortem"
    AUDIT = "audit"


@dataclass(frozen=True)
class Event:
    type: str
    data: dict[str, Any]
    ts: int


class EventBus:
    def __init__(self, queue_size: int = 2048) -> None:
        self._subscribers: set[asyncio.Queue[Event]] = set()
        self._sync_listeners: list[Callable[[Event], None]] = []
        self._queue_size = queue_size
        self._dropped = 0

    def publish(self, type: str, data: dict[str, Any]) -> None:
        event = Event(type=type, data=data, ts=now_ms())
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()  # drop oldest, keep newest
                    self._dropped += 1
                with contextlib.suppress(asyncio.QueueFull):
                    q.put_nowait(event)
        for fn in self._sync_listeners:
            with contextlib.suppress(Exception):
                fn(event)

    def add_listener(self, fn: Callable[[Event], None]) -> None:
        self._sync_listeners.append(fn)

    @contextlib.contextmanager
    def subscription(self) -> Any:
        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers.add(q)
        try:
            yield q
        finally:
            self._subscribers.discard(q)

    async def stream(self) -> AsyncIterator[Event]:
        with self.subscription() as q:
            while True:
                yield await q.get()

    @property
    def dropped(self) -> int:
        return self._dropped

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)
