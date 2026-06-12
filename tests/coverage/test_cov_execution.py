"""Targeted coverage for the execution layer (paper engine, intents, live adapter,
secret stores, isolated live process).

Every test exercises a specific previously-uncovered branch and asserts the
observable behaviour of that branch — no import-only or no-op tests. Source under
``src/`` is treated as frozen; these tests only drive it.
"""

from __future__ import annotations

import io
import sys
import types

import pytest

from hermes_pm.audit.store import AuditStore
from hermes_pm.config import RiskPolicy, load_settings
from hermes_pm.errors import NotFoundError, StateError, ValidationError
from hermes_pm.events import EventType
from hermes_pm.execution import live_process
from hermes_pm.execution.intents import IntentService
from hermes_pm.execution.live_adapter import ComplianceGate, LiveAdapter, SigningVault
from hermes_pm.execution.paper_engine import PaperEngine
from hermes_pm.execution.secrets import (
    EncryptedFileSecretStore,
    EnvSecretStore,
    KeyringSecretStore,
    make_secret_store,
)
from hermes_pm.models import (
    BookLevel,
    Campaign,
    Market,
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


# --------------------------------------------------------------------------- #
# Shared builders
# --------------------------------------------------------------------------- #
def _paper_campaign(paper_engine, db, bankroll: float = 1000.0) -> Campaign:
    camp = Campaign(name="c", mode=Mode.PAPER, bankroll=bankroll)
    db.save_campaign(camp)
    paper_engine.init_campaign(camp)
    return camp


def _intent(cid, *, side=Side.BUY, price=0.51, size=60.0,
            ot=OrderType.MARKETABLE_LIMIT, token="tok") -> TradeIntent:
    return TradeIntent(
        campaign_id=cid, market_id="m", token_id=token, outcome="YES", side=side,
        order_type=ot, limit_price=price, max_size_usd=size, thesis="t",
        counter_thesis="c", confidence=0.6, expires_at="2026-12-30T00:00:00Z",
    )


def _approve(ti: TradeIntent, *, size=None, price=None) -> RiskDecision:
    return RiskDecision(
        intent_id=ti.intent_id, campaign_id=ti.campaign_id, result=RiskResult.APPROVE,
        approved_size_usd=size if size is not None else ti.max_size_usd,
        approved_limit_price=price if price is not None else ti.limit_price,
    )


def _market(**kw) -> Market:
    base = dict(market_id="m", event_id="e", condition_id="c", question="q?",
                resolution_rules="r", resolution_source="s", token_ids={"YES": "tok"},
                enable_order_book=True)
    base.update(kw)
    return Market(**base)


# =========================================================================== #
# paper_engine.py
# =========================================================================== #
def test_place_order_non_paper_campaign_raises_state_error(paper_engine, db):
    """line 99: paper engine refuses any non-PAPER campaign."""
    camp = Campaign(name="c", mode=Mode.RESEARCH, bankroll=1000.0)
    ti = _intent(camp.campaign_id)
    with pytest.raises(StateError):
        paper_engine.place_order(camp, ti, _approve(ti))


def test_simulate_fill_no_book_returns_no_book_reason():
    """line 144: simulate_fill against a missing book rests the whole size."""
    out = PaperEngine.simulate_fill(Side.BUY, 0.5, 25.0, None)
    assert out["reason"] == "no_book"
    assert out["filled_usd"] == 0.0
    assert out["avg_price"] is None
    assert out["would_rest_usd"] == pytest.approx(25.0)
    assert out["shares"] == 0.0


def test_simulate_fill_skips_zero_price_and_zero_size_levels():
    """line 156 (and the zero-price guard): non-economic levels are skipped."""
    book = OrderBookSnapshot(
        token_id="tok",
        bids=[BookLevel(price=0.30, size=100.0)],
        asks=[BookLevel(price=0.0, size=100.0),   # zero price -> skipped
              BookLevel(price=0.40, size=0.0),     # zero size  -> skipped (line 156)
              BookLevel(price=0.45, size=80.0)],   # the only economically-fillable level
    )
    out = PaperEngine.simulate_fill(Side.BUY, 0.50, 30.0, book)
    assert out["filled_usd"] == pytest.approx(30.0)
    assert [f["price"] for f in out["fills"]] == [0.45]
    assert out["avg_price"] == pytest.approx(0.45)


def test_on_book_update_fills_resting_order_and_publishes(paper_engine, db):
    """lines 174-177 + _open_orders_for_token 407-409: a resting passive order
    fills when a later snapshot trades through it, and an ORDER_UPDATE is published."""
    camp = _paper_campaign(paper_engine, db)
    # best ask 0.60 -> a BUY @0.50 does NOT cross -> the order rests OPEN.
    paper_engine.cache.update(OrderBookSnapshot(
        token_id="tok", bids=[BookLevel(price=0.49, size=500)],
        asks=[BookLevel(price=0.60, size=500)]))
    ti = _intent(camp.campaign_id, price=0.50, size=20.0)
    db.save_intent(ti)
    order = paper_engine.place_order(camp, ti, _approve(ti))
    assert order.status is OrderStatus.OPEN
    assert order.filled_size_usd == 0.0

    events = []
    paper_engine.bus.add_listener(events.append)
    # New snapshot: ask drops to 0.48 and trades through the resting BUY @0.50.
    paper_engine.on_book_update(OrderBookSnapshot(
        token_id="tok", bids=[BookLevel(price=0.49, size=500)],
        asks=[BookLevel(price=0.48, size=500)]))

    filled = db.get_order(order.order_id)
    assert filled.status is OrderStatus.FILLED
    assert filled.filled_size_usd == pytest.approx(20.0)
    assert any(e.type == EventType.ORDER_UPDATE for e in events)


def test_match_noop_when_order_already_filled(paper_engine):
    """line 190: _match returns immediately when nothing remains to fill."""
    order = Order(campaign_id="c", intent_id="i", risk_decision_id="d", market_id="m",
                  token_id="tok", side=Side.BUY, order_type=OrderType.MARKETABLE_LIMIT,
                  price=0.5, size_usd=10.0, filled_size_usd=10.0)
    book = OrderBookSnapshot(token_id="tok", asks=[BookLevel(price=0.40, size=100)])
    paper_engine._match(order, book)
    assert order.fills == []


def test_match_skips_zero_price_and_zero_size_levels(paper_engine, db):
    """line 200 (and zero-price guard): _match walks past non-economic levels."""
    camp = _paper_campaign(paper_engine, db)
    order = Order(campaign_id=camp.campaign_id, intent_id="i", risk_decision_id="d",
                  market_id="m", token_id="tok", side=Side.BUY,
                  order_type=OrderType.MARKETABLE_LIMIT, price=0.50, size_usd=20.0)
    db.save_order(order)
    book = OrderBookSnapshot(
        token_id="tok",
        asks=[BookLevel(price=0.0, size=100.0),    # zero price -> skip
              BookLevel(price=0.40, size=0.0),      # zero size  -> skip (line 200)
              BookLevel(price=0.45, size=100.0)])   # fill here
    paper_engine._match(order, book)
    assert order.filled_size_usd == pytest.approx(20.0)
    assert order.status is OrderStatus.FILLED


def test_cancel_order_not_found_raises(paper_engine):
    """line 318: cancelling an unknown order id raises a not-found ValidationError."""
    with pytest.raises(ValidationError):
        paper_engine.cancel_order("does-not-exist")


def test_mark_position_none_mark_is_noop(paper_engine):
    """line 373: marking with a None mark leaves the position untouched."""
    pos = Position(campaign_id="c", market_id="m", token_id="tok", shares=10.0, avg_price=0.5)
    paper_engine._mark_position(pos, None)
    assert pos.mark_price is None
    assert pos.unrealized_pnl == 0.0


def test_mark_to_market_marks_open_positions(paper_engine, db, book_factory):
    """line 375 (via mark_to_market): a real mid updates mark + unrealized P&L."""
    camp = _paper_campaign(paper_engine, db)
    db.upsert_position(Position(campaign_id=camp.campaign_id, market_id="m", token_id="tok",
                                shares=20.0, avg_price=0.50))
    paper_engine.cache.update(book_factory(token_id="tok", bid=0.55, ask=0.57))  # mid 0.56
    paper_engine.mark_to_market(camp.campaign_id)
    marked = db.get_position(camp.campaign_id, "tok")
    assert marked.mark_price == pytest.approx(0.56)
    assert marked.unrealized_pnl == pytest.approx(20.0 * (0.56 - 0.50), abs=1e-6)


def test_cancel_all_cancels_open_orders(paper_engine, db):
    """lines 388-389 + _open_orders 398-402: cancel_all cancels every resting order."""
    camp = _paper_campaign(paper_engine, db)
    paper_engine.cache.update(OrderBookSnapshot(
        token_id="tok", bids=[BookLevel(price=0.49, size=500)],
        asks=[BookLevel(price=0.60, size=500)]))  # BUY @0.50 won't cross -> rests
    orders = []
    for _ in range(2):
        ti = _intent(camp.campaign_id, price=0.50, size=10.0)
        db.save_intent(ti)
        orders.append(paper_engine.place_order(camp, ti, _approve(ti)))
    assert all(o.status is OrderStatus.OPEN for o in orders)

    n = paper_engine.cancel_all(camp.campaign_id)
    assert n == 2
    for o in orders:
        assert db.get_order(o.order_id).status is OrderStatus.CANCELLED


def test_cancel_order_idempotent_when_already_cancelled(paper_engine, db):
    """line 375: cancelling an already-cancelled order returns it unchanged."""
    camp = _paper_campaign(paper_engine, db)
    paper_engine.cache.update(OrderBookSnapshot(
        token_id="tok", bids=[BookLevel(price=0.49, size=500)],
        asks=[BookLevel(price=0.60, size=500)]))  # rests (won't cross)
    ti = _intent(camp.campaign_id, price=0.50, size=10.0)
    db.save_intent(ti)
    order = paper_engine.place_order(camp, ti, _approve(ti))
    first = paper_engine.cancel_order(order.order_id)
    assert first.status is OrderStatus.CANCELLED
    again = paper_engine.cancel_order(order.order_id)  # already cancelled -> returned as-is
    assert again.status is OrderStatus.CANCELLED
    assert again.order_id == order.order_id


# =========================================================================== #
# intents.py
# =========================================================================== #
def test_intent_no_token_for_outcome_raises_not_found(db):
    """line 49: an outcome with no resolvable token id is a NotFoundError."""
    svc = IntentService(db, RiskPolicy())
    camp = Campaign(name="c", bankroll=1000.0)
    with pytest.raises(NotFoundError):
        svc.create(camp, _market(token_ids={}), outcome="YES", side=Side.BUY,
                   limit_price=0.5, max_size_usd=10.0, thesis="t",
                   expires_at="2026-12-30T00:00:00Z", confidence=0.6)


def test_intent_missing_fields_flagged(db):
    """missing evidence / market-resolution / counter-thesis / invalidation flagged."""
    svc = IntentService(db, RiskPolicy())
    camp = Campaign(name="c", bankroll=1000.0)
    # A market with no clear resolution rules also trips the resolution-rules branch.
    market = _market(resolution_rules="", resolution_source="")
    ti = svc.create(camp, market, outcome="YES", side=Side.BUY, limit_price=0.5,
                    max_size_usd=10.0, thesis="t", counter_thesis="",
                    invalidation_criteria="", evidence_refs=[], confidence=0.6,
                    expires_at="2026-12-30T00:00:00Z")
    assert "evidence_refs" in ti.missing_fields
    assert "market_resolution_rules" in ti.missing_fields
    assert "counter_thesis" in ti.missing_fields
    assert "invalidation_criteria" in ti.missing_fields
    assert ti.status == "needs_more_evidence"


def test_similar_past_intents_recalls_same_market(db):
    """similar_past_intents: prior decisions on the same market are recalled (FR-TI-006)."""
    svc = IntentService(db, RiskPolicy())
    camp = Campaign(name="c", bankroll=1000.0)
    first = svc.create(camp, _market(), outcome="YES", side=Side.BUY, limit_price=0.5,
                       max_size_usd=10.0, thesis="t1", counter_thesis="ct",
                       invalidation_criteria="inv", evidence_refs=["off://1"], confidence=0.6,
                       expires_at="2026-12-30T00:00:00Z")
    second = svc.create(camp, _market(), outcome="YES", side=Side.BUY, limit_price=0.55,
                        max_size_usd=10.0, thesis="t2", counter_thesis="ct",
                        invalidation_criteria="inv", evidence_refs=["off://2"], confidence=0.6,
                        expires_at="2026-12-30T00:00:00Z")
    similar = svc.similar_past_intents(camp.campaign_id, "m", exclude_id=second.intent_id)
    assert first.intent_id in similar
    assert second.intent_id not in similar


def test_intent_past_expiry_flagged(db):
    """lines 94-95: an expiry already in the past is flagged, not raised."""
    svc = IntentService(db, RiskPolicy())
    camp = Campaign(name="c", bankroll=1000.0)
    ti = svc.create(camp, _market(), outcome="YES", side=Side.BUY, limit_price=0.5,
                    max_size_usd=10.0, thesis="t", counter_thesis="ct",
                    invalidation_criteria="inv", evidence_refs=["off://1"], confidence=0.6,
                    expires_at="2000-01-01T00:00:00Z")
    assert "expires_at_in_future" in ti.missing_fields


def test_intent_invalid_expiry_raises_validation_error(db):
    """lines 96-97: an unparseable expiry raises a schema ValidationError."""
    svc = IntentService(db, RiskPolicy())
    camp = Campaign(name="c", bankroll=1000.0)
    with pytest.raises(ValidationError):
        svc.create(camp, _market(), outcome="YES", side=Side.BUY, limit_price=0.5,
                   max_size_usd=10.0, thesis="t", counter_thesis="ct",
                   invalidation_criteria="inv", evidence_refs=["off://1"], confidence=0.6,
                   expires_at="not-a-valid-timestamp")


# =========================================================================== #
# live_adapter.py
# =========================================================================== #
class _RaisingStore:
    """A secret store whose every operation raises (a misconfigured backend)."""

    backend = "raising"

    def available(self) -> bool:
        raise RuntimeError("backend exploded")

    def get(self, name: str):
        raise RuntimeError("backend exploded")

    def set(self, name: str, value: str) -> None:
        raise RuntimeError("backend exploded")

    def names(self):
        raise RuntimeError("backend exploded")


def test_signing_vault_available_swallows_store_errors():
    """lines 49-50: a store that raises is treated as 'locked', never propagated."""
    v = SigningVault(_RaisingStore(), "live_signing_key")
    assert v.available is False
    assert v.status()["unlocked"] is False
    assert v.status()["exposes_secrets"] is False


def test_signing_vault_signs_without_exposing_key():
    """lines 65-70: a provisioned vault returns a deterministic HMAC, never the key."""
    store = EnvSecretStore(env={"HPM_SECRET_LIVE_SIGNING_KEY": "PRIVATE-MATERIAL"})
    v = SigningVault(store, "live_signing_key")
    assert v.available is True
    sig = v.sign("order-ref-1")
    assert len(sig) == 64 and all(c in "0123456789abcdef" for c in sig)
    assert "PRIVATE-MATERIAL" not in sig
    assert v.sign("order-ref-1") == sig   # deterministic for the same reference
    assert v.sign("order-ref-2") != sig   # depends on the signed reference


async def test_compliance_gate_geoblock_exception_fails_closed(settings):
    """lines 102-103: a geoblock check that raises fails CLOSED (blocked)."""
    async def boom():
        raise RuntimeError("geoblock endpoint down")

    gate = ComplianceGate(settings, geoblock_check=boom)
    gates = await gate.evaluate(None, None, SigningVault())
    assert gates["geoblock_pass"] is False
    assert gates["all_pass"] is False


async def test_live_adapter_all_gates_pass_pending_review(tmp_path):
    """lines 176-180: even when every gate is green the adapter only ever returns
    'pending_manual_review' with a null order ref — never a live submission."""
    s = load_settings(
        data_dir=str(tmp_path), live_enabled=True, operator_age_verified=True,
        operator_jurisdiction_allowed=True, operator_acknowledged_risk=True,
        red_team_passed=True,
    )
    db = Database(":memory:")
    decision = RiskDecision(intent_id="ti", campaign_id="c", result=RiskResult.APPROVE,
                            approved_size_usd=10.0, approved_limit_price=0.5)

    async def geo_ok():
        return {"blocked": False}

    adapter = LiveAdapter(s, AuditStore(db), lambda _rid: decision, geoblock_check=geo_ok)
    # Unlock the vault with a throwaway in-memory key — no src change, no real key file.
    adapter._vault = SigningVault(
        EnvSecretStore(env={"HPM_SECRET_LIVE_SIGNING_KEY": "test-key"}), "live_signing_key")

    out = await adapter.place_order_intent("ti", "rd", user_confirmation_token="confirm")
    assert out["status"] == "pending_manual_review"
    assert out["compliance_state"]["all_pass"] is True
    assert out["order_ref"] is None


async def test_live_adapter_cancel_and_open_orders(settings):
    """cancel-only is always allowed; no live orders can exist while locked."""
    db = Database(":memory:")
    adapter = LiveAdapter(settings, AuditStore(db), lambda _rid: None)
    assert adapter.enabled is False  # live disabled by default (FR-LIVE-001)
    cancelled = await adapter.cancel_order("ref-9")
    assert cancelled["cancelled"] is True
    assert cancelled["status"] == "cancel_only"
    assert cancelled["order_ref"] == "ref-9"
    assert await adapter.get_open_orders() == []


async def test_live_adapter_freeze_blocks_gate(settings):
    """freeze() records audit + flips the not_frozen gate to False."""
    db = Database(":memory:")
    adapter = LiveAdapter(settings, AuditStore(db), lambda _rid: None)
    adapter.freeze("jurisdiction changed")
    assert adapter._gate.frozen is True
    assert adapter._gate.freeze_reason == "jurisdiction changed"
    state = await adapter._gate.evaluate(None, None, adapter._vault)
    assert state["not_frozen"] is False
    assert state["all_pass"] is False


# =========================================================================== #
# secrets.py
# =========================================================================== #
def test_env_secret_store_names_and_roundtrip():
    """line 54: names() strips the prefix, lowercases and sorts the entries."""
    s = EnvSecretStore(env={"HPM_SECRET_LIVE_SIGNING_KEY": "k",
                            "HPM_SECRET_X_API": "t", "UNRELATED": "z"})
    assert s.available() is True
    assert s.get("x_api") == "t"
    assert s.names() == ["live_signing_key", "x_api"]
    s.set("new_one", "v")
    assert s.get("new_one") == "v"


def test_encrypted_file_set_get_names_roundtrip(tmp_path):
    """lines around _save + 124: write, cold-read, names(), ciphertext-at-rest."""
    path = tmp_path / "s.enc"
    store = EncryptedFileSecretStore(path, "master-pass")
    assert store.available() is True
    store.set("live_signing_key", "VALUE-1")
    assert store.get("live_signing_key") == "VALUE-1"
    assert store.names() == ["live_signing_key"]
    assert "VALUE-1" not in path.read_text(encoding="utf-8")  # encrypted at rest


def test_encrypted_file_empty_file_is_empty_store(tmp_path):
    """lines 93-94: an existing but blank file is treated as an empty store."""
    path = tmp_path / "empty.enc"
    path.write_text("   \n", encoding="utf-8")
    store = EncryptedFileSecretStore(path, "master-pass")
    assert store.names() == []
    assert store.get("anything") is None


def test_encrypted_file_save_guard_requires_passphrase(tmp_path):
    """line 106: _save refuses to persist when the passphrase is unavailable."""
    store = EncryptedFileSecretStore(tmp_path / "s.enc", "master-pass")
    store.set("k", "v")          # populates the in-memory cache
    store._passphrase = None     # passphrase subsequently lost
    with pytest.raises(PermissionError):
        store.set("k2", "v2")    # _load() hits cache, _save() guard fires


def test_encrypted_file_cold_read_and_wrong_passphrase(tmp_path):
    """lines 95-102: a fresh instance decrypts with the right pass and refuses the wrong one."""
    path = tmp_path / "s.enc"
    EncryptedFileSecretStore(path, "right-pass").set("live_signing_key", "MATERIAL")
    # Brand-new instance -> forced cold decrypt (the happy try-body, 95-99 + 102).
    assert EncryptedFileSecretStore(path, "right-pass").get("live_signing_key") == "MATERIAL"
    # Wrong passphrase -> Fernet InvalidToken -> PermissionError (the except, 100-101).
    with pytest.raises(PermissionError):
        EncryptedFileSecretStore(path, "wrong-pass").get("live_signing_key")


def test_encrypted_file_unavailable_without_passphrase(tmp_path):
    """lines 81 & 87: with no passphrase the store is unavailable and _load refuses."""
    store = EncryptedFileSecretStore(tmp_path / "s.enc", None)
    assert store.available() is False
    with pytest.raises(PermissionError):
        store.get("live_signing_key")


def _make_fake_keyring(*, backend_name="SecretServiceKeyring", backing=None,
                       get_exc=None, get_keyring_exc=None):
    fake = types.ModuleType("keyring")
    backing = backing if backing is not None else {}

    class _Backend:
        pass

    _Backend.__name__ = backend_name

    def get_keyring():
        if get_keyring_exc is not None:
            raise get_keyring_exc
        return _Backend()

    def get_password(service, name):
        if get_exc is not None:
            raise get_exc
        return backing.get((service, name))

    def set_password(service, name, value):
        backing[(service, name)] = value

    fake.get_keyring = get_keyring
    fake.get_password = get_password
    fake.set_password = set_password
    return fake, backing


def test_keyring_store_available_get_set_names(monkeypatch):
    """lines 149, 154, 157 + available happy path: a working keyring backend."""
    fake, backing = _make_fake_keyring(backend_name="SecretServiceKeyring")
    monkeypatch.setitem(sys.modules, "keyring", fake)
    store = KeyringSecretStore(service="hpm-test")
    assert store.available() is True
    store.set("live_signing_key", "kv")
    assert backing[("hpm-test", "live_signing_key")] == "kv"
    assert store.get("live_signing_key") == "kv"
    assert store.names() == []  # keyring has no portable enumeration


def test_keyring_store_null_backend_is_unavailable(monkeypatch):
    """available() returns False for a fail/null backend name."""
    fake, _ = _make_fake_keyring(backend_name="NullKeyring")
    monkeypatch.setitem(sys.modules, "keyring", fake)
    assert KeyringSecretStore().available() is False


def test_keyring_store_available_exception_swallowed(monkeypatch):
    """lines 144-145: a keyring backend that raises -> available() is False."""
    fake, _ = _make_fake_keyring(get_keyring_exc=RuntimeError("no backend"))
    monkeypatch.setitem(sys.modules, "keyring", fake)
    assert KeyringSecretStore().available() is False


def test_keyring_store_get_exception_returns_none(monkeypatch):
    """lines 150-151: a get_password that raises -> get() returns None."""
    fake, _ = _make_fake_keyring(get_exc=RuntimeError("locked"))
    monkeypatch.setitem(sys.modules, "keyring", fake)
    assert KeyringSecretStore().get("anything") is None


def test_make_secret_store_selects_each_backend(tmp_path):
    """make_secret_store keyring + encrypted_file + env branches."""
    assert make_secret_store(
        load_settings(data_dir=str(tmp_path), secret_store="keyring")).backend == "keyring"
    assert make_secret_store(
        load_settings(data_dir=str(tmp_path), secret_store="encrypted_file",
                      secret_master_passphrase="p")).backend == "encrypted_file"
    assert make_secret_store(load_settings(data_dir=str(tmp_path))).backend == "env"


# =========================================================================== #
# live_process.py
# =========================================================================== #
class _FakeAdapter:
    """Minimal stand-in for LiveAdapter used to drive _handle / _main."""

    enabled = False

    def vault_status(self) -> dict:
        return {"unlocked": False, "exposes_secrets": False}

    async def place_order_intent(self, trade_intent_id, risk_decision_id,
                                 user_confirmation_token=None) -> dict:
        return {"status": "blocked", "trade_intent_id": trade_intent_id,
                "risk_decision_id": risk_decision_id, "token": user_confirmation_token}

    async def cancel_order(self, order_ref) -> dict:
        return {"status": "cancel_only", "order_ref": order_ref, "cancelled": True}

    async def get_open_orders(self) -> list:
        return []


class _FakeStdin:
    def __init__(self) -> None:
        self.writes: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def drain(self) -> None:
        return None


class _FakeStdout:
    def __init__(self, line: bytes) -> None:
        self._line = line

    async def readline(self) -> bytes:
        return self._line


class _FakeProc:
    def __init__(self, line: bytes) -> None:
        self.stdin = _FakeStdin()
        self.stdout = _FakeStdout(line)
        self.returncode: int | None = None
        self.killed = False

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        return self.returncode if self.returncode is not None else 0


def _client(tmp_path):
    c = live_process.LiveProcessClient(load_settings(data_dir=str(tmp_path)))
    c._timeout = 0.2  # fail fast in tests
    return c


async def test_build_adapter_builds_locked_reference_adapter(tmp_path, monkeypatch):
    """lines 38-45: _build_adapter wires a compliance-locked, geoblock-closed adapter."""
    monkeypatch.setenv("HPM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HPM_DB_FILENAME", "lp_build.sqlite3")
    monkeypatch.setenv("HPM_LIVE_ENABLED", "false")
    adapter = live_process._build_adapter()
    assert isinstance(adapter, LiveAdapter)
    out = await adapter.place_order_intent("ti", "rd")
    assert out["status"] == "blocked"
    assert out["compliance_state"]["geoblock_pass"] is False  # geoblock_check=None -> closed
    assert adapter.vault_status()["exposes_secrets"] is False


async def test_handle_status_returns_vault_and_enabled():
    r = await live_process._handle(_FakeAdapter(), {"cmd": "status"})
    assert r["ok"] is True
    assert r["vault"]["exposes_secrets"] is False
    assert r["enabled"] is False


async def test_handle_place_intent_forwards_references():
    r = await live_process._handle(_FakeAdapter(), {
        "cmd": "place_intent", "trade_intent_id": "ti1", "risk_decision_id": "rd1",
        "user_confirmation_token": "confirm"})
    assert r["ok"] is True
    assert r["result"]["status"] == "blocked"
    assert r["result"]["trade_intent_id"] == "ti1"
    assert r["result"]["token"] == "confirm"


async def test_handle_cancel():
    r = await live_process._handle(_FakeAdapter(), {"cmd": "cancel", "order_ref": "o1"})
    assert r["ok"] is True
    assert r["result"]["cancelled"] is True
    assert r["result"]["order_ref"] == "o1"


async def test_handle_open_orders():
    r = await live_process._handle(_FakeAdapter(), {"cmd": "open_orders"})
    assert r["ok"] is True
    assert r["result"] == []


async def test_handle_unknown_cmd():
    r = await live_process._handle(_FakeAdapter(), {"cmd": "frobnicate"})
    assert r["ok"] is False
    assert "unknown cmd" in r["error"]


def test_main_loop_processes_stdin(monkeypatch):
    """lines 69-84: the stdin loop skips blanks, reports bad json, handles a
    command, and exits cleanly on shutdown."""
    monkeypatch.setattr(live_process, "_build_adapter", lambda: _FakeAdapter())
    feed = "\n".join(["", "not valid json", '{"cmd": "status"}', '{"cmd": "shutdown"}']) + "\n"
    out_io = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(feed))
    monkeypatch.setattr(sys, "stdout", out_io)

    live_process._main()

    text = out_io.getvalue()
    assert '"error": "bad json"' in text       # malformed line reported
    assert '"vault"' in text                    # status command answered
    assert '"enabled": false' in text


