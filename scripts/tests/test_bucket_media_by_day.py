"""bucket_media_by_day is the single function most directly responsible
for the tracker's core promise: every green/red cell reflects real
evidence. Two real production incidents (see check_posts.py's module
docstring and README's "How it works") were both this exact class of
bug - a routine run silently overwriting a previously-confirmed "posted"
day with a false "didn't post" because its fetch simply hadn't reached
that far back. Every test here maps to a specific way that can happen.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from check_posts import bucket_media_by_day

UTC = ZoneInfo("UTC")


def _window(start: date, days: int) -> list[date]:
    return [start + timedelta(days=i) for i in range(days)]


def _post(d: date, code: str = "abc", permalink: str | None = None) -> dict:
    ts = int(datetime(d.year, d.month, d.day, 12, tzinfo=UTC).timestamp())
    return {"taken_at": ts, "code": code, "permalink": permalink or f"https://instagram.com/p/{code}/"}


def test_sparse_recent_media_does_not_claim_older_days_as_missed():
    """The bry.trieu incident: a frequent poster's ~12 most recent posts
    only reach back a few days into a 14-day window. Days before that
    must be left with NO entry at all - not silently marked False."""
    window = _window(date(2026, 8, 10), 7)  # Aug 10..16
    media = [_post(date(2026, 8, 14)), _post(date(2026, 8, 15)), _post(date(2026, 8, 16))]

    results = bucket_media_by_day(media, window, UTC, today=date(2026, 8, 20))

    assert set(results) == {"2026-08-14", "2026-08-15", "2026-08-16"}
    for d in ("2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13"):
        assert d not in results, f"{d} has no evidence and must not be asserted either way"
    assert all(results[d]["posted"] is True for d in results)


def test_zero_media_with_no_coverage_signal_asserts_nothing():
    """The second incident: the id-only crawler fallback recovers zero
    post data (not zero posts - zero *information*). That must not be
    conflated with "confirmed zero posts" - it must write nothing."""
    window = _window(date(2026, 8, 10), 7)

    results = bucket_media_by_day([], window, UTC, today=date(2026, 8, 20))

    assert results == {}


def test_zero_media_with_known_zero_total_count_marks_whole_window_not_posted():
    """Contrast case for the test above: web_profile_info can tell us the
    account's all-time post count is genuinely 0 - that IS real evidence,
    covering the whole window, unlike the fallback path's silence."""
    window = _window(date(2026, 8, 10), 7)

    results = bucket_media_by_day([], window, UTC, today=date(2026, 8, 20), total_post_count=0)

    assert set(results) == {d.isoformat() for d in window}
    assert all(r["posted"] is False for r in results.values())


def test_feed_exhausted_covers_full_window_despite_sparse_media():
    """A real 'more_available: false' signal from feed/user proves we've
    seen the account's entire history, regardless of how few posts landed
    inside the window."""
    window = _window(date(2026, 8, 10), 7)
    media = [_post(date(2026, 8, 16))]

    results = bucket_media_by_day(media, window, UTC, today=date(2026, 8, 20), feed_exhausted=True)

    assert set(results) == {d.isoformat() for d in window}
    assert results["2026-08-16"]["posted"] is True
    assert results["2026-08-10"]["posted"] is False


def test_total_post_count_matching_fetched_count_covers_full_window():
    window = _window(date(2026, 8, 10), 7)
    media = [_post(date(2026, 8, 15), code="a"), _post(date(2026, 8, 16), code="b")]

    results = bucket_media_by_day(media, window, UTC, today=date(2026, 8, 20), total_post_count=2)

    assert set(results) == {d.isoformat() for d in window}


def test_total_post_count_exceeding_fetched_count_only_covers_from_oldest_post():
    """total_post_count says there's more history than we fetched -
    that's NOT full coverage, so older days stay unasserted."""
    window = _window(date(2026, 8, 10), 7)
    media = [_post(date(2026, 8, 15), code="a"), _post(date(2026, 8, 16), code="b")]

    results = bucket_media_by_day(window=window, media=media, tz=UTC, today=date(2026, 8, 20), total_post_count=5)

    assert set(results) == {"2026-08-15", "2026-08-16"}


def test_today_and_future_dates_never_get_an_entry_even_under_full_coverage():
    """For a WINDOW_START_DATE reference window that reaches up to the
    present day, today itself and every day after it must stay
    unasserted - not False. This is the real bug hit in production: a
    run at 9am checked "did they post today," found nothing *yet* (the
    day wasn't over), and wrote a false "didn't post" for a day that was
    still in progress."""
    window = _window(date(2026, 8, 17), 14)  # Aug 17..30
    today = date(2026, 8, 24)

    results = bucket_media_by_day([], window, UTC, today=today, total_post_count=0)

    assert set(results) == {d.isoformat() for d in _window(date(2026, 8, 17), 7)}  # Aug 17..23 - today excluded
    for d in _window(date(2026, 8, 24), 7):  # Aug 24 (today)..30
        assert d.isoformat() not in results


def test_today_itself_is_never_asserted_even_when_it_actually_posted():
    """A post that landed earlier today must not retroactively make today
    a "confirmed posted" day either - today isn't final until it's over,
    so it stays unasserted regardless of which way the evidence points."""
    window = _window(date(2026, 8, 20), 5)  # Aug 20..24
    today = date(2026, 8, 24)
    media = [_post(date(2026, 8, 24))]  # posted earlier today

    results = bucket_media_by_day(media, window, UTC, today=today, feed_exhausted=True)

    assert "2026-08-24" not in results
    assert set(results) == {"2026-08-20", "2026-08-21", "2026-08-22", "2026-08-23"}


def test_post_before_window_extends_coverage_without_appearing_in_results():
    """A post older than the window's first day is still real evidence
    that our fetch reached that far back - it should pull coverage_start
    earlier (covering the whole window) without itself ever appearing in
    the output, since it's outside the tracked window."""
    window = _window(date(2026, 8, 14), 3)  # Aug 14..16
    media = [_post(date(2026, 8, 10), code="old"), _post(date(2026, 8, 15), code="new")]

    results = bucket_media_by_day(media, window, UTC, today=date(2026, 8, 20))

    assert set(results) == {"2026-08-14", "2026-08-15", "2026-08-16"}
    assert results["2026-08-14"]["posted"] is False
    assert results["2026-08-15"]["posted"] is True
    assert results["2026-08-16"]["posted"] is False


def test_multiple_posts_same_day_keeps_first_permalink():
    window = _window(date(2026, 8, 15), 1)
    media = [
        _post(date(2026, 8, 15), code="first", permalink="https://instagram.com/p/first/"),
        _post(date(2026, 8, 15), code="second", permalink="https://instagram.com/p/second/"),
    ]

    results = bucket_media_by_day(media, window, UTC, today=date(2026, 8, 20))

    assert results["2026-08-15"]["permalink"] == "https://instagram.com/p/first/"


def test_posts_missing_taken_at_are_ignored_without_crashing():
    window = _window(date(2026, 8, 15), 1)
    media = [{"code": "no-timestamp", "taken_at": None, "permalink": "https://x/"}]

    results = bucket_media_by_day(media, window, UTC, today=date(2026, 8, 20))

    assert results == {}  # no usable evidence - the None-timestamp post contributes nothing


def test_empty_window_returns_empty_results():
    results = bucket_media_by_day([_post(date(2026, 8, 15))], [], UTC, today=date(2026, 8, 20))
    assert results == {}
