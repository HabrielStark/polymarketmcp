"""Identifier and idempotency-key generation.

``new_id`` produces sortable, prefixed identifiers (time component + random) so
that ids are unique, human-readable, and roughly chronologically ordered.
``idempotency_key`` is a deterministic hash over canonical content used to make
trade intents, risk decisions, orders, and cancellations safely retryable
(NFR-REL-005)."""

from __future__ import annotations

import os
import time

from hermes_pm.util.hashing import hash_obj

_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"


def _base36(n: int, width: int) -> str:
    out = []
    while n:
        n, rem = divmod(n, 36)
        out.append(_ALPHABET[rem])
    s = "".join(reversed(out)) or "0"
    return s.rjust(width, "0")[-width:] if width else s


def new_id(prefix: str) -> str:
    """Return ``<prefix>_<time36><rand36>`` — unique and time-sortable."""
    ts = _base36(int(time.time() * 1000), 9)
    rand = _base36(int.from_bytes(os.urandom(5), "big"), 8)
    return f"{prefix}_{ts}{rand}"


def idempotency_key(*parts: object) -> str:
    """Deterministic key over the given parts (order-sensitive)."""
    return hash_obj(list(parts))
