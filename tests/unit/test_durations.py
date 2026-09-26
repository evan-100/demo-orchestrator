from datetime import timedelta

import pytest

from orchestrator.core.durations import InvalidDurationError, format_duration, parse_duration


@pytest.mark.parametrize(
    "text,expected",
    [
        ("2h", timedelta(hours=2)),
        ("30m", timedelta(minutes=30)),
        ("90s", timedelta(seconds=90)),
        ("1h30m", timedelta(minutes=90)),
        ("8h", timedelta(hours=8)),
    ],
)
def test_parse_valid(text, expected):
    assert parse_duration(text) == expected


@pytest.mark.parametrize(
    "text",
    ["", "0m", "0h0m0s", "-1h", "2H", "1d", "90", " 2h ", "2h 30m", "8h1s", "999h", "h", "m30"],
)
def test_parse_invalid_has_helpful_message(text):
    with pytest.raises(InvalidDurationError) as e:
        parse_duration(text)
    assert "e.g. 30m, 2h, 1h30m" in str(e.value)


@pytest.mark.parametrize(
    "td,text",
    [
        (timedelta(minutes=90), "1h30m"),
        (timedelta(seconds=90), "1m30s"),
        (timedelta(hours=2), "2h"),
    ],
)
def test_format_roundtrip(td, text):
    assert format_duration(td) == text and parse_duration(text) == td
