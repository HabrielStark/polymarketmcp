"""Canonical serialization and hashing for the append-only audit chain.

Canonical JSON sorts keys and uses compact separators so the same logical object
always hashes identically, which is what makes the audit chain verifiable."""

from __future__ import annotations

import hashlib
import json
from typing import Any

GENESIS_HASH = "0" * 64


def _default(obj: Any) -> Any:
    # pydantic models, sets, and other rich types reduce to plain structures.
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if isinstance(obj, (set, frozenset)):
        return sorted(obj)
    if isinstance(obj, bytes):
        return obj.hex()
    return str(obj)


def canonical_json(obj: Any) -> str:
    """Deterministic JSON string: sorted keys, compact, UTF-8 safe."""
    return json.dumps(
        obj, default=_default, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def sha256_hex(data: str | bytes) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def hash_obj(obj: Any) -> str:
    """SHA-256 hex digest of the canonical JSON representation of ``obj``."""
    return sha256_hex(canonical_json(obj))


def chain_hash(previous_hash: str, event_payload: Any) -> str:
    """Hash linking an audit event to its predecessor (blockchain-style)."""
    return sha256_hex(f"{previous_hash}:{hash_obj(event_payload)}")
