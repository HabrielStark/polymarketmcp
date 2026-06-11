"""Audit hash-chain under concurrent writers (NFR-OBS-001, COMP-008).

The append-only chain links each event to its predecessor's hash. Concurrent
appends must serialize so the chain never forks or skips: after a parallel storm
of appends, verify_chain must pass, sequence numbers must be unique and
contiguous, and the event count must be exact. (No bug was found here — the store
caches the head under a lock — this is a regression guard.)
"""

from __future__ import annotations

import threading

from hermes_pm.audit.store import AuditStore
from hermes_pm.persistence.db import Database

N_THREADS = 8
PER_THREAD = 50
TOTAL = N_THREADS * PER_THREAD


def test_concurrent_appends_keep_chain_intact():
    db = Database(":memory:")
    audit = AuditStore(db)
    errors: list[BaseException] = []
    barrier = threading.Barrier(N_THREADS)

    def worker(wid: int) -> None:
        try:
            barrier.wait()
            for i in range(PER_THREAD):
                audit.append("test_event", actor=f"w{wid}", summary=f"{wid}-{i}",
                             references={"w": wid, "i": i})
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(w,)) for w in range(N_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert errors == []
    chain = audit.verify_chain()
    assert chain["ok"] is True, chain
    assert chain["count"] == TOTAL

    events = audit.list_events(limit=10 * TOTAL)
    seqs = sorted(e.seq for e in events)
    assert len(events) == TOTAL
    assert len(set(seqs)) == TOTAL                 # no duplicate seq (no fork)
    assert seqs == list(range(1, TOTAL + 1))       # contiguous 1..TOTAL (no gaps)


def test_chain_detects_tampering_after_concurrent_load():
    # After a concurrent storm, tampering with one stored event must still be caught.
    db = Database(":memory:")
    audit = AuditStore(db)
    for i in range(20):
        audit.append("e", summary=str(i))
    assert audit.verify_chain()["ok"] is True
    # Corrupt one event's stored payload.
    row = db.query_one("SELECT event_id,data FROM audit_events WHERE seq=10")
    tampered = row["data"].replace('"summary":"9"', '"summary":"EDITED"')
    db.execute("UPDATE audit_events SET data=? WHERE event_id=?", (tampered, row["event_id"]))
    result = audit.verify_chain()
    assert result["ok"] is False
    assert result["broken_at_seq"] == 10
