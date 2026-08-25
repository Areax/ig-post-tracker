"""fetch_media_paginated's `exhausted` flag is the other input
bucket_media_by_day relies on to decide whether it's safe to assert a
"didn't post" for the whole window. Getting `exhausted` wrong in either
direction is a real correctness bug: too eager and it asserts false
negatives past real gaps in coverage; too conservative and legitimate
"no more posts" accounts never get their older days confirmed.

fetch_in_page (the actual network call) is mocked throughout - this
tests fetch_media_paginated's own pagination/stopping logic, not the
network layer.
"""
import check_posts


def _patch_pacing(monkeypatch):
    monkeypatch.setattr(check_posts, "MIN_REQUEST_INTERVAL", 0)
    monkeypatch.setattr(check_posts, "REQUEST_JITTER", 0)


def test_empty_first_page_is_exhausted_with_no_media(monkeypatch):
    _patch_pacing(monkeypatch)
    monkeypatch.setattr(check_posts, "MAX_FEED_PAGES", 3)
    monkeypatch.setattr(check_posts, "fetch_in_page", lambda page, url, params, headers: ({"items": []}, None, False))

    media, avatar, error, blocked, exhausted = check_posts.fetch_media_paginated(None, "123")

    assert media == []
    assert error is None
    assert exhausted is True


def test_more_available_false_marks_exhausted(monkeypatch):
    _patch_pacing(monkeypatch)
    monkeypatch.setattr(check_posts, "MAX_FEED_PAGES", 3)
    page = {
        "items": [{"taken_at": 1, "code": "a", "media_type": 1, "product_type": "feed",
                    "user": {"profile_pic_url": "https://avatar/"}}],
        "more_available": False,
        "next_max_id": None,
    }
    monkeypatch.setattr(check_posts, "fetch_in_page", lambda page_, url, params, headers: (page, None, False))

    media, avatar, error, blocked, exhausted = check_posts.fetch_media_paginated(None, "123")

    assert len(media) == 1
    assert exhausted is True
    assert avatar == "https://avatar/"


def test_budget_exhausted_with_more_available_is_not_exhausted(monkeypatch):
    """Ran out of MAX_FEED_PAGES while Instagram still had more - we do
    NOT know the account's full history, so this must not claim we do."""
    _patch_pacing(monkeypatch)
    monkeypatch.setattr(check_posts, "MAX_FEED_PAGES", 2)
    page = {
        "items": [{"taken_at": 1, "code": "a", "media_type": 1, "product_type": "feed"}],
        "more_available": True,
        "next_max_id": "cursor-123",
    }
    monkeypatch.setattr(check_posts, "fetch_in_page", lambda page_, url, params, headers: (page, None, False))

    media, avatar, error, blocked, exhausted = check_posts.fetch_media_paginated(None, "123")

    assert len(media) == 2  # fetched exactly MAX_FEED_PAGES pages
    assert exhausted is False


def test_partial_success_then_error_keeps_partial_media_as_nonfatal(monkeypatch):
    _patch_pacing(monkeypatch)
    monkeypatch.setattr(check_posts, "MAX_FEED_PAGES", 3)
    calls = {"n": 0}

    def fake_fetch(page, url, params, headers):
        calls["n"] += 1
        if calls["n"] == 1:
            return (
                {"items": [{"taken_at": 1, "code": "a", "media_type": 1, "product_type": "feed"}],
                 "more_available": True, "next_max_id": "cursor"},
                None, False,
            )
        return None, "rate limited", True

    monkeypatch.setattr(check_posts, "fetch_in_page", fake_fetch)

    media, avatar, error, blocked, exhausted = check_posts.fetch_media_paginated(None, "123")

    assert len(media) == 1
    assert error is None, "a later-page error with partial data already collected is non-fatal"
    assert exhausted is False


def test_first_page_error_with_no_media_returns_the_error(monkeypatch):
    _patch_pacing(monkeypatch)
    monkeypatch.setattr(check_posts, "MAX_FEED_PAGES", 2)
    monkeypatch.setattr(check_posts, "fetch_in_page", lambda page, url, params, headers: (None, "rate limited", True))

    media, avatar, error, blocked, exhausted = check_posts.fetch_media_paginated(None, "123")

    assert media is None
    assert error == "rate limited"
    assert blocked is True
    assert exhausted is False


def test_no_cursor_stops_pagination_even_if_more_available_is_true(monkeypatch):
    """more_available true but no next_max_id to page with - can't
    continue, and there's no real 'no more data' signal either."""
    _patch_pacing(monkeypatch)
    monkeypatch.setattr(check_posts, "MAX_FEED_PAGES", 3)
    page = {
        "items": [{"taken_at": 1, "code": "a", "media_type": 1, "product_type": "feed"}],
        "more_available": True,
        "next_max_id": None,
    }
    monkeypatch.setattr(check_posts, "fetch_in_page", lambda page_, url, params, headers: (page, None, False))

    media, avatar, error, blocked, exhausted = check_posts.fetch_media_paginated(None, "123")

    assert len(media) == 1
    assert exhausted is True
