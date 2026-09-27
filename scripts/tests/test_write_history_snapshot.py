"""write_history_snapshot: regenerates HISTORY_FILE from the DB for a
given window - shared by a normal run's own final export and EXPORT_ONLY's
no-network path. The scenario this exists for: widening the tracked
window (HISTORY_DAYS) adds trailing dates that are still in the future -
no live check could ever populate them anyway (see bucket_media_by_day),
so they just need to exist as empty/pending keys in the JSON, which is
pure DB export, no Instagram/proxy traffic required.
"""
import json
from datetime import date
from zoneinfo import ZoneInfo

import check_posts
import db

UTC = ZoneInfo("UTC")


def test_writes_the_expected_shape(monkeypatch, conn, tmp_path):
    history_file = tmp_path / "history.json"
    monkeypatch.setattr(check_posts, "HISTORY_FILE", history_file)
    db.upsert_ok(conn, "a", {"2026-08-17": {"status": "ok", "posted": True, "permalink": "https://p/"}}, "t")

    check_posts.write_history_snapshot(conn, ["a"], [date(2026, 8, 17)], UTC)

    snapshot = json.loads(history_file.read_text())
    assert snapshot["handles"] == ["a"]
    assert snapshot["days"]["2026-08-17"]["a"] == {"status": "ok", "posted": True, "permalink": "https://p/"}
    assert "updated_at" in snapshot


def test_widening_the_window_adds_new_dates_with_no_new_checks(monkeypatch, conn, tmp_path):
    """The exact scenario EXPORT_ONLY exists for: a date never checked
    (because it's in the future, or just never got to it) must still
    show up as an empty/pending entry once it's part of the requested
    window - the frontend only renders columns for dates that exist as
    keys in the JSON at all, pending or not."""
    history_file = tmp_path / "history.json"
    monkeypatch.setattr(check_posts, "HISTORY_FILE", history_file)
    db.upsert_ok(conn, "a", {"2026-08-17": {"status": "ok", "posted": True}}, "t")

    wider_window = [date(2026, 8, 17), date(2026, 8, 18), date(2026, 8, 19)]
    check_posts.write_history_snapshot(conn, ["a"], wider_window, UTC)

    snapshot = json.loads(history_file.read_text())
    assert snapshot["days"]["2026-08-17"]["a"]["status"] == "ok"
    assert snapshot["days"]["2026-08-18"] == {}, "never checked - must exist as an empty/pending entry, not be missing"
    assert snapshot["days"]["2026-08-19"] == {}


def test_always_exports_all_handles_passed_in_not_a_narrowed_subset(monkeypatch, conn, tmp_path):
    """Mirrors main()'s own all_handles vs. handles split - callers must
    pass the FULL tracked list here even when a targeted retry narrowed
    who actually got checked, or the site would drop handles."""
    history_file = tmp_path / "history.json"
    monkeypatch.setattr(check_posts, "HISTORY_FILE", history_file)
    db.upsert_ok(conn, "a", {"2026-08-17": {"status": "ok", "posted": True}}, "t")
    db.upsert_ok(conn, "b", {"2026-08-17": {"status": "ok", "posted": False}}, "t")

    check_posts.write_history_snapshot(conn, ["a", "b"], [date(2026, 8, 17)], UTC)

    snapshot = json.loads(history_file.read_text())
    assert snapshot["handles"] == ["a", "b"]
    assert "b" in snapshot["days"]["2026-08-17"]
