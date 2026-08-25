"""check_handle wires resolve_identity -> (optional) fetch_media_paginated
-> bucket_media_by_day -> db writes together. The pieces are unit-tested
elsewhere; this locks down the wiring itself: error short-circuits before
any DB write, MAX_FEED_PAGES actually gates whether the throttled
feed/user endpoint gets touched at all, and a feed/user failure stays
non-fatal. resolve_identity and fetch_media_paginated are mocked - no
Playwright/network involved.
"""
from datetime import date, datetime
from zoneinfo import ZoneInfo

import check_posts
import db

UTC = ZoneInfo("UTC")


def _patch_pacing(monkeypatch):
    monkeypatch.setattr(check_posts, "MIN_REQUEST_INTERVAL", 0)
    monkeypatch.setattr(check_posts, "REQUEST_JITTER", 0)


def test_resolve_identity_error_short_circuits_before_any_db_write(conn, monkeypatch):
    monkeypatch.setattr(
        check_posts, "resolve_identity",
        lambda handle, page: (None, None, None, "boom", True, None),
    )

    results, error, was_blocked, avatar = check_posts.check_handle(
        "torch_boy", [date(2026, 8, 17)], UTC, page=None, conn=conn,
    )

    assert results is None
    assert error == "boom"
    assert was_blocked is True
    assert db.get_user_id(conn, "torch_boy") is None, "must not persist anything on a failed resolve"


def test_max_feed_pages_1_never_touches_feed_user(conn, monkeypatch):
    monkeypatch.setattr(check_posts, "MAX_FEED_PAGES", 1)
    called = {"feed": False}

    def fake_fetch_media_paginated(page, user_id):
        called["feed"] = True
        return [], None, None, False, True

    monkeypatch.setattr(check_posts, "fetch_media_paginated", fake_fetch_media_paginated)
    monkeypatch.setattr(
        check_posts, "resolve_identity",
        lambda handle, page: ("123", "https://avatar/", [], None, False, 0),
    )

    results, error, was_blocked, avatar = check_posts.check_handle(
        "torch_boy", [date(2026, 8, 17)], UTC, page=None, conn=conn,
    )

    assert called["feed"] is False, "daily checks (MAX_FEED_PAGES=1) must never call feed/user"
    assert error is None


def test_max_feed_pages_above_1_does_call_feed_user(conn, monkeypatch):
    _patch_pacing(monkeypatch)
    monkeypatch.setattr(check_posts, "MAX_FEED_PAGES", 2)
    called = {"feed": False}

    def fake_fetch_media_paginated(page, user_id):
        called["feed"] = True
        return [], None, None, False, True

    monkeypatch.setattr(check_posts, "fetch_media_paginated", fake_fetch_media_paginated)
    monkeypatch.setattr(
        check_posts, "resolve_identity",
        lambda handle, page: ("123", None, [], None, False, 0),
    )

    check_posts.check_handle("torch_boy", [date(2026, 8, 17)], UTC, page=None, conn=conn)

    assert called["feed"] is True


def test_feed_user_failure_is_nonfatal_and_keeps_embedded_posts(conn, monkeypatch):
    _patch_pacing(monkeypatch)
    monkeypatch.setattr(check_posts, "MAX_FEED_PAGES", 2)
    ts = int(datetime(2026, 8, 17, 12, tzinfo=UTC).timestamp())
    embedded = [{"taken_at": ts, "code": "abc", "media_type": None, "product_type": None}]
    monkeypatch.setattr(
        check_posts, "resolve_identity",
        lambda handle, page: ("123", None, embedded, None, False, None),
    )
    monkeypatch.setattr(
        check_posts, "fetch_media_paginated",
        lambda page, user_id: (None, None, "rate limited", True, False),
    )

    results, error, was_blocked, avatar = check_posts.check_handle(
        "torch_boy", [date(2026, 8, 17)], UTC, page=None, conn=conn,
    )

    assert error is None, "a feed/user failure must not fail the whole handle"
    assert results["2026-08-17"]["posted"] is True
    assert was_blocked is True, "the block signal itself still propagates for cooldown/circuit-breaker logic"


def test_successful_check_persists_user_id_and_raw_posts(conn, monkeypatch):
    monkeypatch.setattr(check_posts, "MAX_FEED_PAGES", 1)
    ts = int(datetime(2026, 8, 17, 12, tzinfo=UTC).timestamp())
    media = [{"taken_at": ts, "code": "abc", "media_type": None, "product_type": None}]
    monkeypatch.setattr(
        check_posts, "resolve_identity",
        lambda handle, page: ("999", "https://avatar/", media, None, False, None),
    )

    check_posts.check_handle("torch_boy", [date(2026, 8, 17)], UTC, page=None, conn=conn)

    assert db.get_user_id(conn, "torch_boy") == "999"
    posts = conn.execute("SELECT code FROM posts WHERE handle = ?", ("torch_boy",)).fetchall()
    assert posts == [("abc",)]
