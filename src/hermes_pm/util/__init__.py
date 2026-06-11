"""Pure, dependency-free utilities: time, ids, hashing, input sanitization."""

from hermes_pm.util.hashing import canonical_json, chain_hash, hash_obj, sha256_hex
from hermes_pm.util.ids import idempotency_key, new_id
from hermes_pm.util.sanitize import SanitizedText, sanitize_untrusted
from hermes_pm.util.timeutil import iso_to_ms, ms_to_iso, now_iso, now_ms

__all__ = [
    "canonical_json",
    "chain_hash",
    "hash_obj",
    "sha256_hex",
    "new_id",
    "idempotency_key",
    "sanitize_untrusted",
    "SanitizedText",
    "now_ms",
    "now_iso",
    "ms_to_iso",
    "iso_to_ms",
]
