"""db.py owns the tracker's actual persisted state. The invariants that
matter most for business correctness:

  1. A successful run overwrites - it's authoritative for the dates it
     covers (upsert_ok)...
  2. ...except a confirmed `posted: true` can never be downgraded back to
     false by a later write (also upsert_ok). Both of Instagram's post-
     listing endpoints have been confirmed, in production, to sometimes
     return a real post's day missing from an otherwise-gapless-looking
     fetch (feed/user's non-chronological interleaving; a manually-
     reordered profile grid changing web_profile_info's display order) -
     without this rule, a later run's incomplete fetch silently erased a
     real, previously-confirmed post. `false` stays revisable in the
     other direction: a later run can still correct it to `true`.
  3. A failed run never overwrites - it only fills genuine gaps
     (fill_gaps_with_error), so a transient block can't erase history
     that a previous good run already established.

Everything else here (avatars, posts, identities, session state,
export_window's JSON shape) is what the rest of the pipeline and the
live site depend on being exactly right.
"""
from datetime import date

import db


def test_upsert_ok_inserts_new_rows(conn):
    results = {
        "2026-08-17": {"status": "ok", "posted": True, "permalink": "https://instagram.com/p/abc/"},
        "2026-08-18": {"status": "ok", "posted": False},
    }

    db.upsert_ok(conn, "torch_boy", results, "2026-08-19T00:00:00")

    rows = conn.execute(
        "SELECT check_date, status, posted, permalink FROM checks WHERE handle = ? ORDER BY check_date",
        ("torch_boy",),
    ).fetchall()
    assert rows == [
        ("2026-08-17", "ok", 1, "https://instagram.com/p/abc/"),
        ("2026-08-18", "ok", 0, None),
    ]


def test_upsert_ok_overwrites_existing_row_for_the_same_date(conn):
    db.upsert_ok(conn, "torch_boy", {"2026-08-17": {"status": "ok", "posted": False}}, "run-1")
    db.upsert_ok(
        conn, "torch_boy",
        {"2026-08-17": {"status": "ok", "posted": True, "permalink": "https://instagram.com/p/xyz/"}},
        "run-2",
    )

    row = conn.execute(
        "SELECT posted, permalink, checked_at FROM checks WHERE handle = ? AND check_date = ?",
        ("torch_boy", "2026-08-17"),
    ).fetchone()
    assert row == (1, "https://instagram.com/p/xyz/", "run-2")


def test_upsert_ok_never_downgrades_a_confirmed_posted_day_to_false(conn):
    """The core regression this whole invariant exists for: signed.angela
    and bry.trieu both had a real, confirmed posted day silently erased
    by a later run whose fetch happened to have a gap. A later `false`
    for an already-`true` day must be rejected outright, not applied."""
    db.upsert_ok(
        conn, "signed.angela",
        {"2026-08-21": {"status": "ok", "posted": True, "permalink": "https://instagram.com/p/DcUwvYJt7f7/"}},
        "good-run",
    )

    db.upsert_ok(conn, "signed.angela", {"2026-08-21": {"status": "ok", "posted": False}}, "gappy-later-run")

    row = conn.execute(
        "SELECT posted, permalink, checked_at FROM checks WHERE handle = ? AND check_date = ?",
        ("signed.angela", "2026-08-21"),
    ).fetchone()
    assert row == (1, "https://instagram.com/p/DcUwvYJt7f7/", "good-run"), (
        "a confirmed posted day must survive a later run's incomplete fetch"
    )


