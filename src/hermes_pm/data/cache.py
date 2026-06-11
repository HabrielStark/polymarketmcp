"""Hot in-memory order-book cache (FR-DATA-002).

Holds the latest snapshot per token for sub-millisecond reads on the Fast Lane.
Exposes staleness flags to the agent and risk engine (FR-DATA-004): a book is
stale if it is older than its ``stale_after_ms`` budget, OR if upstream
connectivity has been lost beyond the configured threshold, in which case *all*
books are forced stale (NFR-REL-003). No order may be approved against a stale
book."""

from __future__ import annotations

import threading

from hermes_pm.models import OrderBookSnapshot, Side
from hermes_pm.util.timeutil import now_ms


class OrderBookCache:
    def __init__(self, default_stale_after_ms: int = 5_000) -> None:
        self._books: dict[str, OrderBookSnapshot] = {}
        self._stale_after: dict[str, int] = {}
        self._default_stale_after_ms = default_stale_after_ms
        self._lock = threading.RLock()
        self._connectivity_lost: bool = False
        self._last_message_ms: int = now_ms()

    def update(self, snapshot: OrderBookSnapshot, stale_after_ms: int | None = None) -> None:
        with self._lock:
            self._books[snapshot.token_id] = snapshot
            self._stale_after[snapshot.token_id] = stale_after_ms or self._default_stale_after_ms
            self._last_message_ms = now_ms()
            self._connectivity_lost = False

    def get(self, token_id: str) -> OrderBookSnapshot | None:
        with self._lock:
            return self._books.get(token_id)

    def tokens(self) -> list[str]:
        with self._lock:
            return list(self._books.keys())

    def stale_budget(self, token_id: str) -> int:
        return self._stale_after.get(token_id, self._default_stale_after_ms)

    def is_stale(self, token_id: str, now: int | None = None) -> bool:
        with self._lock:
            if self._connectivity_lost:
                return True
            book = self._books.get(token_id)
            if book is None:
                return True
            return book.is_stale(self.stale_budget(token_id), now)

    def age_ms(self, token_id: str, now: int | None = None) -> int:
        with self._lock:
            book = self._books.get(token_id)
            if book is None:
                return 2**31
            now = now if now is not None else now_ms()
            return max(0, now - book.received_at)

    def best(self, token_id: str, side: Side) -> float | None:
        book = self.get(token_id)
        if book is None:
            return None
        return book.best_ask if side is Side.BUY else book.best_bid

    def set_connectivity_lost(self, lost: bool) -> None:
        """Force/clear global staleness on WebSocket loss/restore (NFR-REL-003)."""
        with self._lock:
            self._connectivity_lost = lost

    @property
    def connectivity_lost(self) -> bool:
        return self._connectivity_lost

    def sweep_stale(self, now: int | None = None) -> list[str]:
        """Return token_ids currently stale (for dashboard/metrics)."""
        return [t for t in self.tokens() if self.is_stale(t, now)]
