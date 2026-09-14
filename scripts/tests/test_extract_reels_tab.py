"""extract_reels_tab_codes / fetch_post_date / fetch_additional_reels:
the account's /reels/ tab embeds a separate connection
(`polaris_clips_connection`) from the main grid's own
`polaris_ordered_timeline_connection` - confirmed live, 2026-09-14
against torch_boy, only partially overlapping (6 of 12 shared, 6 new),
giving more distinct posts with zero extra XHR/API calls (unaffected by
web_profile_info rate-limiting, which is specific to that XHR call, not
to plain page loads). Its nodes carry no date field at all, unlike the
main grid's - fetch_post_date recovers one from the individual
post/reel's own page instead, the same accessibility_caption mechanism
extract_embedded_timeline already relies on for the main grid.
"""
from __future__ import annotations

import json

import check_posts
from check_posts import extract_reels_tab_codes, fetch_additional_reels, fetch_post_date
from playwright.sync_api import Error as PlaywrightError


class _FakePage:
    def __init__(self, blocks=None, html=None, goto_error=None):
        self._blocks = blocks or []
        self._html = html or ""
        self._goto_error = goto_error
        self.goto_calls = []
        self.wait_calls = []

    def goto(self, url, wait_until=None, timeout=None):
        self.goto_calls.append(url)
        if self._goto_error:
            raise self._goto_error

    def wait_for_timeout(self, ms):
        self.wait_calls.append(ms)

    def eval_on_selector_all(self, selector, js):
        return self._blocks

    def content(self):
        return self._html


def _clips_block(codes: list[str]) -> str:
    edges = [{"node": {"code": c}} for c in codes]
    return json.dumps({
        "require": [[
            "irrelevant", "wrapper", [], [{
                "__bbox": {"result": {"data": {"xig_user_by_username": {
                    "polaris_clips_connection": {"edges": edges}
                }}}}
            }]
        ]]
    })


def test_extract_reels_tab_codes_navigates_to_the_reels_url(monkeypatch):
    page = _FakePage(blocks=[_clips_block(["abc123"])])

    extract_reels_tab_codes(page, "torch_boy")

    assert page.goto_calls == ["https://www.instagram.com/torch_boy/reels/"]


def test_extract_reels_tab_codes_parses_edges(monkeypatch):
    page = _FakePage(blocks=[_clips_block(["codeA", "codeB", "codeC"])])

    codes = extract_reels_tab_codes(page, "torch_boy")

    assert codes == ["codeA", "codeB", "codeC"]


def test_extract_reels_tab_codes_returns_empty_when_marker_never_appears(monkeypatch):
    page = _FakePage(blocks=["{}", "not relevant either"])

    codes = extract_reels_tab_codes(page, "torch_boy", max_attempts=2, retry_interval_ms=0)

    assert codes == []


def test_extract_reels_tab_codes_returns_empty_on_navigation_failure(monkeypatch):
    page = _FakePage(goto_error=PlaywrightError("net::ERR_TIMED_OUT"))

    codes = extract_reels_tab_codes(page, "torch_boy")

    assert codes == []


def test_fetch_post_date_parses_accessibility_caption(monkeypatch):
    html = '<script>{"accessibility_caption":"Photo by someone on September 06, 2026."}</script>'
    page = _FakePage(html=html)

    taken_at = fetch_post_date(page, "abc123")

    assert taken_at is not None
    assert page.goto_calls == ["https://www.instagram.com/p/abc123/"]


def test_fetch_post_date_returns_none_when_no_caption_matches(monkeypatch):
    page = _FakePage(html="<script>{}</script>")

    assert fetch_post_date(page, "abc123") is None


def test_fetch_post_date_returns_none_on_navigation_failure(monkeypatch):
    page = _FakePage(goto_error=PlaywrightError("net::ERR_TIMED_OUT"))

    assert fetch_post_date(page, "abc123") is None


def test_fetch_additional_reels_only_fetches_codes_not_already_known(monkeypatch):
    monkeypatch.setattr(check_posts, "MIN_REQUEST_INTERVAL", 0)
    monkeypatch.setattr(check_posts, "REQUEST_JITTER", 0)
    monkeypatch.setattr(
        check_posts, "extract_reels_tab_codes",
        lambda page, handle: ["known1", "new1", "known2", "new2"],
    )
    fetched = []

    def fake_fetch_post_date(page, code):
        fetched.append(code)
        return 1234567890

    monkeypatch.setattr(check_posts, "fetch_post_date", fake_fetch_post_date)

    media = fetch_additional_reels(page=None, handle="torch_boy", known_codes={"known1", "known2"})

    assert fetched == ["new1", "new2"]
    assert {m["code"] for m in media} == {"new1", "new2"}
    assert all(m["taken_at"] == 1234567890 for m in media)
    assert all(m["product_type"] == "clips" for m in media)


def test_fetch_additional_reels_skips_codes_with_no_recoverable_date(monkeypatch):
    monkeypatch.setattr(check_posts, "MIN_REQUEST_INTERVAL", 0)
    monkeypatch.setattr(check_posts, "REQUEST_JITTER", 0)
    monkeypatch.setattr(check_posts, "extract_reels_tab_codes", lambda page, handle: ["new1"])
    monkeypatch.setattr(check_posts, "fetch_post_date", lambda page, code: None)

    media = fetch_additional_reels(page=None, handle="torch_boy", known_codes=set())

    assert media == []


def test_fetch_additional_reels_returns_empty_when_nothing_new(monkeypatch):
    monkeypatch.setattr(check_posts, "extract_reels_tab_codes", lambda page, handle: ["known1"])
    calls = []
    monkeypatch.setattr(check_posts, "fetch_post_date", lambda page, code: calls.append(code))

    media = fetch_additional_reels(page=None, handle="torch_boy", known_codes={"known1"})

    assert media == []
    assert calls == []