async def test_client_vault_status_happy(tmp_path, monkeypatch):
    c = _client(tmp_path)

    async def noop():
        return None

    monkeypatch.setattr(c, "_ensure_started_locked", noop)
    c._proc = _FakeProc(b'{"ok": true, "vault": {"unlocked": false}}\n')
    assert await c.vault_status() == {"unlocked": False}


async def test_client_place_order_intent_happy(tmp_path, monkeypatch):
    """lines ~207-211 + _rpc happy path: a command is written and the result unwrapped."""
    c = _client(tmp_path)

    async def noop():
        return None

    monkeypatch.setattr(c, "_ensure_started_locked", noop)
    proc = _FakeProc(b'{"ok": true, "result": {"status": "blocked", "order_ref": null}}\n')
    c._proc = proc
    out = await c.place_order_intent("ti", "rd", "confirm")
    assert out["status"] == "blocked"
    assert out["order_ref"] is None
    assert proc.stdin.writes  # a command line was actually sent to the child


async def test_client_cancel_order_happy(tmp_path, monkeypatch):
    c = _client(tmp_path)

    async def noop():
        return None

    monkeypatch.setattr(c, "_ensure_started_locked", noop)
    c._proc = _FakeProc(b'{"ok": true, "result": {"cancelled": true, "order_ref": "o1"}}\n')
    out = await c.cancel_order("o1")
    assert out["cancelled"] is True
    assert out["order_ref"] == "o1"