def test_upsert_ok_still_allows_false_to_true_correction(conn):
    """The protection is one-directional - a day genuinely marked false
    (no evidence yet) must still be correctable to true once a later
    run's fetch actually includes it."""
    db.upsert_ok(conn, "signed.angela", {"2026-08-21": {"status": "ok", "posted": False}}, "gappy-run")

    db.upsert_ok(
        conn, "signed.angela",
        {"2026-08-21": {"status": "ok", "posted": True, "permalink": "https://instagram.com/p/DcUwvYJt7f7/"}},
        "corrected-run",
    )

    row = conn.execute(
        "SELECT posted, permalink, checked_at FROM checks WHERE handle = ? AND check_date = ?",
        ("signed.angela", "2026-08-21"),
    ).fetchone()
    assert row == (1, "https://instagram.com/p/DcUwvYJt7f7/", "corrected-run")


def test_upsert_ok_true_can_still_refresh_over_true(conn):
    """true -> true must still go through normally (e.g. to refresh
    checked_at or swap in a different permalink for the same day),
    not get silently blocked by the same guard."""
    db.upsert_ok(
        conn, "torch_boy",
        {"2026-08-17": {"status": "ok", "posted": True, "permalink": "https://instagram.com/p/old/"}},
        "run-1",
    )

    db.upsert_ok(
        conn, "torch_boy",
        {"2026-08-17": {"status": "ok", "posted": True, "permalink": "https://instagram.com/p/new/"}},
        "run-2",
    )

    row = conn.execute(
        "SELECT posted, permalink, checked_at FROM checks WHERE handle = ? AND check_date = ?",
        ("torch_boy", "2026-08-17"),
    ).fetchone()
    assert row == (1, "https://instagram.com/p/new/", "run-2")


def test_upsert_ok_downgrade_protection_is_scoped_per_handle_and_date(conn):
    """The guard must key off the exact (handle, check_date) row, not
    accidentally protect/block unrelated rows."""
    db.upsert_ok(conn, "a", {"2026-08-17": {"status": "ok", "posted": True, "permalink": "https://x/"}}, "t1")

    db.upsert_ok(
        conn, "a",
        {
            "2026-08-17": {"status": "ok", "posted": False},  # blocked: same handle+date, was true
            "2026-08-18": {"status": "ok", "posted": False},  # allowed: new date, no prior row
        },
        "t2",
    )
    db.upsert_ok(conn, "b", {"2026-08-17": {"status": "ok", "posted": False}}, "t2")  # allowed: different handle

    rows = {
        (h, d): (posted, checked_at)
        for h, d, posted, checked_at in conn.execute(
            "SELECT handle, check_date, posted, checked_at FROM checks"
        )
    }
    assert rows[("a", "2026-08-17")] == (1, "t1"), "protected - different handle/date rows must not affect it"
    assert rows[("a", "2026-08-18")] == (0, "t2")
    assert rows[("b", "2026-08-17")] == (0, "t2")


def test_fill_gaps_with_error_never_overwrites_a_good_row(conn):
    db.upsert_ok(
        conn, "torch_boy",
        {"2026-08-17": {"status": "ok", "posted": True, "permalink": "https://instagram.com/p/real/"}},
        "good-run",
    )

    db.fill_gaps_with_error(
        conn, "torch_boy", [date(2026, 8, 17), date(2026, 8, 18)], "rate limited", "bad-run",
        today=date(2026, 8, 20),
    )

    rows = {
        r[0]: r
        for r in conn.execute(
            "SELECT check_date, status, posted, permalink, message FROM checks WHERE handle = ?",
            ("torch_boy",),
        )
    }
    assert rows["2026-08-17"] == ("2026-08-17", "ok", 1, "https://instagram.com/p/real/", None), (
        "a failed run must never destroy a previously-confirmed day"
    )
    assert rows["2026-08-18"] == ("2026-08-18", "error", None, None, "rate limited"), (
        "a failed run should still fill a genuine gap"
    )


