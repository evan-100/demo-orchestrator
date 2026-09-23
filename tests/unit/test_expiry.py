from datetime import UTC, datetime, timedelta

import pytest

from orchestrator.core.expiry import (
    TTLExceedsMaxError,
    compute_expires_at,
    from_rfc3339,
    is_expired,
    to_rfc3339,
    validate_total_ttl,
)

T0 = datetime(2026, 9, 24, 15, 0, tzinfo=UTC)


def test_expiry_boundary():
    exp = compute_expires_at(T0, timedelta(hours=2))
    assert not is_expired(exp, exp - timedelta(milliseconds=1))
    assert is_expired(exp, exp)


def test_total_ttl_capped_by_persona_and_hard_max():
    validate_total_ttl(timedelta(hours=4), timedelta(hours=4))
    with pytest.raises(TTLExceedsMaxError):
        validate_total_ttl(timedelta(hours=5), timedelta(hours=4))


def test_rfc3339_roundtrip_and_rejects_naive():
    assert to_rfc3339(T0) == "2026-09-24T15:00:00.000Z"
    assert from_rfc3339(to_rfc3339(T0)) == T0
    with pytest.raises(ValueError):
        from_rfc3339("2026-09-24T15:00:00")  # naive
    with pytest.raises(ValueError):
        from_rfc3339("tomorrow")


def test_from_rfc3339_accepts_explicit_offsets_converted_to_utc():
    assert from_rfc3339("2026-09-24T15:00:00+00:00") == T0
    # 17:00+02:00 is 15:00 UTC.
    assert from_rfc3339("2026-09-24T17:00:00+02:00") == T0


def test_to_rfc3339_rejects_naive_datetime():
    with pytest.raises(ValueError):
        to_rfc3339(datetime(2026, 9, 24, 15, 0))


def test_validate_total_ttl_error_message_states_effective_max_in_hm_form():
    with pytest.raises(TTLExceedsMaxError, match="4h"):
        validate_total_ttl(timedelta(hours=5), timedelta(hours=4))
