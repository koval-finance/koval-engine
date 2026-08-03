"""Tests for date -> epoch-ms conversion."""

from koval.engine.time_range import date_to_ms


def test_date_to_ms_epoch():
    assert date_to_ms("1970-01-01") == 0


def test_date_to_ms_known_date():
    # 2024-01-01T00:00:00Z
    assert date_to_ms("2024-01-01") == 1704067200000


def test_date_to_ms_is_utc_midnight():
    assert date_to_ms("2024-03-01") % 86_400_000 == 0