async def test_client_get_open_orders_happy(tmp_path, monkeypatch):
    c = _client(tmp_path)

    async def noop():
        return None

    monkeypatch.setattr(c, "_ensure_started_locked", noop)
    c._proc = _FakeProc(b'{"ok": true, "result": []}\n')
    assert await c.get_open_orders() == []


async def test_rpc_spawn_failure_returns_clean_error(tmp_path, monkeypatch):
    """lines 172-175: a child that cannot be spawned yields a clean error + fault."""
    c = _client(tmp_path)

    async def boom():
        raise RuntimeError("cannot spawn")

    monkeypatch.setattr(c, "_ensure_started_locked", boom)
    r = await c._rpc({"cmd": "status"})
    assert r["ok"] is False
    assert "failed to start" in r["error"]
    assert c.faults >= 1


async def test_rpc_unavailable_when_pipes_missing(tmp_path, monkeypatch):
    """line 179: a child whose pipes are missing yields 'unavailable'."""
    c = _client(tmp_path)

    async def noop():
        return None

    monkeypatch.setattr(c, "_ensure_started_locked", noop)
    proc = _FakeProc(b'{}\n')
    proc.stdin = None
    c._proc = proc
    r = await c._rpc({"cmd": "status"})
    assert r["ok"] is False
    assert "unavailable" in r["error"]


