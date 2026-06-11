"""SQLite persistence layer.

Each entity is stored as a row carrying its full JSON ``data`` plus a few indexed
columns for filtering. WAL mode + ``synchronous=FULL`` give durable writes so the
system can recover campaign ledger, positions, orders, and fills after a process
restart (NFR-REL-001/002). Idempotency-key uniqueness backs NFR-REL-005.

SQLite work is local and off the millisecond hot path (cache reads / risk checks
are in-memory), so a single connection guarded by a re-entrant lock is used."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from hermes_pm.models import (
    Campaign,
    Fill,
    Lesson,
    Market,
    Order,
    OrderBookSnapshot,
    Position,
    RiskDecision,
    Signal,
    TradeIntent,
)
from hermes_pm.util.timeutil import now_ms

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS campaigns (
  campaign_id TEXT PRIMARY KEY, status TEXT, mode TEXT, created_ms INTEGER, data TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS markets (
  market_id TEXT PRIMARY KEY, category TEXT, enable_order_book INTEGER, data TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS snapshots (
  snapshot_id TEXT PRIMARY KEY, token_id TEXT, sequence INTEGER, received_at INTEGER,
  data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_snap_token ON snapshots(token_id, received_at);

CREATE TABLE IF NOT EXISTS signals (
  signal_id TEXT PRIMARY KEY, market_id TEXT, source_type TEXT, ts INTEGER, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_sig_market ON signals(market_id, ts);

CREATE TABLE IF NOT EXISTS intents (
  intent_id TEXT PRIMARY KEY, campaign_id TEXT, idempotency_key TEXT UNIQUE, data TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS risk_decisions (
  decision_id TEXT PRIMARY KEY, intent_id TEXT, campaign_id TEXT,
  idempotency_key TEXT UNIQUE, data TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS orders (
  order_id TEXT PRIMARY KEY, campaign_id TEXT, intent_id TEXT,
  idempotency_key TEXT UNIQUE, status TEXT, data TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS fills (
  fill_id TEXT PRIMARY KEY, order_id TEXT, created_ms INTEGER, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_fill_order ON fills(order_id);

CREATE TABLE IF NOT EXISTS positions (
  position_id TEXT PRIMARY KEY, campaign_id TEXT, token_id TEXT, data TEXT NOT NULL,
  UNIQUE(campaign_id, token_id));

CREATE TABLE IF NOT EXISTS lessons (
  lesson_id TEXT PRIMARY KEY, campaign_id TEXT, data TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS ledger (
  entry_id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id TEXT, txn_id TEXT,
  account TEXT, debit REAL, credit REAL, created_ms INTEGER, memo TEXT);
CREATE INDEX IF NOT EXISTS ix_ledger_campaign ON ledger(campaign_id);

CREATE TABLE IF NOT EXISTS audit_events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE, type TEXT, actor TEXT,
  campaign_id TEXT, timestamp_ms INTEGER, previous_event_hash TEXT, event_hash TEXT,
  data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_audit_campaign ON audit_events(campaign_id, seq);
"""