def test_fill_gaps_with_error_does_not_overwrite_an_earlier_error_either(conn):
    """DO NOTHING means exactly that - even error-to-error, the first
    message wins rather than being silently replaced."""
    db.fill_gaps_with_error(conn, "torch_boy", [date(2026, 8, 17)], "first error", "run-1", today=date(2026, 8, 20))
    db.fill_gaps_with_error(conn, "torch_boy", [date(2026, 8, 17)], "second error", "run-2", today=date(2026, 8, 20))

    row = conn.execute(
        "SELECT message, checked_at FROM checks WHERE handle = ? AND check_date = ?",
        ("torch_boy", "2026-08-17"),
    ).fetchone()
    assert row == ("first error", "run-1")


def test_fill_gaps_with_error_never_marks_today_or_later_as_errored(conn):
    """The real production bug: a handle whose resolve fails for a
    WINDOW_START_DATE window reaching up to the present day must not get
    today (still in progress) or future days marked as a permanent
    "error" - only fully-elapsed past days are fair game."""
    window = [date(2026, 8, 22), date(2026, 8, 23), date(2026, 8, 24), date(2026, 8, 25), date(2026, 8, 26)]
    today = date(2026, 8, 24)

    db.fill_gaps_with_error(conn, "bry.trieu", window, "resolve failed", "t", today=today)

    rows = {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT check_date, status FROM checks WHERE handle = ?", ("bry.trieu",)
        )
    }
    assert set(rows) == {"2026-08-22", "2026-08-23"}, "only fully-elapsed past days should be filled"
    assert "2026-08-24" not in rows, "today is still in progress"
    assert "2026-08-25" not in rows and "2026-08-26" not in rows, "these haven't happened yet"


def test_purge_future_dates_removes_today_and_later_regardless_of_status(conn):
    db.upsert_ok(conn, "a", {"2026-08-24": {"status": "ok", "posted": False}}, "t")
    db.upsert_ok(conn, "a", {"2026-08-25": {"status": "ok", "posted": True, "permalink": "https://x/"}}, "t")
    db.fill_gaps_with_error(conn, "a", [date(2026, 8, 20)], "boom", "t", today=date(2026, 8, 24))

    deleted = db.purge_future_dates(conn, today=date(2026, 8, 24))

    assert deleted == 2  # the 08-24 and 08-25 rows, not the 08-20 error row
    remaining = {r[0] for r in conn.execute("SELECT check_date FROM checks WHERE handle = ?", ("a",))}
    assert remaining == {"2026-08-20"}


def test_purge_future_dates_is_idempotent(conn):
    db.upsert_ok(conn, "a", {"2026-08-20": {"status": "ok", "posted": True}}, "t")

    first = db.purge_future_dates(conn, today=date(2026, 8, 24))
    second = db.purge_future_dates(conn, today=date(2026, 8, 24))

    assert first == 0  # 08-20 is before today, nothing to purge
    assert second == 0


def test_export_window_shape_distinguishes_ok_error_and_no_data(conn):
    db.upsert_ok(conn, "a", {"2026-08-17": {"status": "ok", "posted": True, "permalink": "https://p/"}}, "t")
    db.fill_gaps_with_error(conn, "a", [date(2026, 8, 18)], "boom", "t", today=date(2026, 8, 20))
    window = [date(2026, 8, 17), date(2026, 8, 18), date(2026, 8, 19)]

    snapshot = db.export_window(conn, ["a"], window)

    assert snapshot["handles"] == ["a"]
    assert snapshot["days"]["2026-08-17"]["a"] == {"status": "ok", "posted": True, "permalink": "https://p/"}
    assert snapshot["days"]["2026-08-18"]["a"] == {"status": "error", "message": "boom"}
    assert snapshot["days"]["2026-08-19"] == {}, "a day with no row must be absent, not a false negative"


def test_export_window_omits_permalink_when_not_posted(conn):
    db.upsert_ok(conn, "a", {"2026-08-17": {"status": "ok", "posted": False}}, "t")

    snapshot = db.export_window(conn, ["a"], [date(2026, 8, 17)])

    assert snapshot["days"]["2026-08-17"]["a"] == {"status": "ok", "posted": False}
    assert "permalink" not in snapshot["days"]["2026-08-17"]["a"]


