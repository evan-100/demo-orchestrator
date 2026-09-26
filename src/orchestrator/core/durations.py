"""Duration parsing and formatting for TTL strings.

Grammar: `^([0-9]+h)?([0-9]+m)?([0-9]+s)?$`, lowercase only, total > 0,
total <= HARD_MAX_TTL_SECONDS (8h). See global-constraints.md.
"""

from __future__ import annotations

import re
from datetime import timedelta

from orchestrator.constants import HARD_MAX_TTL_SECONDS

_PATTERN = re.compile(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?")


class InvalidDurationError(ValueError):
    """Raised when a duration string doesn't match the accepted grammar."""


def _error(text: str) -> InvalidDurationError:
    return InvalidDurationError(
        f"invalid duration {text!r}: use h/m/s units, e.g. 30m, 2h, 1h30m (max 8h)"
    )


def parse_duration(text: str) -> timedelta:
    """Parse a duration string like "2h", "30m", "1h30m" into a `timedelta`.

    Raises `InvalidDurationError` with a helpful message on any invalid input,
    including empty strings, zero totals, wrong-case units, extra characters,
    out-of-order units, or totals exceeding the 8h hard ceiling.
    """
    match = _PATTERN.fullmatch(text)
    if match is None:
        raise _error(text)

    hours_s, minutes_s, seconds_s = match.groups()
    if hours_s is None and minutes_s is None and seconds_s is None:
        raise _error(text)

    hours = int(hours_s) if hours_s is not None else 0
    minutes = int(minutes_s) if minutes_s is not None else 0
    seconds = int(seconds_s) if seconds_s is not None else 0

    total_seconds = hours * 3600 + minutes * 60 + seconds
    if total_seconds <= 0 or total_seconds > HARD_MAX_TTL_SECONDS:
        raise _error(text)

    return timedelta(seconds=total_seconds)


def format_duration(td: timedelta) -> str:
    """Format a `timedelta` back into compact h/m/s form, e.g. 5400s -> "1h30m"."""
    total_seconds = int(td.total_seconds())
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    parts = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if seconds:
        parts.append(f"{seconds}s")
    return "".join(parts)