class Database:
    """Synchronous SQLite store. Thread-safe via a re-entrant lock."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- low-level ---------------------------------------------------------- #
    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    # -- key/value ---------------------------------------------------------- #
    def kv_set(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )

    def kv_get(self, key: str, default: Any = None) -> Any:
        row = self.query_one("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    def kv_add(self, key: str, delta: float, default: float = 0.0) -> float:
        """Atomic read-modify-write of a numeric kv value (race-free under the
        connection lock). Returns the new value."""
        with self._lock:
            row = self._conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
            current = json.loads(row["value"]) if row else default
            new = round(float(current) + delta, 6)
            self._conn.execute(
                "INSERT INTO kv(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(new)),
            )
            self._conn.commit()
            return new

    # -- campaigns ---------------------------------------------------------- #
    def save_campaign(self, c: Campaign) -> None:
        self.execute(
            "INSERT INTO campaigns(campaign_id,status,mode,created_ms,data) VALUES(?,?,?,?,?) "
            "ON CONFLICT(campaign_id) DO UPDATE SET status=excluded.status,data=excluded.data",
            (c.campaign_id, c.status.value, c.mode.value, c.start_ms, c.model_dump_json()),
        )

    def get_campaign(self, campaign_id: str) -> Campaign | None:
        row = self.query_one("SELECT data FROM campaigns WHERE campaign_id=?", (campaign_id,))
        return Campaign.model_validate_json(row["data"]) if row else None

    def list_campaigns(self) -> list[Campaign]:
        return [
            Campaign.model_validate_json(r["data"])
            for r in self.query("SELECT data FROM campaigns ORDER BY created_ms DESC")
        ]

    # -- markets ------------------------------------------------------------ #
    def save_market(self, m: Market) -> None:
        self.execute(
            "INSERT INTO markets(market_id,category,enable_order_book,data) VALUES(?,?,?,?) "
            "ON CONFLICT(market_id) DO UPDATE SET category=excluded.category,"
            "enable_order_book=excluded.enable_order_book,data=excluded.data",
            (m.market_id, m.category, int(m.enable_order_book), m.model_dump_json()),
        )

    def get_market(self, market_id: str) -> Market | None:
        row = self.query_one("SELECT data FROM markets WHERE market_id=?", (market_id,))
        return Market.model_validate_json(row["data"]) if row else None

    def list_markets(self) -> list[Market]:
        return [
            Market.model_validate_json(r["data"]) for r in self.query("SELECT data FROM markets")
        ]

    # -- snapshots (replay provenance) ------------------------------------- #
    def save_snapshot(self, s: OrderBookSnapshot) -> None:
        self.execute(
            "INSERT OR IGNORE INTO snapshots(snapshot_id,token_id,sequence,received_at,data) "
            "VALUES(?,?,?,?,?)",
            (s.snapshot_id, s.token_id, s.sequence, s.received_at, s.model_dump_json()),
        )

    def get_snapshot(self, snapshot_id: str) -> OrderBookSnapshot | None:
        row = self.query_one("SELECT data FROM snapshots WHERE snapshot_id=?", (snapshot_id,))
        return OrderBookSnapshot.model_validate_json(row["data"]) if row else None

    def list_snapshots(self, token_id: str) -> list[OrderBookSnapshot]:
        return [
            OrderBookSnapshot.model_validate_json(r["data"])
            for r in self.query(
                "SELECT data FROM snapshots WHERE token_id=? ORDER BY received_at,sequence",
                (token_id,),
            )
        ]

    # -- signals ------------------------------------------------------------ #
    def save_signal(self, s: Signal) -> None:
        self.execute(
            "INSERT OR REPLACE INTO signals(signal_id,market_id,source_type,ts,data) VALUES(?,?,?,?,?)",
            (s.signal_id, s.market_id, s.source_type.value, s.timestamp, s.model_dump_json()),
        )

    def list_signals(self, market_id: str) -> list[Signal]:
        return [
            Signal.model_validate_json(r["data"])
            for r in self.query(
                "SELECT data FROM signals WHERE market_id=? ORDER BY ts DESC", (market_id,)
            )
        ]

    def purge_signals_before(self, before_ms: int) -> int:
        """Delete stored signals older than ``before_ms`` (NFR-PRIV-003, X data
        retention/deletion). Returns the number removed."""
        cur = self.execute("DELETE FROM signals WHERE ts < ?", (before_ms,))
        return cur.rowcount

    # -- intents (idempotent) ---------------------------------------------- #
    def save_intent(self, t: TradeIntent) -> TradeIntent:
        if t.idempotency_key:
            existing = self.query_one(
                "SELECT data FROM intents WHERE idempotency_key=?", (t.idempotency_key,)
            )
            if existing:
                return TradeIntent.model_validate_json(existing["data"])
        self.execute(
            "INSERT INTO intents(intent_id,campaign_id,idempotency_key,data) VALUES(?,?,?,?) "
            "ON CONFLICT(intent_id) DO UPDATE SET data=excluded.data",
            (t.intent_id, t.campaign_id, t.idempotency_key or None, t.model_dump_json()),
        )
        return t

    def get_intent(self, intent_id: str) -> TradeIntent | None:
        row = self.query_one("SELECT data FROM intents WHERE intent_id=?", (intent_id,))
        return TradeIntent.model_validate_json(row["data"]) if row else None

    def list_intents(self, campaign_id: str) -> list[TradeIntent]:
        return [
            TradeIntent.model_validate_json(r["data"])
            for r in self.query("SELECT data FROM intents WHERE campaign_id=?", (campaign_id,))
        ]

    # -- risk decisions ----------------------------------------------------- #
    def save_risk_decision(self, d: RiskDecision) -> RiskDecision:
        if d.idempotency_key:
            existing = self.query_one(
                "SELECT data FROM risk_decisions WHERE idempotency_key=?", (d.idempotency_key,)
            )
            if existing:
                return RiskDecision.model_validate_json(existing["data"])
        self.execute(
            "INSERT INTO risk_decisions(decision_id,intent_id,campaign_id,idempotency_key,data) "
            "VALUES(?,?,?,?,?) ON CONFLICT(decision_id) DO UPDATE SET data=excluded.data",
            (d.decision_id, d.intent_id, d.campaign_id, d.idempotency_key or None, d.model_dump_json()),
        )
        return d

    def get_risk_decision(self, decision_id: str) -> RiskDecision | None:
        row = self.query_one("SELECT data FROM risk_decisions WHERE decision_id=?", (decision_id,))
        return RiskDecision.model_validate_json(row["data"]) if row else None

    def list_risk_decisions(self, campaign_id: str) -> list[RiskDecision]:
        return [
            RiskDecision.model_validate_json(r["data"])
            for r in self.query(
                "SELECT data FROM risk_decisions WHERE campaign_id=?", (campaign_id,)
            )
        ]

    # -- orders / fills ----------------------------------------------------- #
    def save_order(self, o: Order) -> Order:
        if o.idempotency_key:
            existing = self.query_one(
                "SELECT data FROM orders WHERE idempotency_key=?", (o.idempotency_key,)
            )
            if existing and existing["data"]:
                prior = Order.model_validate_json(existing["data"])
                if prior.order_id != o.order_id:
                    return prior
        self.execute(
            "INSERT INTO orders(order_id,campaign_id,intent_id,idempotency_key,status,data) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(order_id) DO UPDATE SET status=excluded.status,"
            "data=excluded.data",
            (o.order_id, o.campaign_id, o.intent_id, o.idempotency_key or None, o.status.value,
             o.model_dump_json()),
        )
        return o

    def get_order(self, order_id: str) -> Order | None:
        row = self.query_one("SELECT data FROM orders WHERE order_id=?", (order_id,))
        return Order.model_validate_json(row["data"]) if row else None

    def list_orders(self, campaign_id: str) -> list[Order]:
        return [
            Order.model_validate_json(r["data"])
            for r in self.query("SELECT data FROM orders WHERE campaign_id=?", (campaign_id,))
        ]

    def save_fill(self, f: Fill) -> None:
        self.execute(
            "INSERT OR IGNORE INTO fills(fill_id,order_id,created_ms,data) VALUES(?,?,?,?)",
            (f.fill_id, f.order_id, f.created_ms, f.model_dump_json()),
        )

    def list_fills(self, order_id: str) -> list[Fill]:
        return [
            Fill.model_validate_json(r["data"])
            for r in self.query(
                "SELECT data FROM fills WHERE order_id=? ORDER BY created_ms", (order_id,)
            )
        ]

    # -- positions ---------------------------------------------------------- #
    def upsert_position(self, p: Position) -> None:
        self.execute(
            "INSERT INTO positions(position_id,campaign_id,token_id,data) VALUES(?,?,?,?) "
            "ON CONFLICT(campaign_id,token_id) DO UPDATE SET data=excluded.data",
            (p.position_id, p.campaign_id, p.token_id, p.model_dump_json()),
        )

    def get_position(self, campaign_id: str, token_id: str) -> Position | None:
        row = self.query_one(
            "SELECT data FROM positions WHERE campaign_id=? AND token_id=?", (campaign_id, token_id)
        )
        return Position.model_validate_json(row["data"]) if row else None

    def list_positions(self, campaign_id: str) -> list[Position]:
        return [
            Position.model_validate_json(r["data"])
            for r in self.query("SELECT data FROM positions WHERE campaign_id=?", (campaign_id,))
        ]

    def list_positions_for_token(self, token_id: str) -> list[Position]:
        return [
            Position.model_validate_json(r["data"])
            for r in self.query("SELECT data FROM positions WHERE token_id=?", (token_id,))
        ]

    # -- lessons ------------------------------------------------------------ #
    def save_lesson(self, lesson: Lesson) -> None:
        self.execute(
            "INSERT OR REPLACE INTO lessons(lesson_id,campaign_id,data) VALUES(?,?,?)",
            (lesson.lesson_id, lesson.campaign_id, lesson.model_dump_json()),
        )

    def list_lessons(self, campaign_id: str | None = None) -> list[Lesson]:
        if campaign_id:
            rows = self.query("SELECT data FROM lessons WHERE campaign_id=?", (campaign_id,))
        else:
            rows = self.query("SELECT data FROM lessons")
        return [Lesson.model_validate_json(r["data"]) for r in rows]

    # -- ledger (double-entry) --------------------------------------------- #
    def append_ledger(
        self, campaign_id: str, txn_id: str, account: str, debit: float, credit: float, memo: str
    ) -> None:
        self.execute(
            "INSERT INTO ledger(campaign_id,txn_id,account,debit,credit,created_ms,memo) "
            "VALUES(?,?,?,?,?,?,?)",
            (campaign_id, txn_id, account, debit, credit, now_ms(), memo),
        )

    def list_ledger(self, campaign_id: str) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.query(
                "SELECT entry_id,txn_id,account,debit,credit,created_ms,memo FROM ledger "
                "WHERE campaign_id=? ORDER BY entry_id",
                (campaign_id,),
            )
        ]
