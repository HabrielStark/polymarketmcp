"""Unit tests: audit hash-chain, redaction, persistence idempotency & recovery."""

from __future__ import annotations

from hermes_pm.audit.store import AuditStore
from hermes_pm.models import Campaign, Side, TradeIntent
from hermes_pm.persistence.db import Database
from hermes_pm.persistence.redact import redact


def test_chain_verifies(db):
    au = AuditStore(db)
    for i in range(5):
        au.append("evt", inputs={"i": i})
    v = au.verify_chain()
    assert v["ok"] and v["count"] == 5


def test_tamper_detected(db):
    au = AuditStore(db)
    e1 = au.append("evt", summary="orig", inputs={"a": 1})
    au.append("evt2")
    tampered = e1.model_copy(update={"summary": "HACKED"}).model_dump_json()
    db.execute("UPDATE audit_events SET data=? WHERE event_id=?", (tampered, e1.event_id))
    v = au.verify_chain()
    assert not v["ok"] and v["reason"] == "event_hash_mismatch"


def test_reorder_detected(db):
    au = AuditStore(db)
    au.append("a")
    au.append("b")
    # swap previous_event_hash linkage by blanking it
    rows = db.query("SELECT seq, data FROM audit_events ORDER BY seq")
    from hermes_pm.models import AuditEvent
    ev = AuditEvent.model_validate_json(rows[1]["data"])
    broken = ev.model_copy(update={"previous_event_hash": "00" * 32}).model_dump_json()
    db.execute("UPDATE audit_events SET data=? WHERE seq=?", (broken, ev.seq))
    assert not au.verify_chain()["ok"]


def test_redaction_masks_secrets():
    data = {"api_key": "SECRET", "nested": {"bearer_token": "X", "ok": 1}, "list": [{"password": "p"}]}
    r = redact(data)
    assert r["api_key"] == "***REDACTED***"
    assert r["nested"]["bearer_token"] == "***REDACTED***"
    assert r["nested"]["ok"] == 1
    assert r["list"][0]["password"] == "***REDACTED***"


def test_export_redacts_and_verifies(db):
    au = AuditStore(db)
    au.append("evt", inputs={"api_key": "SECRET123"}, campaign_id="c1")
    exp = au.export("c1")
    assert exp["chain_verification"]["ok"]
    assert "SECRET123" not in str(exp)


def test_audit_persists_across_reopen(tmp_path):
    path = tmp_path / "a.sqlite3"
    db1 = Database(path)
    au1 = AuditStore(db1)
    au1.append("evt", inputs={"i": 1})
    head1 = au1.last_hash
    db1.close()
    # reopen: chain continues from persisted head
    db2 = Database(path)
    au2 = AuditStore(db2)
    assert au2.last_hash == head1
    au2.append("evt2")
    assert au2.verify_chain()["ok"]
    db2.close()


def test_intent_idempotent_insert(db):
    ti = TradeIntent(campaign_id="c", market_id="m", token_id="t", side=Side.BUY, limit_price=0.5,
                     max_size_usd=10, thesis="x", confidence=0.5, expires_at="2026-12-30T00:00:00Z",
                     idempotency_key="K")
    db.save_intent(ti)
    dup = ti.model_copy(update={"intent_id": "other"})
    out = db.save_intent(dup)
    assert out.intent_id == ti.intent_id


def test_campaign_recovery(tmp_path):
    path = tmp_path / "c.sqlite3"
    db1 = Database(path)
    c = Campaign(name="recover", bankroll=777)
    db1.save_campaign(c)
    db1.close()
    db2 = Database(path)
    assert db2.get_campaign(c.campaign_id).bankroll == 777
    db2.close()
