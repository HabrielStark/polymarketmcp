"""Security helpers.

``tokens_match`` is a constant-time comparison for access tokens / bearer
credentials. A plain ``==``/``!=`` on secrets leaks, via response timing, how
many leading characters matched — enough to recover a token byte-by-byte over
many requests. ``hmac.compare_digest`` takes time independent of where the first
mismatch is, closing that side channel (NFR-SEC-006)."""

from __future__ import annotations

import hmac


def tokens_match(expected: str | None, provided: str | None) -> bool:
    """Return True iff both tokens are present and equal, compared in constant
    time. A missing expected or provided token is always a non-match."""
    if not expected or not provided:
        return False
    return hmac.compare_digest(expected.encode("utf-8"), provided.encode("utf-8"))