async def test_rpc_malformed_response(tmp_path, monkeypatch):
    """lines 201-202: a non-JSON reply from the child is reported as malformed."""
    c = _client(tmp_path)

    async def noop():
        return None

    monkeypatch.setattr(c, "_ensure_started_locked", noop)
    c._proc = _FakeProc(b'this is definitely not json\n')
    r = await c._rpc({"cmd": "status"})
    assert r["ok"] is False
    assert "malformed" in r["error"]


async def test_terminate_kills_alive_child(tmp_path):
    """line 161: an alive child is killed and reaped on terminate."""
    c = _client(tmp_path)
    proc = _FakeProc(b'{"ok": true}\n')  # returncode None -> alive
    c._proc = proc
    await c._terminate_locked()
    assert proc.killed is True
    assert c._proc is None


async def test_ensure_started_reaps_dead_handle_then_respawns(tmp_path, monkeypatch):
    """line 150: a lingering dead handle is reaped before a fresh child is spawned."""
    c = _client(tmp_path)
    dead = _FakeProc(b'{}\n')
    dead.returncode = 0  # not alive, but the handle still lingers (not None)
    c._proc = dead
    healthy = _FakeProc(b'{"ok": true}\n')

    async def fake_spawn(*_a, **_k):
        return healthy

    monkeypatch.setattr(live_process.asyncio, "create_subprocess_exec", fake_spawn)
    await c.start()
    assert c._proc is healthy
