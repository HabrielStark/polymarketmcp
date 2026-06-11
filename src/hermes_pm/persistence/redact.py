"""Redaction for audit exports (NFR-PRIV-004, NFR-SEC-002).

Walks an arbitrary JSON-like structure and masks any value whose key matches a
secret/PII pattern, so exported audit bundles never leak keys, tokens, auth
headers, seed phrases, or personal identifiers."""

from __future__ import annotations

import re
from typing import Any

_SECRET_KEY = re.compile(
    r"(secret|password|passwd|token|api[_-]?key|private[_-]?key|seed|mnemonic|"
    r"authorization|auth[_-]?header|bearer|signature|wallet|recovery)",
    re.IGNORECASE,
)
_REDACTED = "***REDACTED***"


def redact(obj: Any) -> Any:
    """Return a deep copy of ``obj`` with secret/PII values masked by key name."""
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if isinstance(k, str) and _SECRET_KEY.search(k):
                out[k] = _REDACTED
            else:
                out[k] = redact(v)
        return out
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    if isinstance(obj, tuple):
        return tuple(redact(v) for v in obj)
    return obj
