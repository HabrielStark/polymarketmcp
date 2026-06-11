"""Paper Trading Engine (FR-PAPER-001..007).

Simulates execution against the local hot order book:
  * marketable-limit orders walk the book as a taker on placement,
  * passive limit orders rest and fill only when a later snapshot trades through
    them (pessimistic queue assumption — FR-PAPER-006),
  * partial fills, slippage (via walking displayed levels), and cancellation are
    all modelled,
  * every fill records the exact snapshot used (FR-PAPER-005, replayable AC-004),
  * positions, cash, and realized/unrealized P&L are tracked through a
    double-entry ledger (FR-PAPER-004).

Paper mode is the default and only trading mode here; the engine refuses any
non-paper campaign (live execution is a separate, locked adapter)."""

from __future__ import annotations

import threading

from hermes_pm.audit.store import AuditStore
from hermes_pm.config import RiskPolicy
from hermes_pm.data.cache import OrderBookCache
from hermes_pm.errors import StateError, ValidationError
from hermes_pm.events import EventBus, EventType
from hermes_pm.execution.ledger import CASH, FEES, REALIZED, Ledger, Posting, position_account
from hermes_pm.models import (
    Campaign,
    CloseStatus,
    Fill,
    Mode,
    Order,
    OrderBookSnapshot,
    OrderStatus,
    OrderType,
    Position,
    RiskDecision,
    RiskResult,
    Side,
    TradeIntent,
)
from hermes_pm.persistence.db import Database
from hermes_pm.util.ids import idempotency_key
from hermes_pm.util.timeutil import now_iso