def test_export_window_scopes_strictly_to_requested_handles(conn):
    db.upsert_ok(conn, "a", {"2026-08-17": {"status": "ok", "posted": True}}, "t")
    db.upsert_ok(conn, "b", {"2026-08-17": {"status": "ok", "posted": True}}, "t")

    snapshot = db.export_window(conn, ["a"], [date(2026, 8, 17)])

    assert "b" not in snapshot["days"]["2026-08-17"]


def test_export_window_with_no_handles_returns_empty_shell(conn):
    snapshot = db.export_window(conn, [], [date(2026, 8, 17)])
    assert snapshot == {"handles": [], "days": {"2026-08-17": {}}}


def test_has_avatar_and_save_avatar_round_trip(conn):
    assert db.has_avatar(conn, "torch_boy") is False

    db.save_avatar(conn, "torch_boy", b"fake-jpeg-bytes", "image/jpeg", "t")

    assert db.has_avatar(conn, "torch_boy") is True


def test_export_avatars_writes_files_with_correct_extension_and_skips_missing(conn, tmp_path):
    db.save_avatar(conn, "torch_boy", b"jpegbytes", "image/jpeg", "t")
    db.save_avatar(conn, "angiechack", b"pngbytes", "image/png", "t")
    out_dir = tmp_path / "avatars"

    written = db.export_avatars(conn, ["torch_boy", "angiechack", "no_avatar_handle"], out_dir)

    assert written == {"torch_boy": "torch_boy.jpg", "angiechack": "angiechack.png"}
    assert (out_dir / "torch_boy.jpg").read_bytes() == b"jpegbytes"
    assert (out_dir / "angiechack.png").read_bytes() == b"pngbytes"
    assert "no_avatar_handle" not in written


def test_save_posts_dedupes_by_handle_and_code_refreshing_fetched_at(conn):
    post = {"code": "abc", "taken_at": 100, "permalink": "https://x/"}
    db.save_posts(conn, "torch_boy", [post], "t1")
    db.save_posts(conn, "torch_boy", [post], "t2")

    rows = conn.execute("SELECT code, fetched_at FROM posts WHERE handle = ?", ("torch_boy",)).fetchall()
    assert rows == [("abc", "t2")], "re-fetching the same post must refresh it, not duplicate it"


def test_save_posts_skips_entries_without_a_code(conn):
    db.save_posts(conn, "torch_boy", [{"code": None, "taken_at": 100}], "t1")
    rows = conn.execute("SELECT * FROM posts WHERE handle = ?", ("torch_boy",)).fetchall()
    assert rows == []


def test_get_user_id_and_save_user_id_round_trip(conn):
    assert db.get_user_id(conn, "torch_boy") is None

    db.save_user_id(conn, "torch_boy", "123456", "t1")

    assert db.get_user_id(conn, "torch_boy") == "123456"


def test_save_user_id_overwrites_on_conflict(conn):
    db.save_user_id(conn, "torch_boy", "111", "t1")
    db.save_user_id(conn, "torch_boy", "222", "t2")
    assert db.get_user_id(conn, "torch_boy") == "222"


def test_session_cookies_round_trip(conn):
    assert db.load_session_cookies(conn) is None

    state = {"cookies": [{"name": "csrftoken", "value": "abc"}], "origins": []}
    db.save_session_cookies(conn, state, "t1")

    assert db.load_session_cookies(conn) == state


def test_session_cookies_overwrite_not_duplicate(conn):
    db.save_session_cookies(conn, {"cookies": [], "origins": []}, "t1")
    db.save_session_cookies(conn, {"cookies": [{"name": "datr"}], "origins": []}, "t2")

    assert db.load_session_cookies(conn) == {"cookies": [{"name": "datr"}], "origins": []}
    count = conn.execute("SELECT COUNT(*) FROM session_state").fetchone()[0]
    assert count == 1
