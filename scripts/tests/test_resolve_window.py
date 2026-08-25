"""resolve_window decides what the whole run - and the live site - looks
like. An off-by-one here silently shifts every date column in the UI, or
(for the WINDOW_START_DATE path used by the live workflow right now)
breaks the fixed reference window it's specifically supposed to hold.
"""
from datetime import date, timedelta
from zoneinfo import ZoneInfo

import check_posts

UTC = ZoneInfo("UTC")


def test_window_start_date_produces_a_fixed_forward_window(monkeypatch):
    monkeypatch.setattr(check_posts, "HISTORY_DAYS", 5)
    monkeypatch.setenv("WINDOW_START_DATE", "2026-08-17")
    monkeypatch.delenv("TARGET_DATE", raising=False)

    window = check_posts.resolve_window(UTC)

    assert window == [date(2026, 8, 17) + timedelta(days=i) for i in range(5)]


def test_window_start_date_takes_priority_over_target_date(monkeypatch):
    monkeypatch.setattr(check_posts, "HISTORY_DAYS", 3)
    monkeypatch.setenv("WINDOW_START_DATE", "2026-08-17")
    monkeypatch.setenv("TARGET_DATE", "2026-09-01")

    window = check_posts.resolve_window(UTC)

    assert window[0] == date(2026, 8, 17)


def test_target_date_produces_a_rolling_window_ending_there(monkeypatch):
    monkeypatch.setattr(check_posts, "HISTORY_DAYS", 4)
    monkeypatch.delenv("WINDOW_START_DATE", raising=False)
    monkeypatch.setenv("TARGET_DATE", "2026-08-20")

    window = check_posts.resolve_window(UTC)

    assert window == [date(2026, 8, 17), date(2026, 8, 18), date(2026, 8, 19), date(2026, 8, 20)]


def test_default_window_is_a_rolling_window_ending_yesterday(monkeypatch):
    monkeypatch.setattr(check_posts, "HISTORY_DAYS", 3)
    monkeypatch.delenv("WINDOW_START_DATE", raising=False)
    monkeypatch.delenv("TARGET_DATE", raising=False)

    class FixedDatetime(check_posts.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 8, 24, 12, 0, tzinfo=tz)

    monkeypatch.setattr(check_posts, "datetime", FixedDatetime)

    window = check_posts.resolve_window(UTC)

    assert window == [date(2026, 8, 21), date(2026, 8, 22), date(2026, 8, 23)]


def test_window_is_always_oldest_to_newest():
    window = check_posts.resolve_window(UTC)
    assert window == sorted(window)
    assert len(window) == len(set(window))  # no duplicate dates
