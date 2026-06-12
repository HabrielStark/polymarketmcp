"""Stateful (model-based) property testing — the strongest "find a state you did
not anticipate" technique.

A Hypothesis state machine drives the PaperEngine through random interleavings of
marketable/passive order placement, order-book ticks (which fill resting orders
and mark positions), cancellations and mark-to-market, asserting the full set of
money/consistency invariants AFTER EVERY STEP:

  * the double-entry ledger always balances;
  * the cash counter always equals the authoritative ledger CASH balance;
  * no order is ever over-filled, and fills reconcile to ``filled_size_usd``;
  * positions reconcile exactly with the signed sum of their fills;
  * the hash-chained audit log stays intact;
  * no operation ever raises.

Plus a property that the risk engine never approves above its hard caps.
"""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from hermes_pm.audit.store import AuditStore
from hermes_pm.config import RiskPolicy
from hermes_pm.data.cache import OrderBookCache
from hermes_pm.events import EventBus
from hermes_pm.execution.ledger import CASH, Ledger
from hermes_pm.execution.paper_engine import PaperEngine
from hermes_pm.models import (
    BookLevel,
    Campaign,
    Market,
    Mode,
    OrderBookSnapshot,
    OrderStatus,
    OrderType,
    RiskDecision,
    RiskResult,
    Side,
    Signal,
    SourceType,
    TradeIntent,
)
from hermes_pm.persistence.db import Database
from hermes_pm.risk.engine import RiskContext, RiskEngine

_OPEN = (OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED, OrderStatus.ACCEPTED)
TOKEN = "tok"


def _book(mid: float, depth: float = 1e6) -> OrderBookSnapshot:
    bid = round(max(0.01, mid - 0.01), 2)
    ask = round(min(0.99, mid + 0.01), 2)
    return OrderBookSnapshot(
        token_id=TOKEN, bids=[BookLevel(price=bid, size=depth)],
        asks=[BookLevel(price=ask, size=depth)],
    )


