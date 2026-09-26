"""UTC clock, RFC3339 (de)serialization, and TTL expiry maths.

`utcnow()` is the only clock source in this codebase; call sites must not
use `datetime.now()` directly (monkeypatch `utcnow` in tests instead).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from orchestrator.constants import HARD_MAX_TTL_SECONDS
from orchestrator.core.durations import format_duration

_HARD_MAX_TTL = timedelta(seconds=HARD_MAX_TTL_SECONDS)


def utcnow() -> datetime:
    """Return the current time, timezone-aware in UTC."""
    return datetime.now(UTC)


def to_rfc3339(dt: datetime) -> str:
    """Serialize a tz-aware datetime as RFC3339 UTC, millisecond precision, `Z` suffix.

    Raises `ValueError` if `dt` is naive.
    """
    if dt.tzinfo is None:
        raise ValueError(f"to_rfc3339 requires a timezone-aware datetime, got naive {dt!r}")
    dt_utc = dt.astimezone(UTC)
    return dt_utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt_utc.microsecond // 1000:03d}Z"


def from_rfc3339(s: str) -> datetime:
    """Parse an RFC3339 datetime string, converting any explicit offset to UTC.

    Accepts a `Z` suffix or an explicit numeric offset (e.g. `+00:00`).
    Raises `ValueError` for naive strings (no offset/`Z`) or unparseable ones.
    """
    normalized = s[:-1] + "+00:00" if s.endswith("Z") else s
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        raise ValueError(f"invalid RFC3339 datetime: {s!r}") from None
    if dt.tzinfo is None:
        raise ValueError(f"invalid RFC3339 datetime (naive, missing offset): {s!r}")
    return dt.astimezone(UTC)


def compute_expires_at(created_at: datetime, ttl: timedelta) -> datetime:
    """Return the expiry instant for a resource created at `created_at` with the given `ttl`."""
    return created_at + ttl


def is_expired(expires_at: datetime, now: datetime) -> bool:
    """True once `now` has reached or passed `expires_at`."""
    return now >= expires_at


class TTLExceedsMaxError(ValueError):
    """Raised when a requested TTL exceeds the effective maximum for its persona."""


def validate_total_ttl(ttl: timedelta, persona_max: timedelta) -> None:
    """Raise `TTLExceedsMaxError` if `ttl` exceeds min(persona_max, the 8h hard ceiling)."""
    effective_max = min(persona_max, _HARD_MAX_TTL)
    if ttl > effective_max:
        raise TTLExceedsMaxError(
            f"requested TTL {format_duration(ttl)} exceeds the maximum of "
            f"{format_duration(effective_max)} allowed for this persona"
        )
