"""Hash-chained, append-only audit log.

Every event links to its predecessor via ``previous_event_hash`` and stores its
own ``event_hash`` computed over the *entire* event content excluding the hash
field itself (see :func:`_content_hash`). Because the predecessor hash is part of
that content, any tampering with a past event — or any reordering — breaks the
chain, which ``verify_chain`` detects. This backs FR-LIVE-007, COMP-008,
NFR-OBS-001/002, and the replayability acceptance criterion AC-004."""

from __future__ import annotations

import threading
from typing import Any

from hermes_pm.models import AuditEvent
from hermes_pm.persistence.db import Database
from hermes_pm.persistence.redact import redact
from hermes_pm.util.hashing import GENESIS_HASH, canonical_json, hash_obj, sha256_hex
from hermes_pm.util.timeutil import now_iso, now_ms


def _content_hash(event: AuditEvent) -> str:
    """Hash over the *entire* event except ``event_hash`` itself. Because
    ``previous_event_hash`` is included, this both seals each event's content and
    chains it to its predecessor, so any edit or reordering is detectable."""
    return sha256_hex(canonical_json(event.model_dump(mode="json", exclude={"event_hash"})))


class AuditStore:
    """Serializes audit appends so the hash chain is always consistent."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self._lock = threading.Lock()
        row = db.query_one("SELECT event_hash, seq FROM audit_events ORDER BY seq DESC LIMIT 1")
        self._last_hash: str = row["event_hash"] if row else GENESIS_HASH
        self._last_seq: int = row["seq"] if row else 0

    @property
    def last_hash(self) -> str:
        return self._last_hash

    def append(
        self,
        type: str,
        actor: str = "system",
        *,
        summary: str = "",
        inputs: Any = None,
        outputs: Any = None,
        references: dict[str, Any] | None = None,
        campaign_id: str | None = None,
        latency_ms: float = 0.0,
    ) -> AuditEvent:
        """Append one immutable, hash-chained event. Inputs/outputs are hashed
        (for tamper-evidence) and also stored (redacted) for inspection."""
        with self._lock:
            seq = self._last_seq + 1
            references = references or {}
            safe_inputs = redact(inputs)
            safe_outputs = redact(outputs)
            payload = {
                "type": type,
                "actor": actor,
                "summary": summary,
                "inputs": safe_inputs,
                "outputs": safe_outputs,
                "references": references,
                "campaign_id": campaign_id,
                "timestamp_ms": now_ms(),
            }
            event = AuditEvent(
                seq=seq,
                type=type,
                actor=actor,
                summary=summary,
                input_hash=hash_obj(safe_inputs) if inputs is not None else "",
                output_hash=hash_obj(safe_outputs) if outputs is not None else "",
                references=references,
                payload=payload,
                timestamp=now_iso(),
                timestamp_ms=payload["timestamp_ms"],
                latency_ms=round(latency_ms, 3),
                previous_event_hash=self._last_hash,
            )
            event.event_hash = _content_hash(event)
            self.db.execute(
                "INSERT INTO audit_events(event_id,type,actor,campaign_id,timestamp_ms,"
                "previous_event_hash,event_hash,data) VALUES(?,?,?,?,?,?,?,?)",
                (
                    event.event_id,
                    type,
                    actor,
                    campaign_id,
                    event.timestamp_ms,
                    event.previous_event_hash,
                    event.event_hash,
                    event.model_dump_json(),
                ),
            )
            self._last_hash = event.event_hash
            self._last_seq = seq
            return event

    def get(self, event_id: str) -> AuditEvent | None:
        row = self.db.query_one("SELECT data FROM audit_events WHERE event_id=?", (event_id,))
        return AuditEvent.model_validate_json(row["data"]) if row else None

    def list_events(
        self, campaign_id: str | None = None, limit: int = 500, event_type: str | None = None
    ) -> list[AuditEvent]:
        clauses, params = [], []
        if campaign_id:
            clauses.append("campaign_id=?")
            params.append(campaign_id)
        if event_type:
            clauses.append("type=?")
            params.append(event_type)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        # `where` is composed only of fixed `column=?` fragments; all values are
        # bound parameters, so this is not an injection vector.
        rows = self.db.query(
            f"SELECT data FROM audit_events {where} ORDER BY seq DESC LIMIT ?",  # noqa: S608
            (*params, limit),
        )
        return [AuditEvent.model_validate_json(r["data"]) for r in rows]

    def verify_chain(self, campaign_id: str | None = None) -> dict[str, Any]:
        """Walk the FULL global chain in order and confirm every link. The chain
        is global (each event links to the previous event regardless of
        campaign), so integrity is always verified over all events; the
        ``campaign_id`` argument only labels the report's scope."""
        rows = self.db.query("SELECT data FROM audit_events ORDER BY seq ASC")
        prev = GENESIS_HASH
        count = 0
        for r in rows:
            ev = AuditEvent.model_validate_json(r["data"])
            if ev.previous_event_hash != prev:
                return {"ok": False, "broken_at_seq": ev.seq, "reason": "previous_hash_mismatch",
                        "count": count, "scope": campaign_id}
            if _content_hash(ev) != ev.event_hash:
                return {"ok": False, "broken_at_seq": ev.seq, "reason": "event_hash_mismatch",
                        "count": count, "scope": campaign_id}
            prev = ev.event_hash
            count += 1
        return {"ok": True, "count": count, "head": prev, "scope": campaign_id}

    def export(self, campaign_id: str | None = None) -> dict[str, Any]:
        """Redacted, verifiable audit bundle (NFR-PRIV-004, COMP-008)."""
        events = list(reversed(self.list_events(campaign_id=campaign_id, limit=1_000_000)))
        return {
            "campaign_id": campaign_id,
            "exported_at": now_iso(),
            "chain_verification": self.verify_chain(campaign_id),
            "event_count": len(events),
            "events": [redact(e.model_dump(mode="json")) for e in events],
        }
