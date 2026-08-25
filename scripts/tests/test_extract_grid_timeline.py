"""extract_grid_timeline is the first-tried fallback for accounts where
web_profile_info fails outright. It exists because extract_embedded_timeline
(the Relay-JSON-based fallback) and the id-only crawler-UA HTTP request
have each independently failed in production for the same account on
different runs, while this simpler, structurally different approach -
reading the id and post dates straight off the actually-rendered page -
has not yet been observed to fail. These tests lock in the parsing
itself; see git history for the real account validation.
"""
from __future__ import annotations

from check_posts import extract_grid_timeline
from playwright.sync_api import Error as PlaywrightError


class _FakePage:
    """`html_sequence` and `items_sequence` are lists, one entry per
    successive read attempt (last entry repeats if attempts exceed the
    list length) - lets tests simulate data appearing only after a retry."""

    def __init__(self, html_sequence, items_sequence, raise_error: bool = False):
        self._html_sequence = html_sequence
        self._items_sequence = items_sequence
        self._raise_error = raise_error
        self.read_calls = 0
        self.wait_calls = []

    def content(self):
        return self._html_sequence[min(self.read_calls, len(self._html_sequence) - 1)]

    def eval_on_selector_all(self, selector, js):
        if self._raise_error:
            raise PlaywrightError("navigation context was destroyed")
        result = self._items_sequence[min(self.read_calls, len(self._items_sequence) - 1)]
        self.read_calls += 1
        return result

    def wait_for_timeout(self, ms):
        self.wait_calls.append(ms)


def _item(href: str, alt: str) -> dict:
    return {"href": href, "alt": alt}


HTML_WITH_ID = 'blah blah "profilePage_663398771" more stuff'
HTML_WITHOUT_ID = "no id in here at all"


def test_recovers_user_id_and_dated_posts_from_the_rendered_grid():
    items = [
        _item("/bry.trieu/p/DcC_DnXlG_9/", "Photo by Bryant Trieu on August 14, 2026."),
        _item("/bry.trieu/reel/DcZ1AQpJKwA/", "Video by Bryant Trieu on August 23, 2026."),
    ]
    page = _FakePage([HTML_WITH_ID], [items])

    user_id, media = extract_grid_timeline(page)

    assert user_id == "663398771"
    assert len(media) == 2
    assert media[0]["code"] == "DcC_DnXlG_9"
    assert media[1]["code"] == "DcZ1AQpJKwA"


def test_reel_and_post_urls_both_extract_the_shortcode_correctly():
    items = [
        _item("/handle/p/AbCdEfGhI/", "on August 1, 2026."),
        _item("/handle/reel/XyZ12345/", "on August 2, 2026."),
    ]
    page = _FakePage([HTML_WITHOUT_ID], [items])

    _, media = extract_grid_timeline(page)

    assert {m["code"] for m in media} == {"AbCdEfGhI", "XyZ12345"}


def test_items_without_a_parseable_date_are_skipped():
    items = [
        _item("/handle/p/good_post/", "on August 14, 2026."),
        _item("/handle/p/no_date/", "just some alt text with no date"),
    ]
    page = _FakePage([HTML_WITHOUT_ID], [items])

    _, media = extract_grid_timeline(page)

    assert [m["code"] for m in media] == ["good_post"]


def test_items_with_unparseable_href_are_skipped():
    items = [_item("/handle/somethingelse/not_a_post/", "on August 14, 2026.")]
    page = _FakePage([HTML_WITHOUT_ID], [items])

    _, media = extract_grid_timeline(page)

    assert media == []


def test_returns_user_id_even_when_no_posts_are_dated():
    """The id and the post dates are recovered independently - losing one
    shouldn't cost the other."""
    page = _FakePage([HTML_WITH_ID], [[]])

    user_id, media = extract_grid_timeline(page)

    assert user_id == "663398771"
    assert media == []


def test_retries_and_succeeds_once_the_grid_appears_on_a_later_attempt():
    page = _FakePage(
        html_sequence=[HTML_WITHOUT_ID, HTML_WITH_ID],
        items_sequence=[[], [_item("/handle/p/real_post/", "on August 14, 2026.")]],
    )

    user_id, media = extract_grid_timeline(page, max_attempts=4, retry_interval_ms=1)

    assert user_id == "663398771"
    assert len(media) == 1
    assert page.wait_calls == [1]


def test_gives_up_after_max_attempts_with_nothing_ever_appearing():
    page = _FakePage([HTML_WITHOUT_ID], [[]])

    user_id, media = extract_grid_timeline(page, max_attempts=3, retry_interval_ms=1)

    assert user_id is None
    assert media == []
    assert page.wait_calls == [1, 1]


def test_playwright_error_returns_none_and_empty_list_without_retrying():
    page = _FakePage([HTML_WITH_ID], [[]], raise_error=True)

    user_id, media = extract_grid_timeline(page, max_attempts=5, retry_interval_ms=1)

    assert user_id is None
    assert media == []
    assert page.wait_calls == []