class PaperEngine:
    def __init__(
        self,
        db: Database,
        cache: OrderBookCache,
        bus: EventBus,
        audit: AuditStore,
        policy: RiskPolicy,
    ) -> None:
        self.db = db
        self.cache = cache
        self.bus = bus
        self.audit = audit
        self.policy = policy
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ #
    # Campaign cash lifecycle (persisted -> crash-recoverable, NFR-REL-002)
    # ------------------------------------------------------------------ #
    def _cash_key(self, cid: str) -> str:
        return f"paper_cash:{cid}"

    def _peak_key(self, cid: str) -> str:
        return f"paper_peak:{cid}"

    def init_campaign(self, campaign: Campaign) -> None:
        if self.db.kv_get(self._cash_key(campaign.campaign_id)) is None:
            # Opening balance is itself a money mutation: cash, peak and the
            # opening ledger posting must land together or not at all.
            with self.db.transaction():
                self.db.kv_set(self._cash_key(campaign.campaign_id), campaign.bankroll)
                self.db.kv_set(self._peak_key(campaign.campaign_id), campaign.bankroll)
                Ledger(self.db, campaign.campaign_id).post(
                    [Posting(CASH, campaign.bankroll, "opening balance"),
                     Posting("equity", -campaign.bankroll, "opening balance")]
                )

    def cash(self, cid: str) -> float:
        return float(self.db.kv_get(self._cash_key(cid), 0.0))

    # ------------------------------------------------------------------ #
    # Order placement
    # ------------------------------------------------------------------ #
    def place_order(
        self, campaign: Campaign, intent: TradeIntent, decision: RiskDecision
    ) -> Order:
        with self._lock:
            return self._place_order_locked(campaign, intent, decision)

    def _place_order_locked(
        self, campaign: Campaign, intent: TradeIntent, decision: RiskDecision
    ) -> Order:
        if campaign.mode is not Mode.PAPER:
            raise StateError("paper engine only operates in PAPER mode", code="state_error")
        if decision.result is RiskResult.REJECT or not decision.approved_size_usd:
            raise ValidationError(
                "cannot place order: risk decision is reject or zero size",
                code="risk_rejected", reasons=decision.violated_rules,
            )
        order = Order(
            mode=Mode.PAPER,
            campaign_id=campaign.campaign_id,
            intent_id=intent.intent_id,
            risk_decision_id=decision.decision_id,
            market_id=intent.market_id,
            token_id=intent.token_id,
            side=intent.side,
            order_type=intent.order_type,
            price=decision.approved_limit_price or intent.limit_price,
            size_usd=decision.approved_size_usd,
        )
        order.idempotency_key = idempotency_key(
            "order", intent.intent_id, decision.decision_id, order.size_usd, order.price
        )
        existing = self.db.save_order(order)
        if existing.order_id != order.order_id:
            return existing  # idempotent replay

        book = self.cache.get(order.token_id)
        if order.order_type is OrderType.MARKETABLE_LIMIT and book is not None:
            self._match(order, book)
        if order.status is OrderStatus.ACCEPTED:
            order.status = OrderStatus.OPEN if order.filled_size_usd < order.size_usd else order.status
        self._persist_order(order)
        self.bus.publish(
            EventType.ORDER_UPDATE,
            {"order_id": order.order_id, "status": order.status.value, "paper": True,
             "filled_usd": order.filled_size_usd, "size_usd": order.size_usd},
        )
        return order

    @staticmethod
    def simulate_fill(
        side: Side, price: float, size_usd: float, book: OrderBookSnapshot | None,
    ) -> dict:
        """Read-only projection of a marketable fill against ``book`` — no state
        change, no persistence (backs the simulate_trade_intent tool)."""
        if book is None:
            return {"fills": [], "filled_usd": 0.0, "avg_price": None, "shares": 0.0,
                    "would_rest_usd": round(size_usd, 6), "reason": "no_book"}
        levels = book.asks if side is Side.BUY else book.bids
        crosses = (lambda lp: lp <= price) if side is Side.BUY else (lambda lp: lp >= price)
        remaining, fills, shares = size_usd, [], 0.0
        for level in levels:
            if remaining <= 1e-9 or not crosses(level.price):
                break
            if level.price <= 0.0:
                continue
            fu = min(remaining, level.size)
            if fu <= 1e-9:
                continue
            fills.append({"price": level.price, "size_usd": round(fu, 6)})
            shares += fu / level.price
            remaining -= fu
        filled = round(size_usd - remaining, 6)
        # cost-per-share (matches Position.avg_price), not USD-weighted price
        avg = round(filled / shares, 6) if shares > 1e-12 else None
        return {"fills": fills, "filled_usd": filled, "avg_price": avg, "shares": round(shares, 6),
                "would_rest_usd": round(remaining, 6), "snapshot_id": book.snapshot_id,
                "mid": book.mid}

    def on_book_update(self, snapshot: OrderBookSnapshot) -> None:
        """Drive resting passive orders and mark-to-market on each new snapshot."""
        with self._lock:
            self._on_book_update_locked(snapshot)

    def _on_book_update_locked(self, snapshot: OrderBookSnapshot) -> None:
        for order in self._open_orders_for_token(snapshot.token_id):
            self._match(order, snapshot)
            self._persist_order(order)
            if order.fills:
                self.bus.publish(
                    EventType.ORDER_UPDATE,
                    {"order_id": order.order_id, "status": order.status.value, "paper": True,
                     "filled_usd": order.filled_size_usd, "size_usd": order.size_usd},
                )
        self._mark_token(snapshot)

    # ------------------------------------------------------------------ #
    # Fill matching (taker walk of displayed liquidity)
    # ------------------------------------------------------------------ #
    def _match(self, order: Order, book: OrderBookSnapshot) -> None:
        remaining = order.remaining_usd
        if remaining <= 1e-9:
            return
        levels = book.asks if order.side is Side.BUY else book.bids
        crosses = (lambda lp: lp <= order.price) if order.side is Side.BUY else (lambda lp: lp >= order.price)
        for level in levels:
            if remaining <= 1e-9 or not crosses(level.price):
                break
            if level.price <= 0.0:
                continue  # a 0-price level is non-economic; never "fill" against it
            fill_usd = min(remaining, level.size)
            if fill_usd <= 1e-9:
                continue
            self._apply_fill(order, level.price, fill_usd, book.snapshot_id,
                             reason=f"taker@{level.price}")
            remaining = order.remaining_usd
        order.status = (
            OrderStatus.FILLED
            if order.remaining_usd <= 1e-9
            else (OrderStatus.PARTIALLY_FILLED if order.filled_size_usd > 0 else order.status)
        )
        order.updated_at = now_iso()

    def _apply_fill(
        self, order: Order, price: float, fill_usd: float, snapshot_id: str, reason: str
    ) -> None:
        shares = round(fill_usd / price, 6) if price > 0 else 0.0
        signed = shares if order.side is Side.BUY else -shares
        fee = round(fill_usd * self.policy.fee_bps / 10_000.0, 6)
        fill = Fill(
            order_id=order.order_id, price=price, size_usd=round(fill_usd, 6), shares=shares,
            simulated_or_real="simulated", liquidity_source=reason, snapshot_id=snapshot_id,
            reason=reason,
        )

        # ---- ATOMIC money mutation (FR-PAPER-004) ----------------------------
        # position + cash + the 4 balanced ledger postings + fill record + the
        # order's running fill-state all commit together, or not at all. A failure
        # anywhere (e.g. Ledger.post rejecting an unbalanced set) rolls the whole
        # thing back, so cash, ledger and position can never diverge. The live
        # ``order`` object and audit/events are only touched AFTER the commit, so a
        # rollback leaves both the database and in-memory state consistent.
        with self.db.transaction():
            pos = self.db.get_position(order.campaign_id, order.token_id) or Position(
                campaign_id=order.campaign_id, market_id=order.market_id,
                token_id=order.token_id, outcome="",
            )
            basis_before = pos.shares * pos.avg_price
            self._apply_position(pos, signed, price)
            basis_after = pos.shares * pos.avg_price
            pos.close_status = CloseStatus.CLOSED if abs(pos.shares) < 1e-9 else CloseStatus.OPEN
            self.db.upsert_position(pos)

            # Round each known leg to 6 decimals, then make REALIZED the balancing
            # plug so the transaction sums to exactly zero regardless of rounding.
            cash6 = round(-signed * price - fee, 6)
            basis6 = round(basis_after - basis_before, 6)
            fee6 = round(fee, 6)
            realized6 = round(-(cash6 + basis6 + fee6), 6)
            self.db.kv_add(self._cash_key(order.campaign_id), cash6, default=0.0)
            Ledger(self.db, order.campaign_id).post(
                [
                    Posting(CASH, cash6, reason),
                    Posting(position_account(order.token_id), basis6, reason),
                    Posting(FEES, fee6, "fee"),
                    Posting(REALIZED, realized6, "realized"),
                ]
            )

            self.db.save_fill(fill)
            new_filled = round(order.filled_size_usd + fill_usd, 6)
            # Persist the order's running fill state in the SAME unit of work, via a
            # copy, so the live object stays untouched until the commit succeeds.
            self.db.save_order(order.model_copy(update={
                "fills": [*order.fills, fill], "filled_size_usd": new_filled,
            }))

        # ---- commit succeeded: reflect in live state + emit observation ------
        order.fills.append(fill)
        order.filled_size_usd = new_filled
        self.audit.append(
            EventType.FILL, actor="paper_engine", summary=f"paper fill {shares}@{price}",
            outputs=fill.model_dump(mode="json"), campaign_id=order.campaign_id,
            references={"order_id": order.order_id, "snapshot_id": snapshot_id,
                        "intent_id": order.intent_id},
        )
        self.bus.publish(
            EventType.FILL,
            {"fill_id": fill.fill_id, "order_id": order.order_id, "price": price,
             "shares": shares, "size_usd": fill.size_usd, "paper": True, "snapshot_id": snapshot_id},
        )
        self.bus.publish(
            EventType.POSITION_UPDATE,
            {"token_id": order.token_id, "shares": pos.shares, "avg_price": pos.avg_price,
             "realized_pnl": pos.realized_pnl, "paper": True},
        )

    @staticmethod
    def _apply_position(pos: Position, dq: float, price: float) -> float:
        """Weighted-average accounting; returns realized P&L from this fill."""
        s, a = pos.shares, pos.avg_price
        realized = 0.0
        if s == 0 or (s > 0) == (dq > 0):  # open / increase same direction
            new_s = s + dq
            pos.avg_price = (abs(s) * a + abs(dq) * price) / abs(new_s) if abs(new_s) > 1e-12 else 0.0
            pos.shares = round(new_s, 6)
        else:  # reduce / close / flip
            closing = min(abs(dq), abs(s))
            realized = (price - a) * closing if s > 0 else (a - price) * closing
            new_s = round(s + dq, 6)
            if abs(new_s) < 1e-9:
                pos.avg_price = 0.0
                pos.shares = 0.0
            elif (s > 0) != (new_s > 0):  # flipped through zero
                pos.avg_price = price
                pos.shares = new_s
            else:  # still same side, just smaller
                pos.shares = new_s
        pos.realized_pnl = round(pos.realized_pnl + realized, 6)
        return round(realized, 6)

    # ------------------------------------------------------------------ #
    # Mark-to-market / portfolio (FR-PAPER-004)
    # ------------------------------------------------------------------ #
    def _mark_token(self, snapshot: OrderBookSnapshot) -> None:
        for pos in self.db.list_positions_for_token(snapshot.token_id):
            self._mark_position(pos, snapshot.mid)

    def _mark_position(self, pos: Position, mark: float | None) -> None:
        if mark is None:
            return
        pos.mark_price = mark
        pos.unrealized_pnl = round(pos.shares * (mark - pos.avg_price), 6)
        self.db.upsert_position(pos)

    def mark_to_market(self, campaign_id: str) -> None:
        for pos in self.db.list_positions(campaign_id):
            book = self.cache.get(pos.token_id)
            if book is not None:
                self._mark_position(pos, book.mid)

    def portfolio(self, campaign_id: str, bankroll: float) -> dict:
        self.mark_to_market(campaign_id)
        positions = self.db.list_positions(campaign_id)
        cash = self.cash(campaign_id)
        position_value = sum(
            p.shares * (p.mark_price if p.mark_price is not None else p.avg_price)
            for p in positions
        )
        equity = round(cash + position_value, 6)
        # Realized P&L is taken from the authoritative ledger (always balanced),
        # not the per-position approximation. The REALIZED account accumulates the
        # signed balancing plug, so realized = -(its balance).
        led = Ledger(self.db, campaign_id)
        realized = round(-led.balances().get(REALIZED, 0.0), 6)
        unrealized = round(sum(p.unrealized_pnl for p in positions), 6)
        peak = max(float(self.db.kv_get(self._peak_key(campaign_id), bankroll)), equity)
        self.db.kv_set(self._peak_key(campaign_id), peak)
        drawdown = round(peak - equity, 6)
        return {
            "paper": True,
            "cash": round(cash, 6),
            "position_value": round(position_value, 6),
            "equity": equity,
            "net_pnl": round(equity - bankroll, 6),
            "realized_pnl": realized,
            "unrealized_pnl": unrealized,
            "peak_equity": round(peak, 6),
            "max_drawdown": max(0.0, drawdown),
            "open_positions": [p.model_dump(mode="json") for p in positions if abs(p.shares) > 1e-9],
            "ledger_balanced": led.is_balanced(),
        }

    # ------------------------------------------------------------------ #
    # Order management
    # ------------------------------------------------------------------ #
    def cancel_order(self, order_id: str) -> Order:
        with self._lock:
            order = self.db.get_order(order_id)
            if order is None:
                raise ValidationError(f"order not found: {order_id}", code="not_found")
            if order.status in (OrderStatus.FILLED, OrderStatus.CANCELLED):
                return order
            order.status = OrderStatus.CANCELLED
            order.updated_at = now_iso()
            self.db.save_order(order)
            self.bus.publish(
                EventType.ORDER_UPDATE,
                {"order_id": order.order_id, "status": order.status.value, "paper": True},
            )
            return order

    def cancel_all(self, campaign_id: str) -> int:
        n = 0
        for order in self._open_orders(campaign_id):
            self.cancel_order(order.order_id)
            n += 1
        return n

    def _persist_order(self, order: Order) -> None:
        self.db.save_order(order)

    def _open_orders(self, campaign_id: str) -> list[Order]:
        return [
            o for o in self.db.list_orders(campaign_id)
            if o.status in (OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED, OrderStatus.ACCEPTED)
        ]

    def _open_orders_for_token(self, token_id: str) -> list[Order]:
        rows = self.db.query(
            "SELECT data FROM orders WHERE status IN ('open','partially_filled','accepted')"
        )
        out = []
        for r in rows:
            o = Order.model_validate_json(r["data"])
            if o.token_id == token_id:
                out.append(o)
        return out
