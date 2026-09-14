"""goto_profile: one retry on a navigation timeout specifically (proxy
connections showed real, variable per-request latency in production,
2026-09-13 - 39/47 handles failed with "Page.goto: Timeout 30000ms
exceeded" through IPRoyal, not an IG block). Other navigation errors
(e.g. a real DNS/connection failure) aren't retried - a timeout is the
one failure mode a proxy's own latency plausibly explains as transient.
"""
from playwright.sync_api import Error as PlaywrightError

import check_posts


class FakePage:
    def __init__(self, goto_side_effects):
        self._goto_side_effects = list(goto_side_effects)
        self.goto_calls = 0
        self.wait_calls = 0

    def goto(self, url, wait_until=None, timeout=None):
        self.goto_calls += 1
        effect = self._goto_side_effects.pop(0)
        if effect is not None:
            raise effect

    def wait_for_timeout(self, ms):
        self.wait_calls += 1


def test_succeeds_on_first_try_without_retrying(monkeypatch):
    page = FakePage([None])

    error = check_posts.goto_profile(page, "torch_boy")

    assert error is None
    assert page.goto_calls == 1


def test_retries_once_on_timeout_and_then_succeeds(monkeypatch):
    monkeypatch.setattr(check_posts.time, "sleep", lambda seconds: None)
    page = FakePage([PlaywrightError("Page.goto: Timeout 30000ms exceeded."), None])

    error = check_posts.goto_profile(page, "torch_boy")

    assert error is None
    assert page.goto_calls == 2


def test_gives_up_after_one_retry_if_still_timing_out(monkeypatch):
    monkeypatch.setattr(check_posts.time, "sleep", lambda seconds: None)
    page = FakePage([
        PlaywrightError("Page.goto: Timeout 30000ms exceeded."),
        PlaywrightError("Page.goto: Timeout 30000ms exceeded."),
    ])

    error = check_posts.goto_profile(page, "torch_boy")

    assert error is not None
    assert "Timeout" in error
    assert page.goto_calls == 2


def test_waits_before_retrying_not_immediately(monkeypatch):
    slept = []
    monkeypatch.setattr(check_posts.time, "sleep", lambda seconds: slept.append(seconds))
    page = FakePage([PlaywrightError("Page.goto: Timeout 30000ms exceeded."), None])

    check_posts.goto_profile(page, "torch_boy")

    assert slept == [check_posts.RETRY_BACKOFF_SECONDS]


def test_non_timeout_error_is_not_retried(monkeypatch):
    page = FakePage([PlaywrightError("net::ERR_NAME_NOT_RESOLVED")])

    error = check_posts.goto_profile(page, "torch_boy")

    assert error is not None
    assert "Timeout" not in error
    assert page.goto_calls == 1