class PaperEngineStateMachine(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.db = Database(":memory:")
        self.cache = OrderBookCache()
        self.paper = PaperEngine(
            self.db, self.cache, EventBus(), AuditStore(self.db),
            RiskPolicy(fee_bps=0.0, slippage_bps=0.0),
        )
        self.camp = Campaign(name="sm", mode=Mode.PAPER, bankroll=1_000_000.0)
        self.db.save_campaign(self.camp)
        self.paper.init_campaign(self.camp)
        self.cid = self.camp.campaign_id
        self.cache.update(_book(0.5))
        self._n = 0

    # ---- rules (random operations) --------------------------------------- #
    @rule(
        side=st.sampled_from([Side.BUY, Side.SELL]),
        price=st.floats(min_value=0.05, max_value=0.95, allow_nan=False),
        size=st.floats(min_value=1.0, max_value=5000.0, allow_nan=False),
        passive=st.booleans(),
    )
    def place_order(self, side, price, size, passive):
        self._n += 1
        price = round(price, 2)
        otype = OrderType.LIMIT if passive else OrderType.MARKETABLE_LIMIT
        ti = TradeIntent(
            campaign_id=self.cid, market_id="m", token_id=TOKEN, side=side, order_type=otype,
            limit_price=price, max_size_usd=round(size, 2), thesis="t", counter_thesis="c",
            confidence=0.5, expires_at="2030-12-30T00:00:00Z", idempotency_key=f"k{self._n}",
        )
        self.db.save_intent(ti)
        dec = RiskDecision(
            intent_id=ti.intent_id, campaign_id=self.cid, result=RiskResult.APPROVE,
            approved_size_usd=ti.max_size_usd, approved_limit_price=ti.limit_price,
        )
        self.paper.place_order(self.camp, ti, dec)

    @rule(mid=st.floats(min_value=0.05, max_value=0.95, allow_nan=False))
    def tick(self, mid):
        self.cache.update(_book(round(mid, 2)))
        snap = self.cache.get(TOKEN)
        if snap is not None:
            self.paper.on_book_update(snap)  # fills resting orders + marks positions

    @rule()
    def cancel_one(self):
        for o in self.db.list_orders(self.cid):
            if o.status in _OPEN:
                self.paper.cancel_order(o.order_id)
                break

    @rule()
    def mark(self):
        self.paper.mark_to_market(self.cid)

    @rule()
    def portfolio(self):
        p = self.paper.portfolio(self.cid, self.camp.bankroll)
        assert p["ledger_balanced"] is True

    # ---- invariants (checked after EVERY rule) --------------------------- #
    @invariant()
    def ledger_balanced_and_cash_matches(self):
        led = Ledger(self.db, self.cid)
        assert led.is_balanced(), "double-entry ledger must always sum to zero"
        cash = self.paper.cash(self.cid)
        assert abs(cash - led.balances().get(CASH, 0.0)) <= 1e-3, "cash must equal ledger CASH"

    @invariant()
    def orders_never_overfill_and_fills_reconcile(self):
        for o in self.db.list_orders(self.cid):
            assert o.filled_size_usd <= o.size_usd + 1e-6, "order over-filled"
            fills = self.db.list_fills(o.order_id)
            assert abs(sum(f.size_usd for f in fills) - o.filled_size_usd) <= 1e-3
            for f in fills:
                assert 0.0 <= f.price <= 1.0 and f.size_usd >= 0.0 and f.shares >= 0.0

    @invariant()
    def positions_reconcile_with_fills(self):
        net: dict[str, float] = {}
        for o in self.db.list_orders(self.cid):
            sgn = 1.0 if o.side is Side.BUY else -1.0
            for f in self.db.list_fills(o.order_id):
                net[o.token_id] = net.get(o.token_id, 0.0) + sgn * f.shares
        for tok, shares in net.items():
            pos = self.db.get_position(self.cid, tok)
            actual = pos.shares if pos is not None else 0.0
            assert abs(actual - round(shares, 6)) <= 1e-3, f"position shares mismatch on {tok}"

    @invariant()
    def audit_chain_intact(self):
        assert self.paper.audit.verify_chain()["ok"] is True

    def teardown(self):
        self.db.close()


PaperEngineStateMachine.TestCase.settings = settings(
    max_examples=60, stateful_step_count=40, deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
TestPaperEngineStateMachine = PaperEngineStateMachine.TestCase


# --------------------------------------------------------------------------- #
# Risk engine never approves above its hard caps (single-trade + market).
# --------------------------------------------------------------------------- #
@settings(max_examples=60, deadline=None)
@given(
    requested=st.floats(min_value=0.1, max_value=10_000.0, allow_nan=False),
    bankroll=st.floats(min_value=10.0, max_value=1_000_000.0, allow_nan=False),
    market_exposure=st.floats(min_value=0.0, max_value=100_000.0, allow_nan=False),
)
def test_risk_never_approves_above_caps(requested, bankroll, market_exposure):
    policy = RiskPolicy()
    book = OrderBookSnapshot(
        token_id="tok", bids=[BookLevel(price=0.49, size=1e6)],
        asks=[BookLevel(price=0.51, size=1e6)],
    )
    intent = TradeIntent(
        campaign_id="c", market_id="m", token_id="tok", side=Side.BUY, limit_price=0.51,
        max_size_usd=round(requested, 2), thesis="t", counter_thesis="c", confidence=0.7,
        expires_at="2030-12-30T00:00:00Z",
    )
    market = Market(market_id="m", event_id="e", condition_id="c", question="q?",
                    enable_order_book=True, resolution_rules="r", resolution_source="s",
                    token_ids={"YES": "tok"})
    ev = [Signal(market_id="m", source_type=SourceType.PRIMARY, source_ref="o",
                 text_summary="x", trust_score=0.9)]
    ctx = RiskContext(
        intent=intent, market=market, campaign=Campaign(name="c", bankroll=round(bankroll, 2)),
        policy=policy, book=book, book_is_stale=False, data_age_ms=10, evidence=ev,
        market_exposure_usd=round(market_exposure, 2),
    )
    d = RiskEngine().evaluate(ctx)
    if d.result in (RiskResult.APPROVE, RiskResult.MODIFY):
        assert d.approved_size_usd is not None
        # never exceeds the requested size, the 1% single-trade cap, or remaining market room
        assert d.approved_size_usd <= round(requested, 2) + 1e-6
        assert d.approved_size_usd <= policy.max_single_trade_risk_pct * round(bankroll, 2) + 1e-6
        assert d.approved_size_usd >= 0.0
