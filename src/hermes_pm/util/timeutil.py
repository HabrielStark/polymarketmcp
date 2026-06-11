"""UTC, millisecond-resolution time helpers. All timestamps in the system are
timezone-aware UTC ISO-8601 strings or integer epoch milliseconds."""

from __future__ import annotations

from datetime import UTC, datetime


def now_ms() -> int:
    """Current UTC time as integer epoch milliseconds."""
    return int(datetime.now(UTC).timestamp() * 1000)


def now_iso() -> str:
    """Current UTC time as ISO-8601 string with millisecond precision."""
    return ms_to_iso(now_ms())


def ms_to_iso(ms: int) -> str:
    return (
        datetime.fromtimestamp(ms / 1000, tz=UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def iso_to_ms(value: str) -> int:
    """Parse an ISO-8601 timestamp (accepts trailing 'Z') to epoch milliseconds."""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)
