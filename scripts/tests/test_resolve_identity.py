"""resolve_identity's private/no-visible-posts short-circuit.

IG's own `is_private` flag is the one trustworthy, explicit signal -
resolve_identity acts on it immediately. A response with no edges AND no
count but no `is_private` flag either is ambiguous, not conclusive: it
doesn't reliably show up even for accounts confirmed private by hand
(tested live against Kirossound, 2026-09-13), but the exact same
empty-but-200 shape was also confirmed live, 2026-09-16, for
demonstrably public, actively-posting accounts (abourinaris,
alexyee.ventures, Zhenginmotion) - a soft-block disguised as "no posts
visible", not a real signal about the account. So that ambiguous case no
longer short-circuits: it falls through to the grid/embedded/HTML
fallbacks (the same ones used for an outright API failure), which read
the already-rendered page directly instead of trusting this one XHR.
Only if those also come up empty does it end up reported as private
(via check_handle's own guard, downstream of resolve_identity - not
exercised by this file). goto_profile and fetch_in_page are mocked - no
Playwright/network involved.
"""
import check_posts
from playwright.sync_api import Error as PlaywrightError

PRIVATE_MESSAGE = "account is private or has no visible posts"


class _FakeRequest:
    def get(self, url, headers=None, timeout=None):
        raise PlaywrightError("net::ERR_BLOCKED_BY_CLIENT")


class _FakeContext:
    request = _FakeRequest()


class _FakePage:
    """Just enough of a Page for resolve_identity's HTML fallback request
    (page.context.request.get) to run without real Playwright/network."""

    context = _FakeContext()


def _mock_web_profile_info(monkeypatch, user):
    monkeypatch.setattr(check_posts, "goto_profile", lambda page, handle: None)
    monkeypatch.setattr(
        check_posts, "fetch_in_page",
        lambda page, url, params, headers: ({"data": {"user": user}}, None, False),
    )


def _mock_fallbacks_find_nothing(monkeypatch):
    monkeypatch.setattr(check_posts, "extract_grid_timeline", lambda page: (None, []))
    monkeypatch.setattr(check_posts, "extract_embedded_timeline", lambda page: (None, []))


def test_is_private_flag_short_circuits_with_a_clear_message(monkeypatch):
    _mock_web_profile_info(monkeypatch, {"id": "123", "is_private": True})

    user_id, avatar, media, error, was_blocked, total_post_count = check_posts.resolve_identity(
        "some_private_handle", page=None,
    )

    assert user_id is None
    assert media is None
    assert error == PRIVATE_MESSAGE
    assert was_blocked is False, "a private account is not an IG block"


def test_no_edges_and_no_count_falls_through_to_page_fallbacks_instead_of_trusting_it(monkeypatch):
    """The ambiguous case (no is_private flag, no edges, no count) must
    not be trusted outright - it should try the page-reading fallbacks
    before giving up. Here they also find nothing, so resolve_identity
    ultimately fails - but via the fallback chain's own error, not a
    same-request short-circuit."""
    _mock_web_profile_info(monkeypatch, {"id": "123", "is_private": False})
    _mock_fallbacks_find_nothing(monkeypatch)

    user_id, avatar, media, error, was_blocked, total_post_count = check_posts.resolve_identity(
        "some_handle", page=_FakePage(),
    )

    assert user_id is None
    assert media is None
    assert error.startswith(PRIVATE_MESSAGE)
    assert "HTML fallback" in error
    assert was_blocked is False


def test_no_edges_and_no_count_is_recovered_by_the_grid_fallback(monkeypatch):
    """The actual bug this guards against: web_profile_info's empty-but-
    200 response for a real, public, actively-posting account must not
    be reported as private when the already-rendered page itself still
    has the real post data (confirmed live, 2026-09-16, for
    abourinaris/alexyee.ventures/Zhenginmotion through the proxy)."""
    _mock_web_profile_info(monkeypatch, {"id": "123", "is_private": False})
    monkeypatch.setattr(
        check_posts, "extract_grid_timeline",
        lambda page: ("123", [{"taken_at": 456, "code": "xyz", "media_type": None, "product_type": None}]),
    )

    user_id, avatar, media, error, was_blocked, total_post_count = check_posts.resolve_identity(
        "some_handle", page=_FakePage(),
    )

    assert user_id == "123"
    assert error is None
    assert media == [{"taken_at": 456, "code": "xyz", "media_type": None, "product_type": None}]


def test_public_account_is_unaffected(monkeypatch):
    _mock_web_profile_info(monkeypatch, {
        "id": "123",
        "is_private": False,
        "profile_pic_url": "https://avatar/",
        "edge_owner_to_timeline_media": {"count": 1, "edges": [
            {"node": {"taken_at_timestamp": 123, "shortcode": "abc"}},
        ]},
    })

    user_id, avatar, media, error, was_blocked, total_post_count = check_posts.resolve_identity(
        "some_public_handle", page=None,
    )

    assert user_id == "123"
    assert error is None
    assert total_post_count == 1
    assert media == [{"taken_at": 123, "code": "abc", "media_type": None, "product_type": None}]


def test_confirmed_zero_post_count_is_not_flagged_as_private(monkeypatch):
    """A real `count: 0` (the account's timeline metadata itself confirms
    zero posts, ever) is actual evidence, not an unknown - unlike a
    missing/None count, it must not be treated as private."""
    _mock_web_profile_info(monkeypatch, {
        "id": "123",
        "edge_owner_to_timeline_media": {"count": 0, "edges": []},
    })

    user_id, avatar, media, error, was_blocked, total_post_count = check_posts.resolve_identity(
        "some_handle", page=None,
    )

    assert user_id == "123"
    assert error is None
    assert total_post_count == 0
    assert media == []
